"""The Sims 4 expansion / game / stuff pack discount watcher.

Scrapes EA's Sims 4 store page (public, no bot protection as of 2026-11-11)
and extracts discount info from the page's embedded Next.js data (parsed as
JSON, not regexed). Prices shown by EA to non-logged-in international users
are in USD.

Only the first page of the store (50 packs) is in the HTML; the "Show more"
button loads further pages client-side and there is no plain-HTTP cursor or
page parameter (`?page=2` returns the same 50), so later packs are not seen.
State is therefore merged, not replaced, so packs that merely fall off the
first page are not announced again when they return.

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
from price_watcher.http_headers import USER_AGENT
from price_watcher.paths import data_dir

log = logging_setup.get(__name__)
console = Console()


_EA_STORE_URL = "https://www.ea.com/games/the-sims/the-sims-4/store/categories/all-packs"

_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en",
    "Accept-Encoding": "gzip, deflate, br",
}

# EA's store is a Next.js app: pack data sits in `self.__next_f.push([1, "..."])`
# script chunks whose strings hold "flight" rows (`<hex id>:<json>`).
_PUSH_MARK = "self.__next_f.push("
_PACK_URL_BASE = "https://www.ea.com/games/the-sims/the-sims-4/buy/addon/"
_PAGE_SIZE = 50  # packs delivered in the HTML; more need the "Show more" button


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
        # Verified 2026-11-11: HTTP 200 for e.g. .../buy/addon/the-sims-4-seasons.
        # (.../store/<slug> and .../store/addons/<slug> are HTTP 404.)
        return _PACK_URL_BASE + self.slug


@dataclass
class Sims4Report:
    status: str  # "ok" / "fetch_failed" / "parse_failed"
    packs: list[Sims4Pack] = field(default_factory=list)
    on_sale_now: list[Sims4Pack] = field(default_factory=list)
    newly_on_sale: list[Sims4Pack] = field(default_factory=list)
    discount_ended: list[Sims4Pack] = field(default_factory=list)
    discount_changed: list[tuple[Sims4Pack, int]] = field(default_factory=list)  # (pack, old_pct)
    error: Optional[str] = None
    page_limited: bool = False  # True when only the first store page could be read


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


_NAME_JUNK = re.compile(r"[™®�]")


def _clean_title(title: str) -> str:
    """Drop trademark signs / replacement chars and stray whitespace from a store title."""
    return " ".join(_NAME_JUNK.sub("", title).split())


def _flight_rows(html: str) -> list:
    """Decode the Next.js flight data embedded in the page into JSON values."""
    decoder = json.JSONDecoder()
    text_parts: list[str] = []
    pos = 0
    while True:
        i = html.find(_PUSH_MARK, pos)
        if i < 0:
            break
        start = i + len(_PUSH_MARK)
        pos = start
        try:
            arr, end = decoder.raw_decode(html, start)
        except ValueError:
            continue
        pos = end
        if isinstance(arr, list) and len(arr) == 2 and arr[0] == 1 and isinstance(arr[1], str):
            text_parts.append(arr[1])
    rows = []
    for line in "".join(text_parts).split(chr(10)):
        head, sep, payload = line.partition(":")
        if not sep or not payload or payload[0] not in '[{':
            continue
        try:
            rows.append(json.loads(payload))
        except ValueError:
            continue
    return rows


def _walk_packs(node, out: list[dict]) -> None:
    if isinstance(node, dict):
        if isinstance(node.get("slug"), str) and isinstance(node.get("lowestPriceGameProduct"), dict):
            out.append(node)
        for v in node.values():
            _walk_packs(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_packs(v, out)


def _pack_from_node(node: dict) -> Optional[Sims4Pack]:
    slug = node["slug"]
    info = node["lowestPriceGameProduct"].get("nonSubscriberPricingInfo")
    if not isinstance(info, dict):
        return None
    opt = info.get("lowestPricePurchaseOption")
    if not isinstance(opt, dict) or opt.get("amount") is None:
        return None
    try:
        price = Decimal(str(opt["amount"]))
        orig_raw = opt.get("originalAmount")
        original = Decimal(str(orig_raw)) if orig_raw is not None else price
    except (ValueError, ArithmeticError):
        return None
    try:
        disc = int(opt.get("discountPercentage"))
    except (TypeError, ValueError):
        disc = int(round((1 - price / original) * 100)) if original > price else 0
    title = node.get("title")
    name = _clean_title(title) if isinstance(title, str) and title.strip() else _slug_to_name(slug)
    return Sims4Pack(slug=slug, name=name, price=price, original_price=original, discount_percent=disc)


def parse_packs(html: str) -> list[Sims4Pack]:
    """Extract packs (non-subscriber price, real title) from the store page HTML."""
    nodes: list[dict] = []
    for row in _flight_rows(html):
        _walk_packs(row, nodes)
    packs: list[Sims4Pack] = []
    seen: set[str] = set()
    for node in nodes:
        if node["slug"] in seen:
            continue
        pack = _pack_from_node(node)
        if pack is None:
            continue
        seen.add(pack.slug)
        packs.append(pack)
    return packs


def _fetch(url: str) -> Optional[str]:
    try:
        with httpx.Client(follow_redirects=True, timeout=25, headers=_HEADERS) as client:
            resp = client.get(url)
    # HTTPError also covers TooManyRedirects / DecodingError, not only transport errors.
    except httpx.HTTPError as exc:
        log.error("EA fetch failed: %s", exc)
        return None
    if resp.status_code != 200:
        log.warning("EA returned HTTP %s", resp.status_code)
        return None
    return resp.text


@dataclass
class Sims4Scan:
    """Outcome of reading the store page: the packs, and whether it loaded at all."""
    packs: list[Sims4Pack] = field(default_factory=list)
    pages: int = 0
    fetched: bool = False


# Whether the last scan_ea() call loaded the store page (lets scan_and_report
# tell "EA unreachable" from "page loaded but no packs could be read").
_last_fetch_ok = False


def scan_ea_detailed(fetch=None) -> Sims4Scan:
    """Fetch the all-packs page (first 50 packs only, see the module docstring)."""
    global _last_fetch_ok
    html = (fetch or _fetch)(_EA_STORE_URL)
    _last_fetch_ok = html is not None
    if html is None:
        return Sims4Scan()
    return Sims4Scan(packs=parse_packs(html), pages=1, fetched=True)


def scan_ea() -> list[Sims4Pack]:
    """Fetch the EA Sims 4 all-packs page and extract pack info."""
    return scan_ea_detailed().packs


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

def scan_and_report(scan: Optional[Sims4Scan] = None) -> Sims4Report:
    """Scan EA, compare against stored state, return a report (no email, no save)."""
    global _last_fetch_ok
    log.info("Scanning EA Sims 4 packs")
    if scan is None:
        _last_fetch_ok = False
        found = scan_ea()
        loaded = bool(found) or _last_fetch_ok
        scan = Sims4Scan(packs=found, pages=1 if loaded else 0, fetched=loaded)
    packs = scan.packs
    if not scan.fetched:
        return Sims4Report(status="fetch_failed", error="לא ניתן היה לטעון את החנות של EA")
    if not packs:
        return Sims4Report(status="parse_failed", error="לא זוהו חבילות בעמוד של EA")

    state = _load_state()
    stored = state.get("packs", {})

    report = Sims4Report(status="ok", packs=packs, page_limited=len(packs) >= _PAGE_SIZE)
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
    # Merge, don't replace: a pack that drops off the first store page keeps its
    # last known state, so it is not re-announced when it comes back.
    now = datetime.now().astimezone().isoformat()
    merged = dict(state.get("packs", {}))
    merged.update({p.slug: {**_pack_to_dict(p), "last_seen_at": now} for p in report.packs})
    state["packs"] = merged
    state["last_scanned_at"] = now
    _save_state(state)


def print_report(report: Sims4Report) -> None:
    """Terminal output for a report."""
    if report.status != "ok":
        console.print(f"[red]Sims 4 scan failed: {report.status}[/red]  {report.error or ''}")
        return

    note = " (first store page only; later packs are not visible)" if report.page_limited else ""
    console.print(f"[dim]Scanned {len(report.packs)} packs from EA{note}[/dim]")

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
