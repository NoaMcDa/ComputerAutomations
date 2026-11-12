"""Facebook Marketplace price tracking for the daily email (issue #54).

What it does
------------
For every configured search query, load the public (logged-out) Marketplace
search page in headless Chromium, read the listings (id, title, price, link,
location), compare them with ``marketplace_state.json`` and report:

* **price drops** on listings seen before (ignoring noise, see below), and
* **new listings** for a query that already has a baseline (the first run of
  a query only records a baseline, so the first email is not a flood).

The results appear as a "Facebook Marketplace" section in the daily email.
When ``PW_FB_QUERIES`` is not set the feature is off and the section is
omitted completely.

Live behaviour is UNVERIFIED: the page structure and the logged-out access
described here come from general knowledge of Facebook's Marketplace web app,
not from a request made while writing this module. Facebook frequently shows
a login wall to logged-out / automated visitors; that is reported as a status
in the email, never raised. See ``docs/facebook-marketplace.md``.

Configuration (environment / ``.env``)
--------------------------------------
PW_FB_QUERIES        ``;``-separated search queries. A query may carry its own
                     max price after ``|``: ``iphone 13|2000;כיסא משרדי``.
                     Empty / unset = feature off.
PW_FB_LOCATION       Marketplace location slug or numeric id used in the URL
                     (``https://www.facebook.com/marketplace/<location>/search``).
                     Empty = let Facebook pick from the IP address.
PW_FB_RADIUS_KM      Optional search radius in km (``radius=`` URL parameter).
PW_FB_MAX_PRICE      Default max price (₪) for queries without their own.
PW_FB_NOISE_MAX      Prices at or below this are placeholders (₪0 / ₪1 "contact
                     me" listings) and are ignored. Default 1.
PW_FB_MIN_DROP_ILS   A drop must be at least this many ₪ ... (default 20)
PW_FB_MIN_DROP_PCT   ... and at least this percent of the old price (default 5).
PW_FB_REPORT_NEW     1 (default) = also report new listings; 0 = drops only.
PW_FB_MAX_ITEMS      Max deals shown in the email (default 20).
PW_FB_FORGET_DAYS    Listings not seen for this many days are dropped from
                     state (default 30).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from html import escape
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import quote, urlencode

from price_watcher import logging_setup
from price_watcher.paths import data_dir

log = logging_setup.get(__name__)

_SCHEMA_VERSION = 1
_BASE = "https://www.facebook.com"
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Statuses of one query fetch.
STATUS_OK = "ok"
STATUS_LOGIN_WALL = "login_wall"
STATUS_BLOCKED = "blocked"
STATUS_UNAVAILABLE = "unavailable"   # Playwright / Chromium missing
STATUS_ERROR = "error"

_STATUS_HE = {
    STATUS_OK: "תקין",
    STATUS_LOGIN_WALL: "פייסבוק דרש התחברות (חומת התחברות)",
    STATUS_BLOCKED: "הגישה נחסמה",
    STATUS_UNAVAILABLE: "דפדפן Playwright לא מותקן",
    STATUS_ERROR: "שגיאה",
}

SECTION_TITLE = "Facebook Marketplace — מציאות"
EMPTY_STATE = "לא זוהו היום מציאות חדשות ב-Marketplace."


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SearchQuery:
    text: str
    max_price: Optional[Decimal] = None


@dataclass(frozen=True)
class MarketplaceConfig:
    queries: tuple[SearchQuery, ...]
    location: str = ""
    radius_km: Optional[int] = None
    noise_max: Decimal = Decimal("1")
    min_drop_amount: Decimal = Decimal("20")
    min_drop_pct: Decimal = Decimal("5")
    report_new: bool = True
    max_items: int = 20
    forget_days: int = 30


def _dec_env(name: str, default: Optional[str]) -> Optional[Decimal]:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return Decimal(default) if default is not None else None
    try:
        return Decimal(raw.replace(",", ""))
    except InvalidOperation:
        log.warning("Ignoring invalid %s=%r", name, raw)
        return Decimal(default) if default is not None else None


def _int_env(name: str, default: Optional[int]) -> Optional[int]:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("Ignoring invalid %s=%r", name, raw)
        return default


def parse_queries(raw: str, default_max: Optional[Decimal] = None) -> tuple[SearchQuery, ...]:
    """Parse ``"iphone 13|2000; bike"`` into SearchQuery objects (deduplicated)."""
    out: list[SearchQuery] = []
    seen: set[str] = set()
    for chunk in re.split(r"[;\n]", raw or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        text, _, max_part = chunk.partition("|")
        text = " ".join(text.split())
        if not text or text.casefold() in seen:
            continue
        max_price = default_max
        if max_part.strip():
            try:
                max_price = Decimal(max_part.strip().replace(",", ""))
            except InvalidOperation:
                log.warning("Ignoring invalid max price in Marketplace query %r", chunk)
        seen.add(text.casefold())
        out.append(SearchQuery(text=text, max_price=max_price))
    return tuple(out)


def load_config() -> Optional[MarketplaceConfig]:
    """Read the config from the environment. None = feature not configured."""
    default_max = _dec_env("PW_FB_MAX_PRICE", None)
    queries = parse_queries(os.environ.get("PW_FB_QUERIES", ""), default_max)
    if not queries:
        return None
    return MarketplaceConfig(
        queries=queries,
        location=(os.environ.get("PW_FB_LOCATION") or "").strip().strip("/"),
        radius_km=_int_env("PW_FB_RADIUS_KM", None),
        noise_max=_dec_env("PW_FB_NOISE_MAX", "1"),
        min_drop_amount=_dec_env("PW_FB_MIN_DROP_ILS", "20"),
        min_drop_pct=_dec_env("PW_FB_MIN_DROP_PCT", "5"),
        report_new=(os.environ.get("PW_FB_REPORT_NEW", "1").strip().lower() not in ("0", "false", "no", "off")),
        max_items=_int_env("PW_FB_MAX_ITEMS", 20) or 20,
        forget_days=_int_env("PW_FB_FORGET_DAYS", 30) or 30,
    )


def is_configured() -> bool:
    return load_config() is not None


def search_url(query: SearchQuery, cfg: MarketplaceConfig) -> str:
    """Public Marketplace search URL (URL shape UNVERIFIED live)."""
    path = f"/marketplace/{quote(cfg.location)}/search/" if cfg.location else "/marketplace/search/"
    params: dict[str, str] = {"query": query.text, "exact": "false"}
    if query.max_price is not None:
        params["maxPrice"] = str(int(query.max_price))
    if cfg.radius_km:
        params["radius"] = str(cfg.radius_km)
    return f"{_BASE}{path}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Listing:
    item_id: str
    title: str
    price: Optional[Decimal]
    url: str
    location: Optional[str] = None
    currency: str = "ILS"


@dataclass
class QueryResult:
    query: SearchQuery
    status: str
    listings: list[Listing] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class Deal:
    kind: str  # "drop" / "new"
    item_id: str
    title: str
    price: Decimal
    url: str
    location: Optional[str]
    query: str
    prev_price: Optional[Decimal] = None

    @property
    def delta(self) -> Optional[Decimal]:
        if self.prev_price is None:
            return None
        return self.price - self.prev_price

    @property
    def delta_pct(self) -> Optional[Decimal]:
        if self.prev_price is None or self.prev_price <= 0:
            return None
        return ((self.price - self.prev_price) / self.prev_price * 100).quantize(Decimal("1"))


@dataclass
class MarketplaceReport:
    status: str  # "ok" (every query fetched) / "partial" / "failed"
    results: list[QueryResult] = field(default_factory=list)
    deals: list[Deal] = field(default_factory=list)
    hidden_deals: int = 0
    new_state: Optional[dict] = None  # written by persist()

    @property
    def failures(self) -> list[QueryResult]:
        return [r for r in self.results if r.status != STATUS_OK]


# ---------------------------------------------------------------------------
# Parsing (pure; works on the rendered HTML)
# ---------------------------------------------------------------------------

_ITEM_HREF = re.compile(r"/marketplace/item/(\d+)")
_PRICE_RE = re.compile(r"(?:₪|ILS|NIS)\s*([\d][\d,.]*)|([\d][\d,.]*)\s*(?:₪|ILS|NIS|ש\"ח)")
_FREE_WORDS = ("free", "חינם")


def parse_price(text: Any) -> Optional[Decimal]:
    """Parse ``"₪1,200"`` / ``"1200.00"`` / ``"Free"``. Returns None if no price."""
    if text is None:
        return None
    if isinstance(text, (int, float, Decimal)):
        return Decimal(str(text))
    s = str(text).strip()
    if not s:
        return None
    if s.casefold() in _FREE_WORDS:
        return Decimal("0")
    m = _PRICE_RE.search(s)
    raw = (m.group(1) or m.group(2)) if m else None
    if raw is None:
        m2 = re.fullmatch(r"[\d][\d,]*(?:\.\d+)?", s)
        raw = m2.group(0) if m2 else None
    if raw is None:
        return None
    raw = raw.replace(",", "").rstrip(".")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _iter_dicts(obj: Any) -> Iterable[dict]:
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def _location_from_json(node: dict) -> Optional[str]:
    loc = node.get("location")
    if isinstance(loc, dict):
        rg = loc.get("reverse_geocode")
        if isinstance(rg, dict):
            city_page = rg.get("city_page")
            if isinstance(city_page, dict) and city_page.get("display_name"):
                return str(city_page["display_name"])
            parts = [rg.get("city"), rg.get("state")]
            parts = [str(p) for p in parts if p]
            if parts:
                return ", ".join(parts)
    text = node.get("location_text")
    if isinstance(text, dict) and text.get("text"):
        return str(text["text"])
    return None


def _listing_from_json(node: dict) -> Optional[Listing]:
    title = node.get("marketplace_listing_title") or node.get("custom_title")
    item_id = node.get("id")
    if not title or not item_id or not str(item_id).isdigit():
        return None
    if node.get("is_sold") or node.get("is_hidden"):
        return None
    price = None
    lp = node.get("listing_price")
    if isinstance(lp, dict):
        price = parse_price(lp.get("amount"))
        if price is None:
            price = parse_price(lp.get("formatted_amount"))
    currency = "ILS"
    if isinstance(lp, dict) and isinstance(lp.get("currency"), str):
        currency = lp["currency"]
    return Listing(
        item_id=str(item_id),
        title=" ".join(str(title).split()),
        price=price,
        url=f"{_BASE}/marketplace/item/{item_id}/",
        location=_location_from_json(node),
        currency=currency,
    )


def _listings_from_embedded_json(html: str) -> list[Listing]:
    """Walk every inline JSON script for listing nodes (Relay payload)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    found: dict[str, Listing] = {}
    for script in soup.find_all("script"):
        body = script.string or script.get_text() or ""
        if "marketplace_listing_title" not in body:
            continue
        body = body.strip()
        candidates = [body]
        # Some payloads are wrapped (e.g. "requireLazy(...)"); try the outermost object.
        if not body.startswith(("{", "[")):
            start = body.find("{")
            end = body.rfind("}")
            if start != -1 and end > start:
                candidates = [body[start:end + 1]]
        for cand in candidates:
            try:
                data = json.loads(cand)
            except (json.JSONDecodeError, ValueError):
                continue
            for node in _iter_dicts(data):
                if "marketplace_listing_title" not in node:
                    continue
                listing = _listing_from_json(node)
                if listing and listing.item_id not in found:
                    found[listing.item_id] = listing
    return list(found.values())


def _listings_from_dom(html: str) -> list[Listing]:
    """Fallback: the listing cards are links to /marketplace/item/<id>/ whose
    visible text lines are price, (old price), title, location."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    found: dict[str, Listing] = {}
    for a in soup.find_all("a", href=True):
        m = _ITEM_HREF.search(a["href"])
        if not m:
            continue
        item_id = m.group(1)
        if item_id in found:
            continue
        lines = [ln.strip() for ln in a.get_text("\n").split("\n") if ln.strip()]
        if not lines:
            continue
        price = None
        rest: list[str] = []
        for ln in lines:
            if re.search(r"₪|ILS|NIS", ln) or ln.casefold() in _FREE_WORDS:
                if price is None:  # first price line = current; later = struck-through old price
                    price = parse_price(ln)
                continue
            rest.append(ln)
        if not rest:
            continue
        found[item_id] = Listing(
            item_id=item_id,
            title=rest[0],
            price=price,
            url=f"{_BASE}/marketplace/item/{item_id}/",
            location=rest[1] if len(rest) > 1 else None,
        )
    return list(found.values())


_LOGIN_MARKERS = (
    'id="login_form"',
    'name="login"',
    "/login/?next=",
    "You must log in to continue",
    "Log in to Facebook",
    "Log into Facebook",
    "התחבר/י לפייסבוק",
    "התחברות לפייסבוק",
)
_BLOCK_MARKERS = (
    "/checkpoint/",
    "You’re Temporarily Blocked",
    "You're Temporarily Blocked",
    "temporarily blocked",
    "unusual traffic",
)


def classify_page(html: str, final_url: str = "", http_status: Optional[int] = None) -> tuple[str, list[Listing], Optional[str]]:
    """Decide status + listings for one rendered search page (pure)."""
    if http_status in (403, 429):
        return STATUS_BLOCKED, [], f"HTTP {http_status}"
    url_l = (final_url or "").lower()
    if "/checkpoint" in url_l:
        return STATUS_BLOCKED, [], "redirected to a security checkpoint"
    listings = _listings_from_embedded_json(html) or _listings_from_dom(html)
    if listings:
        return STATUS_OK, listings, None
    if "/login" in url_l:
        return STATUS_LOGIN_WALL, [], "redirected to the login page"
    if any(m in html for m in _BLOCK_MARKERS):
        return STATUS_BLOCKED, [], "Facebook reported unusual traffic / temporary block"
    if any(m in html for m in _LOGIN_MARKERS):
        return STATUS_LOGIN_WALL, [], "login wall shown instead of results"
    if http_status is not None and http_status >= 400:
        return STATUS_ERROR, [], f"HTTP {http_status}"
    # A real search page with zero results is indistinguishable from a page we
    # failed to parse; report "ok" with no listings and log it.
    log.info("Marketplace page had no listings (no results, or the markup changed)")
    return STATUS_OK, [], None


# ---------------------------------------------------------------------------
# Fetch (Playwright; not exercised by tests)
# ---------------------------------------------------------------------------

Fetcher = Callable[[SearchQuery, MarketplaceConfig], QueryResult]


def fetch_query(query: SearchQuery, cfg: MarketplaceConfig) -> QueryResult:
    """Load one search page logged-out in headless Chromium. Never raises."""
    url = search_url(query, cfg)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return QueryResult(query, STATUS_UNAVAILABLE, error="Playwright is not installed")
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(
                    headless=True, args=["--disable-blink-features=AutomationControlled"]
                )
            except Exception as exc:  # browser binary missing
                return QueryResult(query, STATUS_UNAVAILABLE, error=f"Chromium not available: {type(exc).__name__}")
            try:
                ctx = browser.new_context(
                    user_agent=_BROWSER_UA,
                    locale="he-IL",
                    viewport={"width": 1366, "height": 900},
                )
                page = ctx.new_page()
                resp = page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                try:
                    # UNVERIFIED selector: listing cards link to /marketplace/item/<id>/
                    page.wait_for_selector('a[href*="/marketplace/item/"]', timeout=20_000)
                except Exception:
                    pass  # login wall / no results; classified below
                html = page.content()
                status, listings, err = classify_page(html, page.url, resp.status if resp else None)
                return QueryResult(query, status, listings, err)
            finally:
                browser.close()
    except Exception as exc:
        log.warning("Marketplace fetch failed for %r: %s: %s", query.text, type(exc).__name__, exc)
        return QueryResult(query, STATUS_ERROR, error=f"{type(exc).__name__}: {exc}"[:200])


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def state_file() -> Path:
    return data_dir() / "marketplace_state.json"


def _empty_state() -> dict:
    return {"schema_version": _SCHEMA_VERSION, "queries": {}, "items": {}}


def load_state() -> dict:
    p = state_file()
    if not p.exists():
        return _empty_state()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("Marketplace state unreadable; starting fresh")
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()
    data.setdefault("queries", {})
    data.setdefault("items", {})
    return data


def save_state(state: dict) -> None:
    """Atomic write: temp file in the same folder, then os.replace."""
    p = state_file()
    tmp = p.with_name(p.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)


# ---------------------------------------------------------------------------
# Price-delta logic (pure)
# ---------------------------------------------------------------------------

def is_placeholder(price: Optional[Decimal], cfg: MarketplaceConfig) -> bool:
    return price is None or price <= cfg.noise_max


def is_meaningful_drop(prev: Optional[Decimal], cur: Optional[Decimal], cfg: MarketplaceConfig) -> bool:
    """True when cur is a real price, prev was a real price, and the drop
    clears both the absolute (₪) and the percentage thresholds."""
    if is_placeholder(prev, cfg) or is_placeholder(cur, cfg):
        return False
    assert prev is not None and cur is not None
    drop = prev - cur
    if drop <= 0:
        return False
    if drop < cfg.min_drop_amount:
        return False
    return drop / prev * 100 >= cfg.min_drop_pct


def _within_max(price: Decimal, query: SearchQuery) -> bool:
    return query.max_price is None or price <= query.max_price


def compute(results: list[QueryResult], state: dict, cfg: MarketplaceConfig,
            now: Optional[datetime] = None) -> tuple[list[Deal], dict]:
    """Compare fetched listings with state. Returns (deals, new_state). Pure."""
    now = now or datetime.now().astimezone()
    now_iso = now.isoformat()
    new_state = json.loads(json.dumps(state))  # deep copy
    new_state["schema_version"] = _SCHEMA_VERSION
    queries = new_state.setdefault("queries", {})
    items = new_state.setdefault("items", {})
    deals: dict[str, Deal] = {}

    for res in results:
        if res.status != STATUS_OK:
            continue
        qkey = res.query.text
        has_baseline = bool(queries.get(qkey, {}).get("baseline_at"))
        for lst in res.listings:
            rec = items.get(lst.item_id)
            real_price = None if is_placeholder(lst.price, cfg) else lst.price
            if rec is None:
                items[lst.item_id] = {
                    "item_id": lst.item_id,
                    "title": lst.title,
                    "price": str(real_price) if real_price is not None else None,
                    "currency": lst.currency,
                    "url": lst.url,
                    "location": lst.location,
                    "first_seen": now_iso,
                    "last_seen": now_iso,
                    "query": qkey,
                }
                if (cfg.report_new and has_baseline and real_price is not None
                        and _within_max(real_price, res.query) and lst.item_id not in deals):
                    deals[lst.item_id] = Deal("new", lst.item_id, lst.title, real_price,
                                              lst.url, lst.location, qkey)
                continue

            prev = Decimal(rec["price"]) if rec.get("price") not in (None, "") else None
            if (is_meaningful_drop(prev, real_price, cfg) and real_price is not None
                    and _within_max(real_price, res.query)):
                deals[lst.item_id] = Deal("drop", lst.item_id, lst.title, real_price,
                                          lst.url, lst.location, qkey, prev_price=prev)
            rec.update({
                "title": lst.title,
                "url": lst.url,
                "location": lst.location or rec.get("location"),
                "last_seen": now_iso,
            })
            if real_price is not None:  # placeholders never overwrite a real price
                rec["price"] = str(real_price)
        q = queries.setdefault(qkey, {})
        q.setdefault("baseline_at", now_iso)
        q["last_ok_at"] = now_iso

    # Forget listings not seen for a long time.
    cutoff = now - timedelta(days=cfg.forget_days)
    for item_id in list(items):
        try:
            last = datetime.fromisoformat(items[item_id].get("last_seen", ""))
        except (TypeError, ValueError):
            continue
        if last.tzinfo is None:
            last = last.replace(tzinfo=now.tzinfo)
        if last < cutoff:
            del items[item_id]

    ordered = sorted(
        deals.values(),
        key=lambda d: (0 if d.kind == "drop" else 1,
                       d.delta_pct if d.delta_pct is not None else Decimal(0),
                       d.price),
    )
    return ordered, new_state


# ---------------------------------------------------------------------------
# Public stage API (used by the daily report)
# ---------------------------------------------------------------------------

def scan_and_report(cfg: Optional[MarketplaceConfig] = None,
                    fetch: Optional[Fetcher] = None,
                    now: Optional[datetime] = None) -> Optional[MarketplaceReport]:
    """Run every configured query. None when the feature is not configured.
    Does not write state (call persist() after the email is sent)."""
    cfg = cfg or load_config()
    if cfg is None:
        return None
    fetch = fetch or fetch_query
    results: list[QueryResult] = []
    for q in cfg.queries:
        try:
            res = fetch(q, cfg)
        except Exception as exc:  # defensive: a fetcher must never break the email
            res = QueryResult(q, STATUS_ERROR, error=f"{type(exc).__name__}: {exc}"[:200])
        log.info("Marketplace %r: %s, %d listings", q.text, res.status, len(res.listings))
        results.append(res)
    deals, new_state = compute(results, load_state(), cfg, now)
    ok = sum(1 for r in results if r.status == STATUS_OK)
    status = "ok" if ok == len(results) else ("partial" if ok else "failed")
    shown = deals[: cfg.max_items]
    return MarketplaceReport(status=status, results=results, deals=shown,
                             hidden_deals=len(deals) - len(shown), new_state=new_state)


def persist(report: Optional[MarketplaceReport]) -> None:
    if report is None or report.new_state is None:
        return
    save_state(report.new_state)


# ---------------------------------------------------------------------------
# Rendering (text + HTML for the Hebrew daily email)
# ---------------------------------------------------------------------------

def _money(v: Optional[Decimal]) -> str:
    if v is None:
        return "—"
    v = v.quantize(Decimal("1")) if v == v.to_integral_value() else v.quantize(Decimal("0.01"))
    return f"₪{v:,}"


def _deal_price_text(d: Deal) -> str:
    if d.kind == "drop":
        return f"{_money(d.prev_price)} → {_money(d.price)} ({_money(-d.delta)} פחות, {d.delta_pct}%)"
    return f"{_money(d.price)} (מודעה חדשה)"


def _failure_lines(report: MarketplaceReport) -> list[str]:
    return [
        f"«{r.query.text}»: {_STATUS_HE.get(r.status, r.status)}" + (f" — {r.error}" if r.error else "")
        for r in report.failures
    ]


def section_text(report: Optional[MarketplaceReport]) -> list[str]:
    """Plain-text lines for the daily email. [] when the feature is off."""
    if report is None:
        return []
    lines = [SECTION_TITLE, "-" * 50, ""]
    for d in report.deals:
        lines.append(f"• {d.title}")
        lines.append(f"  {_deal_price_text(d)}")
        if d.location:
            lines.append(f"  מיקום: {d.location}")
        lines.append(f"  {d.url}")
    if report.hidden_deals:
        lines.append(f"  (+{report.hidden_deals} נוספות לא הוצגו)")
    if not report.deals:
        lines.append(EMPTY_STATE)
    for fl in _failure_lines(report):
        lines.append(f"  [!] {fl}")
    lines.append("")
    return lines


_F = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif")


def section_html(report: Optional[MarketplaceReport]) -> str:
    """HTML block (heading included) for the daily email. "" when off."""
    if report is None:
        return ""
    parts = [
        '<h3 style="margin:28px 0 12px;font-size:16px;color:#111827;text-align:right;'
        f'padding:6px 0 4px;border-bottom:2px solid #111827">{escape(SECTION_TITLE)}</h3>'
    ]
    if report.deals:
        rows = []
        for d in report.deals:
            if d.kind == "drop":
                price_html = (
                    f'<span dir="ltr" style="color:#6b7280;text-decoration:line-through">{escape(_money(d.prev_price))}</span>'
                    f' <b dir="ltr">{escape(_money(d.price))}</b>'
                    f' <span dir="ltr" style="color:#dc2626">({d.delta_pct}%)</span>'
                )
            else:
                price_html = (f'<b dir="ltr">{escape(_money(d.price))}</b> '
                              '<span style="color:#059669;font-size:12px">חדש</span>')
            rows.append(
                '<tr>'
                f'<td style="padding:6px 8px;border-bottom:1px solid #e5e7eb;text-align:right">'
                f'<a href="{escape(d.url)}" style="color:#2563eb;text-decoration:none">{escape(d.title)}</a></td>'
                f'<td style="padding:6px 8px;border-bottom:1px solid #e5e7eb;white-space:nowrap">{price_html}</td>'
                f'<td style="padding:6px 8px;border-bottom:1px solid #e5e7eb;color:#6b7280;font-size:13px">'
                f'{escape(d.location or "—")}</td>'
                '</tr>'
            )
        parts.append(
            f'<table dir="rtl" style="border-collapse:collapse;width:100%;font-family:{_F};font-size:14px">'
            '<tr style="background:#f9fafb">'
            '<th style="padding:6px 8px;text-align:right">מודעה</th>'
            '<th style="padding:6px 8px;text-align:right">מחיר</th>'
            '<th style="padding:6px 8px;text-align:right">מיקום</th></tr>'
            + "".join(rows) + '</table>'
        )
        if report.hidden_deals:
            parts.append(f'<div style="color:#6b7280;font-size:12px;margin-top:4px">'
                         f'+{report.hidden_deals} נוספות לא הוצגו</div>')
    else:
        parts.append(f'<div style="color:#888;font-style:italic">{escape(EMPTY_STATE)}</div>')
    for fl in _failure_lines(report):
        parts.append(f'<div style="margin-top:8px;color:#991b1b;font-size:13px">{escape(fl)}</div>')
    return "".join(parts)


def print_report(report: Optional[MarketplaceReport]) -> None:
    from rich.console import Console
    console = Console()
    if report is None:
        console.print("[dim]Facebook Marketplace: not configured (PW_FB_QUERIES)[/dim]")
        return
    for line in section_text(report):
        console.print(line, markup=False, highlight=False)
