"""Creatine price watcher.

Scrapes iHerb's creatine search page (which works from a plain httpx client —
confirmed 2026-11-11). iHerb helpfully pre-computes price-per-100g in each
product cell, so we don't need to parse pack sizes ourselves.

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


def _parse_price(text: str) -> Optional[tuple[Decimal, str, Optional[Decimal], str, bool]]:
    """Parse iHerb's price cell text.

    Examples:
      '₪94.64 (₪9.49/100g)'
      '₪61.87 (₪0.26/count)'
      '₪78.64 (₪22.21/100g) 25% off ₪104.85'

    Returns (price, currency, per_unit_price_or_none, unit, on_sale).
    """
    if not text:
        return None
    text = text.strip()

    m = re.match(r"₪\s*([\d,]+\.?\d*)\s*(?:\(\s*₪\s*([\d,]+\.?\d*)\s*/\s*(100g|count|g)\s*\))?", text)
    if not m:
        return None
    try:
        price = Decimal(m.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    per_unit_price = None
    unit = "unknown"
    if m.group(2) and m.group(3):
        try:
            per_unit_price = Decimal(m.group(2).replace(",", ""))
            unit = m.group(3)
        except InvalidOperation:
            pass
    on_sale = "% off" in text.lower()
    return price, "ILS", per_unit_price, unit, on_sale


def scan_iherb() -> list[CreatineProduct]:
    """Fetch and parse iHerb's creatine search page."""
    with httpx.Client(follow_redirects=True, timeout=25, headers=_HEADERS) as client:
        resp = client.get(_IHERB_SEARCH)
    if resp.status_code != 200:
        log.warning("iHerb creatine scan: HTTP %s", resp.status_code)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    products: list[CreatineProduct] = []
    for cell in soup.select(".product-cell"):
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
    """Return the cheapest product by price-per-100g (powder only)."""
    powders = [p for p in products if p.price_per_100g is not None]
    if not powders:
        return None
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
        [p for p in products if p.price_per_100g is not None],
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
    status: str  # "ok" / "no_products" / "no_powder" / "fetch_failed"
    products: list[CreatineProduct] = field(default_factory=list)  # top 10 powders by ₪/100g
    cheapest: Optional[CreatineProduct] = None
    previous_cheapest: Optional[CreatineProduct] = None
    changed: bool = False
    direction: Optional[str] = None  # "dropped" / "rose" / None
    error: Optional[str] = None


def scan_and_report() -> CreatineReport:
    """Scan iHerb, compare against stored cheapest, return a report (no email, no save)."""
    log.info("Scanning iHerb creatine")
    products = scan_iherb()
    if not products:
        return CreatineReport(status="fetch_failed", error="no products scraped (iHerb may be blocking)")

    cheapest = pick_cheapest(products)
    if cheapest is None:
        return CreatineReport(status="no_powder", products=products)

    state = _load_state()
    previous = _product_from_dict(state["cheapest"]) if state.get("cheapest") else None

    changed = False
    direction = None
    if previous is None:
        changed = True  # first observation counts as "to be announced"
    elif previous.price_per_100g != cheapest.price_per_100g or previous.name != cheapest.name:
        changed = True
        if previous.price_per_100g and cheapest.price_per_100g:
            direction = "dropped" if cheapest.price_per_100g < previous.price_per_100g else "rose"

    powders = sorted(
        [p for p in products if p.price_per_100g is not None],
        key=lambda p: p.price_per_100g,
    )[:10]

    return CreatineReport(
        status="ok",
        products=powders,
        cheapest=cheapest,
        previous_cheapest=previous,
        changed=changed,
        direction=direction,
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
