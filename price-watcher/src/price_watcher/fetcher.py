"""Fetcher + extractor for Price Watcher.

Public API
----------
fetch(url, selector=None, *, use_browser_fallback=True) -> FetchResult

Extraction ladder (stops at the first step that gives a result):
  1. Shopify product JSON  (/products/<handle>.js)
  2. JSON-LD Product/Offer  (via extruct)
  3. Open Graph / meta tags
  4. Microdata              (itemprop="price", skipping struck-through prices)
  5. CSS selector           (if `selector` arg provided)
  6. Playwright fallback    (reruns 2-5 on rendered HTML)

A blocked (403/429/challenge) Shopify page is rescued through the product JSON,
with the currency read from /cart.js or /meta.json.
"""

from __future__ import annotations

import copy
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
    # --- HTTP fetch with retries ---
    response, error_msg, http_status, final_url = _http_get(url)

    if response is None:
        return FetchResult(
            status=FetchStatus.UNREACHABLE,
            final_url=final_url or url,
            http_status=http_status,
            error_message=error_msg,
        )

    if http_status in (404, 410):
        return FetchResult(
            status=FetchStatus.NOT_FOUND,
            final_url=final_url,
            http_status=http_status,
            error_message=f"HTTP {http_status}: page moved or removed",
        )

    html = response.text

    # Check for block / captcha
    if http_status in (403, 429) or (http_status == 200 and _is_challenge_page(html)):
        # A Shopify store often blocks the HTML page but still serves the
        # product JSON, so try that before giving up.
        rescued = _try_shopify(url, final_url, None, http_status, None)
        if rescued is not None:
            if rescued.status == FetchStatus.OK and rescued.currency is None:
                rescued = dataclasses.replace(
                    rescued, currency=_shopify_currency(_origin(url))
                )
            log.info("HTML blocked for %s; used the Shopify product JSON", url)
            return rescued
        return FetchResult(
            status=FetchStatus.BLOCKED,
            final_url=final_url,
            http_status=http_status,
            error_message=f"Blocked (HTTP {http_status})",
        )

    # Save raw HTML for debugging
    raw_path = _save_raw(url, html)

    # --- Extraction ladder ---
    result = _extract(html, url, final_url, selector, http_status, raw_path)

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
    Non-ASCII (e.g. Hebrew) URLs are percent-encoded by httpx for the request;
    the caller keeps the readable form for display.
    """
    last_error: Optional[str] = None
    final_url = url

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
            elif status == 429 and attempt < len(_RETRY_DELAYS):
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                if retry_after is not None and retry_after <= 60:
                    log.warning("HTTP 429 on %s; honouring Retry-After=%ss", url, retry_after)
                    time.sleep(retry_after)
                    continue
                return response, None, status, final_url
            else:
                return response, None, status, final_url

        except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
            last_error = str(exc)
            log.warning("Network error on attempt %d for %s: %s", attempt + 1, url, exc)

        # Sleep before next retry (delay is None on the last sentinel iteration)
        if delay is not None:
            time.sleep(delay)

    return None, last_error, None, final_url


def _get_json(url: str) -> Optional[object]:
    """GET *url* and decode JSON; None on any failure (single attempt)."""
    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=20,
            headers=_DEFAULT_HEADERS,
        ) as client:
            resp = client.get(url)
    except Exception as exc:
        log.debug("JSON fetch failed for %s: %s", url, exc)
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except Exception:
        return None


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


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


def _shopify_currency(origin: str) -> Optional[str]:
    """Store currency from Shopify's cheap JSON endpoints.

    The product JSON (`/products/<handle>.js`) carries no currency, so when the
    HTML page is unavailable (blocked) the currency comes from `/cart.js`
    (`currency`) or, failing that, `/meta.json` (`currency`).
    """
    for path in ("/cart.js", "/meta.json"):
        data = _get_json(origin + path)
        if isinstance(data, dict):
            cur = data.get("currency")
            if isinstance(cur, str) and re.fullmatch(r"[A-Za-z]{3}", cur.strip()):
                return cur.strip().upper()
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
    *,
    include_shopify: bool = True,
) -> FetchResult:
    """Run the extraction ladder and return a FetchResult.

    `include_shopify=False` skips step 1 (used for the Playwright fallback,
    which only re-reads rendered HTML).

    A step may return a non-OK result (e.g. NOT_FOUND for a pinned variant
    that is gone); that ends the ladder, so a later step can never silently
    substitute another variant's price.
    """
    soup = BeautifulSoup(html, "html.parser")
    title = _extract_title(soup)

    # 1. Shopify product JSON
    if include_shopify:
        result = _try_shopify(url, final_url, title, http_status, raw_path)
        if result is not None:
            if result.status == FetchStatus.OK and result.currency is None:
                currency = _currency_from_html(soup) or _shopify_currency(_origin(url))
                result = dataclasses.replace(result, currency=currency)
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
    result = _try_microdata(soup, final_url, title, http_status, raw_path)
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


# ---------------------------------------------------------------------------
# Step 1: Shopify
# ---------------------------------------------------------------------------

def _try_shopify(
    url: str,
    final_url: str,
    title: Optional[str],
    http_status: Optional[int],
    raw_path: Optional[str],
) -> Optional[FetchResult]:
    """Read `/products/<handle>.js`.

    Returns None when the URL is not a Shopify product URL or the JSON is not
    available. When the URL pins `?variant=<id>` and that variant is not in the
    product, returns NOT_FOUND (never another variant's price).
    """
    parsed = urlparse(url)
    path_parts = parsed.path.strip("/").split("/")
    if len(path_parts) < 2 or path_parts[0] != "products":
        return None

    handle = path_parts[1]
    js_url = f"{_origin(url)}/products/{handle}.js"

    qs = parse_qs(parsed.query)
    variant_id: Optional[str] = qs["variant"][0] if qs.get("variant") else None

    data = _get_json(js_url)
    if not isinstance(data, dict):
        return None

    variants = data.get("variants") or []
    if not variants:
        return None

    prod_title = data.get("title") or title

    if variant_id:
        variant = next((v for v in variants if str(v.get("id")) == str(variant_id)), None)
        if variant is None:
            return FetchResult(
                status=FetchStatus.NOT_FOUND,
                final_url=final_url,
                title=prod_title,
                http_status=http_status,
                raw_html_path=raw_path,
                error_message=f"Shopify variant {variant_id} not found in product",
            )
    else:
        # No variant pinned: the product's default (first available, else first)
        variant = next((v for v in variants if v.get("available")), variants[0])

    price = _shopify_money(variant.get("price"))
    if price is None:
        return None
    compare_at = _shopify_money(variant.get("compare_at_price"))
    regular_price = compare_at if compare_at is not None and compare_at > price else None

    currency = data.get("currency") or variant.get("currency")
    available = variant.get("available")
    in_stock = bool(available) if available is not None else None

    return FetchResult(
        status=FetchStatus.OK,
        final_url=final_url,
        title=prod_title,
        price=price,
        currency=currency,
        regular_price=regular_price,
        in_stock=in_stock,
        http_status=http_status,
        raw_html_path=raw_path,
    )


def _shopify_money(cents: object) -> Optional[Decimal]:
    """Shopify `.js` prices are integer cents."""
    if cents is None or isinstance(cents, bool):
        return None
    try:
        return Decimal(str(cents)) / Decimal("100")
    except InvalidOperation:
        return None


# ---------------------------------------------------------------------------
# Step 2: JSON-LD
# ---------------------------------------------------------------------------

# Query parameters that pin a variant in the watched URL.
_VARIANT_PARAMS = ("variation", "variant", "sku")

# JSON-LD keys that identify a variant / offer.
_JSONLD_ID_KEYS = ("sku", "productID", "gtin", "gtin8", "gtin12", "gtin13", "gtin14", "mpn")


def _jsonld_wanted_ids(url: str) -> set[str]:
    qs = parse_qs(urlparse(url).query)
    return {v.strip() for k in _VARIANT_PARAMS for v in qs.get(k, []) if v.strip()}


def _jsonld_type_is(node: dict, *names: str) -> bool:
    t = node.get("@type", "")
    types = t if isinstance(t, list) else [t]
    for item in types:
        if isinstance(item, str) and item.rsplit("/", 1)[-1] in names:
            return True
    return False


def _jsonld_flatten(items: list) -> list[dict]:
    flat: list[dict] = []
    stack = list(items)
    while stack:
        item = stack.pop(0)
        if isinstance(item, list):
            stack[0:0] = item
        elif isinstance(item, dict):
            if "@graph" in item:
                stack[0:0] = list(item.get("@graph") or [])
            else:
                flat.append(item)
    return flat


def _as_list(value: object) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _node_ids(node: dict) -> set[str]:
    """Variant identifiers exposed by one JSON-LD node (Product or Offer)."""
    ids: set[str] = set()
    for key in _JSONLD_ID_KEYS:
        for v in _as_list(node.get(key)):
            if isinstance(v, (str, int)) and str(v).strip():
                ids.add(str(v).strip())
    for key in ("url", "@id"):
        for v in _as_list(node.get(key)):
            if isinstance(v, str):
                ids |= _jsonld_wanted_ids(v)
    return ids


def _jsonld_candidates(product: dict) -> list[tuple[dict, dict]]:
    """All (offer, owning product) pairs under a Product / ProductGroup."""
    owners = [product] + [
        v for v in _as_list(product.get("hasVariant")) if isinstance(v, dict)
    ]
    out: list[tuple[dict, dict]] = []
    for owner in owners:
        stack = [o for o in _as_list(owner.get("offers")) if isinstance(o, dict)]
        while stack:
            offer = stack.pop(0)
            nested = [o for o in _as_list(offer.get("offers")) if isinstance(o, dict)]
            if nested:  # AggregateOffer with individual offers
                stack[0:0] = nested
                if offer.get("price") is None and offer.get("lowPrice") is None:
                    continue
            out.append((offer, owner))
    return out


def _offer_price(offer: dict) -> tuple[Optional[Decimal], Optional[str]]:
    raw = offer.get("price")
    if raw is None:
        raw = offer.get("lowPrice")
    currency = offer.get("priceCurrency")
    if raw is None:
        for spec in _as_list(offer.get("priceSpecification")):
            if isinstance(spec, dict) and spec.get("price") is not None:
                raw = spec.get("price")
                currency = currency or spec.get("priceCurrency")
                break
    if raw is None:
        return None, None
    try:
        price, detected = _parse_price(str(raw))
    except ValueError:
        return None, None
    return price, currency or detected


def _availability(value: object) -> Optional[bool]:
    text = str(value or "").lower().replace(" ", "").replace("_", "")
    if not text:
        return None
    if any(k in text for k in ("outofstock", "soldout", "discontinued", "oos")):
        return False
    if any(k in text for k in ("instock", "limitedavailability", "onlineonly", "instoreonly", "preorder")):
        return True
    return None


def _try_jsonld(
    html: str,
    url: str,
    final_url: str,
    title: Optional[str],
    http_status: Optional[int],
    raw_path: Optional[str],
) -> Optional[FetchResult]:
    """Read JSON-LD Product / ProductGroup offers.

    Variant selection:
      * URL names a variant (?variation= / ?variant= / ?sku=) and the page
        exposes variant ids -> use the matching offer; if none matches, return
        NOT_FOUND (never another variant's price).
      * URL names no variant, or the page exposes no ids at all -> the lowest
        offer price (the product's default "from" price).
    """
    try:
        data = extruct.extract(html, base_url=url, syntaxes=["json-ld"])
    except Exception as exc:
        log.debug("extruct json-ld failed for %s: %s", url, exc)
        return None

    flat = _jsonld_flatten(data.get("json-ld", []))
    product = next((n for n in flat if _jsonld_type_is(n, "ProductGroup", "Product")), None)
    if product is None:
        return None

    candidates = []
    for offer, owner in _jsonld_candidates(product):
        price, currency = _offer_price(offer)
        if price is not None:
            candidates.append((offer, owner, price, currency))
    if not candidates:
        return None

    pool = candidates
    wanted = _jsonld_wanted_ids(url)
    if wanted:
        exposes_ids = False
        matching = []
        for cand in candidates:
            offer, owner = cand[0], cand[1]
            ids = _node_ids(offer)
            if owner is not product:  # a hasVariant entry: its ids are variant ids
                ids |= _node_ids(owner)
            elif len(candidates) == 1:
                ids |= _node_ids(owner)
            if ids:
                exposes_ids = True
            if ids & wanted:
                matching.append(cand)
        if matching:
            pool = matching
        elif exposes_ids:
            return FetchResult(
                status=FetchStatus.NOT_FOUND,
                final_url=final_url,
                title=product.get("name") or title,
                http_status=http_status,
                raw_html_path=raw_path,
                error_message=f"variant {', '.join(sorted(wanted))} not found in JSON-LD",
            )

    offer, owner, price, currency = min(pool, key=lambda c: c[2])
    name = owner.get("name") if owner is not product else None

    return FetchResult(
        status=FetchStatus.OK,
        final_url=final_url,
        title=name or product.get("name") or title,
        price=price,
        currency=currency,
        in_stock=_availability(offer.get("availability")),
        http_status=http_status,
        raw_html_path=raw_path,
    )


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

    amount_str = _meta("product:price:amount") or _meta("og:price:amount")
    if not amount_str:
        return None

    try:
        price, detected_currency = _parse_price(amount_str)
    except ValueError:
        return None

    currency = _meta("product:price:currency") or _meta("og:price:currency") or detected_currency
    in_stock = _availability(_meta("product:availability") or _meta("og:availability"))

    return FetchResult(
        status=FetchStatus.OK,
        final_url=final_url,
        title=title,
        price=price,
        currency=currency,
        in_stock=in_stock,
        http_status=http_status,
        raw_html_path=raw_path,
    )


# ---------------------------------------------------------------------------
# Struck-through (old) prices
# ---------------------------------------------------------------------------

_STRUCK_TAGS = ("del", "s", "strike")


def _is_struck(tag) -> bool:
    if getattr(tag, "name", None) in _STRUCK_TAGS:
        return True
    style = (tag.get("style") or "") if hasattr(tag, "get") else ""
    return "line-through" in style.replace(" ", "").lower()


def _inside_struck(el) -> bool:
    node = el
    while node is not None and getattr(node, "name", None) not in (None, "[document]"):
        if _is_struck(node):
            return True
        node = node.parent
    return False


# ---------------------------------------------------------------------------
# Step 4: Microdata
# ---------------------------------------------------------------------------

def _microdata_value(el) -> str:
    return (el.get("content") or el.get("value") or el.get_text(" ", strip=True) or "").strip()


def _try_microdata(
    soup: BeautifulSoup,
    final_url: str,
    title: Optional[str],
    http_status: Optional[int],
    raw_path: Optional[str],
) -> Optional[FetchResult]:
    """Read `itemprop="price"`.

    The first price that is not inside <del>/<s>/<strike> (or a line-through
    style) is the price charged; a struck-through one is the regular price.
    """
    current: Optional[tuple[Decimal, Optional[str]]] = None
    struck: list[Decimal] = []
    for el in soup.find_all(attrs={"itemprop": "price"}):
        try:
            price, detected = _parse_price(_microdata_value(el))
        except ValueError:
            continue
        if _inside_struck(el):
            struck.append(price)
        elif current is None:
            current = (price, detected)
    if current is None:
        return None

    price, currency = current
    cur_el = soup.find(attrs={"itemprop": "priceCurrency"})
    if cur_el is not None and _microdata_value(cur_el):
        currency = _microdata_value(cur_el).upper()
    avail_el = soup.find(attrs={"itemprop": "availability"})
    in_stock = None
    if avail_el is not None:
        in_stock = _availability(avail_el.get("href") or _microdata_value(avail_el))
    regular = max((p for p in struck if p > price), default=None)

    return FetchResult(
        status=FetchStatus.OK,
        final_url=final_url,
        title=title,
        price=price,
        currency=currency,
        regular_price=regular,
        in_stock=in_stock,
        http_status=http_status,
        raw_html_path=raw_path,
    )


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
    """Read the price from the element matched by *selector*.

    When the element holds an old and a sale price (`<del>₪129.90</del>
    <ins>₪99.90</ins>`), the struck-through part is the regular price and the
    rest is the price charged. Plain text that still holds several different
    prices is refused by `_parse_price` (narrow the selector instead).
    """
    try:
        el = soup.select_one(selector)
    except Exception as exc:  # invalid selector syntax
        log.warning("Invalid CSS selector %r: %s", selector, exc)
        return None
    if el is None:
        return None

    el = copy.copy(el)
    regular_texts: list[str] = []
    if not _is_struck(el):
        for struck in el.find_all(_is_struck):
            if struck.parent is None:  # already removed with an ancestor
                continue
            regular_texts.append(struck.get_text(" ", strip=True))
            struck.decompose()

    text = el.get_text(" ", strip=True)
    if not text:
        return None
    try:
        price, currency = _parse_price(text)
    except ValueError as exc:
        log.info("CSS selector %r: %s", selector, exc)
        return None

    regular: Optional[Decimal] = None
    for rt in regular_texts:
        try:
            p, _ = _parse_price(rt)
        except ValueError:
            continue
        if p > price and (regular is None or p > regular):
            regular = p

    return FetchResult(
        status=FetchStatus.OK,
        final_url=final_url,
        title=title,
        price=price,
        currency=currency,
        regular_price=regular,
        http_status=http_status,
        raw_html_path=raw_path,
    )


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
        print(
            "price-watcher: Playwright is not installed. "
            "Install it with: pip install playwright && playwright install chromium"
        )
        return prior_result

    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(headless=True)
            except Exception as exc:
                print(
                    f"price-watcher: Playwright Chromium browser not available ({exc}). "
                    "Run: playwright install chromium"
                )
                return prior_result

            try:
                page = browser.new_page()
                page.goto(url, wait_until="networkidle", timeout=30_000)
                html = page.content()
                final_url = page.url
            except PlaywrightError as exc:
                log.warning("Playwright navigation failed for %s: %s", url, exc)
                return prior_result
            finally:
                browser.close()

        # Save rendered HTML too
        raw_path = _save_raw(url + "#playwright", html)

        result = _extract(
            html, url, final_url, selector, http_status, raw_path, include_shopify=False
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
    'ש"ח': "ILS",
    "ש״ח": "ILS",
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "NIS": "ILS",
    "ILS": "ILS",
    "USD": "USD",
    "EUR": "EUR",
    "GBP": "GBP",
}

# Symbols match anywhere; letter codes only as whole upper-case words, so
# "Details 99" is not read as ILS.
_CURRENCY_RE = "|".join(
    rf"(?<![A-Za-z]){t}(?![A-Za-z])" if t.isascii() and t.isalpha() else re.escape(t)
    for t in sorted(_CURRENCY_MAP, key=len, reverse=True)
)
_CURRENCY_BEFORE = re.compile(rf"(?:{_CURRENCY_RE})\s*$")
_CURRENCY_AFTER = re.compile(rf"^\s*(?:{_CURRENCY_RE})")
_CURRENCY_ANY = re.compile(_CURRENCY_RE)

# One number token: digits joined by separators. Plain spaces are NOT joined
# (so "129.90 99.90" is two tokens); no-break spaces are, as thousands marks.
_NUMBER_TOKEN = re.compile(r"\d+(?:[.,'  ]\d+)*")


def _currency_code(token: str) -> str:
    return _CURRENCY_MAP.get(token.strip(), token.strip())


def _number_from_token(token: str) -> Decimal:
    """Turn ONE number token into a Decimal.

    * both '.' and ',' present -> the last one is the decimal mark
      (1,299.90 / 1.299,90);
    * the same mark repeated -> thousands marks (1.299.000);
    * a single mark -> decimal unless exactly three digits follow it
      (1,299 = 1299; 129,9 = 129.9; 0.999 = 0.999);
    * thousands groups must be 3 digits, so two prices glued together
      ("129.9099.90") are rejected instead of becoming 129909990.
    """
    seps = re.findall(r"[.,'  ]", token)
    groups = re.split(r"[.,'  ]", token)
    if not seps:
        return Decimal(token)

    last = seps[-1]
    decimal_part: Optional[str] = None
    if last in ".," and seps.count(last) == 1:
        if len(seps) > 1:
            is_decimal = True
        else:
            is_decimal = len(groups[-1]) != 3 or groups[0] == "0" or len(groups[0]) > 3
        if is_decimal:
            decimal_part = groups[-1]
            groups = groups[:-1]
            seps = seps[:-1]

    if seps:
        if len(set(seps)) != 1 or not (1 <= len(groups[0]) <= 3) or any(
            len(g) != 3 for g in groups[1:]
        ):
            raise ValueError(f"Malformed number {token!r}")

    normalised = "".join(groups) + (f".{decimal_part}" if decimal_part else "")
    try:
        return Decimal(normalised)
    except InvalidOperation as exc:
        raise ValueError(f"Cannot convert {token!r} to Decimal") from exc


def _parse_price(text: str) -> tuple[Decimal, Optional[str]]:
    """Parse a price string into (Decimal, currency_code | None).

    Handles currency symbols/codes (₪ ש"ח NIS $ € £ ...), thousands marks and
    both 1,299.90 and 1.299,90 styles. Raises ValueError when no single price
    can be read.

    Several numbers in one string are never concatenated. They are resolved as:
      * all numbers equal -> that number;
      * exactly one distinct value has a currency marker next to it -> that one
        ("Pack of 2 - ₪49.90" -> 49.90);
      * otherwise (e.g. "₪129.90 ₪99.90", "Was ₪129.90 Now ₪99.90") -> refused.
        Which of two prices is charged depends on the shop's layout (old-then-new
        or new-then-old), so guessing could record the regular price as the
        price. Callers that see markup (CSS selector, microdata) drop
        <del>/<s>/<strike> parts first; otherwise the user narrows the selector.
    """
    tokens = []  # (value, currency | None)
    for m in _NUMBER_TOKEN.finditer(text):
        value = _number_from_token(m.group(0))
        before = _CURRENCY_BEFORE.search(text[: m.start()])
        after = _CURRENCY_AFTER.search(text[m.end():])
        cur_tok = before.group(0) if before else (after.group(0) if after else None)
        tokens.append((value, _currency_code(cur_tok) if cur_tok else None))

    if not tokens:
        raise ValueError(f"No price in {text!r}")

    values = {v for v, _ in tokens}
    if len(values) == 1:
        chosen = tokens[0][0]
    else:
        with_currency = {v for v, c in tokens if c}
        if len(with_currency) != 1:
            raise ValueError(f"Several prices in {text!r}; refusing to guess")
        chosen = with_currency.pop()

    currency = next((c for v, c in tokens if v == chosen and c), None)
    if currency is None:
        any_cur = _CURRENCY_ANY.search(text)
        currency = _currency_code(any_cur.group(0)) if any_cur else None
    return chosen, currency
