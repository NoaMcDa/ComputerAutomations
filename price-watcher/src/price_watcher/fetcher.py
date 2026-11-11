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
        return FetchResult(
            status=FetchStatus.BLOCKED,
            final_url=final_url,
            http_status=http_status,
            error_message=f"Blocked (HTTP {http_status})",
        )

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

def _try_shopify(
    url: str,
    final_url: str,
    title: Optional[str],
    http_status: Optional[int],
    raw_path: Optional[str],
) -> Optional[FetchResult]:
    parsed = urlparse(url)
    path_parts = parsed.path.strip("/").split("/")
    if len(path_parts) < 2 or path_parts[0] != "products":
        return None

    handle = path_parts[1]
    origin = f"{parsed.scheme}://{parsed.netloc}"
    js_url = f"{origin}/products/{handle}.js"

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

    variants = data.get("variants", [])
    if not variants:
        return None

    variant = None
    if variant_id:
        for v in variants:
            if str(v.get("id")) == str(variant_id):
                variant = v
                break

    if variant is None:
        # Pick first available, or first overall
        variant = next((v for v in variants if v.get("available")), variants[0])

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

    items = data.get("json-ld", [])
    if not items:
        return None

    # Flatten @graph entries
    flat: list[dict] = []
    for item in items:
        if item.get("@type") == "Graph" or "@graph" in item:
            flat.extend(item.get("@graph", []))
        else:
            flat.append(item)

    # Find Product
    product: Optional[dict] = None
    for item in flat:
        t = item.get("@type", "")
        if t == "Product" or (isinstance(t, list) and "Product" in t):
            product = item
            break

    if product is None:
        return None

    offers_raw = product.get("offers")
    if offers_raw is None:
        return None

    # Normalise offers to a list
    if isinstance(offers_raw, dict):
        offers = [offers_raw]
    elif isinstance(offers_raw, list):
        offers = offers_raw
    else:
        return None

    # Pick lowest price among valid offers
    best_price: Optional[Decimal] = None
    best_currency: Optional[str] = None
    in_stock: Optional[bool] = None

    for offer in offers:
        raw_price = offer.get("price")
        if raw_price is None:
            continue
        try:
            p, _ = _parse_price(str(raw_price))
        except Exception:
            continue

        if best_price is None or p < best_price:
            best_price = p
            best_currency = offer.get("priceCurrency")
            availability = offer.get("availability", "")
            if "OutOfStock" in availability:
                in_stock = False
            elif "InStock" in availability:
                in_stock = True

    if best_price is None:
        return None

    prod_title = product.get("name") or title

    return FetchResult(
        status=FetchStatus.OK,
        final_url=final_url,
        title=prod_title,
        price=best_price,
        currency=best_currency,
        in_stock=in_stock,
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
        price, detected_currency = _parse_price(amount_str)
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
    "USD": "USD",
    "EUR": "EUR",
    "GBP": "GBP",
}

# Ordered longest-first so "NIS" matches before "N"
_CURRENCY_TOKENS = sorted(_CURRENCY_MAP.keys(), key=len, reverse=True)


def _parse_price(text: str) -> tuple[Decimal, Optional[str]]:
    """Parse a price string into (Decimal, currency_code | None).

    Handles both US/UK style (1,299.90) and EU style (1.299,90).
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

    # Remove leftover whitespace and miscellaneous non-numeric chars except . , -
    text = re.sub(r"[^\d.,\-]", "", text).strip()

    if not text:
        raise ValueError("No numeric content after stripping currency")

    # Determine decimal separator: whichever separator appears LAST
    # and has 1 or 2 digits after it is the decimal separator.
    dot_pos = text.rfind(".")
    comma_pos = text.rfind(",")

    after_dot = len(text) - dot_pos - 1 if dot_pos != -1 else -1
    after_comma = len(text) - comma_pos - 1 if comma_pos != -1 else -1

    dot_is_decimal = dot_pos != -1 and 1 <= after_dot <= 2
    comma_is_decimal = comma_pos != -1 and 1 <= after_comma <= 2

    if dot_is_decimal and comma_is_decimal:
        # Both qualify — the one that appears LAST is the decimal separator
        if dot_pos > comma_pos:
            # dot is decimal, comma is thousands
            normalised = text.replace(",", "")
        else:
            # comma is decimal, dot is thousands
            normalised = text.replace(".", "").replace(",", ".")
    elif dot_is_decimal:
        normalised = text.replace(",", "")
    elif comma_is_decimal:
        normalised = text.replace(".", "").replace(",", ".")
    else:
        # Neither qualifies as decimal separator — treat as integer
        normalised = text.replace(",", "").replace(".", "")

    try:
        return Decimal(normalised), detected_currency
    except InvalidOperation as exc:
        raise ValueError(f"Cannot convert '{normalised}' to Decimal") from exc
