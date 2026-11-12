"""The Sims 4 expansion / game / stuff pack discount watcher.

Scrapes EA's Sims 4 store page (public, no bot protection as of 2026-11-11)
and extracts discount info from the JSON embedded in the HTML. Prices shown by
EA to non-logged-in international users are in USD.

The page embeds its data as JSON, usually inside JavaScript string literals
(``self.__next_f.push([1,"..."])``, i.e. with escaped quotes) and sometimes as
a plain ``<script type="application/json">`` block. We unescape those strings,
decode every JSON value in them, and walk the result for *pack objects*: dicts
with a ``slug`` (or a locale-prefixed ``id``) and a purchase option. Per pack
we read ``title``, ``slug``, ``type`` and the ``nonSubscriberPricingInfo`` block
(never the EA Play ``subscriberPricingInfo``). A pack without a purchase option
is skipped instead of borrowing its neighbour's price.

The store only renders the first batch of packs ("Show more" loads the rest).
When the page data exposes paging (``pageInfo.hasNextPage`` + ``endCursor``, or
an explicit next-page URL) we follow it; see ``_next_page_url``.

Daily cadence. Alerts on: packs newly on sale, packs whose discount ended,
and packs whose discount percentage changed. Stored state is *merged*, so a
pack that is missing from one scan keeps its last known price.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterator, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from rich.console import Console
from rich.table import Table

from price_watcher import logging_setup
from price_watcher.http_headers import USER_AGENT
from price_watcher.paths import data_dir

log = logging_setup.get(__name__)
console = Console()


_EA_BASE = "https://www.ea.com"
_EA_STORE_URL = f"{_EA_BASE}/games/the-sims/the-sims-4/store/categories/all-packs"
_EA_PACKS_BASE = f"{_EA_BASE}/games/the-sims/the-sims-4/packs"

_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en",
    "Accept-Encoding": "gzip, deflate, br",
}

# Safety cap on "Show more" pages per scan.
MAX_PAGES = 10
# Query parameter used to pass a GraphQL-style ``endCursor`` back to the store
# page. Unverified against the live site (no network when this was written);
# if EA ignores it the next page repeats the first batch, which yields no new
# packs and stops the loop.
_CURSOR_PARAM = "after"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Sims4Pack:
    slug: str
    name: str
    price: Decimal
    original_price: Decimal
    discount_percent: int
    currency: str = "USD"
    pack_type: Optional[str] = None   # EA's type, e.g. "EXPANSION_PACK" / "MICRO_CONTENT"
    store_url: Optional[str] = None   # link found in the pack's own JSON, if any

    @property
    def is_on_sale(self) -> bool:
        return self.discount_percent > 0

    @property
    def url(self) -> str:
        return pack_url(self.slug, self.pack_type, self.name, self.store_url)


@dataclass
class Sims4Report:
    status: str  # "ok" / "fetch_failed" / "parse_failed"
    packs: list[Sims4Pack] = field(default_factory=list)
    on_sale_now: list[Sims4Pack] = field(default_factory=list)
    newly_on_sale: list[Sims4Pack] = field(default_factory=list)
    discount_ended: list[Sims4Pack] = field(default_factory=list)
    discount_changed: list[tuple[Sims4Pack, int]] = field(default_factory=list)  # (pack, old_pct)
    error: Optional[str] = None
    pages_read: int = 0
    catalog_complete: bool = True        # False when the page said more packs exist than we read
    total_reported: Optional[int] = None  # total pack count the page data claims, if any


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

_TYPE_DIRS = {
    "EXPANSION_PACK": "expansion-packs",
    "EXPANSION": "expansion-packs",
    "GAME_PACK": "game-packs",
    "STUFF_PACK": "stuff-packs",
    "KIT": "kits",
    "CREATOR_KIT": "kits",
}
_TITLE_DIRS = (
    (re.compile(r"\bexpansion\s+pack\b", re.I), "expansion-packs"),
    (re.compile(r"\bgame\s+pack\b", re.I), "game-packs"),
    (re.compile(r"\bstuff\s+pack\b", re.I), "stuff-packs"),
    (re.compile(r"\bkit\b", re.I), "kits"),
)


def pack_type_dir(pack_type: Optional[str], title: Optional[str] = None) -> Optional[str]:
    """Directory of EA's pack pages for this pack, or None when unknown.

    The explicit type wins; ``MICRO_CONTENT`` (game packs, stuff packs and kits
    alike) is resolved from the title ("… Stuff Pack", "… Kit").
    """
    if pack_type and pack_type.upper() in _TYPE_DIRS:
        return _TYPE_DIRS[pack_type.upper()]
    for pat, d in _TITLE_DIRS:
        if title and pat.search(title):
            return d
    return None


def pack_url(
    slug: str,
    pack_type: Optional[str] = None,
    title: Optional[str] = None,
    store_url: Optional[str] = None,
) -> str:
    """The link used for a pack in emails and the terminal.

    1. A URL carried by the pack's own JSON (most trustworthy).
    2. EA's pack page ``/games/the-sims/the-sims-4/packs/<type-dir>/<slug>``
       when the type directory is known.
    3. Otherwise the all-packs store page we scrape, which is known to load.

    The old ``/the-sims-4/store/<slug>`` pattern returns 404/500 (#19). The
    pattern in step 2 still needs one real request to be confirmed.
    """
    if store_url:
        return store_url
    type_dir = pack_type_dir(pack_type, title)
    if slug and type_dir:
        return f"{_EA_PACKS_BASE}/{type_dir}/{slug}"
    return _EA_STORE_URL


# ---------------------------------------------------------------------------
# Embedded-JSON extraction
# ---------------------------------------------------------------------------

_SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.DOTALL | re.IGNORECASE)
_JS_STRING_RE = re.compile(r'"((?:[^"\\]|\\.)*)"', re.DOTALL)
_MARKERS = ("PricingInfo", "lowestPricePurchaseOption", "slug", "hasNextPage")
_LOCALE_ID_RE = re.compile(r"^[a-z]{2}-[a-z]{2}_([a-z0-9][a-z0-9-]*)", re.IGNORECASE)
_URL_KEYS = ("url", "href", "link", "pdpUrl", "productUrl", "canonicalUrl", "storeUrl", "path", "pdpPath")
_SKIP_FOR_URL = ("image", "images", "media", "thumbnail", "logo", "icon", "video", "asset", "assets")


def _decode_all(text: str) -> Iterator[Any]:
    """Yield every top-level JSON object/array found anywhere in ``text``."""
    decoder = json.JSONDecoder()
    i, n = 0, len(text)
    while i < n:
        j_obj, j_arr = text.find("{", i), text.find("[", i)
        cands = [j for j in (j_obj, j_arr) if j != -1]
        if not cands:
            return
        j = min(cands)
        try:
            value, end = decoder.raw_decode(text, j)
        except ValueError:
            i = j + 1
            continue
        if isinstance(value, (dict, list)):
            yield value
        i = end


def _embedded_json(html: str) -> Iterator[Any]:
    """Every JSON value embedded in the page's scripts (escaped or not)."""
    for attrs, body in _SCRIPT_RE.findall(html):
        if not any(m in body for m in _MARKERS):
            continue
        if "json" in attrs.lower():
            try:
                yield json.loads(body)
                continue
            except ValueError:
                pass
        # Plain (unescaped) JSON in the script body, e.g. `window.__DATA__ = {...}`.
        if '"slug"' in body or '"title"' in body:
            yield from _decode_all(body)
        # JSON carried inside JS string literals: unescape once, then decode.
        for lit in _JS_STRING_RE.findall(body):
            if not any(m in lit for m in _MARKERS):
                continue
            try:
                text = json.loads(f'"{lit}"')
            except ValueError:
                continue
            yield from _decode_all(text)


def _walk(node: Any) -> Iterator[dict]:
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(reversed(list(cur.values())))
        elif isinstance(cur, list):
            stack.extend(reversed(cur))


def _pack_slug(d: dict) -> Optional[str]:
    slug = d.get("slug")
    if isinstance(slug, str) and slug.strip():
        return slug.strip()
    pid = d.get("id")
    if isinstance(pid, str):
        m = _LOCALE_ID_RE.match(pid)
        if m:
            return m.group(1)
    return None


def _has_price_info(d: dict) -> bool:
    return any(k in d for k in ("lowestPricePurchaseOption", "nonSubscriberPricingInfo", "purchaseOptions"))


def _is_pack(d: dict) -> bool:
    return _has_price_info(d) and _pack_slug(d) is not None


def _find_key(node: Any, key: str, depth: int = 4) -> Any:
    """First value stored under ``key`` within ``depth`` levels, without
    descending into nested pack objects or subscriber pricing."""
    if depth < 0:
        return None
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for k, v in node.items():
            if k == "subscriberPricingInfo" or (isinstance(v, dict) and _is_pack(v)):
                continue
            hit = _find_key(v, key, depth - 1)
            if hit is not None:
                return hit
    elif isinstance(node, list):
        for v in node:
            hit = _find_key(v, key, depth - 1)
            if hit is not None:
                return hit
    return None


def _dec(v: Any) -> Optional[Decimal]:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, dict):
        v = v.get("amount", v.get("value"))
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _price_block(info: Any) -> Optional[tuple[Decimal, Decimal, int, str]]:
    """(price, original, discount_pct, currency) from one pricing block."""
    if not isinstance(info, dict):
        return None
    block = info
    if "amount" not in block:
        block = next((d for d in _walk(info) if "amount" in d), None)
        if block is None:
            return None
    amount = _dec(block.get("amount"))
    if amount is None:
        return None
    original = _dec(block.get("originalAmount"))
    if original is None or original <= 0:
        original = amount
    disc_raw = block.get("discountPercentage")
    try:
        disc = int(Decimal(str(disc_raw))) if disc_raw is not None else None
    except (InvalidOperation, ValueError):
        disc = None
    if disc is None:
        disc = int(((original - amount) / original * 100).to_integral_value()) if original > amount else 0
    disc = max(disc, 0)
    cur = block.get("currency") or block.get("currencyCode") or info.get("currency") or info.get("currencyCode")
    if isinstance(cur, dict):
        cur = cur.get("code") or cur.get("currencyCode")
    currency = cur if isinstance(cur, str) and cur.strip() else "USD"
    return amount, original, disc, currency.strip().upper()


def _pack_pricing(d: dict) -> Optional[tuple[Decimal, Decimal, int, str]]:
    """Non-subscriber price of one pack object."""
    opt = d.get("lowestPricePurchaseOption")
    if "lowestPricePurchaseOption" in d and not isinstance(opt, dict):
        return None  # explicit null: no purchase option
    scope = opt if isinstance(opt, dict) else d
    info = _find_key(scope, "nonSubscriberPricingInfo")
    if info is not None:
        return _price_block(info)
    # Older shape: the amount fields sit directly in the purchase option.
    if isinstance(opt, dict) and "amount" in opt:
        return _price_block(opt)
    return None


def _pack_store_url(d: dict, slug: str) -> Optional[str]:
    """A link to this pack carried by the JSON itself, if it has one."""
    stack: list[tuple[Any, int]] = [(d, 0)]
    while stack:
        node, depth = stack.pop()
        if not isinstance(node, dict) or depth > 2:
            continue
        for k, v in node.items():
            if k in _SKIP_FOR_URL or "Pricing" in k or k == "lowestPricePurchaseOption":
                continue
            if isinstance(v, dict) and not _is_pack(v):
                stack.append((v, depth + 1))
            elif k in _URL_KEYS and isinstance(v, str) and slug in v:
                if re.search(r"\.(?:png|jpe?g|webp|gif|svg|avif)(?:\?|$)", v, re.I):
                    continue
                full = urljoin(_EA_BASE + "/", v)
                host = urlsplit(full).hostname or ""
                if host == "ea.com" or host.endswith(".ea.com"):
                    return full
    return None


def _str_field(d: dict, *keys: str) -> Optional[str]:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


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


@dataclass
class _Paging:
    has_next: bool = False
    cursor: Optional[str] = None
    next_url: Optional[str] = None
    total: Optional[int] = None


def _collect_paging(d: dict, paging: _Paging) -> None:
    if isinstance(d.get("hasNextPage"), bool):
        paging.has_next = paging.has_next or d["hasNextPage"]
        cur = d.get("endCursor")
        if isinstance(cur, (str, int)) and d["hasNextPage"]:
            paging.cursor = str(cur)
    for k in ("nextPageUrl", "nextUrl", "nextPage"):
        v = d.get(k)
        if isinstance(v, str) and (v.startswith("/") or v.startswith("http")):
            paging.next_url = v
            paging.has_next = True
    for k in ("totalCount", "totalResults"):
        v = d.get(k)
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            paging.total = max(paging.total or 0, v)


def parse_store_page(html: str) -> tuple[list[Sims4Pack], _Paging]:
    """All packs (first occurrence per slug) and the paging info on one page."""
    packs: list[Sims4Pack] = []
    seen: set[str] = set()
    paging = _Paging()
    for doc in _embedded_json(html):
        for d in _walk(doc):
            _collect_paging(d, paging)
            if not _is_pack(d):
                continue
            slug = _pack_slug(d)
            if slug in seen:
                continue
            pricing = _pack_pricing(d)
            if pricing is None:
                log.debug("Sims 4: pack %s has no purchase option, skipped", slug)
                continue
            seen.add(slug)
            price, original, disc, currency = pricing
            title = _str_field(d, "title", "name", "displayName")
            pack_type = _str_field(d, "type", "productType", "packType")
            packs.append(Sims4Pack(
                slug=slug,
                name=title or _slug_to_name(slug),
                price=price,
                original_price=original,
                discount_percent=disc,
                currency=currency,
                pack_type=pack_type,
                store_url=_pack_store_url(d, slug),
            ))
    return packs, paging


def _next_page_url(paging: _Paging, current_url: str) -> Optional[str]:
    """URL of the next "Show more" batch, from what the page data exposes."""
    if paging.next_url:
        return urljoin(current_url, paging.next_url)
    if paging.has_next and paging.cursor:
        parts = urlsplit(current_url)
        query = dict(parse_qsl(parts.query))
        query[_CURSOR_PARAM] = paging.cursor
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
    return None


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

def _fetch(url: str) -> Optional[str]:
    try:
        with httpx.Client(follow_redirects=True, timeout=25, headers=_HEADERS) as client:
            resp = client.get(url)
    except httpx.HTTPError as exc:
        log.error("EA fetch failed: %s", exc)
        return None
    if resp.status_code != 200:
        log.warning("EA returned HTTP %s", resp.status_code)
        return None
    return resp.text


@dataclass
class Sims4Scan:
    packs: list[Sims4Pack] = field(default_factory=list)
    pages: int = 0
    fetched: bool = False
    complete: bool = True
    total: Optional[int] = None


def scan_ea_detailed(
    fetch: Optional[Callable[[str], Optional[str]]] = None, max_pages: int = MAX_PAGES
) -> Sims4Scan:
    """Read the all-packs page and follow its paging, de-duplicating on slug."""
    fetch = fetch or _fetch
    result = Sims4Scan()
    seen: set[str] = set()
    url: Optional[str] = _EA_STORE_URL
    visited: set[str] = set()
    paging = _Paging()
    while url and result.pages < max_pages and url not in visited:
        visited.add(url)
        html = fetch(url)
        if html is None:
            break
        result.fetched = True
        result.pages += 1
        page_packs, paging = parse_store_page(html)
        if paging.total:
            result.total = max(result.total or 0, paging.total)
        new = [p for p in page_packs if p.slug not in seen]
        seen.update(p.slug for p in new)
        result.packs.extend(new)
        if not new:
            break
        url = _next_page_url(paging, url)

    more_reported = paging.has_next or (result.total is not None and len(result.packs) < result.total)
    if result.fetched and more_reported:
        result.complete = False
        log.warning(
            "Sims 4: read %d packs on %d page(s) but the store reports more%s",
            len(result.packs), result.pages,
            f" ({result.total} in total)" if result.total else "",
        )
    return result


def scan_ea() -> list[Sims4Pack]:
    """Fetch the EA Sims 4 all-packs page(s) and extract pack info."""
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
        "pack_type": p.pack_type,
        "store_url": p.store_url,
    }


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def scan_and_report(scan: Optional[Sims4Scan] = None) -> Sims4Report:
    """Scan EA, compare against stored state, return a report (no email, no save)."""
    log.info("Scanning EA Sims 4 packs")
    if scan is None:
        scan = scan_ea_detailed()
    if not scan.fetched:
        return Sims4Report(status="fetch_failed", error="לא ניתן היה לטעון את החנות של EA")
    if not scan.packs:
        return Sims4Report(
            status="parse_failed", error="לא זוהו חבילות בעמוד של EA", pages_read=scan.pages,
        )
    packs = scan.packs

    state = _load_state()
    stored = state.get("packs", {})

    report = Sims4Report(
        status="ok",
        packs=packs,
        pages_read=scan.pages,
        catalog_complete=scan.complete,
        total_reported=scan.total,
    )
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
    """Merge the packs seen in this scan into the stored state.

    Packs not seen this time keep their last known entry, so a pack that drops
    out of the scanned batch and comes back is compared against its real
    previous price instead of being announced as "newly on sale" again.
    """
    if report.status != "ok":
        return
    state = _load_state()
    now = datetime.now().astimezone().isoformat()
    packs = dict(state.get("packs") or {})
    for p in report.packs:
        entry = _pack_to_dict(p)
        entry["last_seen_at"] = now
        packs[p.slug] = entry
    state["packs"] = packs
    state["last_scanned_at"] = now
    _save_state(state)


def print_report(report: Sims4Report) -> None:
    """Terminal output for a report."""
    if report.status != "ok":
        console.print(f"[red]Sims 4 scan failed: {report.status}[/red]  {report.error or ''}")
        return

    console.print(f"[dim]Scanned {len(report.packs)} packs from EA ({report.pages_read} page(s))[/dim]")
    if not report.catalog_complete:
        total = f" of {report.total_reported}" if report.total_reported else ""
        console.print(f"[yellow]Only {len(report.packs)}{total} packs could be read (\"Show more\" not followed).[/yellow]")

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
