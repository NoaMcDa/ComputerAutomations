"""Creatine price watcher.

Scrapes iHerb's creatine search page. iHerb sits behind Cloudflare, which as of
2026-11-11 answers plain HTTP clients (httpx) with a 403 "Just a moment..."
challenge, so a plain request usually fails. It is still tried first (it is
cheap and sometimes works, depending on IP); on a 403 or an empty page the page
is loaded in headless Chromium (Playwright) instead. When neither works the
report says which step failed (blocked / browser not installed / challenge not
passed). iHerb pre-computes price-per-100g in each product cell, so we don't
need to parse pack sizes ourselves.

Tracks the cheapest price-per-100g across powder products and emails when it
moves. Separate state file from the normal product watches, same SMTP path.

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
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from rich.console import Console
from rich.table import Table

from price_watcher import logging_setup
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
    unit: str  # "100g" / "count"
    on_sale: bool
    seller: str


# ---------------------------------------------------------------------------
# iHerb scraper
# ---------------------------------------------------------------------------

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "he,en;q=0.8,en-US;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
}

_IHERB_SEARCH = "https://il.iherb.com/search?kw=creatine"


_BIDI_CHARS = dict.fromkeys(
    [0x200E, 0x200F, 0x061C, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
     0x2066, 0x2067, 0x2068, 0x2069, 0x00A0, 0x202F], " ")

_CURRENCIES = {"₪": "ILS", "$": "USD", "€": "EUR", "£": "GBP"}
_CUR = r"(?:US\s*\$|[₪$€£])"
_NUM = r"\d[\d,]*(?:\.\d+)?"
_AMOUNT_RE = re.compile(rf"(?:({_CUR})\s*({_NUM})|({_NUM})\s*({_CUR}))")
# '(₪9.49/100g)', '($2.50 / 100 g)', '(₪22.21/100 גרם)', '(₪0.26/ליחידה)', '(₪95/kg)'
_PER_UNIT_RE = re.compile(
    rf"\(?\s*(?:({_CUR})\s*({_NUM})|({_NUM})\s*({_CUR}))\s*/\s*"
    r"(?P<qty>\d+(?:\.\d+)?)?\s*"
    r"(?P<unit>kg|ק\"ג|קילו|גרם|גר|ליחידה|יחידה|count|ct|oz|ml|מ\"ל|g)(?![A-Za-z])\s*\)?",
    re.IGNORECASE,
)
_SALE_MARK_RE = re.compile(r"\d+\s*%|הנחה|\boff\b", re.IGNORECASE)


def _to_decimal(num: str) -> Optional[Decimal]:
    try:
        return Decimal(num.replace(",", ""))
    except InvalidOperation:
        return None


def _cur_code(sym: str) -> str:
    return _CURRENCIES.get(sym.strip()[-1], "USD")


def _amounts(text: str) -> list[tuple[Decimal, str]]:
    out = []
    for m in _AMOUNT_RE.finditer(text):
        sym, num = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        val = _to_decimal(num)
        if val is not None:
            out.append((val, _cur_code(sym)))
    return out


def _parse_price(text: str) -> Optional[tuple[Decimal, str, Optional[Decimal], str, bool]]:
    """Parse iHerb's price cell text.

    Examples:
      '₪94.64 (₪9.49/100g)'
      '₪61.87 (₪0.26/count)'
      '₪78.64 (₪22.21/100g) 25% off ₪104.85'
      '₪104.85 ₪78.64 (₪22.21/100g)'        (struck-through original first)
      '$24.99 ($2.50/100 g)', '₪95 (₪9.5/kg)', '‏₪94.64 (₪9.49/100 גרם)'

    Returns (price, currency, per_unit_price_or_none, unit, on_sale). Per-kg and
    per-gram prices are normalised to per-100g.
    """
    if not text:
        return None
    text = text.translate(_BIDI_CHARS)
    text = re.sub(r"\s+", " ", text).strip()

    per_unit_price: Optional[Decimal] = None
    unit = "unknown"
    head = text
    pm = _PER_UNIT_RE.search(text)
    if pm:
        head = text[:pm.start()]
        num = pm.group(2) if pm.group(1) else pm.group(3)
        val = _to_decimal(num)
        raw_unit = pm.group("unit").lower()
        qty = _to_decimal(pm.group("qty") or "1")
        if val is not None and qty:
            if raw_unit in ("g", "גרם", "גר"):
                per_unit_price = (val * 100 / qty).quantize(Decimal("0.01"))
                unit = "100g"
            elif raw_unit in ("kg", 'ק"ג', "קילו"):
                per_unit_price = (val / (10 * qty)).quantize(Decimal("0.01"))
                unit = "100g"
            elif raw_unit in ("count", "ct", "ליחידה", "יחידה"):
                per_unit_price, unit = val, "count"
            else:
                per_unit_price, unit = val, raw_unit  # oz / ml: kept, not comparable

    # Prices before the per-unit group: either one price or (original, current).
    # Anything after a '25% off' marker is the original price, not the price paid.
    mark = _SALE_MARK_RE.search(head)
    paid_zone = head[:mark.start()] if mark else head
    found = _amounts(paid_zone)
    if not found:
        return None
    price, currency = min(found, key=lambda a: a[0]) if len(found) > 1 else found[0]
    on_sale = bool(mark) or len(found) > 1 or bool(_SALE_MARK_RE.search(text))
    return price, currency, per_unit_price, unit, on_sale


_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


# Why the last fetch attempt(s) failed, so the report can say "blocked by
# Cloudflare" / "browser not installed" instead of a generic failure.
# Keys: "httpx" and "browser" -> short reason code, reset by scan_iherb().
_fetch_diag: dict[str, str] = {}


def _fetch_html_httpx() -> Optional[str]:
    try:
        with httpx.Client(follow_redirects=True, timeout=25, headers=_HEADERS) as client:
            resp = client.get(_IHERB_SEARCH)
    except httpx.HTTPError as exc:
        log.warning("iHerb creatine scan: %s", type(exc).__name__)
        _fetch_diag["httpx"] = f"network:{type(exc).__name__}"
        return None
    if resp.status_code != 200:
        log.warning("iHerb creatine scan: HTTP %s", resp.status_code)
        _fetch_diag["httpx"] = f"http:{resp.status_code}"
        return None
    return resp.text


def _fetch_html_browser() -> Optional[str]:
    """Load the search page in headless Chromium.

    iHerb's Cloudflare front answers plain HTTP clients with a 403 challenge
    page; a real browser engine with an ordinary desktop identity gets the
    normal page.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("iHerb creatine scan: Playwright not installed, no browser fallback")
        _fetch_diag["browser"] = "playwright_missing"
        return None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                page = browser.new_context(
                    user_agent=_BROWSER_UA,
                    locale="he-IL",
                    viewport={"width": 1366, "height": 900},
                ).new_page()
                resp = page.goto(_IHERB_SEARCH, wait_until="domcontentloaded", timeout=45_000)
                try:
                    page.wait_for_selector(".product-cell", timeout=25_000)
                except Exception:
                    log.warning(
                        "iHerb creatine scan (browser): no products, HTTP %s",
                        resp.status if resp else "?",
                    )
                    _fetch_diag["browser"] = f"no_products:{resp.status if resp else '?'}"
                    return None
                return page.content()
            finally:
                browser.close()
    except Exception as exc:
        log.warning("iHerb creatine scan (browser) failed: %s: %s", type(exc).__name__, exc)
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            _fetch_diag["browser"] = "browser_not_installed"
        else:
            _fetch_diag["browser"] = f"error:{type(exc).__name__}"
        return None


# Name fragments of products that are not plain creatine powder; they can carry
# a per-100g price but should not compete for "cheapest creatine powder".
_NON_POWDER_RE = re.compile(
    r"gumm|capsule|caps\b|tablet|chewable|chews?\b|softgel|liquid|drink|bar\b|"
    r"כמוס|גומי|טבליות|לעיס|נוזל|חטיף",
    re.IGNORECASE,
)


def _is_powder(p: CreatineProduct) -> bool:
    return p.price_per_100g is not None and not _NON_POWDER_RE.search(p.name)


def _parse_cells(html: str) -> tuple[list[CreatineProduct], int]:
    """Parse product cells; returns (products, number_of_cells_seen)."""
    soup = BeautifulSoup(html, "lxml")
    cells = soup.select(".product-cell")
    products: list[CreatineProduct] = []
    for cell in cells:
        title_el = cell.select_one(".product-title")
        if not title_el:
            continue
        name = title_el.get_text(strip=True)

        # Price text — try several candidate selectors
        price_text = None
        for sel in [".product-price", ".price", "p.price", "span[class*='price']"]:
            hit = cell.select_one(sel)
            if hit:
                txt = hit.get_text(" ", strip=True)
                if re.search(r"\d", txt):
                    price_text = txt
                    break
        parsed = _parse_price(price_text or "")
        if parsed is None:
            continue
        price, currency, per_unit, unit, on_sale = parsed

        link_el = cell.select_one("a[href]")
        href = link_el.get("href", "") if link_el else ""
        if href and not href.startswith("http"):
            href = f"https://il.iherb.com{href}"

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


_scan_cells_seen = 0


def scan_iherb() -> list[CreatineProduct]:
    """Fetch and parse iHerb's creatine search page.

    Diagnostics for an empty result are left in `_fetch_diag` and
    `_scan_cells_seen` for scan_and_report().
    """
    global _scan_cells_seen
    _fetch_diag.clear()
    _scan_cells_seen = 0
    html = _fetch_html_httpx()
    products: list[CreatineProduct] = []
    if html is not None:
        products, _scan_cells_seen = _parse_cells(html)
        if _scan_cells_seen == 0:
            _fetch_diag["httpx"] = "no_cells"  # 200 but not a product page (challenge?)
    if not products and not _scan_cells_seen:
        log.info("iHerb creatine scan: retrying with headless browser")
        html = _fetch_html_browser()
        if html is not None:
            products, _scan_cells_seen = _parse_cells(html)
    return products


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

def pick_cheapest(products: list[CreatineProduct]) -> Optional[CreatineProduct]:
    """Return the cheapest product by price-per-100g (powder only).

    Only products in the most common currency compete, so a stray USD cell
    cannot beat ILS prices.
    """
    powders = [p for p in products if _is_powder(p)]
    if not powders:
        return None
    main_cur = max({p.currency for p in powders}, key=lambda c: sum(p.currency == c for p in powders))
    powders = [p for p in powders if p.currency == main_cur]
    return min(powders, key=lambda p: p.price_per_100g)


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


def _print_table(products: list[CreatineProduct], cheapest: Optional[CreatineProduct]) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("#", width=3)
    table.add_column("Name")
    table.add_column("Price")
    table.add_column("₪/100g")
    table.add_column("Sale")

    # Top 10 by price/100g among powders, then up to 5 capsule/other
    powders = sorted(
        [p for p in products if _is_powder(p)],
        key=lambda p: p.price_per_100g,
    )[:10]

    for i, p in enumerate(powders, 1):
        name_cell = p.name[:60]
        if cheapest and p is cheapest:
            name_cell = f"[bold green]{name_cell}[/bold green]"
        table.add_row(
            str(i),
            name_cell,
            f"₪{p.price}",
            f"₪{p.price_per_100g}",
            "✓" if p.on_sale else "",
        )

    console.print(table)


@dataclass
class CreatineReport:
    """Result of a creatine scan — data only, no email side-effect."""
    status: str  # "ok" / "no_powder" / "fetch_failed" / "blocked"
    products: list[CreatineProduct] = field(default_factory=list)  # top 10 powders by ₪/100g
    cheapest: Optional[CreatineProduct] = None
    previous_cheapest: Optional[CreatineProduct] = None
    changed: bool = False
    # "dropped" / "rose" / "changed" (same price per 100g, different product) / None
    direction: Optional[str] = None
    error: Optional[str] = None
    cells_seen: int = 0  # product cells found on the page
    parsed: int = 0      # cells whose price could be parsed
    powders: int = 0     # parsed products counted as plain powder


def _failure_report() -> CreatineReport:
    """Explain why nothing could be scanned, from the fetch diagnostics."""
    httpx_r = _fetch_diag.get("httpx", "")
    browser_r = _fetch_diag.get("browser", "")
    blocked = httpx_r in ("http:403", "http:429", "http:503", "no_cells") or browser_r.startswith("no_products")
    parts: list[str] = []
    if httpx_r in ("http:403", "http:429", "http:503"):
        parts.append(f"iHerb חסם את הבקשה (Cloudflare, HTTP {httpx_r.split(':')[1]})")
    elif httpx_r.startswith("http:"):
        parts.append(f"iHerb החזיר שגיאת HTTP {httpx_r.split(':')[1]}")
    elif httpx_r.startswith("network:"):
        parts.append(f"שגיאת רשת בגישה ל-iHerb ({httpx_r.split(':')[1]})")
    elif httpx_r == "no_cells":
        parts.append("iHerb החזיר דף ללא מוצרים (כנראה אתגר Cloudflare)")
    if browser_r == "playwright_missing":
        parts.append("הדפדפן החלופי לא זמין: Playwright לא מותקן")
    elif browser_r == "browser_not_installed":
        parts.append("הדפדפן החלופי לא זמין: Chromium לא מותקן (יש להריץ: playwright install chromium)")
    elif browser_r.startswith("no_products"):
        parts.append("גם הדפדפן לא הצליח לטעון את רשימת המוצרים (אתגר Cloudflare לא נפתר)")
    elif browser_r.startswith("error:"):
        parts.append(f"הדפדפן החלופי נכשל ({browser_r.split(':', 1)[1]})")
    if not parts:
        parts.append("לא נמצאו מוצרים בעמוד של iHerb")
    return CreatineReport(
        status="blocked" if blocked else "fetch_failed",
        error="; ".join(parts),
    )


def scan_and_report() -> CreatineReport:
    """Scan iHerb, compare against stored cheapest, return a report (no email, no save)."""
    global _scan_cells_seen
    log.info("Scanning iHerb creatine")
    _fetch_diag.clear()
    _scan_cells_seen = 0
    products = scan_iherb()
    cells = _scan_cells_seen
    if not products:
        if cells:
            # The page had product cells but not one price could be read: a
            # layout/currency change on our side, not a block.
            return CreatineReport(
                status="fetch_failed",
                cells_seen=cells,
                error=f"נמצאו {cells} מוצרים בעמוד אך לא ניתן היה לפענח אף מחיר "
                      "(ייתכן שפורמט המחיר בעמוד השתנה)",
            )
        return _failure_report()

    powders_all = [p for p in products if _is_powder(p)]
    cheapest = pick_cheapest(products)
    if cheapest is None:
        return CreatineReport(
            status="no_powder", products=products, cells_seen=cells, parsed=len(products),
            error=f"נמצאו {len(products)} מוצרים אך אף אחד מהם אינו אבקת קריאטין עם מחיר ל-100 גרם",
        )

    state = _load_state()
    previous = _product_from_dict(state["cheapest"]) if state.get("cheapest") else None

    changed = False
    direction = None
    if previous is None:
        changed = True  # first observation counts as "to be announced"
    elif (previous.price_per_100g != cheapest.price_per_100g or previous.name != cheapest.name
          or previous.currency != cheapest.currency):
        changed = True
        if (previous.price_per_100g and cheapest.price_per_100g
                and previous.currency == cheapest.currency):
            if cheapest.price_per_100g < previous.price_per_100g:
                direction = "dropped"
            elif cheapest.price_per_100g > previous.price_per_100g:
                direction = "rose"
            else:
                direction = "changed"  # same price, a different product is now cheapest
        else:
            direction = "changed"

    powders = sorted(powders_all, key=lambda p: p.price_per_100g)[:10]

    return CreatineReport(
        status="ok",
        products=powders,
        cheapest=cheapest,
        previous_cheapest=previous,
        changed=changed,
        direction=direction,
        cells_seen=cells,
        parsed=len(products),
        powders=len(powders_all),
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
    _print_table(report.products, report.cheapest)
    if report.previous_cheapest is None:
        console.print(f"\n[bold]First observation:[/bold] {report.cheapest.name} @ ₪{report.cheapest.price_per_100g}/100g")
    elif report.changed:
        prev = report.previous_cheapest
        console.print(
            f"\n[bold yellow]CHANGE ({report.direction}):[/bold yellow] "
            f"₪{prev.price_per_100g}/100g → ₪{report.cheapest.price_per_100g}/100g"
        )
    else:
        console.print(f"\n[dim]No change — still ₪{report.cheapest.price_per_100g}/100g.[/dim]")
