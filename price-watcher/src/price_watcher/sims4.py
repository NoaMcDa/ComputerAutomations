"""The Sims 4 expansion / game / stuff pack discount watcher.

Scrapes EA's Sims 4 store page (public, no bot protection as of 2026-11-11)
and extracts discount info from the embedded GraphQL JSON. Prices shown by
EA to non-logged-in international users are in USD.

Daily cadence. Alerts on: packs newly on sale, packs whose discount ended,
and (optionally) packs whose discount percentage changed.
"""

from __future__ import annotations

import json
import os
import re
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


_EA_STORE_URL = "https://www.ea.com/games/the-sims/the-sims-4/store/categories/all-packs"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en",
    "Accept-Encoding": "gzip, deflate, br",
}

# The GraphQL JSON embedded in the HTML has escaped quotes (\"key\":value).
# We walk back from each lowestPricePurchaseOption to the nearest id to use
# the id slug as our pack identifier + human-readable name.
_PACK_PAT = re.compile(
    r'\\"id\\":\\"en-us_([a-z0-9-]+?)_[^"]*?\\".*?'
    r'\\"lowestPricePurchaseOption\\":\{'
    r'.*?\\"amount\\":([\d.]+)'
    r'.*?\\"originalAmount\\":([\d.]+)'
    r'.*?\\"discountPercentage\\":(-?\d+)',
    re.DOTALL,
)


@dataclass
class Sims4Pack:
    slug: str
    name: str
    price: Decimal
    original_price: Decimal
    discount_percent: int
    currency: str = "USD"

    @property
    def is_on_sale(self) -> bool:
        return self.discount_percent > 0

    @property
    def url(self) -> str:
        return f"https://www.ea.com/games/the-sims/the-sims-4/store/{self.slug}"


@dataclass
class Sims4Report:
    status: str  # "ok" / "fetch_failed" / "parse_failed"
    packs: list[Sims4Pack] = field(default_factory=list)
    on_sale_now: list[Sims4Pack] = field(default_factory=list)
    newly_on_sale: list[Sims4Pack] = field(default_factory=list)
    discount_ended: list[Sims4Pack] = field(default_factory=list)
    discount_changed: list[tuple[Sims4Pack, int]] = field(default_factory=list)  # (pack, old_pct)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

def _slug_to_name(slug: str) -> str:
    """Convert 'the-sims-4-cats-and-dogs' → 'The Sims 4: Cats and Dogs'."""
    # Strip leading "the-sims-4-"
    name = slug
    for prefix in ("the-sims-4-", "sims-4-"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    parts = name.replace("-", " ").split()
    titled = " ".join(w if w in ("and", "to", "of") else w.capitalize() for w in parts)
    return f"The Sims 4: {titled}" if titled else slug


def scan_ea() -> list[Sims4Pack]:
    """Fetch the EA Sims 4 all-packs page and extract pack info."""
    try:
        with httpx.Client(follow_redirects=True, timeout=25, headers=_HEADERS) as client:
            resp = client.get(_EA_STORE_URL)
    except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
        log.error("EA fetch failed: %s", exc)
        return []
    if resp.status_code != 200:
        log.warning("EA returned HTTP %s", resp.status_code)
        return []

    matches = _PACK_PAT.findall(resp.text)
    seen: set[str] = set()
    packs: list[Sims4Pack] = []
    for slug, amount_s, orig_s, disc_s in matches:
        if slug in seen:
            continue
        seen.add(slug)
        try:
            price = Decimal(amount_s)
            original = Decimal(orig_s)
            disc = int(disc_s)
        except (ValueError, ArithmeticError):
            continue
        packs.append(Sims4Pack(
            slug=slug,
            name=_slug_to_name(slug),
            price=price,
            original_price=original,
            discount_percent=disc,
        ))
    return packs


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def _state_file() -> Path:
    return data_dir() / "sims4-state.json"


def _load_state() -> dict:
    p = _state_file()
    if not p.exists():
        return {"schema_version": 1, "packs": {}, "last_scanned_at": None}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "packs": {}, "last_scanned_at": None}


def _save_state(state: dict) -> None:
    p = _state_file()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def _pack_to_dict(p: Sims4Pack) -> dict:
    return {
        "slug": p.slug,
        "name": p.name,
        "price": str(p.price),
        "original_price": str(p.original_price),
        "discount_percent": p.discount_percent,
        "currency": p.currency,
    }


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def scan_and_report() -> Sims4Report:
    """Scan EA, compare against stored state, return a report (no email, no save)."""
    log.info("Scanning EA Sims 4 packs")
    packs = scan_ea()
    if not packs:
        return Sims4Report(status="fetch_failed", error="no packs parsed from EA page")

    state = _load_state()
    stored = state.get("packs", {})

    report = Sims4Report(status="ok", packs=packs)
    report.on_sale_now = [p for p in packs if p.is_on_sale]

    for pack in packs:
        prev = stored.get(pack.slug)
        prev_disc = prev["discount_percent"] if prev else 0
        if pack.is_on_sale and prev_disc == 0:
            report.newly_on_sale.append(pack)
        elif not pack.is_on_sale and prev_disc > 0:
            # Mark the previous pricing as "ended"
            report.discount_ended.append(pack)
        elif pack.is_on_sale and prev_disc != pack.discount_percent:
            report.discount_changed.append((pack, prev_disc))

    return report


def persist(report: Sims4Report) -> None:
    """Save the current pack prices to state."""
    if report.status != "ok":
        return
    state = _load_state()
    state["packs"] = {p.slug: _pack_to_dict(p) for p in report.packs}
    state["last_scanned_at"] = datetime.now().astimezone().isoformat()
    _save_state(state)


def print_report(report: Sims4Report) -> None:
    """Terminal output for a report."""
    if report.status != "ok":
        console.print(f"[red]Sims 4 scan failed: {report.status}[/red]  {report.error or ''}")
        return

    console.print(f"[dim]Scanned {len(report.packs)} packs from EA[/dim]")

    if report.newly_on_sale:
        console.print(f"\n[bold green]🆕 NEWLY ON SALE ({len(report.newly_on_sale)}):[/bold green]")
        for p in report.newly_on_sale:
            console.print(f"  • [green]{p.discount_percent}% off[/green]  ${p.price} (was ${p.original_price})  {p.name}")

    if report.discount_ended:
        console.print(f"\n[bold yellow]⚠  DISCOUNT ENDED ({len(report.discount_ended)}):[/bold yellow]")
        for p in report.discount_ended:
            console.print(f"  • back to ${p.price}  {p.name}")

    if report.discount_changed:
        console.print(f"\n[bold cyan]🔁 DISCOUNT % CHANGED:[/bold cyan]")
        for p, old in report.discount_changed:
            console.print(f"  • {old}% → {p.discount_percent}%  ${p.price}  {p.name}")

    if report.on_sale_now and not (report.newly_on_sale or report.discount_ended or report.discount_changed):
        console.print(f"\n[bold]All current sales ({len(report.on_sale_now)}):[/bold]")

    if report.on_sale_now:
        table = Table(show_header=True, header_style="bold")
        table.add_column("Pack")
        table.add_column("Was")
        table.add_column("Now")
        table.add_column("Off")
        for p in sorted(report.on_sale_now, key=lambda x: -x.discount_percent):
            table.add_row(p.name, f"${p.original_price}", f"${p.price}", f"{p.discount_percent}%")
        console.print(table)
    elif not (report.newly_on_sale or report.discount_ended):
        console.print("\n[dim]No packs are currently on sale.[/dim]")
