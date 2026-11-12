"""Creatine price watcher.

Scrapes iHerb's creatine search results. A plain httpx request is tried first;
when Cloudflare answers it with a challenge (or the page has no product cells)
the pages are loaded in headless Chromium (Playwright) instead. iHerb
pre-computes a per-unit price in each product cell ("₪9.49/100g", "$0.71/oz",
"₪94.90/kg" ...); we normalise mass units to price-per-100g.

Tracks the cheapest price-per-100g across plain creatine *monohydrate powder*
products (gummies, capsules, blends and flavoured mixes are filtered out by
name) and emails when it moves. Separate state file from the normal product
watches, same SMTP path.

Designed to be extensible — `scan_iherb()` returns a list of CreatineProduct;
adding another seller means adding another `scan_*()` function and merging.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Iterator, Optional
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
from rich.console import Console
from rich.table import Table

from price_watcher import logging_setup
from price_watcher.http_headers import USER_AGENT
from price_watcher.paths import data_dir

log = logging_setup.get(__name__)
console = Console()


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class CreatineProduct:
    name: str
    url: str
    price: Decimal
    currency: str
    price_per_100g: Optional[Decimal]  # None for capsule products (per-count instead)
    unit: str  # "100g" / "count" / "100ml" / "unknown"
    on_sale: bool
    seller: str


# ---------------------------------------------------------------------------
# Price-cell parsing
# ---------------------------------------------------------------------------

_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "he,en;q=0.8,en-US;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
}

_IHERB_BASE = "https://il.iherb.com"
_IHERB_SEARCH_PATH = "/search"
_IHERB_KEYWORD = "creatine"
# iHerb ranks search results by relevance, so "cheapest" on page 1 is only the
# cheapest among the top hits and the set shifts as iHerb re-ranks. We read
# several pages (``p=`` is iHerb's page parameter) and stop early once a page
# adds no new products.
IHERB_MAX_PAGES = 4
# Optional extra query parameters, e.g. a sort order. iHerb's sort parameter
# could not be verified offline, so none is sent by default; set this to
# {"sr": "<value>"} once the price-sort value has been confirmed on the live site.
IHERB_EXTRA_PARAMS: dict[str, str] = {}

# Kept for callers / logs that refer to the first page.
_IHERB_SEARCH = f"{_IHERB_BASE}{_IHERB_SEARCH_PATH}?kw={_IHERB_KEYWORD}"


def search_url(page: int = 1) -> str:
    """URL of results page ``page`` (1-based) of the iHerb creatine search."""
    params: dict[str, str] = {"kw": _IHERB_KEYWORD}
    params.update(IHERB_EXTRA_PARAMS)
    if page > 1:
        params["p"] = str(page)
    return f"{_IHERB_BASE}{_IHERB_SEARCH_PATH}?{urlencode(params)}"


# Bidi controls / invisible marks that iHerb's Hebrew pages sprinkle around
# prices (LRM, RLM, ALM, embeddings, isolates, BOM).
_BIDI_RE = re.compile("[‎‏؜‪-‮⁦-⁩﻿]")

_CURRENCY_SYMBOLS = {
    "₪": "ILS", "ils": "ILS", "nis": "ILS", "ש\"ח": "ILS", "ש״ח": "ILS",
    "$": "USD", "usd": "USD", "us$": "USD",
    "€": "EUR", "eur": "EUR",
    "£": "GBP", "gbp": "GBP",
}
_CUR = r"(?:₪|US\$|\$|€|£|ILS|NIS|USD|EUR|GBP|ש\"ח|ש״ח)"
# "1,234.56" (thousands commas), "19,99" (decimal comma, EU stores), "9.49".
_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+,\d{1,2}(?![\d,])|\d+(?:\.\d+)?"

# Per-unit price, with or without parentheses: "(₪9.49/100g)", "₪9.49 / 100 גרם",
# "($0.71/oz)", "(₪94.90/kg)", "(₪0.26/count)".
_PER_UNIT_RE = re.compile(
    rf"\(?\s*(?P<cur1>{_CUR})?\s*(?P<num>{_NUM})\s*(?P<cur2>{_CUR})?\s*/\s*"
    r"(?P<unit>(?:\d+(?:\.\d+)?\s*)?(?:fl\.?\s*oz|[A-Za-z֐-׿\"״']+))\s*\)?",
    re.IGNORECASE,
)
_AMOUNT_RE = re.compile(
    rf"(?P<cur1>{_CUR})\s*(?P<n1>{_NUM})|(?P<n2>{_NUM})\s*(?P<cur2>{_CUR})",
    re.IGNORECASE,
)
_BARE_AMOUNT_RE = re.compile(rf"(?<![\d.,])(?P<n>{_NUM})(?![\d.,]*\s*%)")

_GRAMS_PER_UNIT = {
    "g": Decimal(1), "gr": Decimal(1), "gram": Decimal(1), "grams": Decimal(1),
    "גרם": Decimal(1), "ג": Decimal(1), "ג'": Decimal(1),
    "kg": Decimal(1000), "kilo": Decimal(1000), "kilogram": Decimal(1000),
    "ק\"ג": Decimal(1000), "ק״ג": Decimal(1000), "קילו": Decimal(1000), "קג": Decimal(1000),
    "oz": Decimal("28.349523125"), "ounce": Decimal("28.349523125"),
    "lb": Decimal("453.59237"), "lbs": Decimal("453.59237"), "pound": Decimal("453.59237"),
}
_ML_PER_UNIT = {
    "ml": Decimal(1), "מ\"ל": Decimal(1), "מ״ל": Decimal(1), "מל": Decimal(1),
    "l": Decimal(1000), "liter": Decimal(1000), "litre": Decimal(1000), "ליטר": Decimal(1000),
    "fl oz": Decimal("29.5735295625"), "fl. oz": Decimal("29.5735295625"),
    "floz": Decimal("29.5735295625"),
}
_COUNT_UNITS = {
    "count", "ct", "unit", "serving", "servings", "capsule", "capsules", "caps",
    "tablet", "tablets", "piece", "gummy", "gummies",
    "ליחידה", "יחידה", "מנה", "למנה", "כמוסה", "קפסולה", "טבליה",
}

_CENT = Decimal("0.01")


def _to_decimal(s: str) -> Optional[Decimal]:
    if re.fullmatch(r"\d+,\d{1,2}", s):
        s = s.replace(",", ".")  # decimal comma
    try:
        return Decimal(s.replace(",", ""))
    except InvalidOperation:
        return None


def _currency_of(sym: Optional[str]) -> Optional[str]:
    if not sym:
        return None
    return _CURRENCY_SYMBOLS.get(sym.lower()) or _CURRENCY_SYMBOLS.get(sym)


def _normalise_unit(raw: str, per_unit: Decimal) -> tuple[Optional[Decimal], str]:
    """Turn ``per_unit`` priced per ``raw`` into (price, canonical_unit).

    Mass units become price-per-100g, volume units price-per-100ml, counts stay
    as-is with unit "count". Unknown units are returned unchanged.
    """
    raw = raw.strip().lower()
    m = re.match(r"(?:(\d+(?:\.\d+)?)\s*)?(.+)$", raw)
    qty = Decimal(m.group(1)) if m and m.group(1) else Decimal(1)
    word = (m.group(2) if m else raw).strip().rstrip(".")
    if qty <= 0:
        return per_unit, raw
    if word in _GRAMS_PER_UNIT:
        grams = qty * _GRAMS_PER_UNIT[word]
        return (per_unit * 100 / grams).quantize(_CENT), "100g"
    if word in _ML_PER_UNIT:
        ml = qty * _ML_PER_UNIT[word]
        return (per_unit * 100 / ml).quantize(_CENT), "100ml"
    if word in _COUNT_UNITS:
        return per_unit, "count"
    return per_unit, word or "unknown"


def _parse_price(text: str) -> Optional[tuple[Decimal, str, Optional[Decimal], str, bool]]:
    """Parse iHerb's price cell text.

    Examples:
      '₪94.64 (₪9.49/100g)'
      '₪61.87 (₪0.26/count)'
      '₪78.64 (₪22.21/100g) 25% off ₪104.85'
      '₪104.85 ₪78.64 (₪22.21/100g)'          (old + sale price)
      'Price: $24.99 ($2.50/100 g)'
      '‏₪94.64 (₪94.90/kg)'

    Returns (price, currency, per_unit_price_or_none, unit, on_sale). Mass
    units are normalised to "100g", volume units to "100ml".
    """
    if not text:
        return None
    text = _BIDI_RE.sub("", text).replace("\xa0", " ").strip()
    if not text:
        return None

    currency: Optional[str] = None
    per_unit_price: Optional[Decimal] = None
    unit = "unknown"

    per = _PER_UNIT_RE.search(text)
    if per:
        value = _to_decimal(per.group("num"))
        if value is not None:
            per_unit_price, unit = _normalise_unit(per.group("unit"), value)
            currency = _currency_of(per.group("cur1") or per.group("cur2"))
        head, tail = text[: per.start()], text[per.end():]
    else:
        head, tail = text, ""

    # Amounts carrying a currency symbol; if the cell has none at all, fall
    # back to bare numbers (never percentages).
    def _amounts(s: str) -> list[tuple[Decimal, Optional[str]]]:
        out = []
        for m in _AMOUNT_RE.finditer(s):
            v = _to_decimal(m.group("n1") or m.group("n2"))
            if v is not None:
                out.append((v, _currency_of(m.group("cur1") or m.group("cur2"))))
        return out

    before, after = _amounts(head), _amounts(tail)
    if not before and not after:
        bare = [_to_decimal(m.group("n")) for m in _BARE_AMOUNT_RE.finditer(head)]
        before = [(v, None) for v in bare if v is not None]
    if not before and not after:
        return None

    # The per-unit figure always follows the price it belongs to, so the
    # amount right before it is the current (sale) price; any other amounts
    # are the struck-through regular price.
    if before:
        price, cur = before[-1]
    else:
        price, cur = min(after, key=lambda a: a[0])
    currency = cur or currency or next((c for _, c in before + after if c), None) or "ILS"

    others = [v for v, _ in before + after if v != price]
    lowered = text.lower()
    on_sale = "% off" in lowered or "הנחה" in text or any(v > price for v in others)
    return price, currency, per_unit_price, unit, on_sale


# ---------------------------------------------------------------------------
# Product filter: plain monohydrate powder only
# ---------------------------------------------------------------------------

_CREATINE_RE = re.compile(r"creatin|קריאטין|creapure", re.IGNORECASE)
_EXCLUDE_RE = re.compile(
    r"gumm|chew|capsul|\bcaps\b|tablet|softgel|\bblend|complex|matrix|stack|"
    r"pre[- ]?workout|electrolyte|beta[- ]?alanine|bcaa|glutamine|protein|"
    r"\bhcl\b|hydrochloride|nitrate|ethyl ester|kre[- ]?alkalyn|buffered|chelate|"
    r"malate|citrate|drink mix|\bmix\b|\+|"
    r"גומי|סוכרי|כמוס|קפסול|טבלי|תערובת|חלבון|מסטיק|ללעיסה",
    re.IGNORECASE,
)
_FLAVOUR_RE = re.compile(r"flavou?r|בטעם|טעם", re.IGNORECASE)
_UNFLAVOURED_RE = re.compile(
    r"un-?flavou?red|no flavou?r|flavou?rless|without flavou?r|ללא טעם|נטול טעם|בלי טעם",
    re.IGNORECASE,
)


def is_monohydrate_powder(p: CreatineProduct) -> bool:
    """True for plain, unflavoured creatine monohydrate powder.

    Requires a per-100g price (i.e. sold by weight), a creatine mention in the
    name, and none of the gummy / capsule / blend / other-salt / flavoured-mix
    markers.
    """
    if p.price_per_100g is None:
        return False
    name = p.name or ""
    if not _CREATINE_RE.search(name):
        return False
    if _EXCLUDE_RE.search(name):
        return False
    if _FLAVOUR_RE.search(name) and not _UNFLAVOURED_RE.search(name):
        return False
    return True


# ---------------------------------------------------------------------------
# Search-page parsing
# ---------------------------------------------------------------------------

_PRODUCT_ID_RE = re.compile(r"/pr/(?:[^/?#]+/)*(\d+)(?:[/?#]|$)")


def _product_key(url: str, name: str = "") -> str:
    """Stable identity: the iHerb product id from ``/pr/.../<id>``, else the URL."""
    if url:
        m = _PRODUCT_ID_RE.search(url)
        if m:
            return f"iherb:{m.group(1)}"
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"), "", ""))
    return f"name:{name.strip().lower()}"


def _product_link(cell, title_el) -> str:
    """The product page link of a cell: the title's own anchor, else ``/pr/``.

    Never the first anchor of the cell: iHerb's own brands carry an
    "iHerb brands" badge (``/iherb-brands``) before the product link.
    """
    candidates = []
    if title_el is not None:
        if title_el.name == "a":
            candidates.append(title_el)
        candidates.append(title_el.find_parent("a"))
        candidates.append(title_el.select_one("a[href]"))
    candidates.extend(cell.select('a[href*="/pr/"]'))
    for a in candidates:
        if a is None:
            continue
        href = (a.get("href") or "").strip()
        if href and "/pr/" in href:
            return urljoin(_IHERB_BASE + "/", href)
    return ""


def _cell_price_text(cell) -> Optional[str]:
    for sel in [".product-price", ".price", "p.price", "span[class*='price']", "[class*='price']"]:
        hit = cell.select_one(sel)
        if hit:
            txt = hit.get_text(" ", strip=True)
            if re.search(r"\d", txt):
                return txt
    return None


def _parse_cells(html: str, seen: Optional[set[str]] = None) -> tuple[list[CreatineProduct], int]:
    """Parse product cells out of one search page.

    Returns (new_products, number_of_cells). Products are de-duplicated on
    their iHerb product id (sponsored + organic copies of the same product);
    ``seen`` carries the keys across pages and is updated in place.
    """
    seen = set() if seen is None else seen
    soup = BeautifulSoup(html, "lxml")
    cells = soup.select(".product-cell")
    products: list[CreatineProduct] = []
    for cell in cells:
        title_el = cell.select_one(".product-title")
        if not title_el:
            continue
        name = title_el.get_text(" ", strip=True)
        if not name:
            continue
        parsed = _parse_price(_cell_price_text(cell) or "")
        if parsed is None:
            continue
        price, currency, per_unit, unit, on_sale = parsed

        href = _product_link(cell, title_el)
        key = _product_key(href, name)
        if key in seen:
            continue
        seen.add(key)

        products.append(CreatineProduct(
            name=name,
            url=href,
            price=price,
            currency=currency,
            price_per_100g=per_unit if unit == "100g" else None,
            unit=unit,
            on_sale=on_sale,
            seller="iHerb",
        ))
    return products, len(cells)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

_BLOCK_MARKERS = (
    "cf-challenge", "challenge-platform", "cf-chl-", "just a moment...",
    "attention required! | cloudflare", "access denied",
)


def _looks_blocked(html: str) -> bool:
    low = html[:20000].lower()
    return any(m in low for m in _BLOCK_MARKERS)


@dataclass
class _Page:
    """One fetched page: HTML or why there is none."""
    html: Optional[str]
    blocked: bool = False
    error: Optional[str] = None


def _fetch_html_httpx(url: str = _IHERB_SEARCH) -> _Page:
    try:
        with httpx.Client(follow_redirects=True, timeout=25, headers=_HEADERS) as client:
            resp = client.get(url)
    except httpx.HTTPError as exc:
        log.warning("iHerb creatine scan: %s", type(exc).__name__)
        return _Page(None, error=type(exc).__name__)
    if resp.status_code != 200:
        log.warning("iHerb creatine scan: HTTP %s", resp.status_code)
        return _Page(
            None,
            blocked=resp.status_code in (401, 403, 429, 503),
            error=f"HTTP {resp.status_code}",
        )
    if _looks_blocked(resp.text) and "product-cell" not in resp.text:
        return _Page(None, blocked=True, error="challenge page")
    return _Page(resp.text)


class _BrowserSession:
    """Headless Chromium kept open across several result pages.

    iHerb's Cloudflare front answers plain HTTP clients with a 403 challenge
    page; a real browser engine with an ordinary desktop identity gets the
    normal page.
    """

    def __init__(self) -> None:
        self._pw_cm = None
        self._browser = None
        self._page = None
        self.error: Optional[str] = None

    def __enter__(self) -> "_BrowserSession":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.warning("iHerb creatine scan: Playwright not installed, no browser fallback")
            self.error = "Playwright not installed"
            return self
        try:
            self._pw_cm = sync_playwright()
            pw = self._pw_cm.__enter__()
            self._browser = pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            self._page = self._browser.new_context(
                user_agent=USER_AGENT,
                locale="he-IL",
                viewport={"width": 1366, "height": 900},
            ).new_page()
        except Exception as exc:
            log.warning("iHerb creatine scan (browser) failed: %s: %s", type(exc).__name__, exc)
            self.error = f"{type(exc).__name__}"
            self.__exit__(None, None, None)
        return self

    def get(self, url: str) -> _Page:
        if self._page is None:
            return _Page(None, error=self.error or "browser unavailable")
        try:
            resp = self._page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            status = resp.status if resp else None
            try:
                self._page.wait_for_selector(".product-cell", timeout=25_000)
            except Exception:
                html = self._page.content()
                log.warning("iHerb creatine scan (browser): no products, HTTP %s", status or "?")
                if (status in (401, 403, 429, 503)) or _looks_blocked(html):
                    return _Page(None, blocked=True, error=f"HTTP {status or '?'} (browser)")
                return _Page(html)
            return _Page(self._page.content())
        except Exception as exc:
            log.warning("iHerb creatine scan (browser) failed: %s: %s", type(exc).__name__, exc)
            return _Page(None, error=type(exc).__name__)

    def __exit__(self, *exc_info) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw_cm is not None:
                self._pw_cm.__exit__(None, None, None)
        except Exception:
            pass
        self._browser = self._page = self._pw_cm = None


def _fetch_html_browser(url: str = _IHERB_SEARCH) -> Optional[str]:
    """Single-page browser fetch (kept for ad-hoc use)."""
    with _BrowserSession() as session:
        return session.get(url).html


@dataclass
class IherbScan:
    """Outcome of reading the iHerb search pages."""
    products: list[CreatineProduct] = field(default_factory=list)  # de-duplicated
    cells: int = 0          # product cells seen across all pages (incl. repeats)
    pages: int = 0          # pages successfully read
    blocked: bool = False   # first page refused by Cloudflare / iHerb
    error: Optional[str] = None


def _iter_pages(max_pages: int) -> Iterator[tuple[int, _Page]]:
    """Yield (page_no, page): httpx first, headless browser once httpx fails."""
    use_browser = False
    browser: Optional[_BrowserSession] = None
    try:
        for page_no in range(1, max_pages + 1):
            url = search_url(page_no)
            page = _Page(None)
            if not use_browser:
                page = _fetch_html_httpx(url)
                if page.html is None or "product-cell" not in page.html:
                    if page_no == 1:
                        log.info("iHerb creatine scan: retrying with headless browser")
                        use_browser = True
                    elif page.html is not None:
                        # A later page with no products: end of results.
                        yield page_no, page
                        return
            if use_browser:
                if browser is None:
                    browser = _BrowserSession().__enter__()
                first_error = page
                page = browser.get(url)
                if page.html is None and page_no == 1:
                    # Report the most telling reason: a block on either path.
                    page.blocked = page.blocked or first_error.blocked
                    page.error = page.error or first_error.error
            yield page_no, page
    finally:
        if browser is not None:
            browser.__exit__(None, None, None)


def scan_iherb_detailed(
    max_pages: int = IHERB_MAX_PAGES,
    pages: Optional[Callable[[int], Iterator[tuple[int, _Page]]]] = None,
) -> IherbScan:
    """Read up to ``max_pages`` search pages and parse their product cells."""
    result = IherbScan()
    seen: set[str] = set()
    page_iter = (pages or _iter_pages)(max_pages)
    try:
        _read_pages(page_iter, result, seen)
    finally:
        close = getattr(page_iter, "close", None)
        if close is not None:
            close()  # shuts the headless browser down promptly
    return result


def _read_pages(page_iter, result: IherbScan, seen: set[str]) -> None:
    for page_no, page in page_iter:
        if page.html is None:
            if page_no == 1:
                result.blocked = page.blocked
                result.error = page.error
            break
        if page_no == 1 and _looks_blocked(page.html) and "product-cell" not in page.html:
            result.blocked = True
            result.error = "challenge page"
            break
        new, cells = _parse_cells(page.html, seen)
        result.pages += 1
        result.cells += cells
        result.products.extend(new)
        if not new:
            break  # empty page or nothing but repeats: no more results


def scan_iherb() -> list[CreatineProduct]:
    """Fetch and parse iHerb's creatine search pages (de-duplicated)."""
    return scan_iherb_detailed().products


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def _state_file() -> Path:
    return data_dir() / "creatine-state.json"


def _load_state() -> dict:
    p = _state_file()
    if not p.exists():
        return {"schema_version": 1, "cheapest": None, "last_scanned_at": None}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "cheapest": None, "last_scanned_at": None}


def _save_state(state: dict) -> None:
    p = _state_file()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def powders_of(products: list[CreatineProduct]) -> list[CreatineProduct]:
    """Plain monohydrate powders, cheapest per 100g first."""
    return sorted(
        (p for p in products if is_monohydrate_powder(p)),
        key=lambda p: p.price_per_100g,
    )


def pick_cheapest(products: list[CreatineProduct]) -> Optional[CreatineProduct]:
    """Return the cheapest monohydrate powder by price-per-100g."""
    powders = powders_of(products)
    return powders[0] if powders else None


def _product_to_dict(p: CreatineProduct) -> dict:
    d = asdict(p)
    d["price"] = str(p.price)
    d["price_per_100g"] = str(p.price_per_100g) if p.price_per_100g is not None else None
    return d


def _product_from_dict(d: dict) -> CreatineProduct:
    return CreatineProduct(
        name=d["name"],
        url=d.get("url", ""),
        price=Decimal(d["price"]),
        currency=d.get("currency", "ILS"),
        price_per_100g=Decimal(d["price_per_100g"]) if d.get("price_per_100g") else None,
        unit=d.get("unit", "100g"),
        on_sale=d.get("on_sale", False),
        seller=d.get("seller", "iHerb"),
    )


def _same_product(a: CreatineProduct, b: CreatineProduct) -> bool:
    """Same product? By iHerb product id when both have one, else by name.

    The id survives the page coming back in another language, the name does not.
    """
    ka, kb = _product_key(a.url), _product_key(b.url)
    if ka.startswith("iherb:") and kb.startswith("iherb:"):
        return ka == kb
    return a.name == b.name


def compare(
    previous: Optional[CreatineProduct], cheapest: CreatineProduct
) -> tuple[bool, Optional[str]]:
    """(changed, direction) between the stored and the new cheapest product.

    direction is "dropped" / "rose" / "same_price" (a different product at
    the identical per-100g price) / None (first observation, or the currency
    changed so the numbers are not comparable).
    """
    if previous is None:
        return True, None  # first observation counts as "to be announced"
    same_price = previous.price_per_100g == cheapest.price_per_100g
    same_currency = previous.currency == cheapest.currency
    if same_price and same_currency and _same_product(previous, cheapest):
        return False, None
    if not same_currency or previous.price_per_100g is None or cheapest.price_per_100g is None:
        return True, None
    if cheapest.price_per_100g < previous.price_per_100g:
        return True, "dropped"
    if cheapest.price_per_100g > previous.price_per_100g:
        return True, "rose"
    return True, "same_price"


def _print_table(products: list[CreatineProduct], cheapest: Optional[CreatineProduct]) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("#", width=3)
    table.add_column("Name")
    table.add_column("Price")
    table.add_column("/100g")
    table.add_column("Sale")

    for i, p in enumerate(products[:10], 1):
        name_cell = p.name[:60]
        if cheapest and p is cheapest:
            name_cell = f"[bold green]{name_cell}[/bold green]"
        table.add_row(
            str(i),
            name_cell,
            f"{p.price} {p.currency}",
            f"{p.price_per_100g} {p.currency}",
            "✓" if p.on_sale else "",
        )

    console.print(table)


@dataclass
class CreatineReport:
    """Result of a creatine scan — data only, no email side-effect."""
    # "ok" / "blocked" / "fetch_failed" / "no_products" / "parse_failed" / "no_powder"
    status: str
    products: list[CreatineProduct] = field(default_factory=list)  # top 10 powders by price/100g
    cheapest: Optional[CreatineProduct] = None
    previous_cheapest: Optional[CreatineProduct] = None
    changed: bool = False
    direction: Optional[str] = None  # "dropped" / "rose" / "same_price" / None
    error: Optional[str] = None
    products_scanned: int = 0  # distinct products parsed (duplicates removed)
    powders_found: int = 0     # of which plain monohydrate powders
    cells_seen: int = 0        # raw product cells on the pages (incl. repeats)
    pages_read: int = 0


def scan_and_report(scan: Optional[IherbScan] = None) -> CreatineReport:
    """Scan iHerb, compare against stored cheapest, return a report (no email, no save)."""
    log.info("Scanning iHerb creatine")
    if scan is None:
        scan = scan_iherb_detailed()
    counts = dict(
        products_scanned=len(scan.products),
        cells_seen=scan.cells,
        pages_read=scan.pages,
    )

    if not scan.products:
        if scan.blocked:
            return CreatineReport(
                status="blocked",
                error=f"iHerb חסם את הבקשה ({scan.error or 'blocked'})",
                **counts,
            )
        if scan.pages == 0:
            return CreatineReport(
                status="fetch_failed",
                error=f"לא ניתן היה לטעון את עמוד החיפוש של iHerb ({scan.error or 'unknown'})",
                **counts,
            )
        if scan.cells == 0:
            return CreatineReport(
                status="no_products",
                error="העמוד נטען אך לא נמצאו בו מוצרים (ייתכן שמבנה העמוד השתנה)",
                **counts,
            )
        return CreatineReport(
            status="parse_failed",
            error=f"פוענחו 0 מתוך {scan.cells} מוצרים בעמוד (ייתכן שתבנית המחיר השתנתה)",
            **counts,
        )

    powders = powders_of(scan.products)
    counts["powders_found"] = len(powders)
    if not powders:
        return CreatineReport(
            status="no_powder",
            error=f"נמצאו {len(scan.products)} מוצרים, אך אף אחד מהם אינו אבקת קריאטין מונוהידרט",
            **counts,
        )
    cheapest = powders[0]

    state = _load_state()
    previous = _product_from_dict(state["cheapest"]) if state.get("cheapest") else None
    changed, direction = compare(previous, cheapest)

    return CreatineReport(
        status="ok",
        products=powders[:10],
        cheapest=cheapest,
        previous_cheapest=previous,
        changed=changed,
        direction=direction,
        **counts,
    )


def persist(report: CreatineReport) -> None:
    """Save the current cheapest to state. Call after the report has been emailed."""
    if report.status != "ok" or report.cheapest is None:
        return
    state = _load_state()
    state["cheapest"] = _product_to_dict(report.cheapest)
    state["last_scanned_at"] = datetime.now().astimezone().isoformat()
    _save_state(state)


def print_report(report: CreatineReport) -> None:
    """Terminal output for a report."""
    if report.status != "ok":
        console.print(f"[red]Creatine scan failed: {report.status}[/red]  {report.error or ''}")
        return
    console.print(
        f"[dim]{report.products_scanned} distinct products on {report.pages_read} page(s) "
        f"({report.cells_seen} cells), {report.powders_found} monohydrate powders[/dim]"
    )
    _print_table(report.products, report.cheapest)
    cur = report.cheapest
    if report.previous_cheapest is None:
        console.print(f"\n[bold]First observation:[/bold] {cur.name} @ {cur.price_per_100g} {cur.currency}/100g")
    elif report.changed:
        prev = report.previous_cheapest
        label = {"same_price": "same price, different product"}.get(report.direction or "", report.direction or "changed")
        console.print(
            f"\n[bold yellow]CHANGE ({label}):[/bold yellow] "
            f"{prev.price_per_100g} {prev.currency}/100g → {cur.price_per_100g} {cur.currency}/100g"
        )
    else:
        console.print(f"\n[dim]No change — still {cur.price_per_100g} {cur.currency}/100g.[/dim]")
