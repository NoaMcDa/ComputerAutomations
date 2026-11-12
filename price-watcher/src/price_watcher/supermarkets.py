"""Israeli supermarket meat-discount scanner.

Uses the government-mandated price transparency XML feeds. Each chain
publishes PriceFull (every item + price) and PromoFull (active promotions)
files per store, refreshed daily.

Scope: only stores located in the regions listed in `_REGIONS` (the Sharon,
Tel Aviv, and the Rishon LeZion area). Each chain's Stores file maps store
ids to cities; every store of every chain inside those regions is scanned
(`PW_STORES_PER_REGION` can cap that per chain for a faster run). Chains
with no store there are skipped. The report keeps the best
`PW_TOP_PER_REGION` deals (default 30) for each region, best first.

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

_PP_BASE = "https://url.publishedprices.co.il"

# Chains hosted on the shared Cerberus host at url.publishedprices.co.il.
# Login credentials are publicly documented (law mandates free access).
# (cerberus_username, chain_display_name)
_PUBLISHEDPRICES_CHAINS: tuple[tuple[str, str], ...] = (
    ("RamiLevi", "רמי לוי"),
    ("yohananof", "יוחננוף"),
    ("HaziHinam", "חצי חינם"),
    ("osherad", "אושר עד"),
    ("TivTaam", "טיב טעם"),
    ("Keshet", "קשת טעמים"),
    ("Freshmarket", "פרש מרקט"),
    ("Doralon", "דור אלון"),
    ("SalachD", "סאלח דבאח"),
    ("Stop_Market", "סטופ מרקט"),
)
_SHUFERSAL_NAME = "שופרסל"


# ---- Regions ----
# Only stores in these regions are scanned. Each city is (CBS locality code,
# Hebrew spellings). The code is matched against the Stores file's <City>;
# the spellings are a fallback for chains that publish City=0 and only name
# the city inside the store name / address. A code of None = name match only.
_REGIONS: tuple[tuple[str, tuple[tuple[Optional[str], tuple[str, ...]], ...]], ...] = (
    ("השרון", (
        ("7400", ("נתניה",)),
        ("6400", ("הרצליה",)),
        ("8700", ("רעננה",)),
        ("6900", ("כפר סבא", 'כפ"ס')),
        ("9700", ("הוד השרון",)),
        ("2650", ("רמת השרון", 'רמה"ש')),
        ("168", ("כפר יונה",)),
        ("182", ("אבן יהודה",)),
        ("154", ("תל מונד",)),
        ("195", ("קדימה", "צורן")),
        ("171", ("פרדסיה",)),
        ("2720", ("טירה",)),
        ("2730", ("טייבה",)),
        ("346", ("גליל ים",)),
        (None, ("קלנסווה",)),
        (None, ("כוכב יאיר",)),
    )),
    ("תל אביב", (
        ("5000", ("תל אביב", "תל-אביב", 'ת"א')),
    )),
    ("ראשון לציון והסביבה", (
        ("8300", ("ראשון לציון", 'ראשל"צ', "ראשון")),
        ("7200", ("נס ציונה",)),
        ("2530", ("באר יעקב",)),
        ("466", ("בית דגן",)),
        ("6600", ("חולון",)),
        ("6200", ("בת ים",)),
    )),
)

_REGION_ORDER: tuple[str, ...] = tuple(name for name, _ in _REGIONS)

# code -> (region, city display name)
_CODE_TO_REGION: dict[str, tuple[str, str]] = {
    code: (region, names[0])
    for region, cities in _REGIONS
    for code, names in cities
    if code is not None
}

# (compiled pattern, region, city display name), longest spelling first.
_NAME_TO_REGION: list[tuple[re.Pattern, str, str]] = sorted(
    (
        (
            re.compile(r"(?:^|[^א-ת])" + re.escape(spelling) + r"(?:$|[^א-ת\"])"),
            region,
            names[0],
            len(spelling),
        )
        for region, cities in _REGIONS
        for _code, names in cities
        for spelling in names
    ),
    key=lambda t: -t[3],
)  # type: ignore[assignment]


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _stores_per_region() -> int:
    """Max stores per chain per region. 0 (default) = every store."""
    return _env_int("PW_STORES_PER_REGION", 0)


def _top_per_region() -> int:
    """How many deals to keep per region in the report."""
    return _env_int("PW_TOP_PER_REGION", 30) or 30


# Parallel store downloads per chain (11 chains run at once, so keep it modest).
_STORE_WORKERS = 3

# A "discount" at or above this is a data artefact (e.g. class-action credit
# rows with no price), not a real deal.
_MAX_SANE_DISCOUNT_PCT = 90


@dataclass
class StoreInfo:
    store_id: int
    name: str
    city: str
    region: str

    @property
    def label(self) -> str:
        return self.name or self.city


def _region_for_store(store: dict[str, str]) -> Optional[tuple[str, str]]:
    """Return (region, city) if the store is inside a target region."""
    city = (store.get("city") or "").strip()
    if city.isdigit() and int(city) != 0:
        return _CODE_TO_REGION.get(str(int(city)))
    if city and not city.isdigit():
        text = city
    else:
        text = f"{store.get('storename', '')} {store.get('address', '')}"
    for pat, region, city_name, _len in _NAME_TO_REGION:
        if pat.search(text):
            return region, city_name
    return None


def _maybe_gunzip(raw: bytes) -> bytes:
    try:
        return gzip.decompress(raw)
    except (OSError, EOFError):
        return raw


def _parse_stores(raw: bytes) -> list[StoreInfo]:
    """Parse a Stores XML file and keep only stores inside a target region."""
    try:
        root = ET.fromstring(_maybe_gunzip(raw))
    except ET.ParseError as exc:
        log.warning("Stores XML parse failed: %s", exc)
        return []
    out: list[StoreInfo] = []
    for el in root.iter():
        if el.tag.lower() != "store":
            continue
        fields = {c.tag.lower(): (c.text or "").strip() for c in el}
        sid = fields.get("storeid", "")
        if not sid.isdigit():
            continue
        hit = _region_for_store(fields)
        if hit is None:
            continue
        region, city = hit
        out.append(StoreInfo(int(sid), fields.get("storename", ""), city, region))
    return out


def _candidates_by_region(stores: list[StoreInfo]) -> dict[str, list[StoreInfo]]:
    """Group stores by region, ordered so different cities come first."""
    out: dict[str, list[StoreInfo]] = {}
    for region in _REGION_ORDER:
        by_city: dict[str, list[StoreInfo]] = {}
        for s in sorted(stores, key=lambda s: s.store_id):
            if s.region == region:
                by_city.setdefault(s.city, []).append(s)
        ordered: list[StoreInfo] = []
        while any(by_city.values()):
            for city in list(by_city):
                if by_city[city]:
                    ordered.append(by_city[city].pop(0))
        if ordered:
            out[region] = ordered
    return out


_FEED_FILE_RE = re.compile(r"-(\d{3})-(\d{3,4})-(\d{8})-?(\d{4,6})")


def _latest_by_store(names: list[str]) -> dict[int, str]:
    """Map store id -> newest feed filename/URL for that store."""
    best: dict[int, tuple[str, str]] = {}
    for name in names:
        m = _FEED_FILE_RE.search(name.split("?")[0])
        if not m:
            continue
        sid, stamp = int(m.group(2)), m.group(3) + m.group(4)
        if sid not in best or stamp > best[sid][0]:
            best[sid] = (stamp, name)
    return {sid: name for sid, (_stamp, name) in best.items()}


# Categories for grouping meat promos side-by-side across chains.
# Order matters: more specific categories must come before generic ones.
_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # --- Specific premium cuts (match before generic cut names) ---
    ("פילה מיניון",            ("פילה מיניון", "פילה-מיניון")),
    ("פיקניה",                 ("פיקניה",)),
    ("אנטריקוט",               ("אנטריקוט",)),
    ("סינטה",                  ("סינטה",)),
    ("טי-בון",                 ("ט-בון", "טיבון")),
    ("צ'אק",                   ("צ'אק", "צ'ק")),
    ("פלאנק / ראמפ",           ("פלאנק", "ראמפ")),
    ("אסאדו",                  ("אסאדו",)),
    ("בריסקט",                 ("בריסקט", "בריסקיט")),
    ("אוסובוקו",               ("אוסובוקו", "אוסו בוקו")),
    ("גולש",                   ("גולש", "גולאש")),
    ("כתף",                    ("כתף",)),
    ("שריר / חגורה",           ("שריר", "חגורה")),
    ("צלעות",                  ("צלעות",)),
    ("סטייק",                  ("סטייק",)),
    ("פילה בקר",               ("פילה",)),
    # --- Organ cuts ---
    ("לשון בקר",               ("לשון",)),
    ("מוח",                    ("מוח",)),
    ("גרון",                   ("גרון",)),
    ("קרביים, קורקבן וכרעיים", ("קרביים", "קורקבן", "כרעיים")),
    # --- Veal / lamb / baby ---
    ("בייבי בקר",              ("בבי בקר", "בייבי בקר", "בייבי-בקר")),
    ("עגל",                    ("עגל",)),
    ("כבש / טלה",              ("כבש", "טלה")),
    # --- Liver / ground ---
    ("כבד בקר",                ("כבד בקר",)),
    ("כבד עוף",                ("כבד עוף", "כבד")),
    ("צלי בקר",                ("צלי",)),
    ("בשר טחון",               ("בשר טחון",)),
    ("בקר – כללי",             ("בקר",)),
    # --- Chicken parts (specific first) ---
    ("חזה עוף",                ("חזה עוף",)),
    ("כנפיים",                 ("כנפיים",)),
    ("ירכיים",                 ("ירכיים", "ירך עוף")),
    ("שוקיים",                 ("שוקיים", "שוק עוף")),
    ("פולקע",                  ("פולקע", "פולקות")),
    ("פרגיות",                 ("פרגית", "פרגיות")),
    ("הודו",                   ("חזה הודו", "הודו")),
    ("עוף – אחר",              ("עוף",)),
    ("בשר – אחר",              ("בשר",)),
)
_CATEGORY_OTHER = "אחר"


def _categorize(promo: "MeatPromo") -> str:
    """Return the category key for a promo (first matching category wins)."""
    text = f"{promo.item_name} {promo.promotion_description}"
    for cat_name, keywords in _CATEGORIES:
        for kw in keywords:
            if kw in text:
                return cat_name
    return _CATEGORY_OTHER


# ---- Hebrew keyword classifier ----

_MEAT_KEYWORDS: tuple[str, ...] = (
    # Fresh beef / cow parts
    "בשר", "בקר", "עגל", "כבש", "טלה", "כבד",
    "אנטריקוט", "פילה", "סטייק", "צלעות", "צלי",
    # Specific cuts (Hebrew / Argentinian / international)
    "אסאדו", "בריסקט", "בריסקיט", "פיקניה", "סינטה",
    "אוסובוקו", "אוסו בוקו", "פלאנק", "ראמפ",
    "ט-בון", "טיבון", "צ'אק", "צ'ק",
    "כתף", "שריר", "חגורה", "גולש", "גולאש",
    "פילה מיניון", "פילה-מיניון",
    "בבי בקר", "בייבי בקר", "בייבי-בקר",
    # Organ / utility cuts
    "מוח", "גרון", "קורקבן", "קרביים", "כרעיים",
    "פולקע", "פולקות", "לשון",
    # Fresh poultry parts
    "עוף", "הודו", "פרגית", "פרגיות",
    "שוקיים", "כנפיים", "חזה עוף", "חזה הודו", "ירך עוף", "שוק עוף",
    "אטליז",
)

_EXCLUDE_KEYWORDS: tuple[str, ...] = (
    # Fish / seafood
    "דג", "סלמון", "טונה", "סרדין", "הרינג", "שרימפ", "קרפיון",
    "אמנון", "דניס", "לברק", "פורל", "בורי", "מושט", "נילוס",
    "סיבס", "מוסר", "פילה דג", "דג סלמון",
    "גפילטע", "פיש", "מולים",
    # Plant-based
    "טבעוני", "צמחוני", "צמחי", "VEGAN", "ווגן",
    "סויה", "טופו", "סייטן",
    "טבעול", "ביונד", "beyond",
    "ללא בשר", "ללא עוף",
    # Dairy / cheese
    "חלבי", "גבינה", "גאודה", "גאודת", "אמנטל", "מוצרלה", "צ'דר",
    "קוטג", "לבנה",
    # Processed / deli (user wants fresh cuts only, not cured / smoked / cooked)
    "נקניק", "נקניקיה", "פסטרמה", "סלמי", "קבנוס",
    "קבב", "המבורגר", "קציצה", "קציצות",
    "שווארמה", "צ'וריסו", "מרגז", "מרגז'",
    "שניצל", "שיפוד",
    "מעושן", "מעושנ", "מתובל", "בייקון", "פסטרמ",
    "סלסול", "סלסולי",  # deli slices
    "דק דק",
    # Packaged soups / broths / spices / bouillon
    "מרק", "אבקת", "בטעם", "קוביות", "בולון",
    # Not food (sanitary / household that mentions meat keywords)
    "תחבושות", "קוטקס", "אולווייז", "טמפון", "מגבונים", "חיתולים",
    # Pet food
    "חתולים", "כלבים", "חיות", "חיות מחמד",
    "פריסקיז", "פרוסקיז", "רויאל", "פרוקל", "פרופלן", "ווסקוויק",
    "ווסקס", "סמלט", "פדיגרי", "פרינה", "פטה",
    # Breaded / coated / nuggets (processed kids products)
    "בציפוי", "ציפוי", "נאגטס", "נתחונים",
    # Instant / soluble soup
    "נמס", "נמס בכוס", "בכוס",
    # More cheeses
    "בולגרית", "בולגרי", "פטה גבינה",
    # Confusing
    "מדומה",  # mock steak (often fish/soy)
    # Noodles / snacks / frozen processed shapes / kitchenware / cat food brands
    "נודלס", "צ'יפס", "ציפס", "נגיסי", "נגיסים", "כוכבי",
    "סכין", "קרש", "פנסי", "פיסט",
    "תיבול", "תבלין", "תבלינים",
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


def _is_meat_product(name: str, context: str = "") -> bool:
    """Decide if a promo item is a fresh meat/poultry cut.

    `name` is the product name from PriceFull; `context` is the promotion
    description — used as a secondary exclusion source because PromoFull
    descriptions often reveal that a truncated product name is actually
    something else (e.g. "פילה סל" → desc "מנות סלמון").
    """
    if not name:
        return False
    combined = f"{name} {context}"
    for kw in _EXCLUDE_KEYWORDS:
        if kw in combined:
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
    store_name: str = ""
    city: str = ""
    regions: list[str] = field(default_factory=list)  # one region per promo row
    cities: list[str] = field(default_factory=list)   # cities it was seen in
    store_count: int = 1                              # stores it was seen in

    @property
    def region(self) -> str:
        return self.regions[0] if self.regions else ""

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
    stores_scanned: list[str] = field(default_factory=list)  # "chain – store (city)"
    stores_by_region: dict[str, int] = field(default_factory=dict)
    promos_by_region: dict[str, int] = field(default_factory=dict)  # before the top-N cut
    promos: list[MeatPromo] = field(default_factory=list)
    error: Optional[str] = None


# ---- Shared per-store scan ----

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


def _scan_store(
    chain_name: str,
    store: StoreInfo,
    price_raw: Optional[bytes],
    promo_raw: Optional[bytes],
) -> list[MeatPromo]:
    """Parse one store's PriceFull + PromoFull and return its meat promos."""
    if not price_raw or not promo_raw:
        log.warning("%s / %s: download failed", chain_name, store.label)
        return []
    try:
        price_root = ET.fromstring(_maybe_gunzip(price_raw))
        promo_root = ET.fromstring(_maybe_gunzip(promo_raw))
    except ET.ParseError as exc:
        log.warning("%s / %s: parse failed: %s", chain_name, store.label, exc)
        return []
    item_index = _build_item_index(price_root)
    promos = _extract_meat_promos(chain_name, f"{store.store_id:03d}", promo_root, item_index)
    for p in promos:
        p.store_name = store.name
        p.city = store.city
        p.regions = [store.region]
        p.cities = [store.city]
    log.info(
        "%s / %s (%s, %s): %d items, %d meat promos",
        chain_name, store.label, store.city, store.region, len(item_index), len(promos),
    )
    return promos


def _scan_chain(
    chain_name: str,
    stores: list[StoreInfo],
    fetch,  # (StoreInfo) -> (price_bytes, promo_bytes) | None when the store has no files
) -> tuple[list[MeatPromo], list[StoreInfo]]:
    """Scan every store of the chain inside the target regions.

    Returns (promos, stores that yielded at least one meat promo).
    """
    from concurrent.futures import ThreadPoolExecutor

    limit = _stores_per_region()
    candidates = _candidates_by_region(stores)
    if not candidates:
        log.info("%s: no stores in the target regions — skipped", chain_name)
        return [], []
    todo = [
        store
        for region_stores in candidates.values()
        for store in (region_stores[:limit] if limit else region_stores)
    ]
    log.info("%s: scanning %d stores in the target regions", chain_name, len(todo))

    def _one(store: StoreInfo) -> tuple[StoreInfo, list[MeatPromo]]:
        try:
            files = fetch(store)
            if files is None:
                return store, []
            return store, _scan_store(chain_name, store, *files)
        except Exception as exc:  # one bad store must not sink the chain
            log.warning("%s / %s: %s: %s", chain_name, store.label, type(exc).__name__, exc)
            return store, []

    promos: list[MeatPromo] = []
    used: list[StoreInfo] = []
    with ThreadPoolExecutor(max_workers=_STORE_WORKERS) as pool:
        for store, found in pool.map(_one, todo):
            if found:
                promos.extend(found)
                used.append(store)
    return promos, used


# ---- Shufersal ----

def _shufersal_links(cat: int, store_id: Optional[int] = None) -> list[str]:
    url = _SHUFERSAL_CATEGORY_URL.format(cat=cat)
    if store_id is not None:
        url += f"&storeId={store_id}"
    r = httpx.get(url, headers=_HEADERS, timeout=30)
    if r.status_code != 200:
        return []
    links = re.findall(r'href="([^"]+)"', r.text)
    return [l.replace("&amp;", "&") for l in links if ".gz" in l or ".xml" in l]


def _download(url: str) -> Optional[bytes]:
    r = httpx.get(url, timeout=120, follow_redirects=True)
    if r.status_code != 200:
        log.warning("download failed: HTTP %s", r.status_code)
        return None
    return r.content


def scan_shufersal() -> tuple[list[MeatPromo], list[StoreInfo]]:
    """Meat promotions from Shufersal stores inside the target regions."""
    log.info("%s: fetching store list", _SHUFERSAL_NAME)
    store_links = _shufersal_links(cat=5)
    raw = _download(store_links[0]) if store_links else None
    if not raw:
        log.warning("%s: could not fetch Stores file", _SHUFERSAL_NAME)
        return [], []
    stores = _parse_stores(raw)

    def _fetch(store: StoreInfo):
        promo_url = _latest_by_store(_shufersal_links(4, store.store_id)).get(store.store_id)
        price_url = _latest_by_store(_shufersal_links(2, store.store_id)).get(store.store_id)
        if not promo_url or not price_url:
            return None
        return _download(price_url), _download(promo_url)

    return _scan_chain(_SHUFERSAL_NAME, stores, _fetch)


# ---- Published prices (Cerberus host) ----

def _pp_csrf(client: httpx.Client, path: str = "/login") -> Optional[str]:
    r = client.get(f"{_PP_BASE}{path}")
    m = re.search(r'name="csrftoken"\s+content="([^"]+)"', r.text)
    return m.group(1) if m else None


def _pp_login(client: httpx.Client, username: str) -> bool:
    token = _pp_csrf(client, "/login")
    if not token:
        return False
    r = client.post(
        f"{_PP_BASE}/login/user",
        data={"username": username, "password": "", "r": "", "csrftoken": token},
    )
    if r.status_code != 200:
        return False
    # Confirm login: /file page should contain "logout"
    check = client.get(f"{_PP_BASE}/file")
    return "logout" in check.text.lower()


def _pp_list_files(client: httpx.Client, prefix: str, limit: int = 50) -> list[dict]:
    """List files whose name starts with `prefix` (e.g. 'PromoFull')."""
    token = _pp_csrf(client, "/file")
    if not token:
        return []
    r = client.post(
        f"{_PP_BASE}/file/json/dir",
        data={"csrftoken": token, "iDisplayLength": limit, "iDisplayStart": 0, "sSearch": prefix},
    )
    try:
        return r.json().get("aaData", [])
    except Exception:
        return []


def _pp_download(client: httpx.Client, filename: str) -> Optional[bytes]:
    r = client.get(f"{_PP_BASE}/file/d/{filename}", timeout=120)
    if r.status_code != 200:
        return None
    return r.content


def scan_publishedprices_chain(
    username: str,
    chain_name: str,
) -> tuple[list[MeatPromo], list[StoreInfo]]:
    """Scan one chain hosted on url.publishedprices.co.il (target regions only)."""
    log.info("%s: logging in to publishedprices", chain_name)
    with httpx.Client(follow_redirects=True, timeout=60, headers=_HEADERS) as client:
        if not _pp_login(client, username):
            log.warning("%s: login failed", chain_name)
            return [], []

        def _names(prefix: str, limit: int) -> list[str]:
            return [
                f.get("fname", "")
                for f in _pp_list_files(client, prefix, limit=limit)
                if f.get("fname", "").startswith(prefix)
            ]

        stores_files = sorted(_names("Stores", 20))
        if not stores_files:
            log.warning("%s: no Stores file listed", chain_name)
            return [], []
        stores_raw = _pp_download(client, stores_files[-1])
        if not stores_raw:
            log.warning("%s: Stores file download failed", chain_name)
            return [], []
        stores = _parse_stores(stores_raw)

        # The host's search box only matches short prefixes, so list everything
        # and pick each store's newest file locally.
        promo_by_store = _latest_by_store(_names("PromoFull", 1000))
        price_by_store = _latest_by_store(_names("PriceFull", 1000))

        def _fetch(store: StoreInfo):
            promo_fname = promo_by_store.get(store.store_id)
            price_fname = price_by_store.get(store.store_id)
            if not promo_fname or not price_fname:
                return None
            return _pp_download(client, price_fname), _pp_download(client, promo_fname)

        return _scan_chain(chain_name, stores, _fetch)


def _extract_meat_promos(
    chain_name: str,
    store_id: str,
    promo_root: ET.Element,
    item_index: dict[str, tuple[str, Optional[Decimal]]],
) -> list[MeatPromo]:
    """Shared promo-filtering logic used by every chain."""
    results: list[MeatPromo] = []
    promos_container = promo_root.find("Promotions") or promo_root.find(".//Promotions")
    if promos_container is None:
        return []
    seen: set[tuple[str, str]] = set()
    for promo in promos_container.findall("Promotion"):
        desc_el = promo.find("PromotionDescription")
        desc = (desc_el.text or "").strip() if desc_el is not None else ""
        end_el = promo.find("PromotionEndDateTime")
        end = (end_el.text or "").strip() if end_el is not None else ""

        is_coupon_el = promo.find("AdditionalIsCoupon")
        if is_coupon_el is not None and (is_coupon_el.text or "").strip() == "1":
            continue
        if "פיצוי" in desc:
            continue
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
            if not _is_meat_product(name, context=desc):
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
            if discount_rate is None and discounted_price is None:
                continue

            results.append(MeatPromo(
                chain=chain_name,
                store_id=store_id,
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
    """Scan every configured chain in parallel and return a combined report."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    log.info(
        "Running supermarket meat scan across %d chains (regions: %s)",
        1 + len(_PUBLISHEDPRICES_CHAINS), ", ".join(_REGION_ORDER),
    )
    report = SupermarketReport(status="ok")

    def _shufersal_task() -> tuple[str, list[MeatPromo], list[StoreInfo]]:
        promos, used = scan_shufersal()
        return (_SHUFERSAL_NAME, promos, used)

    def _pp_task(user: str, name: str) -> tuple[str, list[MeatPromo], list[StoreInfo]]:
        promos, used = scan_publishedprices_chain(user, name)
        return (name, promos, used)

    tasks = [_shufersal_task] + [
        (lambda u=u, n=n: _pp_task(u, n)) for u, n in _PUBLISHEDPRICES_CHAINS
    ]

    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        futures = [pool.submit(task) for task in tasks]
        for fut in as_completed(futures):
            try:
                name, promos, used = fut.result()
                if promos:
                    report.promos.extend(promos)
                    report.chains_scanned.append(name)
                    for store in used:
                        report.stores_scanned.append(f"{name} – {store.label} ({store.city})")
                        report.stores_by_region[store.region] = (
                            report.stores_by_region.get(store.region, 0) + 1
                        )
                else:
                    log.warning("%s: no promos returned", name)
            except Exception as exc:
                log.error("chain scan failed: %s: %s", type(exc).__name__, exc)
    report.chains_scanned.sort()
    report.stores_scanned.sort()

    # Dedupe inside each region: the same promotion runs in many stores of a
    # chain and often covers several item variants (different brands of the
    # same cut). Keep one row per (region, chain, promotion_description,
    # rounded savings_pct) and remember the cities / store count behind it.
    seen: dict[tuple[str, str, str, int], MeatPromo] = {}
    seen_stores: dict[tuple[str, str, str, int], set[str]] = {}
    for p in report.promos:
        pct = p.savings_pct
        if pct is None or pct <= 0 or pct >= _MAX_SANE_DISCOUNT_PCT:
            continue
        key = (p.region, p.chain, p.promotion_description, int(pct))
        kept = seen.get(key)
        if kept is None:
            seen[key] = kept = p
            seen_stores[key] = set()
        seen_stores[key].add(p.store_id)
        if p.city not in kept.cities:
            kept.cities.append(p.city)
    for key, kept in seen.items():
        kept.store_count = len(seen_stores[key])

    def _rank_key(p: MeatPromo) -> tuple:
        pct = p.savings_pct or Decimal("0")
        amt = p.savings_amount or Decimal("0")
        return (-float(pct), -float(amt))

    # Best deals first, region by region; keep the top N of each region.
    top_n = _top_per_region()
    ranked: list[MeatPromo] = []
    for region in _REGION_ORDER:
        in_region = sorted((p for p in seen.values() if p.region == region), key=_rank_key)
        report.promos_by_region[region] = len(in_region)
        ranked.extend(in_region[:top_n])
    report.promos = ranked
    if not report.chains_scanned:
        report.status = "fetch_failed"
        report.error = "לא נמצאו מבצעים באף רשת באזורים שנבחרו"
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
    table.add_column("Region", width=10)
    table.add_column("Chain", width=10)
    table.add_column("Item")
    table.add_column("Was")
    table.add_column("Now")
    table.add_column("Off")
    table.add_column("Promo")
    for p in report.promos:
        was = f"₪{p.regular_price}" if p.regular_price else "—"
        now = f"₪{p.discounted_price}" if p.discounted_price else "—"
        off = f"{p.savings_pct}%" if p.savings_pct is not None else "—"
        table.add_row(p.region, p.chain, p.item_name[:50], was, now, off, p.promotion_description[:40])
    console.print(table)
