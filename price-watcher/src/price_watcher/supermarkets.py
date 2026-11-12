"""Israeli supermarket meat-discount scanner.

Uses the government-mandated price transparency XML feeds. Each chain
publishes PriceFull (every item + price) and PromoFull (active promotions)
files per store, refreshed daily.

Scope: only stores in the cities enabled by `PW_MEAT_REGIONS` (default:
Herzliya and Ramat HaSharon). Every city in `_REGIONS` (the Sharon, Tel Aviv
and the Rishon LeZion area) is still known to the matcher, so any of them can
be switched back on from `.env`. Each chain's Stores file maps store ids to
cities; every store of every chain inside the enabled cities is scanned
(`PW_STORES_PER_REGION` can cap that per chain for a faster run).

Promotions are filtered down to fresh meat / poultry cuts and sorted into
categories of one cut of one animal ("חזה הודו", "צלעות טלה", ...). For each
category the report keeps the `PW_MEAT_TOP_PER_CATEGORY` cheapest deals
(default 5), by promo price normalised per kg where the feed allows it.
Catch-all categories ("עוף – אחר", "בשר – אחר", "אחר") mix unrelated cuts, so
they are listed unranked at the end.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

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

# Bareket (ברקת) does not publish on url.publishedprices.co.il. Its files are
# served by the "Bina Projects" price portal (one sub-domain per chain). The
# source is configurable with PW_BAREKET_SOURCE:
#   bina:<portal base URL>          (default below)
#   publishedprices:<username>      (if the chain moves to the Cerberus host)
#   off                             (skip the chain)
_BAREKET_NAME = "ברקת"
_BAREKET_DEFAULT_SOURCE = "bina:https://superbareket.binaprojects.com"


# ---- Text normalisation ----

_HEB = "א-ת"


def _norm(text: Optional[str]) -> str:
    """Unify Hebrew punctuation so spellings match however a feed typed them.

    Gershayim / geresh (״ ׳) and typographic quotes become ASCII " and ',
    hyphens / dashes / maqaf become spaces, runs of whitespace collapse.
    """
    t = text or ""
    t = (
        t.replace("״", '"').replace("“", '"').replace("”", '"')
        .replace("''", '"')
        .replace("׳", "'").replace("’", "'").replace("‘", "'").replace("`", "'")
    )
    t = re.sub(r"[-‐-―־]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# ---- Regions ----

@dataclass(frozen=True)
class _City:
    code: Optional[str]                  # CBS locality code, None = name match only
    slug: str                            # ASCII key for PW_MEAT_REGIONS
    name: str                            # display name (always a spelling too)
    spellings: tuple[str, ...] = ()      # other ways feeds write the city
    neighbourhoods: tuple[str, ...] = () # names that only occur inside this city


# Every known city, grouped into regions: (region name, region slug, cities).
# The code is matched against the Stores file's <City>; spellings and
# neighbourhoods are a fallback for chains that publish City=0 and only name
# the city inside the store name / address.
_REGIONS: tuple[tuple[str, str, tuple[_City, ...]], ...] = (
    ("השרון", "sharon", (
        _City("7400", "netanya", "נתניה"),
        _City("6400", "herzliya", "הרצליה", ("הרצלייה",),
              ("נוף ים", "נווה עמל", "יד התשעה", "נווה ישראל")),
        _City("8700", "raanana", "רעננה"),
        _City("6900", "kfar-saba", "כפר סבא", ('כפ"ס',)),
        _City("9700", "hod-hasharon", "הוד השרון"),
        _City("2650", "ramat-hasharon", "רמת השרון", ('רמה"ש',),
              ("מורשה", "נווה מגן", "נווה גן")),
        _City("168", "kfar-yona", "כפר יונה"),
        _City("182", "even-yehuda", "אבן יהודה"),
        _City("154", "tel-mond", "תל מונד"),
        _City("195", "kadima-zoran", "קדימה", ("צורן",)),
        _City("171", "pardesiya", "פרדסיה"),
        _City("2720", "tira", "טירה"),
        _City("2730", "taibe", "טייבה"),
        _City("346", "glil-yam", "גליל ים"),
        _City(None, "kalansawe", "קלנסווה"),
        _City(None, "kochav-yair", "כוכב יאיר"),
    )),
    ("תל אביב", "tel-aviv", (
        _City("5000", "tel-aviv", "תל אביב", ('ת"א', "תל אביב יפו"),
              ("יד אליהו", "רמת אביב", "נווה צדק", "פלורנטין", "רמת החייל", "צהלה", "בבלי")),
    )),
    ("ראשון לציון והסביבה", "rishon", (
        # No bare "ראשון" ("first"): it matches far too much.
        _City("8300", "rishon-lezion", "ראשון לציון", ('ראשל"צ', "ראשלצ"),
              ("רמת אליהו", "נחלת יהודה")),
        _City("7200", "ness-ziona", "נס ציונה"),
        _City("2530", "beer-yaakov", "באר יעקב"),
        _City("466", "beit-dagan", "בית דגן"),
        _City("6600", "holon", "חולון"),
        _City("6200", "bat-yam", "בת ים"),
    )),
)

# Scanned when PW_MEAT_REGIONS is not set (issue #50: for now only these two).
_DEFAULT_MEAT_AREAS: tuple[str, ...] = ("herzliya", "ramat-hasharon")

_REGION_ORDER: tuple[str, ...] = tuple(name for name, _slug, _cities in _REGIONS)

_ALL_CITIES: tuple[tuple[str, _City], ...] = tuple(
    (region, city) for region, _slug, cities in _REGIONS for city in cities
)
_CITY_ORDER: tuple[str, ...] = tuple(city.name for _region, city in _ALL_CITIES)

# code -> (region, city display name)
_CODE_TO_REGION: dict[str, tuple[str, str]] = {
    city.code: (region, city.name) for region, city in _ALL_CITIES if city.code is not None
}

# (compiled pattern, region, city display name, spelling length), longest
# spelling first. An optional one-letter prefix (ב / ו / ה / ל) is allowed:
# "ברמת השרון", "וחולון".
_NAME_TO_REGION: list[tuple[re.Pattern, str, str, int]] = sorted(
    (
        (
            re.compile(
                rf"(?<![{_HEB}])[בוהל]?" + re.escape(_norm(spelling)) + rf"(?![{_HEB}\"'])"
            ),
            region,
            city.name,
            len(_norm(spelling)),
        )
        for region, city in _ALL_CITIES
        for spelling in (city.name, *city.spellings, *city.neighbourhoods)
    ),
    key=lambda t: -t[3],
)

# A city spelling right after one of these is a street name ("רחוב נתניה").
_STREET_BEFORE = re.compile(
    rf"(?:^|[^{_HEB}])(?:רחוב|רח'|רח|דרך|דר'|שדרות|שדרת|שד'|שד|סמטת|סמ'|כיכר|ככר)\.?\s*$"
)
# ... and one followed by a house number is a street address ("תל אביב 11").
_NUMBER_AFTER = re.compile(r"^\s*\d")


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _stores_per_region() -> int:
    """Max stores per chain per region. 0 (default) = every store."""
    return _env_int("PW_STORES_PER_REGION", 0)


def _top_per_category() -> int:
    """How many of the cheapest deals to keep per meat category."""
    return _env_int("PW_MEAT_TOP_PER_CATEGORY", 5) or 5


# Rows listed per catch-all category (they are not ranked, so keep it short).
_UNRANKED_LIMIT = 10


def _enabled_cities() -> frozenset[str]:
    """City display names to scan, from PW_MEAT_REGIONS.

    Comma-separated city or region names (Hebrew or the ASCII slugs in
    `_REGIONS`, e.g. "herzliya,ramat-hasharon", "sharon,tel-aviv"), or "all".
    """
    raw = os.environ.get("PW_MEAT_REGIONS", "").strip()
    tokens = [t for t in (s.strip() for s in re.split(r"[,;]", raw)) if t] if raw else []
    if not tokens:
        tokens = list(_DEFAULT_MEAT_AREAS)
    out: set[str] = set()
    for tok in tokens:
        key = _norm(tok).lower()
        if key in ("all", "*", "הכל", "הכול"):
            return frozenset(_CITY_ORDER)
        matched = False
        for region, rslug, cities in _REGIONS:
            if key in (_norm(rslug), _norm(region)):
                out.update(c.name for c in cities)
                matched = True
                continue
            for city in cities:
                if key in (_norm(city.slug), _norm(city.name)):
                    out.add(city.name)
                    matched = True
        if not matched:
            log.warning("PW_MEAT_REGIONS: unknown city / region %r ignored", tok)
    if not out:
        log.warning("PW_MEAT_REGIONS matched nothing; using the default %s", _DEFAULT_MEAT_AREAS)
        return _enabled_cities_default()
    return frozenset(out)


def _enabled_cities_default() -> frozenset[str]:
    return frozenset(
        city.name for _region, city in _ALL_CITIES if city.slug in _DEFAULT_MEAT_AREAS
    )


def scan_area_label() -> str:
    """Enabled cities in display order, e.g. "הרצליה, רמת השרון"."""
    enabled = _enabled_cities()
    return ", ".join(name for name in _CITY_ORDER if name in enabled)


@dataclass
class StoreInfo:
    store_id: int
    name: str
    city: str
    region: str

    @property
    def label(self) -> str:
        return self.name or self.city


def _find_city(text: str, *, address: bool) -> Optional[tuple[str, str]]:
    """First (region, city) whose spelling appears in `text`, longest spelling first.

    A spelling preceded by a street word ("רחוב", "דרך", "שד'" ...) is a street
    name, not the city. In an address, so is a spelling followed by a house
    number ("תל אביב 11").
    """
    text = _norm(text)
    if not text:
        return None
    for pat, region, city_name, _len in _NAME_TO_REGION:
        for m in pat.finditer(text):
            if _STREET_BEFORE.search(text[: m.start()]):
                continue
            if address and _NUMBER_AFTER.match(text[m.end():]):
                continue
            return region, city_name
    return None


def _region_for_store(store: dict[str, str]) -> Optional[tuple[str, str]]:
    """Return (region, city) if the store is in a known city (enabled or not)."""
    city = (store.get("city") or "").strip()
    if city.isdigit() and int(city) != 0:
        return _CODE_TO_REGION.get(str(int(city)))
    if city and not city.isdigit():
        return _find_city(city, address=False)
    # City=0 / missing: the store name first, the address only as a fallback.
    return (
        _find_city(store.get("storename", ""), address=False)
        or _find_city(store.get("address", ""), address=True)
    )


def _maybe_decompress(raw: bytes) -> bytes:
    """Feeds come as gzip, zip (one member) or plain XML."""
    if raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)
        except (OSError, EOFError):
            return raw
    if raw[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                names = [n for n in zf.namelist() if not n.endswith("/")]
                if names:
                    return zf.read(names[0])
        except (zipfile.BadZipFile, OSError):
            return raw
    return raw


def _local(tag: object) -> str:
    """Lower-case tag name without any XML namespace."""
    return str(tag).rsplit("}", 1)[-1].lower()


def _fields(el: ET.Element) -> dict[str, str]:
    return {_local(c.tag): (c.text or "").strip() for c in el}


def _dec(text: Optional[str]) -> Optional[Decimal]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def _parse_stores(raw: bytes, cities: Optional[frozenset[str]] = None) -> list[StoreInfo]:
    """Parse a Stores XML file and keep only stores in the enabled cities."""
    enabled = cities if cities is not None else _enabled_cities()
    try:
        root = ET.fromstring(_maybe_decompress(raw))
    except ET.ParseError as exc:
        log.warning("Stores XML parse failed: %s", exc)
        return []
    out: list[StoreInfo] = []
    for el in root.iter():
        if _local(el.tag) != "store":
            continue
        fields = _fields(el)
        sid = fields.get("storeid", "")
        if not sid.isdigit():
            continue
        hit = _region_for_store(fields)
        if hit is None:
            continue
        region, city = hit
        if city not in enabled:
            continue
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


# <chain>-<subchain>-<store>-<YYYYMMDD>[-]<HHMM[SS]>  (publishedprices)
_FEED_FILE_RE = re.compile(r"-(\d{3})-(\d{3,4})-(\d{8})-?(\d{4,6})")
# <chain 13 digits>-<store>-<YYYYMMDDHHMM[SS]>  (no sub-chain)
_FEED_FILE_RE_SHORT = re.compile(r"\d{13}-(\d{3,4})-(\d{12,14})(?!\d)")


def _store_and_stamp(name: str) -> Optional[tuple[int, str]]:
    base = name.split("?")[0].rsplit("/", 1)[-1]
    m = _FEED_FILE_RE.search(base)
    if m:
        return int(m.group(2)), m.group(3) + m.group(4)
    m = _FEED_FILE_RE_SHORT.search(base)
    if m:
        return int(m.group(1)), m.group(2)
    return None


def _latest_by_store(names: list[str]) -> dict[int, str]:
    """Map store id -> newest feed filename/URL for that store."""
    best: dict[int, tuple[str, str]] = {}
    for name in names:
        parsed = _store_and_stamp(name)
        if parsed is None:
            continue
        sid, stamp = parsed
        if sid not in best or stamp > best[sid][0]:
            best[sid] = (stamp, name)
    return {sid: name for sid, (_stamp, name) in best.items()}


# ---- Hebrew word matching ----

@lru_cache(maxsize=None)
def _word_re(word: str) -> re.Pattern:
    """`word` as a whole Hebrew word, with an optional ו / ה / ב prefix."""
    return re.compile(rf"(?<![{_HEB}])[והב]?{re.escape(_norm(word))}(?![{_HEB}])")


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    return any(_word_re(w).search(text) for w in words)


# ---- Fresh / frozen ----

_FROZEN_WORDS = ("קפוא", "קפואה", "קפואים", "קפואות", "מוקפא", "מוקפאת", "מוקפאים")
_FRESH_WORDS = ("טרי", "טריה", "טרייה", "טריים", "טריות")


def _freshness(name: str) -> str:
    """'frozen', 'fresh' or '' when the name does not say."""
    t = _norm(name)
    if _has_any(t, _FROZEN_WORDS):
        return "frozen"
    if _has_any(t, _FRESH_WORDS):
        return "fresh"
    return ""


FRESHNESS_HE = {"frozen": "קפוא", "fresh": "טרי", "": ""}


# ---- Categories: one cut of one animal ----

_SPECIES_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("turkey", ("הודו",)),
    ("goose", ("אווז", "אווזים")),
    ("duck", ("ברווז", "ברווזים")),
    ("lamb", ("טלה", "טלאים", "כבש", "כבשים")),
    ("veal", ("עגל", "עגלים")),
    ("chicken", ("עוף", "עופות", "פרגית", "פרגיות", "פולקע", "פולקות", "קורקבן", "קורקבנים")),
    ("beef", ("בקר", "אנגוס", "וואגיו", "ואגיו")),
)

# Poultry cuts sold without a species word ("כנפיים טרי") are chicken.
_POULTRY_ONLY_WORDS = (
    "כנפיים", "כנפי", "כנף", "כרעיים", "כרעי", "שוקיים", "שוקי",
    "ירכיים", "ירכי", "לבבות", "כבד", "חזה",
)

_CATEGORY_OTHER = "אחר"
_CATCH_ALL = {
    "chicken": "עוף – אחר", "turkey": "עוף – אחר", "goose": "עוף – אחר", "duck": "עוף – אחר",
    "beef": "בשר – אחר", "veal": "בשר – אחר", "lamb": "בשר – אחר",
}
CATCH_ALL_CATEGORIES: tuple[str, ...] = ("עוף – אחר", "בשר – אחר", _CATEGORY_OTHER)

_Rule = tuple[Optional[str], tuple[str, ...]]  # (category, words); None = catch-all


def _poultry_cuts(sp: str) -> tuple[_Rule, ...]:
    return (
        (f"גרון {sp}", ("גרון", "גרונות", "צוואר", "צווארים", "צווארי")),
        (f"כבד {sp}", ("כבד", "כבדים", "כבדי")),
        (f"לבבות {sp}", ("לבבות", "לב")),
        (f"קורקבן {sp}", ("קורקבן", "קורקבנים")),
        (f"כנפיים {sp}", ("כנפיים", "כנפי", "כנף")),
        (f"כרעיים {sp}", ("כרעיים", "כרעי", "כרע")),
        (f"שוקיים {sp}", ("שוקיים", "שוקי", "שוק")),
        (f"ירכיים {sp}", ("ירכיים", "ירכי", "ירך")),
        (f"{sp} טחון", ("טחון", "טחונה")),
        (f"חזה {sp}", ("חזה", "חזות", "פילה")),
    )


# (beef category, veal base name, words). Order matters: specific first.
_RED_CUTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("פילה מיניון", "פילה מיניון", ("פילה מיניון", "פילה מניון")),
    ("פיקניה", "פיקניה", ("פיקניה", "פיקאניה")),
    ("אנטריקוט", "אנטריקוט", ("אנטריקוט",)),
    ("סינטה", "סינטה", ("סינטה",)),
    ("טי בון", "טי בון", ("טי בון", "ט בון", "טיבון")),
    ("צ'אק", "צ'אק", ("צ'אק", "צ'ק")),
    ("פלאנק", "פלאנק", ("פלאנק",)),
    ("ראמפ", "ראמפ", ("ראמפ",)),
    ("אסאדו", "אסאדו", ("אסאדו", "אסדו")),
    ("בריסקט", "בריסקט", ("בריסקט", "בריסקיט")),
    ("אוסובוקו", "אוסובוקו", ("אוסובוקו", "אוסו בוקו")),
    ("שייטל", "שייטל", ("שייטל", "שיטל")),
    ("רוסטביף", "רוסטביף", ("רוסטביף", "רוסט ביף")),
    ("דנבר", "דנבר", ("דנבר",)),
    ("לשון בקר", "לשון", ("לשון",)),
    ("מוח בקר", "מוח", ("מוח",)),
    ("לב בקר", "לב", ("לב", "לבבות")),
    ("כבד בקר", "כבד", ("כבד",)),
    ("קרביים", "קרביים", ("קרביים",)),
    ("בשר ראש", "בשר ראש", ("ראש",)),
    ("גרון בקר", "גרון", ("גרון", "צוואר")),
    ("צלעות בקר", "צלעות", ("צלעות", "צלע")),
    ("כתף בקר", "כתף", ("כתף",)),
    ("שריר בקר", "שריר", ("שריר",)),
    ("חגורה", "חגורה", ("חגורה",)),
    ("גולש", "גולש", ("גולש", "גולאש")),
    ("בקר טחון", "טחון", ("טחון", "טחונה")),
    ("פילה בקר", "פילה", ("פילה",)),
)

# Red-meat words that imply beef when the name has no species word. Words
# that also name a poultry cut (כבד, לב, גרון, חזה, טחון) are left out.
_BEEF_HINTS: tuple[str, ...] = tuple(
    w for _b, _v, words in _RED_CUTS for w in words
    if w not in ("לב", "לבבות", "כבד", "גרון", "צוואר", "טחון", "טחונה")
) + ("בשר", "סטייק", "צלי")

_NUMBERED_CUT = re.compile(r"(?:נתח|מס'?|מספר|#)\s*(\d{1,2})(?!\d)")

_CUT_RULES: dict[str, tuple[_Rule, ...]] = {
    "beef": tuple((beef, words) for beef, _veal, words in _RED_CUTS),
    "veal": tuple(
        (f"{veal} עגל", words) for _beef, veal, words in _RED_CUTS
    ),
    "lamb": (
        ("צוואר טלה", ("צוואר", "צווארון", "גרון")),
        ("כתף טלה", ("כתף",)),
        ("צלעות טלה", ("צלעות", "צלע", "קרה", "קארה", "פרנץ'", "פרנץ")),
        ("חזה טלה", ("חזה",)),
        ("רבע טלה", ("רבע",)),
        ("שוק טלה", ("שוק", "שוקיים", "רגל")),
        ("טלה טחון", ("טחון", "טחונה")),
        ("כבד טלה", ("כבד",)),
    ),
    "chicken": (
        # Wing tips / half chickens are priced nothing like wings / whole birds.
        (None, ("קצוות כנפיים", "קצות כנפיים", "קצה כנף", "קצוות", "חצי עוף")),
        ("פרגיות", ("פרגית", "פרגיות")),
        ("פולקע", ("פולקע", "פולקות")),
        *_poultry_cuts("עוף"),
        ("עוף שלם", ("שלם", "שלמה", "שלמים")),
    ),
    "turkey": (
        ("שישליק הודו", ("שישליק",)),
        ("הודו אדום", ("אדום", "אדומה")),
        *_poultry_cuts("הודו"),
    ),
    "goose": _poultry_cuts("אווז"),
    "duck": _poultry_cuts("ברווז"),
}

# Display order: red meat, then poultry; catch-alls last.
CATEGORY_ORDER: tuple[str, ...] = tuple(dict.fromkeys(
    cat
    for sp in ("beef", "veal", "lamb", "chicken", "turkey", "goose", "duck")
    for cat, _words in _CUT_RULES[sp]
    if cat is not None
))


def _species(text: str) -> Optional[str]:
    t = _norm(text)
    for key, words in _SPECIES_WORDS:
        if _has_any(t, words):
            return key
    if _has_any(t, _POULTRY_ONLY_WORDS):
        return "chicken"
    if _has_any(t, _BEEF_HINTS):
        return "beef"
    return None


def _categorize_text(text: str) -> str:
    t = _norm(text)
    sp = _species(t)
    if sp is None:
        return _CATEGORY_OTHER
    for cat, words in _CUT_RULES[sp]:
        if _has_any(t, words):
            return cat or _CATCH_ALL[sp]
    if sp in ("beef", "veal"):
        m = _NUMBERED_CUT.search(t)
        if m:
            return f"נתח מס' {int(m.group(1))}" + (" עגל" if sp == "veal" else "")
    return _CATCH_ALL[sp]


def _categorize(promo: "MeatPromo") -> str:
    """Category (one cut of one animal) for a promo, from its item name."""
    cat = _categorize_text(promo.item_name)
    if cat == _CATEGORY_OTHER and promo.promotion_description:
        cat = _categorize_text(promo.promotion_description)
    return cat


def is_ranked_category(category: str) -> bool:
    return category.split(" · ")[0] not in CATCH_ALL_CATEGORIES


def _category_label(promo: "MeatPromo") -> str:
    """Category, split by fresh / frozen for ranked categories."""
    cat = _categorize(promo)
    if cat not in CATCH_ALL_CATEGORIES and promo.freshness == "frozen":
        return f"{cat} · קפוא"
    return cat


def _category_sort_key(label: str) -> tuple:
    base, _sep, suffix = label.partition(" · ")
    frozen = 1 if suffix else 0
    if base in CATCH_ALL_CATEGORIES:
        return (2, CATCH_ALL_CATEGORIES.index(base), frozen, label)
    if base in CATEGORY_ORDER:
        return (0, CATEGORY_ORDER.index(base), frozen, label)
    return (1, 0, frozen, label)  # numbered cuts


# ---- Hebrew keyword classifier (is it a fresh meat cut at all?) ----

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
    "שייטל", "שיטל", "רוסטביף", "דנבר",
    # Organ / utility cuts
    "מוח", "גרון", "קורקבן", "קרביים", "כרעיים",
    "פולקע", "פולקות", "לשון",
    # Fresh poultry parts
    "עוף", "הודו", "פרגית", "פרגיות", "אווז", "ברווז",
    "שוקיים", "כנפיים", "ירכיים", "חזה עוף", "חזה הודו", "ירך עוף", "שוק עוף",
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
    # Dairy / cheese ("סטרצ'טלה" contains "טלה")
    "חלבי", "גבינה", "גאודה", "גאודת", "אמנטל", "מוצרלה", "צ'דר",
    "קוטג", "לבנה",
    "סטרצ'טלה", "סטרצטלה", "סטרציאטלה", "סטרצ'יאטלה", "בוראטה", "בורטה",
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
_EXCLUDE_NORM: tuple[str, ...] = tuple(dict.fromkeys(_norm(k) for k in _EXCLUDE_KEYWORDS))


# A Hebrew-safe word-boundary: the keyword must be preceded/followed by a non-letter
# (space, punctuation, digit, or start/end). Prevents "לב" matching "שלב".
def _build_keyword_pattern() -> re.Pattern:
    escaped = sorted(
        {re.escape(_norm(k)) for k in _MEAT_KEYWORDS}, key=len, reverse=True
    )
    return re.compile(
        rf"(?:^|[^{_HEB}])(?:" + "|".join(escaped) + rf")(?:$|[^{_HEB}])",
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
    combined = _norm(f"{name} {context}")
    for kw in _EXCLUDE_NORM:
        if kw in combined:
            return False
    return bool(_MEAT_PAT.search(_norm(name)))


# ---- Data model ----

_CENT = Decimal("0.01")


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
    end_date: Optional[str] = None                    # YYYY-MM-DD
    store_name: str = ""
    city: str = ""
    regions: list[str] = field(default_factory=list)  # one region per promo row
    cities: list[str] = field(default_factory=list)   # cities it was seen in
    store_count: int = 1                              # stores it was seen in
    store_names: list[str] = field(default_factory=list)
    weighted: bool = False                            # price is per kg
    pack_kg: Optional[Decimal] = None                 # pack weight, when known
    min_qty: Optional[Decimal] = None                 # "N for X" promos
    category: str = ""                                # set by scan_and_report
    ranked: bool = True                               # False for catch-alls

    @property
    def region(self) -> str:
        return self.regions[0] if self.regions else ""

    @property
    def promo_price(self) -> Optional[Decimal]:
        """Price of one unit (or one kg, for weighed items) under the promo."""
        price = self.discounted_price
        if price is not None and price > 0:
            # "2 for ₪50": some feeds give the bundle total, not the unit price.
            if (
                self.min_qty is not None and self.min_qty > 1
                and self.regular_price is not None and price > self.regular_price
            ):
                price = price / self.min_qty
            return price.quantize(_CENT)
        rate = self.discount_rate
        if rate is not None and 0 < rate < 100 and self.regular_price:
            return (self.regular_price * (100 - rate) / 100).quantize(_CENT)
        return None

    @property
    def savings_pct(self) -> Optional[Decimal]:
        if self.discount_rate is not None and 0 < self.discount_rate:
            return self.discount_rate
        promo = self.promo_price
        if self.regular_price and promo is not None and self.regular_price > 0:
            delta = (self.regular_price - promo) / self.regular_price * 100
            return delta.quantize(Decimal("1"))
        return None

    @property
    def savings_amount(self) -> Optional[Decimal]:
        promo = self.promo_price
        if self.regular_price and promo is not None:
            return (self.regular_price - promo).quantize(_CENT)
        return None

    def _per_kg(self, price: Optional[Decimal]) -> Optional[Decimal]:
        if price is None:
            return None
        if self.weighted:
            return price.quantize(_CENT)
        if self.pack_kg:
            return (price / self.pack_kg).quantize(_CENT)
        return None

    @property
    def price_per_kg(self) -> Optional[Decimal]:
        """Promo price per kg, or None when the pack weight is unknown."""
        return self._per_kg(self.promo_price)

    @property
    def regular_per_kg(self) -> Optional[Decimal]:
        return self._per_kg(self.regular_price)

    @property
    def freshness(self) -> str:
        return _freshness(self.item_name)

    @property
    def end_date_display(self) -> str:
        """End date as DD/MM/YYYY, or '' when unknown."""
        if not self.end_date:
            return ""
        try:
            return datetime.strptime(self.end_date[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
        except ValueError:
            return self.end_date


@dataclass
class SupermarketReport:
    status: str
    chains_scanned: list[str] = field(default_factory=list)       # scan worked
    chains_without_promos: list[str] = field(default_factory=list)  # worked, 0 meat promos
    chains_no_stores: list[str] = field(default_factory=list)     # no branch in the area
    chains_failed: dict[str, str] = field(default_factory=dict)   # chain -> reason
    stores_scanned: list[str] = field(default_factory=list)  # "chain – store (city)"
    cities: list[str] = field(default_factory=list)          # enabled cities
    stores_by_city: dict[str, int] = field(default_factory=dict)
    promos_by_category: dict[str, int] = field(default_factory=dict)  # before the top-N cut
    top_per_category: int = 5
    # Ranked categories (cheapest first, at most top_per_category each), then
    # the unranked catch-alls. Rows of one category are contiguous.
    promos: list[MeatPromo] = field(default_factory=list)
    error: Optional[str] = None


def group_by_category(promos: list[MeatPromo]) -> list[tuple[str, bool, list[MeatPromo]]]:
    """(category, ranked, rows) in report order."""
    groups: dict[str, list[MeatPromo]] = {}
    for p in promos:
        groups.setdefault(p.category or _category_label(p), []).append(p)
    return [(cat, is_ranked_category(cat), rows) for cat, rows in groups.items()]


@dataclass
class _ChainResult:
    name: str
    status: str                                   # "ok" | "no_stores" | "failed"
    promos: list[MeatPromo] = field(default_factory=list)
    stores: list[StoreInfo] = field(default_factory=list)  # scanned successfully
    error: str = ""


# ---- Shared per-store scan ----

@dataclass(frozen=True)
class _Item:
    name: str
    price: Optional[Decimal]
    weighted: bool = False
    pack_kg: Optional[Decimal] = None


_WEIGHT_IN_NAME = re.compile(
    rf"(\d+(?:\.\d+)?)\s*(ק\"ג|קג|קילו|גרם|גר'?|ג')(?![{_HEB}])"
)


def _pack_kg(unit: str, qty: str, name: str) -> Optional[Decimal]:
    """Pack weight in kg from UnitQty + Quantity, else from the item name."""
    kg: Optional[Decimal] = None
    u = _norm(unit).lower().replace(".", "")
    q = _dec(qty)
    if q is not None and q > 0 and u:
        if any(k in u for k in ('ק"ג', "קג", "קילו", "kg")):
            kg = q
        elif "גר" in u or u in ("ג", "ג'", "g", "gr"):
            kg = q / 1000
    if kg is None:
        m = _WEIGHT_IN_NAME.search(_norm(name))
        if m:
            amount = _dec(m.group(1))
            if amount is not None and amount > 0:
                kg = amount if m.group(2) in ('ק"ג', "קג", "קילו") else amount / 1000
    if kg is not None and Decimal("0.01") <= kg <= Decimal("50"):
        return kg
    return None


def _build_item_index(pricefull_root: ET.Element) -> dict[str, _Item]:
    """Map ItemCode → _Item from a PriceFull XML tree (any tag case)."""
    index: dict[str, _Item] = {}
    for el in pricefull_root.iter():
        if _local(el.tag) not in ("item", "product"):
            continue
        f = _fields(el)
        code = f.get("itemcode", "")
        name = f.get("itemname") or f.get("itemnm") or f.get("manufactureritemdescription") or ""
        if not code or not name:
            continue
        weighted = f.get("bisweighted", "") == "1" or f.get("isweighted", "") == "1"
        index[code] = _Item(
            name=name,
            price=_dec(f.get("itemprice")),
            weighted=weighted,
            pack_kg=None if weighted else _pack_kg(
                f.get("unitqty", ""), f.get("quantity", ""), name
            ),
        )
    return index


def _scan_store(
    chain_name: str,
    store: StoreInfo,
    price_raw: Optional[bytes],
    promo_raw: Optional[bytes],
) -> Optional[list[MeatPromo]]:
    """Parse one store's PriceFull + PromoFull; None when the files are unusable."""
    if not price_raw or not promo_raw:
        log.warning("%s / %s: download failed", chain_name, store.label)
        return None
    try:
        price_root = ET.fromstring(_maybe_decompress(price_raw))
        promo_root = ET.fromstring(_maybe_decompress(promo_raw))
    except ET.ParseError as exc:
        log.warning("%s / %s: parse failed: %s", chain_name, store.label, exc)
        return None
    item_index = _build_item_index(price_root)
    promos = _extract_meat_promos(chain_name, f"{store.store_id:03d}", promo_root, item_index)
    for p in promos:
        p.store_name = store.name
        p.city = store.city
        p.regions = [store.region]
        p.cities = [store.city]
        p.store_names = [store.label]
    log.info(
        "%s / %s (%s, %s): %d items, %d meat promos",
        chain_name, store.label, store.city, store.region, len(item_index), len(promos),
    )
    return promos


def _scan_chain(
    chain_name: str,
    stores: list[StoreInfo],
    fetch: Callable[[StoreInfo], Optional[tuple[Optional[bytes], Optional[bytes]]]],
) -> _ChainResult:
    """Scan every store of the chain inside the enabled cities.

    `fetch(store)` returns (price_bytes, promo_bytes), or None when the store
    has no files. A chain whose stores parse fine but carry no meat promos is
    still a successful scan.
    """
    from concurrent.futures import ThreadPoolExecutor

    limit = _stores_per_region()
    candidates = _candidates_by_region(stores)
    if not candidates:
        log.info("%s: no stores in the enabled cities — skipped", chain_name)
        return _ChainResult(chain_name, "no_stores")
    todo = [
        store
        for region_stores in candidates.values()
        for store in (region_stores[:limit] if limit else region_stores)
    ]
    log.info("%s: scanning %d stores in the enabled cities", chain_name, len(todo))

    def _one(store: StoreInfo) -> tuple[StoreInfo, Optional[list[MeatPromo]]]:
        try:
            files = fetch(store)
            if files is None:
                log.warning("%s / %s: no price files listed", chain_name, store.label)
                return store, None
            return store, _scan_store(chain_name, store, *files)
        except Exception as exc:  # one bad store must not sink the chain
            log.warning("%s / %s: %s: %s", chain_name, store.label, type(exc).__name__, exc)
            return store, None

    promos: list[MeatPromo] = []
    scanned: list[StoreInfo] = []
    with ThreadPoolExecutor(max_workers=_STORE_WORKERS) as pool:
        for store, found in pool.map(_one, todo):
            if found is None:
                continue
            scanned.append(store)
            promos.extend(found)
    if not scanned:
        return _ChainResult(
            chain_name, "failed", error=f"לא ניתן היה להוריד את קובצי המחירים ({len(todo)} סניפים)"
        )
    return _ChainResult(chain_name, "ok", promos, scanned)


# Parallel store downloads per chain (12 chains run at once, so keep it modest).
_STORE_WORKERS = 3

# A "discount" at or above this is a data artefact (e.g. class-action credit
# rows with no price), not a real deal.
_MAX_SANE_DISCOUNT_PCT = 90


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


def scan_shufersal() -> _ChainResult:
    """Meat promotions from Shufersal stores inside the enabled cities."""
    log.info("%s: fetching store list", _SHUFERSAL_NAME)
    store_links = _shufersal_links(cat=5)
    raw = _download(store_links[0]) if store_links else None
    if not raw:
        log.warning("%s: could not fetch Stores file", _SHUFERSAL_NAME)
        return _ChainResult(_SHUFERSAL_NAME, "failed", error="קובץ הסניפים לא הורד")
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
    *,
    client: Optional[httpx.Client] = None,
) -> _ChainResult:
    """Scan one chain hosted on url.publishedprices.co.il (enabled cities only)."""
    log.info("%s: logging in to publishedprices", chain_name)
    own = client is None
    if client is None:
        client = httpx.Client(follow_redirects=True, timeout=60, headers=_HEADERS)
    try:
        if not _pp_login(client, username):
            log.warning("%s: login failed", chain_name)
            return _ChainResult(chain_name, "failed", error="ההתחברות לאתר המחירים נכשלה")

        def _names(prefix: str, limit: int) -> list[str]:
            return [
                f.get("fname", "")
                for f in _pp_list_files(client, prefix, limit=limit)
                if f.get("fname", "").startswith(prefix)
            ]

        stores_files = sorted(_names("Stores", 20))
        if not stores_files:
            log.warning("%s: no Stores file listed", chain_name)
            return _ChainResult(chain_name, "failed", error="לא נמצא קובץ סניפים")
        stores_raw = _pp_download(client, stores_files[-1])
        if not stores_raw:
            log.warning("%s: Stores file download failed", chain_name)
            return _ChainResult(chain_name, "failed", error="קובץ הסניפים לא הורד")
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
    finally:
        if own:
            client.close()


# ---- Bina Projects portal (<chain>.binaprojects.com) ----
#
# File list:  GET <base>/MainIO_Hok.aspx?WStore=&WDate=<dd/mm/yyyy>&WFileType=0
#             -> JSON array of {"FileNm": "...", ...}
# Download:   GET <base>/Download.aspx?FileNm=<name>
#             -> JSON [{"SPath": "<file url>"}] (or the file itself)
# Files are usually zipped XML. Not confirmed against the live portal (no
# network access when this was written); every step is tolerant of variants.

_BINA_NAME_KEYS = ("filenm", "filename", "fname", "name")
_FEED_NAME_IN_TEXT = re.compile(r"((?:Stores|PriceFull|PromoFull)[\w.\-]+)", re.IGNORECASE)


def _bina_names(r: httpx.Response) -> list[str]:
    if r.status_code != 200:
        return []
    names: list[str] = []
    try:
        data = r.json()
    except Exception:
        data = None
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), [])
    if isinstance(data, list):
        for row in data:
            if isinstance(row, dict):
                low = {str(k).lower(): v for k, v in row.items()}
                name = next((low[k] for k in _BINA_NAME_KEYS if low.get(k)), None)
                if name:
                    names.append(str(name).strip())
            elif isinstance(row, str):
                names.append(row.strip())
    if not names:
        names = _FEED_NAME_IN_TEXT.findall(r.text)
    return list(dict.fromkeys(names))


def _bina_list_files(client: httpx.Client, base: str) -> list[str]:
    today = datetime.now().strftime("%d/%m/%Y")
    for wdate in (today, ""):
        r = client.get(
            f"{base}/MainIO_Hok.aspx",
            params={"WStore": "", "WDate": wdate, "WFileType": "0"},
        )
        names = _bina_names(r)
        if names:
            return names
    return []


def _looks_like_feed(body: bytes) -> bool:
    if body[:2] == b"\x1f\x8b" or body[:4] == b"PK\x03\x04":
        return True
    head = body[:200].lstrip(b"\xef\xbb\xbf \r\n\t").lower()
    return head.startswith(b"<?xml") or (head.startswith(b"<") and not head.startswith((b"<!doctype", b"<html")))


def _bina_download(client: httpx.Client, base: str, name: str) -> Optional[bytes]:
    r = client.get(f"{base}/Download.aspx", params={"FileNm": name}, timeout=120)
    if r.status_code != 200:
        return None
    if _looks_like_feed(r.content):
        return r.content
    try:
        data = r.json()
    except Exception:
        return None
    rows = data if isinstance(data, list) else [data]
    for row in rows:
        if isinstance(row, dict):
            low = {str(k).lower(): v for k, v in row.items()}
            url = low.get("spath") or low.get("url")
            if url:
                url = str(url)
                if not url.startswith("http"):
                    url = f"{base}/{url.lstrip('/')}"
                r2 = client.get(url, timeout=120)
                return r2.content if r2.status_code == 200 else None
    return None


def scan_bina_chain(
    base_url: str,
    chain_name: str,
    *,
    client: Optional[httpx.Client] = None,
) -> _ChainResult:
    """Scan one chain on a Bina Projects price portal (enabled cities only)."""
    base = base_url.rstrip("/")
    log.info("%s: listing files on %s", chain_name, base)
    own = client is None
    if client is None:
        client = httpx.Client(follow_redirects=True, timeout=60, headers=_HEADERS)
    try:
        names = _bina_list_files(client, base)
        if not names:
            return _ChainResult(chain_name, "failed", error="רשימת הקבצים באתר המחירים ריקה")

        def _with(prefix: str) -> list[str]:
            return [n for n in names if n.lower().startswith(prefix.lower())]

        stores_files = sorted(_with("Stores"))
        if not stores_files:
            return _ChainResult(chain_name, "failed", error="לא נמצא קובץ סניפים")
        stores_raw = _bina_download(client, base, stores_files[-1])
        if not stores_raw:
            return _ChainResult(chain_name, "failed", error="קובץ הסניפים לא הורד")
        stores = _parse_stores(stores_raw)
        promo_by_store = _latest_by_store(_with("PromoFull"))
        price_by_store = _latest_by_store(_with("PriceFull"))

        def _fetch(store: StoreInfo):
            promo_name = promo_by_store.get(store.store_id)
            price_name = price_by_store.get(store.store_id)
            if not promo_name or not price_name:
                return None
            return _bina_download(client, base, price_name), _bina_download(client, base, promo_name)

        return _scan_chain(chain_name, stores, _fetch)
    finally:
        if own:
            client.close()


def _bareket_source() -> Optional[tuple[str, str]]:
    """(kind, argument) from PW_BAREKET_SOURCE, or None when switched off."""
    raw = os.environ.get("PW_BAREKET_SOURCE", "").strip() or _BAREKET_DEFAULT_SOURCE
    if raw.lower() in ("off", "none", "0", "false", "no"):
        return None
    kind, _sep, arg = raw.partition(":")
    kind, arg = kind.strip().lower(), arg.strip()
    if kind in ("bina", "publishedprices") and arg:
        return kind, arg
    log.warning("PW_BAREKET_SOURCE=%r not understood; Bareket skipped", raw)
    return None


# ---- Promotions ----

def _desc_texts(el: ET.Element, tag: str) -> list[str]:
    return [(d.text or "").strip() for d in el.iter() if _local(d.tag) == tag]


def _extract_meat_promos(
    chain_name: str,
    store_id: str,
    promo_root: ET.Element,
    item_index: dict[str, _Item],
) -> list[MeatPromo]:
    """Shared promo-filtering logic used by every chain."""
    results: list[MeatPromo] = []
    seen: set[tuple[str, str]] = set()
    for promo in promo_root.iter():
        if _local(promo.tag) != "promotion":
            continue
        pf = _fields(promo)
        desc = pf.get("promotiondescription", "")
        end = pf.get("promotionenddatetime") or pf.get("promotionenddate") or ""

        if "1" in _desc_texts(promo, "additionaliscoupon"):
            continue
        if "פיצוי" in desc:
            continue
        clubs = [c for c in _desc_texts(promo, "clubid") if c]
        if clubs and not any(c.startswith("0") or "כלל" in c for c in clubs):
            continue

        promo_dp = _dec(pf.get("discountedprice"))
        promo_dr = _dec(pf.get("discountrate"))
        promo_min = _dec(pf.get("minqty"))

        for pi in promo.iter():
            if pi is promo or _local(pi.tag) not in ("promotionitem", "item"):
                continue
            fi = _fields(pi)
            code = fi.get("itemcode", "")
            if not code or code == "0" * len(code):
                continue
            if fi.get("isgiftitem", "") == "1":
                continue
            item = item_index.get(code)
            if item is None:
                continue
            if not _is_meat_product(item.name, context=desc):
                continue
            key = (code, desc)
            if key in seen:
                continue
            seen.add(key)

            discount_rate = _dec(fi.get("discountrate")) or promo_dr
            discounted_price = _dec(fi.get("discountedprice")) or promo_dp
            if discount_rate is None and discounted_price is None:
                continue

            results.append(MeatPromo(
                chain=chain_name,
                store_id=store_id,
                item_code=code,
                item_name=item.name,
                regular_price=item.price,
                discounted_price=discounted_price,
                discount_rate=discount_rate,
                promotion_description=desc,
                end_date=end[:10] if end else None,
                weighted=item.weighted,
                pack_kg=item.pack_kg,
                min_qty=_dec(fi.get("minqty")) or promo_min,
            ))
    return results


# ---- Orchestrator ----

def _chain_tasks() -> list[tuple[str, Callable[[], _ChainResult]]]:
    """(chain name, scan function) for every chain to scan."""
    tasks: list[tuple[str, Callable[[], _ChainResult]]] = [(_SHUFERSAL_NAME, scan_shufersal)]
    tasks += [
        (name, (lambda u=user, n=name: scan_publishedprices_chain(u, n)))
        for user, name in _PUBLISHEDPRICES_CHAINS
    ]
    source = _bareket_source()
    if source is not None:
        kind, arg = source
        if kind == "bina":
            tasks.append((_BAREKET_NAME, lambda: scan_bina_chain(arg, _BAREKET_NAME)))
        else:
            tasks.append((_BAREKET_NAME, lambda: scan_publishedprices_chain(arg, _BAREKET_NAME)))
    return tasks


def _price_rank_key(p: MeatPromo) -> tuple:
    per_kg = p.price_per_kg
    if per_kg is not None:
        return (0, per_kg, p.chain, p.item_name)
    return (1, p.promo_price or Decimal("0"), p.chain, p.item_name)


def _rank_promos(
    promos: list[MeatPromo], top_n: int
) -> tuple[list[MeatPromo], dict[str, int]]:
    """Dedupe, categorise, keep the `top_n` cheapest per ranked category.

    Returns (rows in display order, rows per category before the cut).
    """
    merged: dict[tuple, MeatPromo] = {}
    store_keys: dict[tuple, set[str]] = {}
    for p in promos:
        price = p.promo_price
        if price is None or price <= 0:
            continue
        pct = p.savings_pct
        if pct is not None and (pct <= 0 or pct >= _MAX_SANE_DISCOUNT_PCT):
            continue
        label = _category_label(p)
        # One row per (category, chain, product, price): the same promotion
        # runs in many stores of a chain.
        key = (label, p.chain, _norm(p.item_name), price)
        kept = merged.get(key)
        if kept is None:
            p.category = label
            p.ranked = is_ranked_category(label)
            p.cities = [p.city] if p.city else []
            p.store_names = [p.store_name or p.city] if (p.store_name or p.city) else []
            merged[key] = kept = p
            store_keys[key] = set()
        store_keys[key].add(p.store_id)
        if p.city and p.city not in kept.cities:
            kept.cities.append(p.city)
        label_store = p.store_name or p.city
        if label_store and label_store not in kept.store_names:
            kept.store_names.append(label_store)
        if p.end_date and (not kept.end_date or p.end_date < kept.end_date):
            kept.end_date = p.end_date
    for key, kept in merged.items():
        kept.store_count = len(store_keys[key])

    groups: dict[str, list[MeatPromo]] = {}
    for p in merged.values():
        groups.setdefault(p.category, []).append(p)
    rows: list[MeatPromo] = []
    counts: dict[str, int] = {}
    for label in sorted(groups, key=_category_sort_key):
        items = groups[label]
        counts[label] = len(items)
        if is_ranked_category(label):
            rows.extend(sorted(items, key=_price_rank_key)[:top_n])
        else:
            rows.extend(sorted(items, key=lambda p: (_norm(p.item_name), p.chain))[:_UNRANKED_LIMIT])
    return rows, counts


def scan_and_report() -> SupermarketReport:
    """Scan every configured chain in parallel and return a combined report."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    enabled = _enabled_cities()
    tasks = _chain_tasks()
    report = SupermarketReport(
        status="ok",
        cities=[c for c in _CITY_ORDER if c in enabled],
        top_per_category=_top_per_category(),
    )
    log.info(
        "Running supermarket meat scan across %d chains (cities: %s)",
        len(tasks), ", ".join(report.cities),
    )

    results: list[_ChainResult] = []
    with ThreadPoolExecutor(max_workers=max(1, len(tasks))) as pool:
        futures = {pool.submit(fn): name for name, fn in tasks}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                results.append(fut.result())
            except Exception as exc:
                log.error("%s: chain scan failed: %s: %s", name, type(exc).__name__, exc)
                results.append(_ChainResult(name, "failed", error=f"{type(exc).__name__}: {exc}"))

    raw: list[MeatPromo] = []
    for res in results:
        if res.status == "ok":
            report.chains_scanned.append(res.name)
            if not res.promos:
                log.info("%s: scanned, no meat promos today", res.name)
                report.chains_without_promos.append(res.name)
            raw.extend(res.promos)
            for store in res.stores:
                report.stores_scanned.append(f"{res.name} – {store.label} ({store.city})")
                report.stores_by_city[store.city] = report.stores_by_city.get(store.city, 0) + 1
        elif res.status == "no_stores":
            report.chains_no_stores.append(res.name)
        else:
            log.warning("%s: scan failed: %s", res.name, res.error)
            report.chains_failed[res.name] = res.error or "הסריקה נכשלה"
    report.chains_scanned.sort()
    report.chains_without_promos.sort()
    report.chains_no_stores.sort()
    report.stores_scanned.sort()

    report.promos, report.promos_by_category = _rank_promos(raw, report.top_per_category)
    if not report.chains_scanned and report.chains_failed:
        report.status = "fetch_failed"
        report.error = "הסריקה נכשלה בכל הרשתות שיש להן סניפים באזור"
    return report


# ---- State persistence (so we can diff tomorrow if desired) ----

def _state_file() -> Path:
    return data_dir() / "supermarkets-state.json"


def _promo_to_dict(p: MeatPromo) -> dict:
    out = {
        k: (str(v) if isinstance(v, Decimal) else v)
        for k, v in asdict(p).items()
    }
    per_kg = p.price_per_kg
    out["price_per_kg"] = str(per_kg) if per_kg is not None else None
    return out


def persist(report: SupermarketReport) -> None:
    """Write the latest report atomically (temp file + os.replace)."""
    if report.status != "ok":
        return
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps({
            "schema_version": 2,
            "last_scanned_at": datetime.now().astimezone().isoformat(),
            "cities": report.cities,
            "promos": [_promo_to_dict(p) for p in report.promos],
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(tmp, path)


# ---- Terminal output ----

def print_report(report: SupermarketReport) -> None:
    if report.status != "ok":
        console.print(f"[red]Supermarket scan failed: {report.status}[/red]  {report.error or ''}")
        return
    console.print(f"[dim]Cities: {', '.join(report.cities)}[/dim]")
    console.print(f"[dim]Chains scanned: {', '.join(report.chains_scanned)}[/dim]")
    if report.chains_failed:
        console.print(f"[yellow]Chains failed: {', '.join(report.chains_failed)}[/yellow]")
    if not report.promos:
        console.print("[dim]No meat promos found today.[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("Category")
    table.add_column("Chain", width=10)
    table.add_column("Store")
    table.add_column("Item")
    table.add_column("Promo")
    table.add_column("Per kg")
    table.add_column("Until")
    for p in report.promos:
        now = f"₪{p.promo_price}" if p.promo_price is not None else "—"
        per_kg = f"₪{p.price_per_kg}" if p.price_per_kg is not None else "—"
        store = p.store_names[0] if p.store_names else p.store_name
        table.add_row(p.category, p.chain, store, p.item_name[:40], now, per_kg, p.end_date_display)
    console.print(table)
