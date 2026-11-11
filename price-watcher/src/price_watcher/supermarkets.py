"""Israeli supermarket meat-discount scanner.

Uses the government-mandated price transparency XML feeds. Each chain
publishes PriceFull (every item + price) and PromoFull (active promotions)
files per store, refreshed daily.

v1 scope: Shufersal (most-open feed), Tel Aviv area flagship store.
Pattern is extensible — add another chain by writing a `_fetch_<chain>()`
and `_parse_<chain>()` pair.

Filters promotions down to items whose product name matches any Hebrew
meat / deli keyword (fresh cuts, poultry, processed, cold cuts), excluding
fish, dairy, and plant-based.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

import httpx
from rich.console import Console
from rich.table import Table

from price_watcher import logging_setup
from price_watcher.paths import data_dir

log = logging_setup.get(__name__)
console = Console()


_HEADERS = {"User-Agent": "Mozilla/5.0 (price-watcher)"}
_SHUFERSAL_CATEGORY_URL = "https://prices.shufersal.co.il/FileObject/UpdateCategory?catID={cat}"


# ---- Hebrew keyword classifier ----

_MEAT_KEYWORDS: tuple[str, ...] = (
    # Fresh meat / cuts (dropped ambiguous short ones: לב, לחי, לשון)
    "בשר", "בקר", "עגל", "כבש", "טלה", "כבד",
    "אנטריקוט", "פילה", "סטייק", "צלעות", "צלי",
    # Poultry
    "עוף", "הודו", "פרגית", "שוקיים", "כנפיים",
    "חזה עוף", "חזה הודו", "ירך עוף", "שוק עוף", "פרגיות",
    # Processed / deli (מעדנייה / אטליז)
    "נקניק", "נקניקיה", "נקניקיות", "נקניקים",
    "פסטרמה", "סלמי", "קבנוס",
    "קבב", "המבורגר", "קציצה", "קציצות",
    "שווארמה", "צ'וריסו", "מרגז", "מרגז'",
    "שניצל", "שניצלים", "שיפוד", "שיפודים",
    "אטליז",
)

_EXCLUDE_KEYWORDS: tuple[str, ...] = (
    # Fish / seafood
    "דג", "סלמון", "טונה", "סרדין", "הרינג", "שרימפ", "קרפיון",
    "אמנון", "דניס", "לברק", "פורל", "בורי", "מושט", "נילוס",
    "סיבס", "מוסר", "פילה דג", "דג סלמון",
    "גפילטע", "פיש",
    # Plant-based
    "טבעוני", "צמחי", "VEGAN", "ווגן",
    "סויה", "טופו", "סייטן",
    "ללא בשר", "ללא עוף",
    # Dairy / cheese (sometimes labeled with "בקר" for shape/brand)
    "חלבי", "גבינה", "גאודה", "גאודת", "אמנטל", "מוצרלה", "צ'דר",
    "קוטג", "לבנה",
    # Confusing categories
    "מדומה",  # mock steak / mock fillet (often fish-based or soy)
)


# A Hebrew-safe word-boundary: the keyword must be preceded/followed by a non-letter
# (space, punctuation, digit, or start/end). Prevents "לב" matching "שלב".
def _build_keyword_pattern() -> re.Pattern:
    escaped = sorted((re.escape(k) for k in _MEAT_KEYWORDS), key=len, reverse=True)
    return re.compile(
        r"(?:^|[^א-ת])(?:" + "|".join(escaped) + r")(?:$|[^א-ת])",
        re.UNICODE,
    )


_MEAT_PAT = _build_keyword_pattern()


def _is_meat_product(name: str) -> bool:
    if not name:
        return False
    for kw in _EXCLUDE_KEYWORDS:
        if kw in name:
            return False
    return bool(_MEAT_PAT.search(name))


# ---- Data model ----

@dataclass
class MeatPromo:
    chain: str
    store_id: str
    item_code: str
    item_name: str
    regular_price: Optional[Decimal]
    discounted_price: Optional[Decimal]
    discount_rate: Optional[Decimal]  # percent
    promotion_description: str
    end_date: Optional[str] = None

    @property
    def savings_pct(self) -> Optional[Decimal]:
        if self.discount_rate is not None and self.discount_rate > 0:
            return self.discount_rate
        if self.regular_price and self.discounted_price and self.regular_price > 0:
            delta = (self.regular_price - self.discounted_price) / self.regular_price * 100
            return delta.quantize(Decimal("1"))
        return None

    @property
    def savings_amount(self) -> Optional[Decimal]:
        if self.regular_price and self.discounted_price:
            return (self.regular_price - self.discounted_price).quantize(Decimal("0.01"))
        return None


@dataclass
class SupermarketReport:
    status: str
    chains_scanned: list[str] = field(default_factory=list)
    promos: list[MeatPromo] = field(default_factory=list)
    error: Optional[str] = None


# ---- Shufersal ----

def _shufersal_latest_file(cat: int, store_id: str | None = None) -> Optional[str]:
    """Pick the newest file URL from the Shufersal category listing.

    If `store_id` is given, prefer a file whose name matches that store.
    """
    r = httpx.get(_SHUFERSAL_CATEGORY_URL.format(cat=cat), headers=_HEADERS, timeout=30)
    if r.status_code != 200:
        return None
    links = re.findall(r'href="([^"]+)"', r.text)
    links = [l.replace("&amp;", "&") for l in links if ".gz" in l or ".xml" in l]
    if not links:
        return None
    if store_id:
        matching = [l for l in links if f"-{store_id}-" in l]
        if matching:
            return matching[0]
    return links[0]


def _download_and_parse(url: str) -> Optional[ET.Element]:
    r = httpx.get(url, timeout=120, follow_redirects=True)
    if r.status_code != 200:
        log.warning("download failed: HTTP %s", r.status_code)
        return None
    try:
        xml_bytes = gzip.decompress(r.content)
    except (OSError, EOFError):
        xml_bytes = r.content
    try:
        return ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        log.warning("XML parse failed: %s", exc)
        return None


def _build_item_index(pricefull_root: ET.Element) -> dict[str, tuple[str, Optional[Decimal]]]:
    """Map ItemCode → (ItemName, Price) from a PriceFull XML tree."""
    index: dict[str, tuple[str, Optional[Decimal]]] = {}
    items = pricefull_root.find("Items") or pricefull_root.find(".//Items")
    if items is None:
        return index
    for item in items.findall("Item"):
        code_el = item.find("ItemCode")
        name_el = item.find("ItemName")
        price_el = item.find("ItemPrice")
        if code_el is None or name_el is None:
            continue
        code = (code_el.text or "").strip()
        name = (name_el.text or "").strip()
        price_text = (price_el.text or "").strip() if price_el is not None else ""
        try:
            price = Decimal(price_text) if price_text else None
        except (ArithmeticError, ValueError):
            price = None
        if code and name:
            index[code] = (name, price)
    return index


def scan_shufersal(store_id: str | None = None) -> list[MeatPromo]:
    """Return meat promotions for one Shufersal store."""
    log.info("Shufersal: fetching PromoFull + PriceFull")
    promo_url = _shufersal_latest_file(cat=4, store_id=store_id)
    price_url = _shufersal_latest_file(cat=2, store_id=store_id)
    if not promo_url or not price_url:
        log.warning("Shufersal: could not resolve feed URLs")
        return []

    # Infer store id from URL if not provided
    if store_id is None:
        m = re.search(r"-(\d{3})-(\d{3})-", promo_url)
        if m:
            store_id = m.group(2)

    log.info("Shufersal: downloading PriceFull...")
    price_root = _download_and_parse(price_url)
    log.info("Shufersal: downloading PromoFull...")
    promo_root = _download_and_parse(promo_url)
    if price_root is None or promo_root is None:
        return []

    item_index = _build_item_index(price_root)
    log.info("Shufersal: indexed %d items", len(item_index))

    results: list[MeatPromo] = []
    promos_container = promo_root.find("Promotions") or promo_root.find(".//Promotions")
    if promos_container is None:
        return []

    seen: set[tuple[str, str]] = set()  # (item_code, promo_description)
    for promo in promos_container.findall("Promotion"):
        desc_el = promo.find("PromotionDescription")
        desc = (desc_el.text or "").strip() if desc_el is not None else ""
        end_el = promo.find("PromotionEndDateTime")
        end = (end_el.text or "").strip() if end_el is not None else ""

        # Skip customer-service compensation coupons (restricted to specific users)
        is_coupon_el = promo.find("AdditionalIsCoupon")
        if is_coupon_el is not None and (is_coupon_el.text or "").strip() == "1":
            continue
        if "פיצוי" in desc:  # explicit compensation coupons
            continue
        # Skip club-restricted promos (only "0 - כלל הלקוחות" / general public counts)
        club_el = promo.find("ClubID")
        if club_el is not None and club_el.text:
            club_text = club_el.text.strip()
            if club_text and not (club_text.startswith("0") or "כלל" in club_text):
                continue

        items_container = promo.find(".//PromotionItems")
        if items_container is None:
            continue

        for pi in items_container.findall("PromotionItem"):
            code_el = pi.find("ItemCode")
            if code_el is None:
                continue
            code = (code_el.text or "").strip()
            if not code or code == "0" * len(code):
                continue
            indexed = item_index.get(code)
            if not indexed:
                continue
            name, regular_price = indexed
            if not _is_meat_product(name):
                continue
            key = (code, desc)
            if key in seen:
                continue
            seen.add(key)

            dr_el = pi.find("DiscountRate")
            dp_el = pi.find("DiscountedPrice")
            try:
                discount_rate = Decimal((dr_el.text or "").strip()) if dr_el is not None and dr_el.text else None
            except (ArithmeticError, ValueError):
                discount_rate = None
            try:
                discounted_price = Decimal((dp_el.text or "").strip()) if dp_el is not None and dp_el.text else None
            except (ArithmeticError, ValueError):
                discounted_price = None

            # Skip entries with no discount signal at all
            if discount_rate is None and discounted_price is None:
                continue

            results.append(MeatPromo(
                chain="Shufersal",
                store_id=store_id or "?",
                item_code=code,
                item_name=name,
                regular_price=regular_price,
                discounted_price=discounted_price,
                discount_rate=discount_rate,
                promotion_description=desc,
                end_date=end[:10] if end else None,
            ))

    return results


# ---- Orchestrator ----

def scan_and_report() -> SupermarketReport:
    """Scan all configured chains and return a combined report."""
    log.info("Running supermarket meat scan")
    report = SupermarketReport(status="ok")
    try:
        # Store 001 is a flagship Tel Aviv branch with a representative promo list.
        promos = scan_shufersal(store_id="001")
        report.promos.extend(promos)
        report.chains_scanned.append("Shufersal")
    except Exception as exc:
        log.error("Shufersal scan failed: %s: %s", type(exc).__name__, exc)
    # Dedupe: the same promotion description often covers multiple item
    # variants (different brands of the same cut). Keep one representative
    # per (chain, promotion_description, rounded savings_pct) tuple.
    seen: set[tuple[str, str, int]] = set()
    deduped: list[MeatPromo] = []
    for p in report.promos:
        pct_key = int(p.savings_pct) if p.savings_pct is not None else -1
        key = (p.chain, p.promotion_description, pct_key)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(p)
    report.promos = deduped

    def _rank_key(p: MeatPromo) -> tuple:
        pct = p.savings_pct or Decimal("0")
        amt = p.savings_amount or Decimal("0")
        return (-float(pct), -float(amt))
    report.promos.sort(key=_rank_key)
    report.promos = report.promos[:30]
    if not report.chains_scanned:
        report.status = "fetch_failed"
        report.error = "all chains failed to scan"
    return report


# ---- State persistence (so we can diff tomorrow if desired) ----

def _state_file() -> Path:
    return data_dir() / "supermarkets-state.json"


def _promo_to_dict(p: MeatPromo) -> dict:
    return {
        **asdict(p),
        "regular_price": str(p.regular_price) if p.regular_price is not None else None,
        "discounted_price": str(p.discounted_price) if p.discounted_price is not None else None,
        "discount_rate": str(p.discount_rate) if p.discount_rate is not None else None,
    }


def persist(report: SupermarketReport) -> None:
    if report.status != "ok":
        return
    _state_file().write_text(
        json.dumps({
            "schema_version": 1,
            "last_scanned_at": datetime.now().astimezone().isoformat(),
            "promos": [_promo_to_dict(p) for p in report.promos],
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ---- Terminal output ----

def print_report(report: SupermarketReport) -> None:
    if report.status != "ok":
        console.print(f"[red]Supermarket scan failed: {report.status}[/red]  {report.error or ''}")
        return
    console.print(f"[dim]Chains scanned: {', '.join(report.chains_scanned)}[/dim]")
    if not report.promos:
        console.print("[dim]No meat promos found today.[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("Chain", width=10)
    table.add_column("Item")
    table.add_column("Was")
    table.add_column("Now")
    table.add_column("Off")
    table.add_column("Promo")
    for p in report.promos[:20]:
        was = f"₪{p.regular_price}" if p.regular_price else "—"
        now = f"₪{p.discounted_price}" if p.discounted_price else "—"
        off = f"{p.savings_pct}%" if p.savings_pct is not None else "—"
        table.add_row(p.chain, p.item_name[:50], was, now, off, p.promotion_description[:40])
    console.print(table)
