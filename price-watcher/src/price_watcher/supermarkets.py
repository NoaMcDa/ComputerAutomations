"""Israeli supermarket meat-discount scanner.

Uses the government-mandated price transparency XML feeds. Each chain
publishes PriceFull (every item + price) and PromoFull (active promotions)
files per store, refreshed daily.

Chains: Shufersal, the chains on url.publishedprices.co.il
(`_PUBLISHEDPRICES_CHAINS`) and the Bina Projects portals (`_BINA_CHAINS`,
Super Bareket).

Scope: only stores in the cities switched ON in `_REGIONS` (now Herzliya and
Ramat HaSharon; the other Sharon cities, Tel Aviv and the Rishon LeZion area
are listed but OFF), or the cities named in PW_MEAT_CITIES. Each chain's
Stores file maps store ids to cities; every store of every chain in those
cities is scanned (`PW_STORES_PER_REGION` can cap that per chain and region).
A chain with no store there is reported as empty ("אין סניפים בערים
שנבחרו"). The report is organised by meat category: each category keeps its
`PW_TOP_PER_CATEGORY` cheapest deals (default 5) by promo price PER KG; a
deal whose price per kg cannot be derived reliably is left out of the ranking
and counted (`unpriced_by_category`, see `_pack_grams`). A category is one
cut of one animal, fresh or frozen ("כנפיים עוף", "לשון בקר – קפוא"); the
catch-all groups ("בשר – אחר", ...) are counted but not ranked (`_CATCH_ALL`).

For every store the newest PromoFull and PriceFull of the chain's own chain
id are paired; files older than `_MAX_FEED_AGE_DAYS` are ignored. Chains that
fail (login, no files, downloads) or return no meat deals are listed in the
report (`chains_failed` / `chains_empty`) instead of silently disappearing.

Only fresh / frozen raw meat and poultry cuts are kept (`_is_meat_product`):
a Hebrew meat word in the product name (an unambiguous cut, two meat words,
or one meat word plus a freshness / weighed-item signal from the feed), and
no exclusion word (fish, pet food, dairy, plant-based, processed / deli,
ready meals, snacks, non-food) in the name, the manufacturer or the promotion
description. Multi-buy promotions ("2 ב-90") are converted to a per-unit
price; compensation credits and payment-method discounts are skipped.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
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
# Hazi Hinam is not listed: its account on this host lists no files any more
# (it publishes on its own site now), so it could only ever come back empty.
_PUBLISHEDPRICES_CHAINS: tuple[tuple[str, str], ...] = (
    ("RamiLevi", "רמי לוי"),
    ("yohananof", "יוחננוף"),
    ("osherad", "אושר עד"),
    ("TivTaam", "טיב טעם"),
    ("Keshet", "קשת טעמים"),
    ("Freshmarket", "פרש מרקט"),
    ("Doralon", "דור אלון"),
    ("SalachD", "סאלח דבאח"),
    ("Stop_Market", "סטופ מרקט"),
)
_SHUFERSAL_NAME = "שופרסל"

# Chains hosted by Bina Projects, each on its own <subdomain>.binaprojects.com
# portal (an ASP.NET page whose JSON endpoints list and hand out the files).
# (subdomain, chain_display_name)
_BINA_URL = "https://{sub}.binaprojects.com/"
_BINA_CHAINS: tuple[tuple[str, str], ...] = (
    ("superbareket", "סופר ברקת"),
)


# ---- Scan area: regions and cities ----
#
# Which cities are scanned. Every city is written as
#     (CBS locality code, (Hebrew spellings...), ON or OFF)
# ON = stores in that city are scanned, OFF = known but skipped. To switch a
# city on or off, change ON/OFF here — or, without editing code, set
# PW_MEAT_CITIES in .env to a comma-separated list of city (or region) names,
# e.g. "PW_MEAT_CITIES=הרצליה,רמת השרון,תל אביב", or "הכל" for every city
# below. PW_MEAT_CITIES, when set, replaces the ON/OFF column.
#
# The code is matched against the Stores file's <City>; the spellings are used
# for chains that publish City=0 and only name the city inside the store name
# or address (see `_region_for_store`). A code of None = name match only. OFF
# cities still matter: a store recognised as being in an OFF city is left out
# even when its address mentions an ON city ("רחוב הרצליה" in Netanya).
ON, OFF = True, False

_REGIONS: tuple[tuple[str, tuple[tuple[Optional[str], tuple[str, ...], bool], ...]], ...] = (
    ("השרון", (
        ("6400", ("הרצליה", "הרצלייה"), ON),
        ("2650", ("רמת השרון", "רמת-השרון", 'רמה"ש'), ON),
        ("7400", ("נתניה",), OFF),
        ("8700", ("רעננה",), OFF),
        ("6900", ("כפר סבא", "כפר-סבא", 'כפ"ס'), OFF),
        ("9700", ("הוד השרון", "הוד-השרון"), OFF),
        ("168", ("כפר יונה",), OFF),
        ("182", ("אבן יהודה",), OFF),
        ("154", ("תל מונד",), OFF),
        ("195", ("קדימה", "צורן"), OFF),
        ("171", ("פרדסיה",), OFF),
        ("2720", ("טירה",), OFF),
        ("2730", ("טייבה",), OFF),
        ("346", ("גליל ים",), OFF),
        (None, ("קלנסווה",), OFF),
        (None, ("כוכב יאיר",), OFF),
    )),
    ("תל אביב", (
        ("5000", ("תל אביב", "תל-אביב", 'ת"א', "יד אליהו", "רמת אביב", "נווה צדק", "פלורנטין"), OFF),
    )),
    ("ראשון לציון והסביבה", (
        ("8300", ("ראשון לציון", "ראשון-לציון", 'ראשל"צ', "ראשלצ"), OFF),
        ("7200", ("נס ציונה", "נס-ציונה"), OFF),
        ("2530", ("באר יעקב",), OFF),
        ("466", ("בית דגן",), OFF),
        ("6600", ("חולון",), OFF),
        ("6200", ("בת ים", "בת-ים"), OFF),
    )),
)

_REGION_ORDER: tuple[str, ...] = tuple(name for name, _ in _REGIONS)

# Every known city in table order: (region, city display name, enabled flag).
_ALL_CITIES: tuple[tuple[str, str, bool], ...] = tuple(
    (region, names[0], on) for region, cities in _REGIONS for _code, names, on in cities
)
_CITY_ORDER: tuple[str, ...] = tuple(city for _r, city, _on in _ALL_CITIES)

# code -> (region, city display name)
_CODE_TO_REGION: dict[str, tuple[str, str]] = {
    code: (region, names[0])
    for region, cities in _REGIONS
    for code, names, _on in cities
    if code is not None
}


def _norm(text: str) -> str:
    """Unify Hebrew geresh / gershayim so 'ק״ג' and 'ק"ג' compare equal."""
    return (text or "").replace("״", '"').replace("׳", "'").replace("`", "'")


# (compiled pattern, region, city display name, spelling length), longest
# spelling first. A spelling must stand as whole words ("הרצל" street never
# matches "הרצליה", "רמת השרוני" is not "רמת השרון").
_NAME_TO_REGION: list[tuple[re.Pattern, str, str, int]] = sorted(
    (
        (
            re.compile(r"(?<![א-ת])" + re.escape(_norm(spelling)) + r"(?![א-ת\"])"),
            region,
            names[0],
            len(spelling),
        )
        for region, cities in _REGIONS
        for _code, names, _on in cities
        for spelling in names
    ),
    key=lambda t: -t[3],
)

# A city spelling right after one of these words names a street, not the city
# ("רחוב נתניה", "דרך רעננה", "שד' ראשון לציון").
_STREET_WORD_RE = re.compile(
    r"(?<![א-ת])(?:רחוב|רח'|רח\.|רח|דרך|שדרות|שדרת|שד'|שד\.|שד|כביש|סמטת|סמ'|כיכר|ככר|מבוא)"
    r"\s*$"
)
# In an address, a spelling followed by a house number is a street ("תל אביב 11").
_HOUSE_NUMBER_RE = re.compile(r"^\s*-?\s*\d")


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _stores_per_region() -> int:
    """Max stores per chain per region. 0 (default) = every store."""
    return _env_int("PW_STORES_PER_REGION", 0)


def _top_per_category() -> int:
    """How many deals to keep per meat category in the report."""
    return _env_int("PW_TOP_PER_CATEGORY", 5) or 5


_ALL_CITIES_WORDS = {"הכל", "הכול", "all", "*"}


def _enabled_cities() -> tuple[str, ...]:
    """City display names to scan, in table order.

    PW_MEAT_CITIES (comma-separated city or region names, any spelling from
    `_REGIONS`, or "הכל") overrides the ON/OFF column. Unknown names are
    logged and ignored; if nothing valid is left the ON/OFF column is used.
    """
    raw = (os.environ.get("PW_MEAT_CITIES") or "").strip()
    default = tuple(city for _r, city, on in _ALL_CITIES if on)
    if not raw:
        return default
    wanted: set[str] = set()
    for part in re.split(r"[,;\n]+", raw):
        part = _norm(part).strip()
        if not part:
            continue
        if part.lower() in _ALL_CITIES_WORDS:
            return _CITY_ORDER
        hits = {city for region, city, _on in _ALL_CITIES if _norm(region) == part}
        for region, cities in _REGIONS:
            for _code, names, _on in cities:
                if part in {_norm(n) for n in names}:
                    hits.add(names[0])
        if not hits:
            log.warning("PW_MEAT_CITIES: unknown city or region %r ignored", part)
        wanted |= hits
    if not wanted:
        log.warning("PW_MEAT_CITIES names no known city; using the ON/OFF list in supermarkets.py")
        return default
    return tuple(city for city in _CITY_ORDER if city in wanted)


def _active_regions(cities: Optional[tuple[str, ...]] = None) -> tuple[str, ...]:
    """Regions with at least one enabled city, in `_REGION_ORDER`."""
    cities = _enabled_cities() if cities is None else cities
    return tuple(
        region for region in _REGION_ORDER
        if any(r == region and c in cities for r, c, _on in _ALL_CITIES)
    )


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

    @property
    def branch(self) -> str:
        """Branch name with its city, e.g. 'סוקולוב הרצליה' or 'קרית אליעזר (חיפה)'."""
        name = (self.name or "").strip()
        if not name or name == self.city:
            return self.city
        if self.city and self.city not in _norm(name) and self.city not in name:
            return f"{name} ({self.city})"
        return name


def _match_city(text: str, address: bool = False) -> Optional[tuple[str, str]]:
    """(region, city) of the longest city spelling in `text` that names the city.

    A spelling right after a street word ("רחוב", "דרך", "שד'") is a street
    name; in an address, so is a spelling followed by a house number.
    """
    text = _norm(text)
    for pat, region, city_name, _len in _NAME_TO_REGION:
        for m in pat.finditer(text):
            if _STREET_WORD_RE.search(text[: m.start()]):
                continue
            if address and _HOUSE_NUMBER_RE.match(text[m.end():]):
                continue
            return region, city_name
    return None


def _region_for_store(store: dict[str, str]) -> Optional[tuple[str, str]]:
    """Return (region, city) when the store is in a city listed in `_REGIONS`.

    Any listed city counts, ON or OFF (the caller filters by `_enabled_cities`).
    1. A real CBS code in <City> decides — unless the store NAME names another
       listed city: chains do mistype codes (Fresh Market's "סופר יודה רמת
       השרון" carries Holon's code), and a name is chosen by the chain itself.
    2. A city written out in <City> is matched as text.
    3. City=0: the store name first, the address only when the name gives
       nothing — never a street named after a city ("תל אביב 11", "רח' הרצליה").
    """
    city = (store.get("city") or "").strip()
    name_hit = _match_city(store.get("storename", ""))
    if city.isdigit() and int(city) != 0:
        code_hit = _CODE_TO_REGION.get(str(int(city)))
        if code_hit is not None and name_hit is not None and name_hit != code_hit:
            return name_hit
        return code_hit
    if city and not city.isdigit() and city.lower() != "unknown":
        return _match_city(city)
    return name_hit or _match_city(store.get("address", ""), address=True)


def _maybe_gunzip(raw: bytes) -> bytes:
    try:
        return gzip.decompress(raw)
    except (OSError, EOFError):
        return raw


def _parse_stores(raw: bytes, cities: Optional[tuple[str, ...]] = None) -> list[StoreInfo]:
    """Parse a Stores XML file and keep only stores in the enabled cities."""
    try:
        root = ET.fromstring(_maybe_gunzip(raw))
    except ET.ParseError as exc:
        log.warning("Stores XML parse failed: %s", exc)
        return []
    enabled = set(_enabled_cities() if cities is None else cities)
    out: list[StoreInfo] = []
    for el in root.iter():
        if el.tag.lower() != "store":
            continue
        fields = {c.tag.lower(): (c.text or "").strip() for c in el}
        sid = fields.get("storeid", "")
        if not sid.isdigit():
            continue
        hit = _region_for_store(fields)
        if hit is None or hit[1] not in enabled:
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


# <Kind><13-digit chain id>-[<subchain>-]<store>-<YYYYMMDD>[-]<HHMM[SS]>, e.g.
# PromoFull7290027600007-001-001-20261111-032159.gz or (Keshet)
# PromoFull7290785400000-105-202611110010.gz. Placeholders such as
# PromoFull0000000000000-00-1-202411010501.gz do not match; files of another
# chain id or from an old year (PriceFull7290055700007-0820-202403281445.gz in
# Keshet's listing) are filtered out by `_latest_by_store`.
_FEED_FILE_RE = re.compile(r"(\d{13})-(?:(\d{3})-)?(\d{3,4})-(\d{8})-?(\d{4,6})")

# Feeds are refreshed daily; anything older is a leftover from a closed store
# or a stale upload and must not be paired with today's prices.
_MAX_FEED_AGE_DAYS = 7


def _latest_by_store(
    names: list[str],
    chain_id: Optional[str] = None,
    max_age_days: Optional[int] = _MAX_FEED_AGE_DAYS,
    now: Optional[datetime] = None,
) -> dict[int, str]:
    """Map store id -> newest feed filename/URL for that store.

    Only files of one chain id are used: `chain_id` when given, otherwise the
    chain id most of the listed files carry (listings sometimes contain files
    of another chain). Files older than `max_age_days` are dropped.
    """
    parsed: list[tuple[str, int, str, str]] = []  # (chain, store, stamp, name)
    for name in names:
        m = _FEED_FILE_RE.search(name.split("?")[0].rsplit("/", 1)[-1])
        if not m:
            continue
        parsed.append((m.group(1), int(m.group(3)), m.group(4) + m.group(5).ljust(6, "0"), name))
    if not parsed:
        return {}
    if chain_id is None:
        chain_id = Counter(p[0] for p in parsed).most_common(1)[0][0]
    cutoff = None
    if max_age_days is not None:
        cutoff = ((now or datetime.now()) - timedelta(days=max_age_days)).strftime("%Y%m%d")
    best: dict[int, tuple[str, str]] = {}
    for chain, sid, stamp, name in parsed:
        if chain != chain_id:
            continue
        if cutoff is not None and stamp[:8] < cutoff:
            continue
        if sid not in best or stamp > best[sid][0]:
            best[sid] = (stamp, name)
    return {sid: name for sid, (_stamp, name) in best.items()}


def _dominant_chain_id(*name_lists: list[str]) -> Optional[str]:
    """The chain id carried by most feed files across the given listings."""
    ids = Counter(
        m.group(1)
        for names in name_lists
        for name in names
        if (m := _FEED_FILE_RE.search(name.split("?")[0].rsplit("/", 1)[-1]))
    )
    return ids.most_common(1)[0][0] if ids else None


# ---- Hebrew word matching helpers ----

_HEB = "א-ת"


def _alt(words) -> str:
    return "|".join(sorted((re.escape(_norm(w)) for w in words), key=len, reverse=True))


def _words_pat(words, prefixes: str = "", stem: bool = False) -> re.Pattern:
    """Match whole Hebrew words (or word stems when `stem`).

    `prefixes` are one-letter Hebrew prefixes allowed in front of the word
    (ו/ה/ל/ב/מ/כ/ש). A stem match accepts any Hebrew suffix, so 'חתול' also
    catches 'חתולים' and, with prefix 'ל', 'לחתול'.
    """
    pre = f"[{prefixes}]?" if prefixes else ""
    end = "" if stem else f"(?![{_HEB}])"
    return re.compile(f"(?<![{_HEB}]){pre}(?:{_alt(words)}){end}", re.IGNORECASE)


_PREFIXES = "הולבמכש"


# ---- Categories ----
# Grouping for the email. A ranked category is ONE cut of ONE animal, in one
# state (fresh or frozen), so the prices in its table are comparable (#51).
#
# 1. The animal comes from the product name (`_species`): turkey, goose,
#    duck, chicken, beef / veal, lamb, pork. When the name names none, the
#    promotion description is tried, then a poultry-only cut word
#    ("כנפיים טרי", "שוקיים מחפוד") means chicken, and anything else is beef.
# 2. The rules of that animal are tried in order against the name, then
#    against the description. A keyword with "+" needs all its parts
#    ("אנטריקוט+עם עצם"). Beef cut numbers ("בשר מס' 8") count as the cut.
# 3. A frozen item (name says קפוא, or the description does and the name does
#    not say טרי) goes to the frozen twin of its category ("לשון בקר – קפוא").
# Catch-all groups (`_CATCH_ALL`: "בשר – אחר", "עוף – אחר", ...) mix
# unrelated cuts; they are counted but not ranked or shown in the email.

_TURKEY_WORDS = ("הודו",)
_GOOSE_WORDS = ("אווז",)
_DUCK_WORDS = ("ברווז", "מגרה")
_CHICKEN_WORDS = ("עוף", "עופות", "פרגית", "פרגיות", "פטם")
_BEEF_WORDS = ("בקר", "עגל", "עגלה", "אנגוס", "וואגיו")
_LAMB_WORDS = ("כבש", "טלה", "כבשים")
_PORK_WORDS = ("חזיר", "חזירון")
# Cut words that only poultry has (plus the Machpud poultry brand).
_POULTRY_CUT_WORDS = (
    "כנפיים", "כנף", "כנפי", "כנפית", "כרעיים", "כרעים", "כרעי", "ירכיים", "ירכי",
    "שוקיים", "שוקי", "פולקע", "פולקות", "קורקבן", "קורקבנים", "קוקטייל", "קוקטיל",
    "להקפצה", "מחפוד", "מחפו",
)
_POULTRY_SPECIES = ("chicken", "turkey", "goose", "duck")

_NECK = ("גרון", "גרונות", "צוואר")
_WINGS = ("כנפיים", "כנף", "כנפי", "כנפית")
_DRUMSTICKS = ("שוקיים", "שוק", "שוקי")
_THIGHS = ("ירכיים", "ירך", "ירכי")
_GROUND = ("טחון", "טחונה")

_CATEGORY_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # label, species, keywords
    # --- Chicken ---
    ("טחון עוף",                  "chicken", _GROUND),
    ("פרגיות",                    "chicken", ("פרגית", "פרגיות")),
    ("חזה / פילה עוף",            "chicken", ("חזה", "פילה", "להקפצה")),
    ("כנפיים עוף",                "chicken", ("כנפיים", "כנף", "כנפי")),  # "כנפית" (tip): other
    ("כרעיים עוף",                "chicken", ("כרעיים", "כרעים", "כרעי", "קוקטייל", "קוקטיל")),
    ("ירכיים עוף",                "chicken", _THIGHS),
    ("שוקיים עוף",                "chicken", _DRUMSTICKS),
    ("פולקע",                     "chicken", ("פולקע", "פולקות")),
    ("כבד עוף",                   "chicken", ("כבד",)),
    ("קורקבן עוף",                "chicken", ("קורקבן", "קורקבנים", "קרביים")),
    ("לבבות עוף",                 "chicken", ("לבבות", "לב")),
    ("גרון / צוואר עוף",          "chicken", _NECK),
    ("עוף שלם / חצי עוף",         "chicken", ("שלם", "חצי")),
    ("עוף – אחר",                 "chicken", _CHICKEN_WORDS + _POULTRY_CUT_WORDS),
    # --- Turkey ---
    ("טחון הודו אדום",            "turkey",  ("טחון+אדום", "טחונה+אדום")),
    ("טחון הודו",                 "turkey",  _GROUND),
    ("חזה הודו",                  "turkey",  ("חזה", "פילה", "שניצל")),
    ("גרון / צוואר הודו",         "turkey",  _NECK),
    ("כנפיים הודו",               "turkey",  _WINGS),
    ("שוקיים הודו",               "turkey",  _DRUMSTICKS),
    ("ירך הודו",                  "turkey",  _THIGHS),
    ("הודו אדום",                 "turkey",  ("אדום",)),
    ("שישליק / נתחי הודו",        "turkey",  ("שישליק", "נתחי", "נתחים", "קוביות")),
    ("כבד הודו",                  "turkey",  ("כבד",)),
    ("הודו – אחר",                "turkey",  _TURKEY_WORDS),
    # --- Goose / duck ---
    ("חזה אווז",                  "goose",   ("חזה",)),
    ("שוק אווז",                  "goose",   _DRUMSTICKS),
    ("כנפיים אווז",               "goose",   _WINGS),
    ("כבד אווז",                  "goose",   ("כבד",)),
    ("אווז – אחר",                "goose",   _GOOSE_WORDS),
    ("חזה ברווז",                 "duck",    ("חזה", "מגרה")),
    ("ברווז – אחר",               "duck",    _DUCK_WORDS),
    # --- Lamb ---
    ("טחון כבש",                  "lamb",    _GROUND),
    ("צלעות כבש",                 "lamb",    ("צלעות", "צלע", "קרה")),
    ("צוואר כבש",                 "lamb",    _NECK),
    ("כתף כבש",                   "lamb",    ("כתף",)),
    ("חזה כבש",                   "lamb",    ("חזה",)),
    ("שוק כבש",                   "lamb",    _DRUMSTICKS + ("רגל",)),
    ("כבש – אחר",                 "lamb",    _LAMB_WORDS),
    # --- Pork (one catch-all group) ---
    ("חזיר",                      "pork",    _PORK_WORDS),
    # --- Beef / veal: specific cuts first ---
    ("טחון בקר",                  "beef",    _GROUND),
    ("מינוט סטייק",               "beef",    ("מינוט סטייק", "סטייק מינוט", "מינוט")),
    ("פילה מדומה",                "beef",    ("פילה מדומה",)),
    ("פילה מיניון",               "beef",    ("פילה מיניון", "פילה-מיניון")),
    ("פיקניה",                    "beef",    ("פיקניה",)),
    ("אנטריקוט עם עצם",           "beef",    ("אנטריקוט+עם עצם", "אנטריקוט+עם עצ", "אנטריקוט+ע.ע",
                                              "ריבס אנטריקוט", "ריב איי+עם עצם")),
    ("אנטריקוט",                  "beef",    ("אנטריקוט", "ריב איי", "ריבאיי")),
    ("סינטה",                     "beef",    ("סינטה",)),
    ("טי-בון",                    "beef",    ("ט-בון", "טי-בון", "טי בון", "טיבון", "פורטר האוס")),
    ("צ'אק",                      "beef",    ("צ'אק", "צאק", "שפיץ")),
    ("פלאנק / ראמפ",              "beef",    ("פלאנק", "ראמפ")),
    ("שייטל",                     "beef",    ("שייטל",)),
    ("דנבר",                      "beef",    ("דנבר",)),
    ("רוסטביף",                   "beef",    ("רוסטביף", "לרוסטביף")),
    ("אסאדו",                     "beef",    ("אסאדו", "אונטריב", "אונטריפ", "אונטר ריב")),
    ("בריסקט",                    "beef",    ("בריסקט", "בריסקיט", "חזה")),
    ("אוסובוקו",                  "beef",    ("אוסובוקו", "אוסבוקו", "אוסו בוקו")),
    ("גולש",                      "beef",    ("גולש", "גולאש")),
    ("כתף בקר",                   "beef",    ("כתף",)),
    ("שריר / זרוע",               "beef",    ("שריר", "חגורה", "זרוע")),
    ("צלעות בקר",                 "beef",    ("צלעות", "צלע")),
    ("סטייק",                     "beef",    ("סטייק", "דלמוניקו")),
    ("פילה בקר",                  "beef",    ("פילה",)),
    # --- Beef / veal organs ---
    ("לשון בקר",                  "beef",    ("לשון",)),
    ("עצמות מוח",                 "beef",    ("עצמות", "עצמות מוח")),
    ("מוח",                       "beef",    ("מוח", "מח")),
    ("גרון / צוואר בקר",          "beef",    _NECK),
    ("לבבות וקרביים בקר",         "beef",    ("קרביים", "לבבות", "לב")),
    ("כבד בקר",                   "beef",    ("כבד",)),
    ("בשר ראש",                   "beef",    ("ראש",)),
    ("ריאות",                     "beef",    ("ריאות", "ריאה")),
    # --- Generic (catch-all) ---
    ("בייבי בקר",                 "beef",    ("בבי בקר", "בייבי בקר", "בייבי-בקר")),
    ("צלי בקר",                   "beef",    ("צלי",)),
    ("עגל – כללי",                "beef",    ("עגל", "עגלה")),
    ("בקר – כללי",                "beef",    ("בקר",)),
    ("בשר – אחר",                 "beef",    ("בשר",)),
)
_CATEGORY_OTHER = "אחר"
_FROZEN_SUFFIX = " – קפוא"

# Groups that mix unrelated cuts: counted, never ranked (`unranked_by_category`).
_CATCH_ALL: frozenset[str] = frozenset({
    "עוף – אחר", "הודו – אחר", "אווז – אחר", "ברווז – אחר", "כבש – אחר", "חזיר",
    "בייבי בקר", "צלי בקר", "עגל – כללי", "בקר – כללי", "בשר – אחר", _CATEGORY_OTHER,
})

# Israeli beef cut numbers ("בשר מס' 8" = foreshank) -> the cut word.
_CUT_NUMBERS = {"1": "אנטריקוט", "2": "צלעות", "3": "חזה", "4": "כתף", "5": "כתף",
                "6": "פילה מדומה", "8": "שריר"}
_CUT_NUMBER_RE = re.compile(r"(?<![א-ת])(?:מס|מספר)\s*['`.]?\s*(\d{1,2})(?!\d)")


def _species_pat(words) -> re.Pattern:
    # No geresh before the word either: "סטרצ'טלה" (cheese) is not "טלה".
    return re.compile(r"(?<!['\"])" + _words_pat(words, prefixes="ו").pattern, re.IGNORECASE)


_SPECIES_PATS: tuple[tuple[str, re.Pattern], ...] = (
    ("turkey", _species_pat(_TURKEY_WORDS)),
    ("goose", _species_pat(_GOOSE_WORDS)),
    ("duck", _species_pat(_DUCK_WORDS)),
    ("chicken", _species_pat(_CHICKEN_WORDS)),
    ("beef", _species_pat(_BEEF_WORDS)),   # before lamb: "טחון בקר עם כבש" is beef
    ("lamb", _species_pat(_LAMB_WORDS)),
    ("pork", _species_pat(_PORK_WORDS)),
)
_POULTRY_CUT_PAT = _words_pat(_POULTRY_CUT_WORDS, prefixes="ו")
_FROZEN_PAT = _words_pat(("קפוא", "קפואה", "קפואים", "קפואות", "קפו", "קפ", "מוקפא", "מקפוא",
                          "מוקפאים", "פרוזן"), prefixes="ו")
_FRESH_NAME_PAT = _words_pat(("טרי", "טריה", "טריים", "טריות", "מצונן", "מצוננת"), prefixes="ו")


def _rule_matcher(keywords: tuple[str, ...]):
    groups = [
        [_words_pat((part,), prefixes="ו") for part in kw.split("+")]
        for kw in keywords
    ]
    return lambda text: any(all(p.search(text) for p in group) for group in groups)


_CATEGORY_PATS = tuple(
    (label, species, _rule_matcher(kws)) for label, species, kws in _CATEGORY_RULES
)


def _frozen_label(label: str) -> str:
    return label + _FROZEN_SUFFIX


# Display order of the tables: red meat first (beef, then lamb), poultry
# after it. `_CATEGORY_RULES` is in MATCHING order (poultry rules must be
# tried first), so the display order is derived separately; inside one
# animal the rule order is kept.
_SPECIES_DISPLAY_ORDER: tuple[str, ...] = (
    "beef", "lamb", "pork", "chicken", "turkey", "goose", "duck",
)


def _display_labels() -> list[str]:
    species_of: dict[str, str] = {}
    for label, species, _kws in _CATEGORY_RULES:
        species_of.setdefault(label, species)
    rank = {species: i for i, species in enumerate(_SPECIES_DISPLAY_ORDER)}
    # sorted() is stable: labels of one animal stay in rule order.
    return sorted(species_of, key=lambda label: rank.get(species_of[label], len(rank)))


# (label, keywords) in display order, labels de-duplicated, each followed by
# its frozen twin (email_sender orders its groups by this).
_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = tuple(
    entry
    for label in _display_labels()
    for entry in (
        (label, tuple(kw for lb, _sp, kws in _CATEGORY_RULES if lb == label for kw in kws)),
        (_frozen_label(label), ()),
    )
) + ((_CATEGORY_OTHER, ()),)


def _species(text: str) -> Optional[str]:
    """The animal a text names, or None."""
    for species, pat in _SPECIES_PATS:
        if pat.search(text):
            return species
    return None


def _with_cut_number(text: str) -> str:
    """Append the cut word for a beef cut number: 'בשר מס 8 חלק' -> '... שריר'."""
    words = [_CUT_NUMBERS[m.group(1)] for m in _CUT_NUMBER_RE.finditer(text) if m.group(1) in _CUT_NUMBERS]
    return " ".join([text, *words]) if words else text


def _categorize_text(text: str, species: str) -> Optional[str]:
    if not text.strip():
        return None
    if species == "beef":
        text = _with_cut_number(text)
    for label, rule_species, matches in _CATEGORY_PATS:
        if rule_species == species and matches(text):
            return label
    return None


def _is_frozen(name: str, desc: str = "") -> bool:
    if _FROZEN_PAT.search(name):
        return True
    if _FRESH_NAME_PAT.search(name):
        return False
    return bool(desc and _FROZEN_PAT.search(desc))


def _categorize(promo: "MeatPromo") -> str:
    """Category label for a promo: by item name, else by promotion description.

    The species comes from the name; only when the name names none (e.g.
    "כבד במגש לצלייה") is it taken from the description ("כבד עוף טרי").
    """
    name = _norm(promo.item_name)
    desc = _norm(promo.promotion_description)
    species = _species(name) or _species(desc)
    if species is None:
        species = "chicken" if _POULTRY_CUT_PAT.search(name) else "beef"
    label = (
        _categorize_text(name, species)
        or _categorize_text(desc, species)
        or _CATEGORY_OTHER
    )
    if label != _CATEGORY_OTHER and _is_frozen(name, desc):
        return _frozen_label(label)
    return label


def _is_catch_all(label: str) -> bool:
    return label.removesuffix(_FROZEN_SUFFIX) in _CATCH_ALL


# ---- Meat classifier ----
#
# Gate 1 (exclusions): a non-meat word in the product name, the manufacturer
# or (for the subset that is safe there) the promotion description rejects
# the item. Gate 2 (meat evidence) needs one of:
#   * an unambiguous cut / species word ("אנטריקוט", "שוקיים", "כבש"), or
#   * two different meat words ("חזה עוף", "בשר בקר", "פילה עוף"), or
#   * one ambiguous meat word ("כתף", "לשון", "בקר") plus a freshness word in
#     the name ("טרי", "קפוא", "ק"ג") or the feed marking it as weighed.
# The feed's bIsWeighted flag alone is not enough: weighed cheeses, spices,
# fish and deli are sold the same way.

# Words that on their own identify a raw meat / poultry cut.
_MEAT_STRONG: tuple[str, ...] = (
    "עגל", "כבש", "טלה",
    "אנטריקוט", "אסאדו", "פיקניה", "סינטה", "בריסקט", "בריסקיט", "שייטל",
    "אוסובוקו", "אוסבוקו", "אוסו בוקו", "פלאנק", "ראמפ", "דנבר", "טומהוק",
    "ריב איי", "ט-בון", "טי-בון", "טי בון", "פילה מיניון", "פילה-מיניון",
    "מינוט סטייק", "דלמוניקו", "צ'אק", "אונטריב", "אונטר ריב", "ספריבס",
    "גולש", "גולאש", "בבי בקר", "בייבי בקר", "בייבי-בקר",
    "פרגית", "פרגיות", "שוקיים", "כרעיים", "כרעים", "ירכיים",
    "קורקבן", "קורקבנים", "קרביים", "פולקע", "פולקות", "ניקור",
    "אטליז", "נתח קצבים", "חזיר", "חזירון",
)

# Meat words that also occur in non-meat products ("לכסניות גרון",
# "אורז בסמטי הודו", "תיק כתף", "בקר השקיה", "כנפיים" on sanitary pads).
_MEAT_AMBIGUOUS: tuple[str, ...] = (
    "בשר", "בקר", "עוף", "הודו", "אווז", "ברווז", "עגלה", "פטם",
    "כתף", "לשון", "גרון", "גרונות", "מוח", "שריר", "חגורה", "זרוע", "צלי",
    "כבד", "סטייק", "צלעות", "צלע", "כנפיים", "כנף", "כנפי",
    "חזה", "ירך", "ירכי", "שוקי", "כרעי", "צוואר", "לסת", "רוסטביף", "לרוסטביף",
)

# Meat words that are too common elsewhere to count on their own even with a
# freshness signal (ground spices, fish fillets, palm hearts, "שוק." chocolate,
# celery heads): they only count as the second word of a pair ("פילה עוף").
_MEAT_WEAK: tuple[str, ...] = (
    "טחון", "טחונה", "פילה", "לב", "לבבות", "שוק", "גב", "ראש", "רגל", "זנב",
    "נתח", "נתחי", "קוקטייל",
)

# Freshness / butcher-counter words that make a single ambiguous word credible.
_FRESH_MARKERS: tuple[str, ...] = (
    "טרי", "טריה", "טריים", "טריות", "קפוא", "קפואה", "קפואים", "קפואות",
    "מוכשר", "צרכני", "צרכנית", "חלק", "גלאט", "מהדרין", "ארוז", "במגש",
    "לצליה", "לצלייה", "לטחינה", "עצמי", "ק\"ג", "קג", "לק\"ג", "לקג", "ק\"ג.",
)

# Exclusions matched as word STEMS with an optional one-letter prefix.
_EXCLUDE_STEMS: tuple[str, ...] = (
    # Fish / seafood
    "דג", "סלמון", "טונה", "סרדין", "הרינג", "שרימפ", "קרפיון", "אמנון", "דניס",
    "לברק", "פורל", "מושט", "נילוס", "סיבס", "ברמונדי", "ברבוני", "מקרל",
    "הליבוט", "פנגסיוס", "בקלה", "הייק", "מרלוז", "טילפי", "לוקוס", "טרית",
    "אנשובי", "שפרוט", "קלמרי", "תמנון", "סרטן", "מולים", "גפילטע", "פיש",
    "סושי", "קוויאר", "מטיאס", "אינטיאס", "ילוטייל", "אלבקור", "טראכון", "בוניטו",
    "אלתית", "פארידה", "לקרדה", "זהבון", "נסיכת", "אדמונית", "מקרוסקה", "פלמידה",
    # Pet food
    "חתול", "חתלת", "כלב", "פריסקיז", "פרוסקיז", "רויאל", "פרוקל", "פרופלן",
    "ווסקס", "ויסקס", "ווסקוויק", "סמלט", "פדיגרי", "פורינה", "פנסי", "פיסט",
    "דוגלי", "בונזו", "רנדי", "דלידוג", "דלידג", "פאוץ",
    "נוטרילאב", "חטיף",
    # Plant-based
    "טבעונ", "צמחונ", "צמחי", "סויה", "טופו", "סייטן", "טבעול", "ביונד",
    "תחליפ", "רידפיין", "רידיפיין", "vegan", "ווגן", "beyond",
    # Dairy
    "גבינ", "גאוד", "אמנטל", "מוצרל", "צ'דר", "קוטג", "לבנה", "לאבנה", "חלב",
    "יוגורט", "שמנת", "קממבר", "בולגרי", "חמאה", "צפתית", "עיזים", "חלומי",
    "מנצגו", "פרמזן", "מסקרפונה", "טבורוג", "סטרצ'טלה", "סטרצטלה", "סטרציאטלה", "בוראטה",
    # Processed / deli / ready meals (fresh cuts only)
    "נקניק", "פסטרמ", "סלמי", "קבנוס", "קבב", "המבורגר", "קציצ", "שווארמ", "שוארמ",
    "צ'וריסו", "מרגז", "שניצל", "מעושן", "מעושנ", "עישון", "מתובל",
    "בייקון", "סלסול", "נאגטס", "נגיס", "כוכבי", "צונגו", "אצבעות", "ציפוי",
    "פריך", "קריספי", "נתחונ", "מוקפץ", "מבושל", "ממול", "מילוי", "קרפצ'יו",
    "קרפציו", "פרושוטו", "ג'רקי", "בילטונג", "ממרח", "מעדן", "מחית", "כיסונ",
    "כיסוני", "פלמני", "פלמיני", "ורניקי", "גיוזה", "בורקס", "פשטיד", "מאפה",
    "פיצה", "לחמני", "כריך", "סנדוויץ", "ארוח", "ברביקיו", "מפולפל", "בדבש",
    "זוגלובק", "קורנביף", "קורנד", "רוטב", "מוכן", "שימורי", "בלינצס", "בלינציס",
    "אגרול", "נקנק", "פסטל", "כבוש", "פפרוני", "פיקנטי", "ויצינה", "רולדה",
    "בוזנינה", "פייקון", "כמהין", "חינקלי", "קרפלך", "טושונקה", "גרקי", "צומח",
    "הצומח", "מהצומח", "המצומח", "צאנק", "מעובד", "רומני", "קישקע", "פריזר",
    "בורגר", "סטייקבורגר", "דקניק", "דקדק", "אמפנד", "סיגר", "ממתק", "אסיתי",
    # Other foods, spices, snacks
    "נודלס", "אטריות", "מגיק", "ראמן", "צ'יפס", "ציפס", "תירס", "חציל", "פטרי",
    "סלט", "תבלין", "תבלינ", "תיבול", "אבקת", "בולון", "קוביית", "מרק", "טעם",
    "אורז", "לכסני", "פסטה", "פתיתים", "קרקר", "במבה", "ביסלי", "סוכרי",
    "שוקולד", "עוגי", "דקל", "חסה", "קיסר", "ארטישוק", "לקישוט", "מרשמלו",
    # Not food
    "תחבוש", "קוטקס", "אולוו", "טמפון", "מגבונ", "חיתול", "סכין", "קרש",
    "שקית", "שקיות", "מנקה", "השקיה", "ספוג", "מברשת", "תבנית", "מלקחי", "כפפ",
)

# Exclusions that must match the whole word (too short / too common as stems).
_EXCLUDE_WORDS: tuple[str, ...] = (
    "קוד", "מוסר", "סול", "בורי", "גור", "גורים", "בוגר", "קיטן", "מחמד", "חיות",
    "פטה", "מוס", "קובה", "קובות", "באו", "באן", "גריל", "לוף", "ציר", "לחם",
    "פרמיו", "פרימיו", "גומי", "תיק", "פד", "פדים", "סיר", "קט", "דוג", "וואן", "דים סאם", "פאד תאי", "מנה חמה",
    "פירות ים", "ללא בשר", "ללא עוף", "סגנון", "דמוי", "דמויי", "מאמא עוף",
    "דק דק", "צלוי", "בס", "באס", "בסה", "ים", "סל", "ברי", "אדם", "לקס", "פסט",
    "קרם", "כמו", "טירת", "מונג", "פוף", "שום", "דורות", "קרח",
)

# Exclusions matched as stems but without a prefix letter ("לשיפודים" = cubes
# for skewers is a raw cut, "שיפודי פרגית" is a prepared product).
_EXCLUDE_STEMS_NO_PREFIX: tuple[str, ...] = ("שיפוד",)

# Abbreviations: "גב." = cheese, "שוק." = chocolate, "ש." = schnitzel,
# "פסט." = pastrami.
_EXCLUDE_ABBREV_PAT = re.compile(r"(?<![א-ת])(?:גב|שוק|ש|פסט|פי)\.")

# Exclusions that are safe to apply to the promotion description as well:
# a description naming fish, pets, substitutes or non-meat goods reveals what
# a truncated product name really is ("פילה סל" → "מנות סלמון").
_DESC_EXCLUDE_STEMS: tuple[str, ...] = (
    "דג", "סלמון", "טונה", "הרינג", "מקרל", "ברמונדי", "ברבוני", "אמנון", "דניס",
    "לברק", "פורל", "סושי",
    "חתול", "חתלת", "כלב", "רנדי", "פאוץ", "שלוקים", "סקוויז",
    "דוגלי", "בונזו", "פריסקיז", "פנסי", "דלידוג", "חטיף",
    "תחליפ", "טבעונ", "צמחונ", "טבעול", "רידפיין", "רידיפיין",
    "נודלס", "מגיק", "אטריות", "ראמן", "תבלינ", "תבלין", "סלטי", "פטרי", "ציפס",
    "צ'יפס", "תירס", "מטוגנ", "זוגלובק", "תחבוש",
)
_DESC_EXCLUDE_WORDS: tuple[str, ...] = ("דים סאם", "מזון", "מנה חמה", "פרמיו", "פרימיו")

# Manufacturers that only make non-meat / processed products.
_EXCLUDE_MANUFACTURERS: tuple[str, ...] = (
    "פורינה", "פריסקיז", "פנסי פיסט", "ראפא", "טבעול", "רידיפיין", "רידפיין",
    "דגים", "דגת", "דים סאם", "נודלס", "אסם", "קנור", "יוניליוור", "תבליני",
    "סלטי", "גבינות", "מחלבות", "קוטקס", "שלושת האופים", "מאמא עוף", "זוגלובק",
    "יחיעם", "מעדני", "חטיפי", "דלידג", "טירת צבי",
)

_MEAT_STRONG_PAT = _words_pat(_MEAT_STRONG, prefixes="ול")  # "קוביות בקר לגולש"
_MEAT_AMBIG_PAT = _words_pat(_MEAT_AMBIGUOUS, prefixes="ו")
_MEAT_WEAK_PAT = _words_pat(_MEAT_WEAK, prefixes="ו")
_EXCLUDE_STEM_NP_PAT = _words_pat(_EXCLUDE_STEMS_NO_PREFIX, stem=True)
_FRESH_PAT = _words_pat(_FRESH_MARKERS)
_EXCLUDE_STEM_PAT = _words_pat(_EXCLUDE_STEMS, prefixes=_PREFIXES, stem=True)
_EXCLUDE_WORD_PAT = _words_pat(_EXCLUDE_WORDS, prefixes=_PREFIXES)
_DESC_EXCLUDE_STEM_PAT = _words_pat(_DESC_EXCLUDE_STEMS, prefixes=_PREFIXES, stem=True)
_DESC_EXCLUDE_WORD_PAT = _words_pat(_DESC_EXCLUDE_WORDS, prefixes=_PREFIXES)
_EXCLUDE_MANUFACTURER_PAT = _words_pat(_EXCLUDE_MANUFACTURERS, prefixes=_PREFIXES, stem=True)

# Kept for backwards compatibility: "does the name contain any meat word".
_MEAT_PAT = re.compile(
    f"{_MEAT_STRONG_PAT.pattern}|{_MEAT_AMBIG_PAT.pattern}|{_MEAT_WEAK_PAT.pattern}", re.IGNORECASE
)


def _is_meat_product(name: str, context: str = "", item: object = None) -> bool:
    """Decide if a promo item is a fresh / frozen raw meat or poultry cut.

    `name` is the product name from PriceFull; `context` is the promotion
    description (only a safe subset of exclusions is applied to it); `item`
    is the PriceFull row (`_FeedItem`) when available — its manufacturer and
    weighed-item flag are used as extra signals.
    """
    name = _norm(name)
    if not name.strip():
        return False
    if (
        _EXCLUDE_STEM_PAT.search(name)
        or _EXCLUDE_WORD_PAT.search(name)
        or _EXCLUDE_STEM_NP_PAT.search(name)
        or _EXCLUDE_ABBREV_PAT.search(name)
    ):
        return False
    desc = _norm(context)
    if desc and (_DESC_EXCLUDE_STEM_PAT.search(desc) or _DESC_EXCLUDE_WORD_PAT.search(desc)):
        return False
    manufacturer = _norm(getattr(item, "manufacturer", "") or "")
    if manufacturer and _EXCLUDE_MANUFACTURER_PAT.search(manufacturer):
        return False
    if _MEAT_STRONG_PAT.search(name):
        return True
    ambiguous = {m.group(0).lstrip("ו") for m in _MEAT_AMBIG_PAT.finditer(name)}
    weak = {m.group(0).lstrip("ו") for m in _MEAT_WEAK_PAT.finditer(name)}
    if len(ambiguous | weak) >= 2:
        return True
    if len(ambiguous) == 1:
        return bool(_FRESH_PAT.search(name) or getattr(item, "weighted", False))
    return False


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
    regions: list[str] = field(default_factory=list)  # regions it was seen in (one per raw row)
    cities: list[str] = field(default_factory=list)   # cities it was seen in
    store_count: int = 1                              # stores it was seen in
    category: str = ""                                # meat category (set by the report)
    # Multi-buy deals ("2 ב-90"): `discounted_price` is always the PER-UNIT
    # promo price (comparable with `regular_price`); these keep the deal itself.
    min_qty: Optional[Decimal] = None                 # units needed (> 1 only)
    bundle_price: Optional[Decimal] = None            # total price for min_qty units
    is_weighted: bool = False                         # sold by weight: prices are per kg
    # Kilograms in one unit of `regular_price` / `discounted_price`: 1 for
    # weighed items, the pack weight for packaged ones, None when unknown.
    unit_kg: Optional[Decimal] = None
    stores: list[str] = field(default_factory=list)   # branches: "name (city)"

    @property
    def region(self) -> str:
        return self.regions[0] if self.regions else ""

    @property
    def kg_per_unit(self) -> Optional[Decimal]:
        if self.unit_kg is not None and self.unit_kg > 0:
            return self.unit_kg
        return Decimal(1) if self.is_weighted else None

    @property
    def is_promo(self) -> bool:
        """False for an item listed at its regular price (not on sale)."""
        return self.discounted_price is not None

    @property
    def price_per_kg(self) -> Optional[Decimal]:
        """Price per kg the shopper pays (the ranking key): the promo price,
        or the regular price for an item that is not on sale. None when it
        cannot be derived."""
        kg = self.kg_per_unit
        price = self.discounted_price if self.discounted_price is not None else self.regular_price
        if kg is None or price is None:
            return None
        return (price / kg).quantize(Decimal("0.01"))

    @property
    def regular_per_kg(self) -> Optional[Decimal]:
        kg = self.kg_per_unit
        if kg is None or self.regular_price is None:
            return None
        return (self.regular_price / kg).quantize(Decimal("0.01"))

    @property
    def pack_text(self) -> str:
        """'מארז 500 גרם' for packaged items with a known weight, '' otherwise."""
        if self.is_weighted or self.unit_kg is None:
            return ""
        grams = (self.unit_kg * 1000).quantize(Decimal("1"))
        if grams >= 1000 and grams % 100 == 0:
            return f'מארז {(grams / 1000).normalize():f} ק"ג'
        return f"מארז {grams} גרם"

    @property
    def deal_text(self) -> str:
        """'2 ב-₪90' for a multi-buy deal, '' otherwise."""
        if self.min_qty is None or self.bundle_price is None or self.min_qty <= 1:
            return ""
        return f"{self.min_qty.normalize():f} ב-₪{self.bundle_price}"

    @property
    def savings_pct(self) -> Optional[Decimal]:
        """Percent off, from the per-unit prices (falls back to DiscountRate)."""
        if self.regular_price and self.discounted_price is not None and self.regular_price > 0:
            delta = (self.regular_price - self.discounted_price) / self.regular_price * 100
            return delta.quantize(Decimal("1"))
        if self.discount_rate is not None and self.discount_rate > 0:
            return self.discount_rate
        return None

    @property
    def savings_amount(self) -> Optional[Decimal]:
        """Saving per unit (per kg for weighed items)."""
        if self.regular_price and self.discounted_price is not None:
            return (self.regular_price - self.discounted_price).quantize(Decimal("0.01"))
        return None


@dataclass
class SupermarketReport:
    status: str
    chains_scanned: list[str] = field(default_factory=list)
    stores_scanned: list[str] = field(default_factory=list)  # "chain – store (city)"
    stores_by_region: dict[str, int] = field(default_factory=dict)
    promos_by_region: dict[str, int] = field(default_factory=dict)  # before the top-N cut
    # Items with a price per kg (on sale or at the regular price), per shown
    # category, before the top-N cut.
    promos_by_category: dict[str, int] = field(default_factory=dict)
    # After the scan: grouped by category (in `_CATEGORIES` order), cheapest
    # price per kg first inside each category. Rows are deals and, where they
    # are cheaper, items at their regular price (`MeatPromo.is_promo`).
    promos: list[MeatPromo] = field(default_factory=list)
    error: Optional[str] = None
    # chain -> Hebrew reason. Failed = could not be scanned (login, files,
    # downloads); empty = scanned fine but nothing to show (no store in the
    # cities, no meat deals today).
    chains_failed: dict[str, str] = field(default_factory=dict)
    chains_empty: dict[str, str] = field(default_factory=dict)
    # Scan area at scan time (empty = not recorded; readers fall back to the
    # current settings).
    cities: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    stores_by_city: dict[str, int] = field(default_factory=dict)
    # Deals left out of the ranking because no price per kg could be derived.
    unpriced_by_category: dict[str, int] = field(default_factory=dict)
    # Deals in catch-all groups ("בשר – אחר", "עוף – אחר", ...): not ranked, not shown.
    unranked_by_category: dict[str, int] = field(default_factory=dict)

    @property
    def unranked_count(self) -> int:
        return sum(self.unranked_by_category.values())

    @property
    def unpriced_count(self) -> int:
        return sum(self.unpriced_by_category.values())


class ChainScanError(Exception):
    """A chain produced nothing; `reason` is a short Hebrew explanation."""

    def __init__(self, reason: str, failed: bool = True):
        super().__init__(reason)
        self.reason = reason
        self.failed = failed


class _StoreFeedError(Exception):
    """One store's feed files could not be downloaded or parsed."""


# ---- Shared per-store scan ----

@dataclass
class _FeedItem:
    """One <Item> of a PriceFull file (only the fields the scanner uses)."""
    name: str
    price: Optional[Decimal]
    manufacturer: str = ""
    weighted: bool = False
    unit_of_measure: str = ""
    unit_price: Optional[Decimal] = None  # UnitOfMeasurePrice
    unit_qty: str = ""                    # UnitQty: the unit Quantity is in
    quantity: Optional[Decimal] = None    # Quantity: pack content in UnitQty


def _dec(text: Optional[str]) -> Optional[Decimal]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        value = Decimal(text)
    except (ArithmeticError, ValueError, InvalidOperation):
        return None
    return value if value.is_finite() else None


def _build_item_index(pricefull_root: ET.Element) -> dict[str, _FeedItem]:
    """Map ItemCode → _FeedItem from a PriceFull XML tree."""
    index: dict[str, _FeedItem] = {}
    items = pricefull_root.find("Items")
    if items is None:
        items = pricefull_root.find(".//Items")
    if items is None:
        return index
    for item in items.findall("Item"):
        fields = {c.tag: (c.text or "").strip() for c in item}
        code = fields.get("ItemCode", "")
        name = fields.get("ItemName", "")
        if code and name:
            index[code] = _FeedItem(
                name=name,
                price=_dec(fields.get("ItemPrice")),
                manufacturer=fields.get("ManufactureName", ""),
                weighted=fields.get("bIsWeighted", "") == "1",
                unit_of_measure=fields.get("UnitOfMeasure", ""),
                unit_price=_dec(fields.get("UnitOfMeasurePrice")),
                unit_qty=fields.get("UnitQty", ""),
                quantity=_dec(fields.get("Quantity")),
            )
    return index


# ---- Price per kg ----
#
# Prices inside a category are ranked per kilogram. What the PriceFull fields
# mean (checked against Shufersal, Rami Levy, Osher Ad, Tiv Taam and Bareket
# files of 2026-11-11):
#   * bIsWeighted=1: ItemPrice is per kg. UnitQty/UnitOfMeasure say so
#     ("קילוגרם", '1 ק"ג') except at Osher Ad, which writes "יחידות"/"יחידה"
#     for weighed meat priced per kg — the flag is trusted, not the unit text.
#   * packaged (bIsWeighted=0): Quantity is the pack content in UnitQty
#     ("גרם" 500 / "קילוגרם" 1). Osher Ad writes UnitQty "לא ידוע" with the unit
#     in UnitOfMeasure ('ק"ג', Quantity 1.20). UnitOfMeasurePrice is the price
#     per UnitOfMeasure ("100 גרם" -> per 100 g), so it implies a pack size too.
# A pack weight is used only when every source that states one agrees (within
# 5%): Quantity+unit, UnitOfMeasurePrice, and a weight written in the item
# name ("500 גרם"). Otherwise — no weight anywhere (Quantity 0, "יחידות"), or
# sources that contradict each other — the deal has no price per kg and is
# left out of the ranking (counted in `unpriced_by_category`).

_GRAM_UNITS = ("גרם", "גר", "גר'", "ג", "ג'")
_KG_UNITS = ("קילוגרם", "קילו", "ק\"ג", "קג", "ק\"", "ק'ג")
_UNIT_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)?\s*([א-ת\"'.]+)\s*$")
_NAME_WEIGHT_RE = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)\s*(קילוגרם|קילו|ק\"ג|קג|ק\"|גרם|גר'|גר|ג'|ג)(?![א-ת])"
)
_PACK_TOLERANCE = Decimal("0.05")
_MIN_PACK_G, _MAX_PACK_G = Decimal("50"), Decimal("10000")


def _grams_per(unit: str) -> Optional[Decimal]:
    unit = _norm(unit).strip().rstrip(".")
    if unit in _GRAM_UNITS:
        return Decimal(1)
    if unit in _KG_UNITS:
        return Decimal(1000)
    return None


def _measure_grams(text: str) -> Optional[Decimal]:
    """'100 גרם' -> 100, '1קילוגרם' / 'ק"ג' -> 1000, 'יחידה' -> None."""
    m = _UNIT_RE.match(_norm(text))
    if not m:
        return None
    per = _grams_per(m.group(2))
    if per is None:
        return None
    qty = _dec(m.group(1)) if m.group(1) else Decimal(1)
    return qty * per if qty and qty > 0 else None


def _name_grams(name: str) -> Optional[Decimal]:
    m = _NAME_WEIGHT_RE.search(_norm(name))
    if not m:
        return None
    qty, per = _dec(m.group(1)), _grams_per(m.group(2))
    return qty * per if qty and per and qty > 0 else None


def _pack_grams(item: _FeedItem) -> Optional[Decimal]:
    """Pack weight in grams of a packaged item, or None when not reliable."""
    stated: list[Decimal] = []
    qty = item.quantity
    if qty is not None and qty > 0:
        per = _grams_per(item.unit_qty)
        if per is None:
            # UnitQty "לא ידוע": the unit is in UnitOfMeasure ('ק"ג').
            base = _measure_grams(item.unit_of_measure)
            per = base if base in (Decimal(1), Decimal(1000)) else None
        if per is not None:
            stated.append(qty * per)
    from_name = _name_grams(item.name)
    if from_name is not None:
        stated.append(from_name)
    if not stated:
        return None
    base = _measure_grams(item.unit_of_measure)
    if base is not None and item.unit_price and item.unit_price > 0 and item.price and item.price > 0:
        stated.append(item.price / item.unit_price * base)
    first = stated[0]
    if any(abs(s - first) > first * _PACK_TOLERANCE for s in stated[1:]):
        return None
    if not (_MIN_PACK_G <= first <= _MAX_PACK_G):
        return None
    return first


def _unit_kg(item: _FeedItem, weighted: bool) -> Optional[Decimal]:
    """Kilograms in one unit of the item's price: 1 for weighed items."""
    if weighted:
        # A weighed item priced per 100 g would rank 10x too cheap.
        base = _measure_grams(item.unit_of_measure)
        return None if base is not None and base != Decimal(1000) else Decimal(1)
    grams = _pack_grams(item)
    return grams / 1000 if grams is not None else None


def _scan_store(
    chain_name: str,
    store: StoreInfo,
    price_raw: Optional[bytes],
    promo_raw: Optional[bytes],
) -> list[MeatPromo]:
    """Parse one store's PriceFull + PromoFull and return its meat promos.

    Raises _StoreFeedError when a file is missing or unparsable.
    """
    if not price_raw or not promo_raw:
        raise _StoreFeedError("download failed")
    try:
        price_root = ET.fromstring(_maybe_gunzip(price_raw))
        promo_root = ET.fromstring(_maybe_gunzip(promo_raw))
    except ET.ParseError as exc:
        raise _StoreFeedError(f"parse failed: {exc}") from exc
    item_index = _build_item_index(price_root)
    store_code = f"{store.store_id:03d}"
    promos = _extract_meat_promos(chain_name, store_code, promo_root, item_index)
    n_deals = len(promos)
    # Items at their regular price compete with the deals in the ranking, so
    # a table shows the cheapest price per kg whether or not it is a sale.
    promos += _extract_regular_items(
        chain_name, store_code, item_index, {p.item_code for p in promos},
    )
    for p in promos:
        p.store_name = store.name
        p.city = store.city
        p.regions = [store.region]
        p.cities = [store.city]
        p.stores = [store.branch]
    log.info(
        "%s / %s (%s, %s): %d items, %d meat promos, %d meat items at regular price",
        chain_name, store.label, store.city, store.region, len(item_index),
        n_deals, len(promos) - n_deals,
    )
    return promos


def _extract_regular_items(
    chain_name: str,
    store_id: str,
    item_index: dict[str, "_FeedItem"],
    on_sale_codes: set[str],
) -> list["MeatPromo"]:
    """Meat items of one store that are not in a usable promotion, as rows
    with `discounted_price=None` (their regular price is what you pay)."""
    rows: list[MeatPromo] = []
    for code, item in item_index.items():
        if code in on_sale_codes or item.price is None or item.price <= 0:
            continue
        if not _is_meat_product(item.name, context="", item=item):
            continue
        rows.append(MeatPromo(
            chain=chain_name,
            store_id=store_id,
            item_code=code,
            item_name=item.name,
            regular_price=item.price,
            discounted_price=None,
            discount_rate=None,
            promotion_description="",
            is_weighted=item.weighted,
            unit_kg=_unit_kg(item, item.weighted),
        ))
    return rows


def _scan_chain(
    chain_name: str,
    stores: list[StoreInfo],
    fetch,  # (StoreInfo) -> (price_bytes, promo_bytes) | None when the store has no files
) -> tuple[list[MeatPromo], list[StoreInfo]]:
    """Scan every store of the chain in the enabled cities.

    Returns (promos, stores that yielded at least one meat promo). Raises
    ChainScanError when no store could be scanned at all, so the report can
    say why the chain is missing.
    """
    from concurrent.futures import ThreadPoolExecutor

    limit = _stores_per_region()
    candidates = _candidates_by_region(stores)
    if not candidates:
        log.info("%s: no stores in the selected cities — skipped", chain_name)
        raise ChainScanError("אין סניפים בערים שנבחרו", failed=False)
    todo = [
        store
        for region_stores in candidates.values()
        for store in (region_stores[:limit] if limit else region_stores)
    ]
    log.info("%s: scanning %d stores in the selected cities", chain_name, len(todo))

    def _one(store: StoreInfo) -> tuple[StoreInfo, str, list[MeatPromo]]:
        try:
            files = fetch(store)
            if files is None:
                log.info("%s / %s: no current feed files", chain_name, store.label)
                return store, "nofiles", []
            return store, "ok", _scan_store(chain_name, store, *files)
        except Exception as exc:  # one bad store must not sink the chain
            log.warning("%s / %s: %s: %s", chain_name, store.label, type(exc).__name__, exc)
            return store, "error", []

    promos: list[MeatPromo] = []
    used: list[StoreInfo] = []
    outcomes: Counter = Counter()
    with ThreadPoolExecutor(max_workers=_STORE_WORKERS) as pool:
        for store, outcome, found in pool.map(_one, todo):
            outcomes[outcome] += 1
            if found:
                promos.extend(found)
                used.append(store)
    if not outcomes["ok"]:
        if outcomes["error"]:
            raise ChainScanError(f"הורדת הקבצים נכשלה ב-{outcomes['error']} סניפים")
        raise ChainScanError("אין קבצי מחירים ומבצעים עדכניים לסניפים שנבחרו")
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
    """Meat promotions from Shufersal stores inside the target regions.

    Raises ChainScanError when the chain could not be scanned.
    """
    log.info("%s: fetching store list", _SHUFERSAL_NAME)
    store_links = _shufersal_links(cat=5)
    raw = _download(store_links[0]) if store_links else None
    if not raw:
        log.warning("%s: could not fetch Stores file", _SHUFERSAL_NAME)
        raise ChainScanError("הורדת קובץ הסניפים נכשלה")
    stores = _parse_stores(raw)

    def _fetch(store: StoreInfo):
        promo_links = _shufersal_links(4, store.store_id)
        price_links = _shufersal_links(2, store.store_id)
        chain_id = _dominant_chain_id(promo_links, price_links)
        promo_url = _latest_by_store(promo_links, chain_id).get(store.store_id)
        price_url = _latest_by_store(price_links, chain_id).get(store.store_id)
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
    """Scan one chain hosted on url.publishedprices.co.il (target regions only).

    Raises ChainScanError when the chain could not be scanned.
    """
    log.info("%s: logging in to publishedprices", chain_name)
    with httpx.Client(follow_redirects=True, timeout=60, headers=_HEADERS) as client:
        if not _pp_login(client, username):
            log.warning("%s: login failed", chain_name)
            raise ChainScanError("ההתחברות לאתר המחירים נכשלה")

        def _names(prefix: str, limit: int) -> list[str]:
            return [
                f.get("fname", "")
                for f in _pp_list_files(client, prefix, limit=limit)
                if f.get("fname", "").startswith(prefix)
            ]

        stores_files = sorted(_names("Stores", 20))
        if not stores_files:
            log.warning("%s: no Stores file listed", chain_name)
            raise ChainScanError("לא נמצאו קבצים באתר המחירים")
        stores_raw = _pp_download(client, stores_files[-1])
        if not stores_raw:
            log.warning("%s: Stores file download failed", chain_name)
            raise ChainScanError("הורדת קובץ הסניפים נכשלה")
        stores = _parse_stores(stores_raw)

        # The host's search box only matches short prefixes, so list everything
        # and pick each store's newest file locally — PromoFull and PriceFull
        # of the same chain id, both recent.
        promo_names = _names("PromoFull", 1000)
        price_names = _names("PriceFull", 1000)
        chain_id = _dominant_chain_id(promo_names, price_names)
        promo_by_store = _latest_by_store(promo_names, chain_id)
        price_by_store = _latest_by_store(price_names, chain_id)
        if not promo_by_store or not price_by_store:
            log.warning("%s: no current PromoFull/PriceFull files listed", chain_name)
            raise ChainScanError("אין קבצי מחירים ומבצעים עדכניים באתר")

        def _fetch(store: StoreInfo):
            promo_fname = promo_by_store.get(store.store_id)
            price_fname = price_by_store.get(store.store_id)
            if not promo_fname or not price_fname:
                return None
            return _pp_download(client, price_fname), _pp_download(client, promo_fname)

        return _scan_chain(chain_name, stores, _fetch)


# ---- Bina Projects portals (<chain>.binaprojects.com) ----
#
# Main.aspx is a page whose script calls three JSON endpoints (no login):
#   POST MainIO_Hok.aspx  wReshet=הכל, WStore=<store id or 0>, WDate=<dd/mm/yyyy
#                         or empty = all days>, WFileType=<1 Stores, 2 Price,
#                         3 Promo, 4 PriceFull, 5 PromoFull>
#        -> [{"FileNm": "PriceFull7290875100001-002-061-20261111-040711.GZ", ...}]
#   POST Download.aspx?FileNm=<name>  -> [{"SPath": "<direct URL of the file>"}]
# The files are gzipped XML in the same <Root><Items>/<Promotions> layout as
# the other chains, so the shared parsers read them as they are.

_BINA_STORES, _BINA_PRICEFULL, _BINA_PROMOFULL = "1", "4", "5"


def _bina_list(client: httpx.Client, base: str, file_type: str, store: int = 0) -> list[str]:
    r = client.post(
        base + "MainIO_Hok.aspx",
        data={"wReshet": "הכל", "WStore": str(store), "WDate": "", "WFileType": file_type},
    )
    if r.status_code != 200:
        log.warning("bina listing %s: HTTP %s", base, r.status_code)
        return []
    try:
        rows = r.json()
    except ValueError:
        return []
    return [str(row.get("FileNm", "")).strip() for row in rows if isinstance(row, dict)]


def _bina_download(client: httpx.Client, base: str, filename: str) -> Optional[bytes]:
    r = client.post(base + "Download.aspx", params={"FileNm": filename})
    if r.status_code != 200:
        return None
    try:
        path = (r.json() or [{}])[0].get("SPath", "")
    except (ValueError, AttributeError, IndexError):
        return None
    if not path:
        return None
    r = client.get(path, timeout=120)
    return r.content if r.status_code == 200 else None


def scan_bina_chain(subdomain: str, chain_name: str) -> tuple[list[MeatPromo], list[StoreInfo]]:
    """Scan one chain hosted by Bina Projects (enabled cities only).

    Raises ChainScanError when the chain could not be scanned.
    """
    base = _BINA_URL.format(sub=subdomain)
    log.info("%s: fetching store list from %s", chain_name, base)
    with httpx.Client(follow_redirects=True, timeout=60, headers=_HEADERS) as client:
        try:
            stores_files = sorted(
                n for n in _bina_list(client, base, _BINA_STORES) if n.startswith("Stores")
            )
        except httpx.HTTPError as exc:
            log.warning("%s: price portal unreachable: %s", chain_name, exc)
            raise ChainScanError("אתר המחירים של הרשת אינו זמין") from exc
        if not stores_files:
            log.warning("%s: no Stores file listed", chain_name)
            raise ChainScanError("לא נמצאו קבצים באתר המחירים")
        stores_raw = _bina_download(client, base, stores_files[-1])
        if not stores_raw:
            log.warning("%s: Stores file download failed", chain_name)
            raise ChainScanError("הורדת קובץ הסניפים נכשלה")
        stores = _parse_stores(stores_raw)

        # Listings are per store (a few dozen files each) rather than one
        # listing of the whole chain, which the portal caps at 1000 rows.
        def _fetch(store: StoreInfo):
            names = {
                kind: [n for n in _bina_list(client, base, ft, store.store_id) if n.startswith(kind)]
                for kind, ft in (("PriceFull", _BINA_PRICEFULL), ("PromoFull", _BINA_PROMOFULL))
            }
            chain_id = _dominant_chain_id(names["PromoFull"], names["PriceFull"])
            price = _latest_by_store(names["PriceFull"], chain_id).get(store.store_id)
            promo = _latest_by_store(names["PromoFull"], chain_id).get(store.store_id)
            if not price or not promo:
                return None
            return _bina_download(client, base, price), _bina_download(client, base, promo)

        return _scan_chain(chain_name, stores, _fetch)


# ---- Promotion parsing ----

# Not real promotions: class-action compensation credits ("הנחה בגין תביעה
# ייצוגית"), refunds, and permanent payment-method / loyalty discounts
# (Shufersal's "תו זהב 5% הנחה מותג שופרסל", valid 2014–2031).
_NOT_A_PROMO_PAT = _words_pat(
    ("תביעה", "תביעת", "ייצוגית", "יצוגית", "פיצוי", "זיכוי", "החזר", "תו זהב",
     "אשראי", "כרטיס"),
    prefixes=_PREFIXES,
)

# Two prices for the same unit may differ by rounding; beyond this share of
# the regular price they describe different units (per kg vs per 100 g).
_PRICE_TOLERANCE = Decimal("0.03")


def _per_unit_promo_price(
    regular: Optional[Decimal],
    discount_rate: Optional[Decimal],
    discounted_price: Optional[Decimal],
    per_mida: Optional[Decimal],
    unit_price: Optional[Decimal],
    min_qty: Optional[Decimal],
) -> Optional[Decimal]:
    """The promo price of ONE unit (or kg), or None when the row is inconsistent.

    `discounted_price` is the total for `min_qty` units ("3 ב-40"), so the
    per-unit price is discounted_price / min_qty. `per_mida` is the promo price
    per unit of measure, comparable with PriceFull's UnitOfMeasurePrice. When
    the feed gives a DiscountRate, the chosen price must agree with it.
    """
    qty = min_qty if min_qty is not None and min_qty >= 1 else Decimal(1)
    candidates: list[Decimal] = []
    if discounted_price is not None and discounted_price > 0:
        candidates.append(discounted_price / qty)
    if regular and regular > 0 and per_mida and per_mida > 0 and unit_price and unit_price > 0:
        candidates.append(regular * per_mida / unit_price)
    if not regular or regular <= 0:
        return None
    tolerance = regular * _PRICE_TOLERANCE
    if discount_rate is not None and 0 < discount_rate < 100:
        target = regular * (1 - discount_rate / 100)
        if not candidates:
            return target.quantize(Decimal("0.01"))
        best = min(candidates, key=lambda c: abs(c - target))
        return best.quantize(Decimal("0.01")) if abs(best - target) <= tolerance else None
    if not candidates:
        return None
    if len(candidates) == 2 and abs(candidates[0] - candidates[1]) > tolerance:
        return None
    return candidates[0].quantize(Decimal("0.01"))


# "2 ב 100", "ב2- ב 20 שח", "3 ק ג ב  40", "2קג ב42שח", "4ב10" -> (qty, total)
_DESC_DEAL_RE = re.compile(
    r"(?<![\d.])(\d{1,2})\s*(?:ק\"?ג|ק ג|קג|ק|יח'?)?\s*-?\s*ב\s*-?\s*(\d+(?:\.\d+)?)(?![\d%])"
)


def _desc_unit_price(desc: str) -> Optional[Decimal]:
    """Per-unit price stated by a multi-buy description ("2 ב 100" -> 50)."""
    m = _DESC_DEAL_RE.search(_norm(desc))
    if not m:
        return None
    qty, total = Decimal(m.group(1)), _dec(m.group(2))
    if qty < 2 or total is None or total <= 0:
        return None
    return total / qty


def _extract_meat_promos(
    chain_name: str,
    store_id: str,
    promo_root: ET.Element,
    item_index: dict[str, _FeedItem],
) -> list[MeatPromo]:
    """Shared promo-filtering logic used by every chain."""
    results: list[MeatPromo] = []
    promos_container = promo_root.find("Promotions")
    if promos_container is None:
        promos_container = promo_root.find(".//Promotions")
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
        if _NOT_A_PROMO_PAT.search(_norm(desc)):
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
            fields = {c.tag: (c.text or "").strip() for c in pi}
            code = fields.get("ItemCode", "")
            if not code or code == "0" * len(code):
                continue
            item = item_index.get(code)
            if item is None:
                continue
            if not _is_meat_product(item.name, context=desc, item=item):
                continue
            key = (code, desc)
            if key in seen:
                continue
            seen.add(key)

            discount_rate = _dec(fields.get("DiscountRate"))
            bundle = _dec(fields.get("DiscountedPrice"))
            min_qty = _dec(fields.get("MinQty"))
            now = _per_unit_promo_price(
                item.price, discount_rate, bundle,
                _dec(fields.get("DiscountedPricePerMida")), item.unit_price, min_qty,
            )
            if now is None or item.price is None or now >= item.price:
                continue  # inconsistent units / no real saving
            stated = _desc_unit_price(desc)
            if stated is not None and abs(stated - now) > item.price * _PRICE_TOLERANCE:
                # The feed's numbers contradict the deal it advertises
                # (seen live: "2 קג ב 60" published as 15/kg in some stores).
                continue
            weighted = item.weighted or fields.get("bIsWeighted", "") == "1"
            multi = (
                not weighted and min_qty is not None and min_qty > 1
                and min_qty == min_qty.to_integral_value() and bundle is not None
            )

            results.append(MeatPromo(
                chain=chain_name,
                store_id=store_id,
                item_code=code,
                item_name=item.name,
                regular_price=item.price,
                discounted_price=now,
                discount_rate=discount_rate,
                promotion_description=desc,
                end_date=end[:10] if end else None,
                min_qty=min_qty.to_integral_value() if multi else None,
                bundle_price=bundle if multi else None,
                is_weighted=weighted,
                unit_kg=_unit_kg(item, weighted),
            ))
    return results


# ---- Orchestrator ----

def scan_and_report() -> SupermarketReport:
    """Scan every configured chain in parallel and return a combined report."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    cities = _enabled_cities()
    report = SupermarketReport(
        status="ok", cities=list(cities), regions=list(_active_regions(cities)),
    )

    tasks: dict[str, object] = {_SHUFERSAL_NAME: scan_shufersal}
    for user, name in _PUBLISHEDPRICES_CHAINS:
        tasks[name] = (lambda u=user, n=name: scan_publishedprices_chain(u, n))
    for sub, name in _BINA_CHAINS:
        tasks[name] = (lambda s=sub, n=name: scan_bina_chain(s, n))
    log.info(
        "Running supermarket meat scan across %d chains (cities: %s)",
        len(tasks), ", ".join(cities),
    )

    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        futures = {pool.submit(task): name for name, task in tasks.items()}  # type: ignore[arg-type]
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                promos, used = fut.result()
            except ChainScanError as exc:
                log.warning("%s: %s", name, exc.reason)
                (report.chains_failed if exc.failed else report.chains_empty)[name] = exc.reason
                continue
            except Exception as exc:
                log.error("%s: chain scan failed: %s: %s", name, type(exc).__name__, exc)
                report.chains_failed[name] = f"שגיאה: {type(exc).__name__}"
                continue
            if not promos:
                log.warning("%s: no meat items returned", name)
                report.chains_empty[name] = "לא נמצאו מוצרי בשר"
                continue
            report.promos.extend(promos)
            report.chains_scanned.append(name)
            for store in used:
                report.stores_scanned.append(f"{name} – {store.label} ({store.city})")
                report.stores_by_region[store.region] = (
                    report.stores_by_region.get(store.region, 0) + 1
                )
                report.stores_by_city[store.city] = report.stores_by_city.get(store.city, 0) + 1
    report.chains_scanned.sort()
    report.stores_scanned.sort()
    report.chains_failed = dict(sorted(report.chains_failed.items()))
    report.chains_empty = dict(sorted(report.chains_empty.items()))
    report.stores_by_city = {
        c: report.stores_by_city[c] for c in _CITY_ORDER if c in report.stores_by_city
    }
    _finalize_report(report)
    if report.unpriced_count:
        log.info(
            "%d deals left out of the ranking: no reliable price per kg (%s)",
            report.unpriced_count,
            ", ".join(f"{c} {n}" for c, n in report.unpriced_by_category.items()),
        )
    if not report.chains_scanned:
        report.status = "fetch_failed"
        report.error = "לא נמצאו מבצעים באף רשת בערים שנבחרו"
    return report


def _finalize_report(report: SupermarketReport) -> None:
    """Dedupe, drop implausible rows and keep the cheapest items of each category."""
    # Dedupe: the same promotion runs in many stores of a chain and often
    # covers several item variants (different brands of the same cut). Keep
    # one row per (chain, promotion description, promo price per kg) — or,
    # without a price per kg, per rounded percent — and remember the regions /
    # cities / branches behind it. An item at its regular price is one row
    # per (chain, item name, price per kg).
    seen: dict[tuple, MeatPromo] = {}
    seen_stores: dict[tuple, set[str]] = {}
    for p in report.promos:
        per_kg = p.price_per_kg
        if p.is_promo:
            pct = p.savings_pct
            if pct is None or pct <= 0 or pct >= _MAX_SANE_DISCOUNT_PCT:
                continue
            key = (p.chain, p.promotion_description, per_kg if per_kg is not None else f"{int(pct)}%")
        else:
            if per_kg is None:
                continue  # cannot be compared, and it is not a deal worth counting
            key = (p.chain, "", p.item_name, per_kg)
        kept = seen.get(key)
        if kept is None:
            seen[key] = kept = p
            seen_stores[key] = set()
            kept.stores = list(p.stores)
        elif p.stores:
            for branch in p.stores:
                if branch not in kept.stores:
                    kept.stores.append(branch)
        seen_stores[key].add(p.store_id)
        for region in p.regions:
            if region not in kept.regions:
                kept.regions.append(region)
        if p.city and p.city not in kept.cities:
            kept.cities.append(p.city)
    order = {region: i for i, region in enumerate(_REGION_ORDER)}
    for key, kept in seen.items():
        kept.store_count = len(seen_stores[key])
        kept.regions.sort(key=lambda r: order.get(r, len(order)))
        kept.category = _categorize(kept)

    for region in _REGION_ORDER:
        report.promos_by_region[region] = sum(1 for p in seen.values() if region in p.regions)

    # One block per category, in the fixed category order, cheapest first.
    # A category is shown only when it has at least one deal; inside it the
    # deals and the regular-priced items are ranked together by price per kg.
    by_category: dict[str, list[MeatPromo]] = {}
    for p in seen.values():
        by_category.setdefault(p.category, []).append(p)
    labels = [label for label, _kws in _CATEGORIES]
    labels += sorted(c for c in by_category if c not in labels)
    top_n = _top_per_category()
    ranked: list[MeatPromo] = []
    for label in labels:
        pool = by_category.get(label)
        if not pool:
            continue
        deals = [p for p in pool if p.is_promo]
        if not deals:
            continue  # nothing on sale here: no deal for a regular price to beat
        if _is_catch_all(label):
            report.unranked_by_category[label] = len(deals)
            continue
        unpriced = sum(1 for p in deals if p.price_per_kg is None)
        if unpriced:
            report.unpriced_by_category[label] = unpriced
        priced = [p for p in pool if p.price_per_kg is not None]
        if any(p.is_promo for p in priced):
            report.promos_by_category[label] = len(priced)
            ranked.extend(sorted(priced, key=_price_rank_key)[:top_n])
    report.promos = ranked


def _price_rank_key(p: MeatPromo) -> tuple:
    """Cheapest price per kg first; ties: a deal before a regular price, the
    bigger discount first, then by name."""
    return (p.price_per_kg, -(p.savings_pct or Decimal("0")), p.chain, p.item_name)


# ---- State persistence (so we can diff tomorrow if desired) ----

def _state_file() -> Path:
    return data_dir() / "supermarkets-state.json"


def _promo_to_dict(p: MeatPromo) -> dict:
    return {
        **asdict(p),
        "regular_price": str(p.regular_price) if p.regular_price is not None else None,
        "discounted_price": str(p.discounted_price) if p.discounted_price is not None else None,
        "discount_rate": str(p.discount_rate) if p.discount_rate is not None else None,
        "min_qty": str(p.min_qty) if p.min_qty is not None else None,
        "bundle_price": str(p.bundle_price) if p.bundle_price is not None else None,
        "unit_kg": str(p.unit_kg) if p.unit_kg is not None else None,
        "price_per_kg": str(p.price_per_kg) if p.price_per_kg is not None else None,
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

def _print_chain_problems(report: SupermarketReport) -> None:
    for title, chains in (("Chains failed", report.chains_failed), ("Chains empty", report.chains_empty)):
        if chains:
            console.print(f"[yellow]{title}: " + ", ".join(f"{c} ({r})" for c, r in chains.items()) + "[/yellow]")


def print_report(report: SupermarketReport) -> None:
    if report.status != "ok":
        console.print(f"[red]Supermarket scan failed: {report.status}[/red]  {report.error or ''}")
        _print_chain_problems(report)
        return
    console.print(f"[dim]Chains scanned: {', '.join(report.chains_scanned)}[/dim]")
    if report.cities:
        console.print(f"[dim]Cities: {', '.join(report.cities)}[/dim]")
    _print_chain_problems(report)
    if report.unpriced_count:
        console.print(f"[dim]{report.unpriced_count} deals without a price per kg were not ranked.[/dim]")
    if report.unranked_count:
        console.print(
            f"[dim]{report.unranked_count} deals in catch-all groups not shown: "
            + ", ".join(f"{c} ({n})" for c, n in report.unranked_by_category.items()) + "[/dim]"
        )
    if not report.promos:
        console.print("[dim]No meat promos found today.[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("Category", width=12)
    table.add_column("Chain", width=10)
    table.add_column("Stores", width=16)
    table.add_column("Item")
    table.add_column("Now/kg")
    table.add_column("Was/kg")
    table.add_column("Off")
    table.add_column("Deal")
    previous = None
    for p in report.promos:
        category = p.category or _categorize(p)
        if previous is not None and category != previous:
            table.add_section()
        now = f"₪{p.price_per_kg}" if p.price_per_kg is not None else "—"
        was = f"₪{p.regular_per_kg}" if p.regular_per_kg is not None else "—"
        off = f"{p.savings_pct}%" if p.savings_pct is not None else "—"
        deal = " · ".join(x for x in (p.deal_text, p.pack_text) if x)
        table.add_row(
            category if category != previous else "", p.chain, ", ".join(p.stores or p.cities),
            p.item_name[:50], now, was, off, deal or p.promotion_description[:40],
        )
        previous = category
    console.print(table)
