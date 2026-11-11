"""Fetcher + extractor for Price Watcher.

Public API
----------
fetch(url, selector=None, *, use_browser_fallback=True) -> FetchResult

Extraction ladder (stops at first price found):
  1. Shopify product JSON  (/products/<handle>.js)
  2. JSON-LD Product/Offer  (via extruct)
  3. Open Graph / meta tags
  4. Microdata              (via extruct)
  5. CSS selector           (if `selector` arg provided)
  6. Playwright fallback    (reruns 2-5 on rendered HTML)

Blocked responses (403 / 429 / challenge page) skip the ladder: the Shopify
JSON (step 1) and then the Playwright fallback are tried, and the result is
used only if it is a definite answer; otherwise the status is BLOCKED.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Optional
from urllib.parse import parse_qs, urlparse

import extruct
import httpx
from bs4 import BeautifulSoup

from price_watcher import logging_setup
from price_watcher import paths
from price_watcher import sites
from price_watcher.contracts import FetchResult, FetchStatus

log = logging_setup.get(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_DEFAULT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "he,en;q=0.8,en-US;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
}

_RETRY_DELAYS = (1, 2, 4)  # seconds

# HTTP 429 handling: at most one retry per URL, and only when the server asks
# for a Retry-After of at most this many seconds. A host that is still
# rate-limiting after that is remembered for _RATE_LIMIT_MEMORY_SEC and later
# URLs on it get no 429 retry at all, so one host cannot stall a whole run.
_MAX_429_WAIT_SEC = 15.0
_RATE_LIMIT_MEMORY_SEC = 15 * 60
_rate_limited_hosts: dict[str, float] = {}  # host -> time.monotonic() expiry


def _host_is_rate_limited(host: str) -> bool:
    expiry = _rate_limited_hosts.get(host)
    if expiry is None:
        return False
    if time.monotonic() >= expiry:
        _rate_limited_hosts.pop(host, None)
        return False
    return True


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse a Retry-After header. Returns seconds (float) or None if unparseable.

    Accepts either an integer seconds value or an HTTP-date.
    """
    if not value:
        return None
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        from datetime import datetime, timezone
        target = parsedate_to_datetime(value)
        delta = (target - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delta)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def fetch(
    url: str,
    selector: Optional[str] = None,
    *,
    use_browser_fallback: bool = True,
) -> FetchResult:
    """Fetch *url* and extract price information.

    Never raises; always returns a FetchResult.
    """
    try:
        return _fetch(url, selector, use_browser_fallback=use_browser_fallback)
    except Exception as exc:  # last-resort guard: one bad URL must not kill a run
        log.exception("Unexpected error while fetching %s", url)
        return FetchResult(
            status=FetchStatus.UNREACHABLE,
            final_url=url,
            error_message=f"Unexpected error: {type(exc).__name__}: {exc}",
        )


def _fetch(
    url: str,
    selector: Optional[str],
    *,
    use_browser_fallback: bool,
) -> FetchResult:
    # --- HTTP fetch with retries ---
    response, error_msg, http_status, final_url = _http_get(url)

    if response is None:
        return FetchResult(
            status=FetchStatus.UNREACHABLE,
            final_url=final_url or url,
            http_status=http_status,
            error_message=error_msg,
        )

    if http_status == 404:
        return FetchResult(
            status=FetchStatus.NOT_FOUND,
            final_url=final_url,
            http_status=http_status,
            error_message="404 Not Found",
        )

    html = response.text

    # Check for block / captcha
    if http_status in (403, 429) or (http_status == 200 and _is_challenge_page(html)):
        blocked = FetchResult(
            status=FetchStatus.BLOCKED,
            final_url=final_url,
            http_status=http_status,
            error_message=f"Blocked (HTTP {http_status})",
        )
        # The Shopify product JSON is a different endpoint and is sometimes
        # served when the HTML page is not.
        shopify = _try_shopify(url, final_url, None, None, None)
        if shopify is not None:
            return shopify
        # A real browser often gets past bot checks; only accept it if it
        # actually yields a price, otherwise report the original block.
        if use_browser_fallback:
            log.info("Blocked (HTTP %s); trying Playwright fallback for %s", http_status, url)
            rendered = _playwright_fallback(url, selector, http_status, None, blocked)
            if rendered.status == FetchStatus.OK:
                return rendered
        return blocked

    # Save raw HTML for debugging
    raw_path = _save_raw(url, html)

    # --- Extraction ladder ---
    result = _extract(html, url, final_url, selector, http_status, raw_path, response)

    # --- Playwright fallback ---
    if use_browser_fallback and result.status == FetchStatus.PRICE_NOT_FOUND:
        log.info("Trying Playwright fallback for %s", url)
        result = _playwright_fallback(url, selector, http_status, raw_path, result)

    # --- Site-specific refinement ---
    site_extractor = sites.for_url(url)
    if site_extractor is not None:
        try:
            result = site_extractor.extract(html, url, result)
        except Exception as exc:
            log.warning("Site extractor failed for %s: %s", url, exc)

    return result


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _http_get(
    url: str,
) -> tuple[Optional[httpx.Response], Optional[str], Optional[int], str]:
    """Return (response, error_msg, http_status, final_url).

    Retries on network errors and 5xx responses with exponential backoff.
    Returns (None, error, None, url) on failure after all retries.
    """
    last_error: Optional[str] = None
    final_url = url
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        host = ""
    retried_429 = False

    for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
        try:
            with httpx.Client(
                follow_redirects=True,
                timeout=20,
                headers=_DEFAULT_HEADERS,
            ) as client:
                response = client.get(url)

            final_url = str(response.url)
            status = response.status_code

            if status >= 500:
                last_error = f"HTTP {status}"
                log.warning("HTTP %s on attempt %d for %s", status, attempt + 1, url)
            elif status == 429:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                if (
                    not retried_429
                    and attempt < len(_RETRY_DELAYS)
                    and not _host_is_rate_limited(host)
                    and retry_after is not None
                    and retry_after <= _MAX_429_WAIT_SEC
                ):
                    log.warning("HTTP 429 on %s; honouring Retry-After=%ss", url, retry_after)
                    retried_429 = True
                    time.sleep(retry_after)
                    continue
                if host:
                    _rate_limited_hosts[host] = time.monotonic() + _RATE_LIMIT_MEMORY_SEC
                return response, None, status, final_url
            else:
                return response, None, status, final_url

        except httpx.UnsupportedProtocol as exc:  # a TransportError, but permanent
            last_error = f"{type(exc).__name__}: {exc}"
            log.warning("HTTP error for %s: %s", url, last_error)
            return None, last_error, None, final_url
        except httpx.TransportError as exc:  # timeouts, connection/network/protocol errors
            last_error = str(exc) or type(exc).__name__
            log.warning("Network error on attempt %d for %s: %s", attempt + 1, url, exc)
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            # Not transient (too many redirects, undecodable body, bad URL...):
            # retrying would not help.
            last_error = f"{type(exc).__name__}: {exc}"
            log.warning("HTTP error for %s: %s", url, last_error)
            return None, last_error, None, final_url

        # Sleep before next retry (delay is None on the last sentinel iteration)
        if delay is not None:
            time.sleep(delay)

    return None, last_error, None, final_url


def _currency_from_html(soup: BeautifulSoup) -> Optional[str]:
    """Pull a currency code from meta tags or JSON-LD when a prior extractor got price but no currency."""
    for prop in ("og:price:currency", "product:price:currency"):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if tag and tag.get("content"):
            return tag["content"].strip().upper()
    for script in soup.find_all("script", type="application/ld+json"):
        text = script.string or script.get_text() or ""
        m = re.search(r'"priceCurrency"\s*:\s*"([A-Z]{3})"', text)
        if m:
            return m.group(1)
    return None


def _is_challenge_page(html: str) -> bool:
    """Only flag an HTTP-200 page as blocked if it's clearly a challenge/interstitial.

    Real content pages often reference reCAPTCHA or Cloudflare in embedded scripts,
    so we only match on the title or the challenge-platform script path.
    """
    lower = html.lower()
    if len(lower) > 50_000:
        return False
    return (
        "<title>just a moment..." in lower
        or "/cdn-cgi/challenge-platform/" in lower
        or 'id="challenge-form"' in lower
    )


# ---------------------------------------------------------------------------
# Raw HTML persistence
# ---------------------------------------------------------------------------

def _save_raw(url: str, html: str) -> Optional[str]:
    try:
        sha = hashlib.sha1(url.encode()).hexdigest()[:16]
        path = paths.raw_response_dir() / f"{sha}.html"
        path.write_text(html, encoding="utf-8")
        return str(path)
    except Exception as exc:
        log.warning("Could not save raw HTML for %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Extraction orchestration
# ---------------------------------------------------------------------------

def _extract(
    html: str,
    url: str,
    final_url: str,
    selector: Optional[str],
    http_status: Optional[int],
    raw_path: Optional[str],
    response: Optional[httpx.Response],
) -> FetchResult:
    """Run the extraction ladder and return a FetchResult."""
    soup = BeautifulSoup(html, "html.parser")
    title = _extract_title(soup)

    # 1. Shopify product JSON
    result = _try_shopify(url, final_url, title, http_status, raw_path)
    if result is not None:
        if result.currency is None:
            result = dataclasses.replace(result, currency=_currency_from_html(soup))
        return result

    # 2. JSON-LD
    result = _try_jsonld(html, url, final_url, title, http_status, raw_path)
    if result is not None:
        return result

    # 3. Open Graph / meta tags
    result = _try_meta(soup, final_url, title, http_status, raw_path)
    if result is not None:
        return result

    # 4. Microdata
    result = _try_microdata(html, url, final_url, title, http_status, raw_path)
    if result is not None:
        return result

    # 5. CSS selector
    if selector:
        result = _try_selector(soup, selector, final_url, title, http_status, raw_path)
        if result is not None:
            return result

    return FetchResult(
        status=FetchStatus.PRICE_NOT_FOUND,
        final_url=final_url,
        title=title,
        http_status=http_status,
        raw_html_path=raw_path,
    )


def _extract_from_html(
    html: str,
    url: str,
    final_url: str,
    selector: Optional[str],
    http_status: Optional[int],
    raw_path: Optional[str],
) -> FetchResult:
    """Like _extract but without Shopify step (used in Playwright fallback)."""
    soup = BeautifulSoup(html, "html.parser")
    title = _extract_title(soup)

    result = _try_jsonld(html, url, final_url, title, http_status, raw_path)
    if result is not None:
        return result

    result = _try_meta(soup, final_url, title, http_status, raw_path)
    if result is not None:
        return result

    result = _try_microdata(html, url, final_url, title, http_status, raw_path)
    if result is not None:
        return result

    if selector:
        result = _try_selector(soup, selector, final_url, title, http_status, raw_path)
        if result is not None:
            return result

    return FetchResult(
        status=FetchStatus.PRICE_NOT_FOUND,
        final_url=final_url,
        title=title,
        http_status=http_status,
        raw_html_path=raw_path,
    )


# ---------------------------------------------------------------------------
# Step 1: Shopify
# ---------------------------------------------------------------------------

_LOCALE_SEGMENT = re.compile(r"^[a-z]{2}(?:-[a-z]{2,4})?$", re.IGNORECASE)


def _shopify_js_url(url: str) -> Optional[str]:
    """Return the Shopify product-JSON URL for a product page URL, else None.

    Recognises ``/products/<handle>``, ``/collections/<c>/products/<handle>``
    and an optional leading locale segment (``/en-us/products/<handle>``);
    the locale prefix is kept so the JSON uses the same market/currency.
    """
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if "products" not in parts:
        return None
    idx = parts.index("products")
    if idx + 1 >= len(parts):
        return None
    handle = parts[idx + 1]
    prefix = parts[:idx]
    locale: list[str] = []
    if prefix and _LOCALE_SEGMENT.match(prefix[0]):
        locale, prefix = prefix[:1], prefix[1:]
    # Only bare, collection-scoped or locale-prefixed product paths qualify.
    if prefix and not (len(prefix) == 2 and prefix[0] == "collections"):
        return None
    if handle.endswith((".js", ".json")) or not parsed.netloc:
        return None
    base = "/".join([*locale, "products", handle])
    return f"{parsed.scheme}://{parsed.netloc}/{base}.js"


def _try_shopify(
    url: str,
    final_url: str,
    title: Optional[str],
    http_status: Optional[int],
    raw_path: Optional[str],
) -> Optional[FetchResult]:
    js_url = _shopify_js_url(url)
    if js_url is None:
        return None
    parsed = urlparse(url)

    # Check for variant in query string
    qs = parse_qs(parsed.query)
    variant_id: Optional[str] = None
    if "variant" in qs:
        variant_id = qs["variant"][0]

    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=20,
            headers=_DEFAULT_HEADERS,
        ) as client:
            resp = client.get(js_url)
    except Exception as exc:
        log.debug("Shopify .js fetch failed for %s: %s", js_url, exc)
        return None

    if resp.status_code != 200:
        return None

    try:
        data = resp.json()
    except Exception:
        return None

    if not isinstance(data, dict):
        return None
    variants = [v for v in (data.get("variants") or []) if isinstance(v, dict)]
    if not variants:
        return None

    if http_status is None:  # called without a usable HTML response
        http_status = resp.status_code

    if variant_id:
        variant = next((v for v in variants if str(v.get("id")) == str(variant_id)), None)
        if variant is None:
            # Never silently track a different variant than the one in the URL.
            return FetchResult(
                status=FetchStatus.NOT_FOUND,
                final_url=final_url,
                title=data.get("title") or title,
                http_status=http_status,
                error_message=f"Shopify variant {variant_id} not found in product",
                raw_html_path=raw_path,
            )
    else:
        # No pinned variant: always the first (default) variant, with its own
        # availability, so the tracked item never changes between runs.
        variant = variants[0]

    price_cents = variant.get("price")
    if price_cents is None:
        return None

    try:
        price = Decimal(str(price_cents)) / Decimal("100")
    except InvalidOperation:
        return None

    currency = data.get("currency") or variant.get("currency")
    prod_title = data.get("title") or title
    available = variant.get("available")
    in_stock = bool(available) if available is not None else None

    return FetchResult(
        status=FetchStatus.OK,
        final_url=final_url,
        title=prod_title,
        price=price,
        currency=currency,
        in_stock=in_stock,
        http_status=http_status,
        raw_html_path=raw_path,
    )


# ---------------------------------------------------------------------------
# Step 2: JSON-LD
# ---------------------------------------------------------------------------

def _try_jsonld(
    html: str,
    url: str,
    final_url: str,
    title: Optional[str],
    http_status: Optional[int],
    raw_path: Optional[str],
) -> Optional[FetchResult]:
    try:
        data = extruct.extract(html, base_url=url, syntaxes=["json-ld"])
    except Exception as exc:
        log.debug("extruct json-ld failed for %s: %s", url, exc)
        return None

    items = data.get("json-ld", []) if isinstance(data, dict) else []
    if not items:
        return None

    candidates = _jsonld_candidates(items)
    if not candidates:
        return None

    # Prefer the offer for the variant named in the watched URL; otherwise
    # the lowest price across all offers (previous behaviour).
    wanted = _jsonld_wanted_ids(url)
    matching = [c for c in candidates if wanted and _jsonld_matches(c, wanted)]
    pool = matching or candidates
    best = min(pool, key=lambda c: c[0])  # min() keeps the first of equal prices
    price, currency, availability, product, group = best

    in_stock: Optional[bool] = None
    if "OutOfStock" in availability or "SoldOut" in availability:
        in_stock = False
    elif "InStock" in availability:
        in_stock = True

    # A specific variant keeps its own name; otherwise name the product group.
    name = product.get("name") if (matching or group is None) else group.get("name")
    prod_title = (name if isinstance(name, str) and name else None) or title

    return FetchResult(
        status=FetchStatus.OK,
        final_url=final_url,
        title=prod_title,
        price=price,
        currency=currency,
        in_stock=in_stock,
        http_status=http_status,
        raw_html_path=raw_path,
    )


def _jsonld_has_type(node: dict, *names: str) -> bool:
    t = node.get("@type")
    types = t if isinstance(t, list) else [t]
    return any(isinstance(x, str) and x.split("/")[-1] in names for x in types)


def _as_list(value: object) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


# (price, currency, availability, product node, product-group node or None)
_JsonLdCandidate = tuple[Decimal, Optional[str], str, dict, Optional[dict]]


def _jsonld_candidates(items: list) -> list[_JsonLdCandidate]:
    """Collect every priced offer of every Product / ProductGroup variant.

    Walks nested ``@graph`` lists and ``ProductGroup.hasVariant``; accepts
    ``Offer.price`` and ``AggregateOffer.lowPrice`` (and offers nested inside
    an AggregateOffer). Anything that is not a dict is skipped.
    """
    products: list[tuple[dict, Optional[dict]]] = []

    def _walk(node: object, group: Optional[dict], depth: int = 0) -> None:
        if depth > 10:
            return
        if isinstance(node, list):
            for child in node:
                _walk(child, group, depth + 1)
            return
        if not isinstance(node, dict):
            return
        if "@graph" in node:
            _walk(node.get("@graph"), group, depth + 1)
        if _jsonld_has_type(node, "ProductGroup"):
            products.append((node, None))
            _walk(node.get("hasVariant"), node, depth + 1)
        elif _jsonld_has_type(node, "Product"):
            products.append((node, group))

    _walk(items, None)

    candidates: list[_JsonLdCandidate] = []

    def _add_offers(offers: object, product: dict, group: Optional[dict], depth: int = 0) -> None:
        for offer in _as_list(offers):
            if not isinstance(offer, dict) or depth > 3:
                continue
            raw_price = offer.get("price")
            if raw_price is None or raw_price == "":
                raw_price = offer.get("lowPrice")
            if raw_price is not None and not isinstance(raw_price, (dict, list, bool)):
                try:
                    p, _ = _parse_price(str(raw_price), machine_readable=True)
                except Exception:
                    p = None
                if p is not None:
                    currency = offer.get("priceCurrency")
                    availability = offer.get("availability")
                    candidates.append((
                        p,
                        currency if isinstance(currency, str) and currency else None,
                        availability if isinstance(availability, str) else "",
                        product,
                        group,
                    ))
            # AggregateOffer may carry the individual offers as well
            if "offers" in offer:
                _add_offers(offer.get("offers"), product, group, depth + 1)

    for product, group in products:
        _add_offers(product.get("offers"), product, group)
    return candidates


def _jsonld_wanted_ids(url: str) -> set[str]:
    """Variant / SKU identifiers named in the watched URL's query string."""
    qs = parse_qs(urlparse(url).query)
    return {
        v.strip()
        for key in ("variation", "variant", "sku")
        for v in qs.get(key, [])
        if v.strip()
    }


def _jsonld_matches(candidate: _JsonLdCandidate, wanted: set[str]) -> bool:
    _, _, _, product, _ = candidate
    for node in (product, *[o for o in _as_list(product.get("offers")) if isinstance(o, dict)]):
        for key in ("sku", "productID", "@id"):
            value = node.get(key)
            if isinstance(value, (str, int)) and str(value) in wanted:
                return True
        offer_url = node.get("url")
        if isinstance(offer_url, str) and offer_url:
            if wanted & _jsonld_wanted_ids(offer_url):
                return True
    return False


# ---------------------------------------------------------------------------
# Step 3: Open Graph / meta tags
# ---------------------------------------------------------------------------

def _try_meta(
    soup: BeautifulSoup,
    final_url: str,
    title: Optional[str],
    http_status: Optional[int],
    raw_path: Optional[str],
) -> Optional[FetchResult]:
    def _meta(name: str) -> Optional[str]:
        tag = soup.find("meta", attrs={"property": name}) or soup.find(
            "meta", attrs={"name": name}
        )
        if tag and tag.get("content"):
            return tag["content"]
        return None

    amount_str = (
        _meta("product:price:amount")
        or _meta("og:price:amount")
        or _meta("product:price:currency")  # fallback order intentional; checked below
    )
    # We need to be specific — re-check in proper order
    amount_str = _meta("product:price:amount") or _meta("og:price:amount")
    if not amount_str:
        return None

    try:
        price, detected_currency = _parse_price(amount_str, machine_readable=True)
    except Exception:
        return None

    currency = _meta("product:price:currency") or _meta("og:price:currency") or detected_currency

    return FetchResult(
        status=FetchStatus.OK,
        final_url=final_url,
        title=title,
        price=price,
        currency=currency,
        http_status=http_status,
        raw_html_path=raw_path,
    )


# ---------------------------------------------------------------------------
# Step 4: Microdata
# ---------------------------------------------------------------------------

def _try_microdata(
    html: str,
    url: str,
    final_url: str,
    title: Optional[str],
    http_status: Optional[int],
    raw_path: Optional[str],
) -> Optional[FetchResult]:
    try:
        data = extruct.extract(html, base_url=url, syntaxes=["microdata"])
    except Exception as exc:
        log.debug("extruct microdata failed for %s: %s", url, exc)
        return None

    items = data.get("microdata", [])

    def _walk(node: object) -> Optional[str]:
        if isinstance(node, dict):
            props = node.get("properties", {})
            # Direct price property
            price_val = props.get("price")
            if price_val:
                if isinstance(price_val, list) and price_val:
                    return str(price_val[0])
                if isinstance(price_val, str):
                    return price_val
            # Recurse
            for v in props.values():
                if isinstance(v, list):
                    for child in v:
                        found = _walk(child)
                        if found:
                            return found
        return None

    for item in items:
        raw_price = _walk(item)
        if raw_price:
            try:
                price, currency = _parse_price(raw_price)
                return FetchResult(
                    status=FetchStatus.OK,
                    final_url=final_url,
                    title=title,
                    price=price,
                    currency=currency,
                    http_status=http_status,
                    raw_html_path=raw_path,
                )
            except Exception:
                continue

    return None


# ---------------------------------------------------------------------------
# Step 5: CSS selector
# ---------------------------------------------------------------------------

def _try_selector(
    soup: BeautifulSoup,
    selector: str,
    final_url: str,
    title: Optional[str],
    http_status: Optional[int],
    raw_path: Optional[str],
) -> Optional[FetchResult]:
    el = soup.select_one(selector)
    if el is None:
        return None
    text = el.get_text(strip=True)
    if not text:
        return None
    try:
        price, currency = _parse_price(text)
        return FetchResult(
            status=FetchStatus.OK,
            final_url=final_url,
            title=title,
            price=price,
            currency=currency,
            http_status=http_status,
            raw_html_path=raw_path,
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Step 6: Playwright fallback
# ---------------------------------------------------------------------------

def _playwright_fallback(
    url: str,
    selector: Optional[str],
    http_status: Optional[int],
    raw_path: Optional[str],
    prior_result: FetchResult,
) -> FetchResult:
    try:
        from playwright.sync_api import sync_playwright, Error as PlaywrightError
    except ImportError:
        # Logged (not printed): scheduled runs use pythonw.exe, which has no console.
        log.warning(
            "Playwright is not installed. "
            "Install it with: pip install playwright && playwright install chromium"
        )
        return prior_result

    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(headless=True)
            except Exception as exc:
                first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
                log.warning(
                    "Playwright Chromium browser not available (%s). "
                    "Run: python -m playwright install chromium",
                    first_line,
                )
                return prior_result

            page = browser.new_page()
            try:
                page.goto(url, wait_until="networkidle", timeout=30_000)
                html = page.content()
                final_url = page.url
            except PlaywrightError as exc:
                log.warning("Playwright navigation failed for %s: %s", url, exc)
                browser.close()
                return prior_result
            finally:
                browser.close()

        # Save rendered HTML too
        raw_path = _save_raw(url + "#playwright", html)

        result = _extract_from_html(
            html, url, final_url, selector, http_status, raw_path
        )

        # Carry over in-stock info from prior if still missing
        if result.in_stock is None and prior_result.in_stock is not None:
            result.in_stock = prior_result.in_stock

        return result

    except Exception as exc:
        log.warning("Playwright fallback error for %s: %s", url, exc)
        return prior_result


# ---------------------------------------------------------------------------
# Title extraction
# ---------------------------------------------------------------------------

def _extract_title(soup: BeautifulSoup) -> Optional[str]:
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return og["content"].strip()
    t = soup.find("title")
    if t:
        return t.get_text(strip=True)
    return None


# ---------------------------------------------------------------------------
# Price parsing
# ---------------------------------------------------------------------------

# Currency symbol/code -> ISO code
_CURRENCY_MAP: dict[str, str] = {
    "₪": "ILS",
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "NIS": "ILS",
    "ILS": "ILS",
    'ש"ח': "ILS",
    "ש״ח": "ILS",  # with a gershayim (U+05F4)
    "ש''ח": "ILS",
    "USD": "USD",
    "US$": "USD",
    "EUR": "EUR",
    "GBP": "GBP",
    "JPY": "JPY",
    "A$": "AUD",
    "AU$": "AUD",
    "AUD": "AUD",
    "C$": "CAD",
    "CA$": "CAD",
    "CAD": "CAD",
    "CHF": "CHF",
}

# Ordered longest-first so "NIS" matches before "N"
_CURRENCY_TOKENS = sorted(_CURRENCY_MAP.keys(), key=len, reverse=True)


def _parse_price(text: str, *, machine_readable: bool = False) -> tuple[Decimal, Optional[str]]:
    """Parse a price string into (Decimal, currency_code | None).

    Handles both US/UK style (1,299.90) and EU style (1.299,90).
    With a single kind of separator occurring once, it is the decimal point
    unless exactly three digits follow (``1,299`` / ``1.299``), which reads as
    a thousands group -- except for a ``0`` integer part, and except for a dot
    when *machine_readable* is set (JSON-LD / meta values such as ``19.990``
    use a dot as decimal point by definition).
    Raises ValueError if no valid number can be extracted.
    """
    text = text.strip()

    # Detect and remove currency symbols / codes
    detected_currency: Optional[str] = None
    for token in _CURRENCY_TOKENS:
        if token in text:
            detected_currency = _CURRENCY_MAP[token]
            text = text.replace(token, "")
            break

    # Scientific notation ("1e3") is not a price; stripping the "e" would
    # silently turn it into 13.
    if re.search(r"\d[eE][+\-]?\d", text):
        raise ValueError(f"Not a plain price: {text!r}")

    # Remove leftover whitespace and miscellaneous non-numeric chars except . , -
    text = re.sub(r"[^\d.,\-]", "", text).strip()

    if not text:
        raise ValueError("No numeric content after stripping currency")

    dot_pos = text.rfind(".")
    comma_pos = text.rfind(",")

    if dot_pos != -1 and comma_pos != -1:
        # Both present — the one that appears LAST is the decimal separator
        if dot_pos > comma_pos:
            # dot is decimal, comma is thousands
            normalised = text.replace(",", "")
        else:
            # comma is decimal, dot is thousands
            normalised = text.replace(".", "").replace(",", ".")
    elif dot_pos != -1 or comma_pos != -1:
        sep, pos = (".", dot_pos) if dot_pos != -1 else (",", comma_pos)
        digits_after = len(text) - pos - 1
        int_part = text[:pos].lstrip("-")
        if text.count(sep) > 1 or digits_after == 0:
            is_decimal = False  # "1.299.000" / "1,299,000" / "10."
        elif digits_after != 3:
            is_decimal = True  # "89.9", "89.90", "89.9000"
        else:  # exactly 3 digits: thousands group or 3-decimal price?
            is_decimal = int_part in ("", "0") or (sep == "." and machine_readable)
        normalised = text.replace(sep, ".") if is_decimal else text.replace(sep, "")
    else:
        normalised = text

    try:
        return Decimal(normalised), detected_currency
    except InvalidOperation as exc:
        raise ValueError(f"Cannot convert '{normalised}' to Decimal") from exc
