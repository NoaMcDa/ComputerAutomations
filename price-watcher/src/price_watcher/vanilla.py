"""Vanilla extract / vanilla bean paste on iHerb, for the daily email.

Uses the creatine module's iHerb fetch (plain HTTP first, headless Chromium on
Cloudflare's 403) on two searches, keeps only real vanilla extract and vanilla
bean paste (iHerb's vanilla search is mostly lotions, protein powders and
vanilla-flavoured supplements), and compares each product's price with the
last emailed one. Extract is compared per 100 ml, paste per 100 g.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from html import escape
from pathlib import Path
from typing import Optional

from price_watcher import creatine as _iherb
from price_watcher import logging_setup
from price_watcher.paths import data_dir

log = logging_setup.get(__name__)

SECTION_TITLE = "וניל (iHerb)"
_SEARCHES = (
    "https://il.iherb.com/search?kw=vanilla+extract",
    "https://il.iherb.com/search?kw=vanilla+paste",
)

# Decided on the product link's English slug, which does not change with the
# page language: '.../mccormick-pure-vanilla-extract-1-fl-oz-29-ml/99195'.
_SLUG_RE = re.compile(r"/pr/([^/?#]+)/(\d+)")
_WANTED_RE = re.compile(r"vanilla.*(extract|paste|flavoring)|(extract|paste).*vanilla")
_UNWANTED_RE = re.compile(
    r"stevia|oil|lotion|balm|cream|shampoo|soap|toothpaste|lavender|protein|powder|ground|"
    r"liposomal|packets|sugar-free|syrup|tea|coffee|whole-vanilla-beans"
)
# '..., 29 מ"ל (אונקיית נוזל 1)' -> drop the bracketed ounces
_OUNCES_RE = re.compile(r"\s*\((?=[^)]*(אונקי|oz|fl))[^)]*\)")

KIND_EXTRACT = "extract"
KIND_PASTE = "paste"
KIND_HE = {KIND_EXTRACT: "תמצית וניל", KIND_PASTE: "מחית וניל"}
UNIT_HE = {KIND_EXTRACT: 'ל-100 מ"ל', KIND_PASTE: "ל-100 גרם"}


@dataclass
class VanillaItem:
    name: str
    url: str
    product_id: str
    kind: str                      # KIND_EXTRACT / KIND_PASTE
    price: Decimal
    currency: str
    per_100: Optional[Decimal]     # per 100 ml (extract) or 100 g (paste)
    on_sale: bool
    previous_price: Optional[Decimal] = None
    direction: Optional[str] = None  # "dropped" / "rose" / "new" / None (unchanged)


@dataclass
class VanillaReport:
    status: str                    # "ok" / "none_found" / "blocked" / "fetch_failed"
    items: list[VanillaItem] = field(default_factory=list)
    first_run: bool = False
    error: Optional[str] = None

    @property
    def dropped(self) -> list[VanillaItem]:
        return [i for i in self.items if i.direction == "dropped"]

    @property
    def rose(self) -> list[VanillaItem]:
        return [i for i in self.items if i.direction == "rose"]

    def by_kind(self, kind: str) -> list[VanillaItem]:
        return [i for i in self.items if i.kind == kind]


def classify(url: str) -> Optional[tuple[str, str]]:
    """(kind, product_id) for vanilla extract / paste links, else None."""
    m = _SLUG_RE.search(url or "")
    if not m:
        return None
    slug = m.group(1).lower()
    if not _WANTED_RE.search(slug) or _UNWANTED_RE.search(slug):
        return None
    return (KIND_PASTE if "paste" in slug else KIND_EXTRACT), m.group(2)


def short_name(name: str) -> str:
    """'McCormick‏, תמצית וניל טהור, 29 מ"ל (אונקיית נוזל 1)' -> without the ounces."""
    name = name.replace("‏", "").replace("‎", "")
    return re.sub(r"\s{2,}", " ", _OUNCES_RE.sub("", name)).strip(" ,")


def to_items(products: list) -> list[VanillaItem]:
    items: dict[str, VanillaItem] = {}
    for p in products:
        hit = classify(p.url)
        if hit is None or hit[1] in items:
            continue
        kind, pid = hit
        wanted_unit = "100ml" if kind == KIND_EXTRACT else "100g"
        items[pid] = VanillaItem(
            name=short_name(p.name), url=p.url, product_id=pid, kind=kind,
            price=p.price, currency=p.currency,
            per_100=p.per_unit if p.unit == wanted_unit else None,
            on_sale=p.on_sale,
        )
    return sorted(items.values(), key=lambda i: (i.kind, i.per_100 is None, i.per_100 or 0, i.price))


# ---------------------------------------------------------------------------
# State: last emailed price per product id
# ---------------------------------------------------------------------------

def _state_file() -> Path:
    return data_dir() / "vanilla-state.json"


def _load_state() -> dict:
    try:
        return json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def apply_previous(items: list[VanillaItem], state: dict) -> bool:
    """Fill previous_price/direction from the state. Returns True on a first run."""
    prices = state.get("prices")
    if not prices:
        return True
    for i in items:
        prev = prices.get(i.product_id)
        if prev is None:
            i.direction = "new"
            continue
        i.previous_price = Decimal(prev)
        if i.price < i.previous_price:
            i.direction = "dropped"
        elif i.price > i.previous_price:
            i.direction = "rose"
    return False


def persist(report: VanillaReport) -> None:
    """Save the current prices. Call after the report has been emailed."""
    if report.status != "ok":
        return
    state = _load_state()
    prices = state.get("prices") or {}
    prices.update({i.product_id: str(i.price) for i in report.items})
    state.update(prices=prices, last_scanned_at=datetime.now().astimezone().isoformat())
    p = _state_file()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

def _fetch(url: str) -> list:
    html = _iherb._fetch_html_httpx(url)
    if html:
        products, cells = _iherb._parse_cells(html)
        if cells:
            return products
    html = _iherb._fetch_html_browser(url)
    return _iherb._parse_cells(html)[0] if html else []


def scan_and_report() -> VanillaReport:
    """Scan iHerb, compare with the stored prices; no email, no save."""
    log.info("Scanning iHerb vanilla")
    _iherb._fetch_diag.clear()
    products: list = []
    for url in _SEARCHES:
        products.extend(_fetch(url))
    if not products:
        failed = _iherb._failure_report()
        return VanillaReport(status=failed.status, error=failed.error)
    items = to_items(products)
    if not items:
        return VanillaReport(
            status="none_found",
            error=f"נבדקו {len(products)} מוצרים בחיפוש, אף אחד מהם לא תמצית או מחית וניל",
        )
    first = apply_previous(items, _load_state())
    return VanillaReport(status="ok", items=items, first_run=first)


def print_report(report: VanillaReport) -> None:
    for line in section_text(report):
        print(line)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def _sym(cur: str) -> str:
    return {"ILS": "₪", "USD": "$", "EUR": "€", "GBP": "£"}.get(cur, cur + " ")


def headline(report: Optional[VanillaReport]) -> str:
    """Subject-line blurb: only when a price dropped."""
    if report is None or report.status != "ok" or not report.dropped:
        return ""
    best = min(report.dropped, key=lambda i: i.price)
    return f"ירידת מחיר בוניל ב-iHerb ({_sym(best.currency)}{best.price})"


def _change_text(i: VanillaItem) -> str:
    s = _sym(i.currency)
    if i.direction == "dropped":
        return f"ירד מ-{s}{i.previous_price}"
    if i.direction == "rose":
        return f"עלה מ-{s}{i.previous_price}"
    if i.direction == "new":
        return "חדש ברשימה"
    return ""


def section_text(report: Optional[VanillaReport]) -> list[str]:
    lines = [SECTION_TITLE, "-" * 50, ""]
    if report is None or report.status != "ok":
        lines.append("  הסריקה נכשלה" + (f": {report.error}" if report and report.error else ""))
        return lines
    for kind in (KIND_EXTRACT, KIND_PASTE):
        rows = report.by_kind(kind)
        if not rows:
            continue
        lines.append(f"  {KIND_HE[kind]}:")
        for i in rows:
            s = _sym(i.currency)
            per = f" ({s}{i.per_100} {UNIT_HE[kind]})" if i.per_100 is not None else ""
            extra = [x for x in (("מבצע" if i.on_sale else ""), _change_text(i)) if x]
            lines.append(f"    {s}{i.price}{per} · {i.name}" + (f" · {' · '.join(extra)}" if extra else ""))
            lines.append(f"      {i.url}")
    lines.append("")
    lines.append("  " + ("תצפית ראשונה, מהמייל הבא יסומנו שינויי מחיר." if report.first_run
                         else ("יש שינויי מחיר." if report.dropped or report.rose
                               else "אין שינוי מחיר מאתמול.")))
    return lines


def section_html(report: Optional[VanillaReport]) -> str:
    box = ("margin:0 0 24px;padding:14px 16px;border:1px solid #e5e7eb;border-radius:10px;"
           "background:#fafafa;text-align:right")
    if report is None or report.status != "ok":
        return (f'<div style="{box.replace("#e5e7eb", "#fecaca").replace("#fafafa", "#fef2f2")};'
                f'color:#991b1b"><b>סריקת הוניל נכשלה</b>'
                + (f" — {escape(report.error)}" if report and report.error else "") + "</div>")
    blocks = []
    for kind in (KIND_EXTRACT, KIND_PASTE):
        rows = report.by_kind(kind)
        if not rows:
            continue
        trs = []
        for n, i in enumerate(rows):
            s = escape(_sym(i.currency))
            badges = ""
            if i.on_sale:
                badges += ('<span style="background:#dcfce7;color:#166534;border-radius:4px;'
                           'padding:1px 6px;font-size:12px">מבצע</span> ')
            change = _change_text(i)
            if change:
                color = {"dropped": "#166534", "rose": "#991b1b"}.get(i.direction or "", "#6b7280")
                badges += f'<span style="color:{color};font-size:12px;font-weight:600">{escape(change)}</span>'
            per = (f'<div style="color:#6b7280;font-size:12px" dir="rtl">'
                   f'<span dir="ltr">{s}{i.per_100}</span> {UNIT_HE[kind]}</div>'
                   if i.per_100 is not None else "")
            trs.append(
                f'<tr style="border-top:1px solid #eee;{"background:#f0fdf4" if n == 0 else ""}">'
                f'<td style="padding:7px 6px;text-align:right"><a href="{escape(i.url)}" dir="auto" '
                f'style="color:#111827;text-decoration:none">{escape(i.name)}</a>'
                + (f"<br>{badges}" if badges else "") + "</td>"
                f'<td style="padding:7px 6px;text-align:left;white-space:nowrap;vertical-align:top">'
                f'<b style="font-size:15px" dir="ltr">{s}{i.price}</b>{per}</td></tr>'
            )
        blocks.append(
            f'<div style="margin:6px 0 2px;font-weight:600;color:#374151">{KIND_HE[kind]}</div>'
            f'<table dir="rtl" style="border-collapse:collapse;width:100%;font-size:14px">{"".join(trs)}</table>'
        )
    note = ("תצפית ראשונה, מהמייל הבא יסומנו שינויי מחיר." if report.first_run
            else ("" if report.dropped or report.rose else "אין שינוי מחיר מאתמול."))
    return (f'<div style="{box}">' + "".join(blocks)
            + (f'<div style="margin-top:8px;color:#6b7280;font-size:12px">{note}</div>' if note else "")
            + "</div>")
