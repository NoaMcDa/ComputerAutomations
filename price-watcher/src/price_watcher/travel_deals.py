"""Travel deals section for the daily email (issues #52 and #53).

What it does
------------
Reads travel deals (flight-only or flight + hotel packages) from a JSON feed
file and turns them into a "Travel deals" section grouped by continent:

* **#52 – hotel quality.** Only 4- and 5-star hotels are eligible. A star
  rating must be a number in ``[4, 5]``; 4.5 passes, 3.5 / 3 / missing /
  unparseable ratings are rejected. A package keeps only its qualifying hotel
  options and is dropped when none qualify, so a lower-tier hotel is never
  shown as a fallback. Flight-only deals carry no hotel and pass unchanged.
* **#53 – continents, quotas and windows.**
  - Europe: departures from today up to and including today + 50 days, at
    most ``PW_TRAVEL_EUROPE_LIMIT`` deals (the baseline volume, default 10).
  - Every other continent: departures from today up to and including
    today + 365 days, exactly 4 deals when that many qualify.
  - Asia: at least one Japan and one Thailand deal when either exists in the
    window. A missing one is reported in the section, not silently skipped.
  Within each continent deals are ordered by price, cheapest first.

Data source
-----------
The repository has no travel-deal scraper yet, so this module does not fetch
anything itself. It reads ``travel_deals.json`` from the app-data folder (or
the path in ``PW_TRAVEL_DEALS_FILE``). When the file is absent the feature is
off and the section is omitted. Expected shape::

    {"deals": [
      {"destination": "Tokyo", "country": "Japan",
       "depart_date": "2026-12-01", "return_date": "2026-12-08",
       "price": 2890, "currency": "ILS", "url": "https://...",
       "hotels": [{"name": "Hotel X", "stars": 4.5, "url": "https://..."}]}
    ]}

``continent`` may be given explicitly; otherwise it is derived from
``country`` (name or ISO-2 code). ``hotels`` may be omitted for a flight-only
deal; a single ``hotel`` object is accepted as well.

Configuration (environment / ``.env``)
--------------------------------------
PW_TRAVEL_DEALS_FILE     Path of the feed file (default: app-data/travel_deals.json).
PW_TRAVEL_EUROPE_LIMIT   Max Europe deals (default 10).
PW_TRAVEL_OTHER_LIMIT    Deals per non-Europe continent (default 4).
PW_TRAVEL_EUROPE_DAYS    Europe departure window in days (default 50).
PW_TRAVEL_OTHER_DAYS     Departure window for other continents (default 365).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from html import escape
from pathlib import Path
from typing import Any, Iterable, Optional

from price_watcher import logging_setup
from price_watcher.paths import data_dir

log = logging_setup.get(__name__)

MIN_STARS = Decimal(4)
MAX_STARS = Decimal(5)

EUROPE = "Europe"
ASIA = "Asia"
CONTINENT_ORDER = [EUROPE, ASIA, "North America", "South America", "Africa", "Oceania"]
CONTINENT_HE = {
    EUROPE: "אירופה",
    ASIA: "אסיה",
    "North America": "צפון אמריקה",
    "South America": "דרום אמריקה",
    "Africa": "אפריקה",
    "Oceania": "אוקיאניה",
}
ASIA_REQUIRED = ("Japan", "Thailand")

# Country name / ISO-2 -> continent. Covers the destinations a Tel Aviv
# traveller is likely to see; unknown countries are reported as skipped.
_COUNTRIES: dict[str, tuple[str, str]] = {
    # Europe
    "AL": ("Albania", EUROPE), "AT": ("Austria", EUROPE), "BE": ("Belgium", EUROPE),
    "BG": ("Bulgaria", EUROPE), "HR": ("Croatia", EUROPE), "CY": ("Cyprus", EUROPE),
    "CZ": ("Czechia", EUROPE), "DK": ("Denmark", EUROPE), "EE": ("Estonia", EUROPE),
    "FI": ("Finland", EUROPE), "FR": ("France", EUROPE), "DE": ("Germany", EUROPE),
    "GE": ("Georgia", EUROPE), "GR": ("Greece", EUROPE), "HU": ("Hungary", EUROPE),
    "IS": ("Iceland", EUROPE), "IE": ("Ireland", EUROPE), "IT": ("Italy", EUROPE),
    "LV": ("Latvia", EUROPE), "LT": ("Lithuania", EUROPE), "LU": ("Luxembourg", EUROPE),
    "MT": ("Malta", EUROPE), "ME": ("Montenegro", EUROPE), "NL": ("Netherlands", EUROPE),
    "NO": ("Norway", EUROPE), "PL": ("Poland", EUROPE), "PT": ("Portugal", EUROPE),
    "RO": ("Romania", EUROPE), "RS": ("Serbia", EUROPE), "SK": ("Slovakia", EUROPE),
    "SI": ("Slovenia", EUROPE), "ES": ("Spain", EUROPE), "SE": ("Sweden", EUROPE),
    "CH": ("Switzerland", EUROPE), "GB": ("United Kingdom", EUROPE),
    "UK": ("United Kingdom", EUROPE),
    # Asia
    "JP": ("Japan", ASIA), "TH": ("Thailand", ASIA), "VN": ("Vietnam", ASIA),
    "IN": ("India", ASIA), "CN": ("China", ASIA), "KR": ("South Korea", ASIA),
    "SG": ("Singapore", ASIA), "PH": ("Philippines", ASIA), "ID": ("Indonesia", ASIA),
    "MY": ("Malaysia", ASIA), "LK": ("Sri Lanka", ASIA), "MV": ("Maldives", ASIA),
    "AE": ("United Arab Emirates", ASIA), "UZ": ("Uzbekistan", ASIA),
    "AZ": ("Azerbaijan", ASIA), "HK": ("Hong Kong", ASIA), "TW": ("Taiwan", ASIA),
    "NP": ("Nepal", ASIA), "KH": ("Cambodia", ASIA),
    # Americas
    "US": ("United States", "North America"), "CA": ("Canada", "North America"),
    "MX": ("Mexico", "North America"), "CU": ("Cuba", "North America"),
    "DO": ("Dominican Republic", "North America"), "PA": ("Panama", "North America"),
    "CR": ("Costa Rica", "North America"),
    "BR": ("Brazil", "South America"), "AR": ("Argentina", "South America"),
    "CL": ("Chile", "South America"), "PE": ("Peru", "South America"),
    "CO": ("Colombia", "South America"),
    # Africa
    "EG": ("Egypt", "Africa"), "MA": ("Morocco", "Africa"), "ZA": ("South Africa", "Africa"),
    "KE": ("Kenya", "Africa"), "TZ": ("Tanzania", "Africa"), "ET": ("Ethiopia", "Africa"),
    "SC": ("Seychelles", "Africa"), "MU": ("Mauritius", "Africa"),
    # Oceania
    "AU": ("Australia", "Oceania"), "NZ": ("New Zealand", "Oceania"), "FJ": ("Fiji", "Oceania"),
}
_BY_NAME: dict[str, tuple[str, str]] = {name.lower(): (name, cont) for name, cont in _COUNTRIES.values()}
_BY_NAME.update({
    "usa": ("United States", "North America"), "england": ("United Kingdom", EUROPE),
    "korea": ("South Korea", ASIA), "uae": ("United Arab Emirates", ASIA),
    "czech republic": ("Czechia", EUROPE), "holland": ("Netherlands", EUROPE),
    "יפן": ("Japan", ASIA), "תאילנד": ("Thailand", ASIA),
})
_CONTINENT_ALIASES = {c.lower(): c for c in CONTINENT_ORDER}
_CONTINENT_ALIASES.update({"americas": "North America", "australia": "Oceania"})


# ---------------------------------------------------------------- models
@dataclass(frozen=True)
class Hotel:
    name: str
    stars: Optional[Decimal]
    url: Optional[str] = None


@dataclass(frozen=True)
class Deal:
    destination: str
    country: str               # canonical English name when known
    continent: Optional[str]
    depart_date: date
    price: Decimal
    currency: str = "ILS"
    url: Optional[str] = None
    return_date: Optional[date] = None
    hotels: tuple[Hotel, ...] = ()
    is_package: bool = False   # True when the feed listed hotels for this deal


@dataclass
class ContinentGroup:
    continent: str
    deals: list[Deal]
    limit: int
    window_days: int
    missing_required: list[str] = field(default_factory=list)


@dataclass
class TravelReport:
    groups: list[ContinentGroup]
    today: date
    dropped_low_star: int = 0
    skipped_unknown: int = 0
    status: str = "ok"         # ok | error
    error: Optional[str] = None


# ------------------------------------------------------- hotel filter (#52)
def parse_stars(value: Any) -> Optional[Decimal]:
    """Star rating as a Decimal, or None when missing / not a number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        raw = str(value)
    elif isinstance(value, str):
        raw = value.strip().replace("★", "").replace("*", "").replace(",", ".")
        raw = raw.split()[0] if raw.split() else ""
    else:
        return None
    try:
        d = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def hotel_qualifies(stars: Any) -> bool:
    """True only for a verified rating in [4, 5] (4, 4.5 and 5 pass)."""
    s = stars if isinstance(stars, Decimal) else parse_stars(stars)
    return s is not None and MIN_STARS <= s <= MAX_STARS


def filter_hotels(deal: Deal) -> Optional[Deal]:
    """Keep only 4–5 star hotels; drop a package left with none.

    Flight-only deals (no hotels listed) are returned unchanged.
    """
    if not deal.is_package:
        return deal
    good = tuple(h for h in deal.hotels if hotel_qualifies(h.stars))
    if not good:
        return None
    return replace(deal, hotels=good)


# --------------------------------------------------- continents (#53)
def resolve_country(country: str | None) -> tuple[str, Optional[str]]:
    """(canonical country name, continent or None)."""
    c = (country or "").strip()
    if not c:
        return "", None
    hit = _COUNTRIES.get(c.upper()) if len(c) == 2 else None
    hit = hit or _BY_NAME.get(c.lower())
    return hit if hit else (c, None)


def in_window(d: date, today: date, days: int) -> bool:
    """Departure between today and today + days, both inclusive."""
    return today <= d <= today + timedelta(days=days)


def _cheapest(deals: Iterable[Deal]) -> list[Deal]:
    return sorted(deals, key=lambda d: (d.price, d.depart_date, d.destination))


def select_asia(deals: list[Deal], limit: int) -> tuple[list[Deal], list[str]]:
    """Cheapest ``limit`` Asia deals, guaranteeing Japan and Thailand slots."""
    ordered = _cheapest(deals)
    chosen: list[Deal] = []
    missing: list[str] = []
    for country in ASIA_REQUIRED:
        pick = next((d for d in ordered if d.country == country), None)
        if pick is None:
            missing.append(country)
        elif len(chosen) < limit:
            chosen.append(pick)
    for d in ordered:
        if len(chosen) >= limit:
            break
        if d not in chosen:
            chosen.append(d)
    return _cheapest(chosen), missing


def group_by_continent(
    deals: Iterable[Deal],
    today: date,
    *,
    europe_limit: int = 10,
    other_limit: int = 4,
    europe_days: int = 50,
    other_days: int = 365,
) -> list[ContinentGroup]:
    buckets: dict[str, list[Deal]] = {}
    for d in deals:
        if not d.continent:
            continue
        window = europe_days if d.continent == EUROPE else other_days
        if in_window(d.depart_date, today, window):
            buckets.setdefault(d.continent, []).append(d)

    groups: list[ContinentGroup] = []
    for cont in CONTINENT_ORDER:
        items = buckets.get(cont, [])
        if cont == EUROPE:
            groups.append(ContinentGroup(cont, _cheapest(items)[:europe_limit], europe_limit, europe_days))
        elif cont == ASIA:
            chosen, missing = select_asia(items, other_limit)
            groups.append(ContinentGroup(cont, chosen, other_limit, other_days, missing))
        else:
            groups.append(ContinentGroup(cont, _cheapest(items)[:other_limit], other_limit, other_days))
    return groups


# ------------------------------------------------------------ feed parsing
def _parse_date(v: Any) -> Optional[date]:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def deal_from_dict(raw: dict) -> Optional[Deal]:
    """Build a Deal from one feed entry, or None when it is unusable."""
    if not isinstance(raw, dict):
        return None
    depart = _parse_date(raw.get("depart_date"))
    try:
        price = Decimal(str(raw.get("price")))
    except (InvalidOperation, ValueError):
        return None
    if depart is None or not price.is_finite() or price <= 0:
        return None
    country, continent = resolve_country(raw.get("country"))
    explicit = _CONTINENT_ALIASES.get(str(raw.get("continent") or "").strip().lower())
    continent = explicit or continent

    hotels_raw = raw.get("hotels")
    if hotels_raw is None and isinstance(raw.get("hotel"), dict):
        hotels_raw = [raw["hotel"]]
    is_package = hotels_raw is not None
    hotels = tuple(
        Hotel(name=str(h.get("name") or ""), stars=parse_stars(h.get("stars")), url=h.get("url"))
        for h in (hotels_raw or []) if isinstance(h, dict)
    )
    return Deal(
        destination=str(raw.get("destination") or country or "?"),
        country=country,
        continent=continent,
        depart_date=depart,
        return_date=_parse_date(raw.get("return_date")),
        price=price,
        currency=str(raw.get("currency") or "ILS"),
        url=raw.get("url"),
        hotels=hotels,
        is_package=is_package,
    )


def _int_env(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, "").strip() or default)
    except ValueError:
        log.warning("%s is not an integer; using %d", name, default)
        return default
    return v if v >= 0 else default


def feed_path() -> Path:
    p = os.environ.get("PW_TRAVEL_DEALS_FILE", "").strip()
    return Path(p).expanduser() if p else data_dir() / "travel_deals.json"


def build_report(raw_deals: Iterable[dict], today: date) -> TravelReport:
    parsed = [d for d in (deal_from_dict(r) for r in raw_deals) if d is not None]
    kept: list[Deal] = []
    dropped = 0
    for d in parsed:
        f = filter_hotels(d)
        if f is None:
            dropped += 1
        else:
            kept.append(f)
    unknown = sum(1 for d in kept if not d.continent)
    groups = group_by_continent(
        kept, today,
        europe_limit=_int_env("PW_TRAVEL_EUROPE_LIMIT", 10),
        other_limit=_int_env("PW_TRAVEL_OTHER_LIMIT", 4),
        europe_days=_int_env("PW_TRAVEL_EUROPE_DAYS", 50),
        other_days=_int_env("PW_TRAVEL_OTHER_DAYS", 365),
    )
    return TravelReport(groups=groups, today=today, dropped_low_star=dropped, skipped_unknown=unknown)


def scan_and_report(today: Optional[date] = None) -> Optional[TravelReport]:
    """Report from the feed file, or None when the feature is off (no file)."""
    today = today or date.today()
    path = feed_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw = data.get("deals", []) if isinstance(data, dict) else data
        if not isinstance(raw, list):
            raise ValueError("'deals' must be a list")
    except (OSError, ValueError) as e:
        log.warning("travel deals feed unreadable: %s", e)
        return TravelReport(groups=[], today=today, status="error", error=str(e))
    return build_report(raw, today)


# ---------------------------------------------------------------- output
_SYMBOLS = {"ILS": "₪", "USD": "$", "EUR": "€", "GBP": "£"}


def _price(d: Deal) -> str:
    sym = _SYMBOLS.get(d.currency.upper())
    num = f"{d.price:,.0f}" if d.price == d.price.to_integral() else f"{d.price:,.2f}"
    return f"{sym}{num}" if sym else f"{num} {d.currency}"


def _stars(h: Hotel) -> str:
    s = h.stars
    if s is None:
        return ""
    return f"{s.normalize():f}★"


def _dates(d: Deal) -> str:
    out = d.depart_date.strftime("%d.%m.%Y")
    if d.return_date:
        out += f"–{d.return_date.strftime('%d.%m.%Y')}"
    return out


def _hotel_line(d: Deal) -> str:
    if not d.is_package:
        return "טיסה בלבד"
    best = max(d.hotels, key=lambda h: (h.stars or Decimal(0)))
    extra = f" (+{len(d.hotels) - 1} מלונות)" if len(d.hotels) > 1 else ""
    return f"{best.name} {_stars(best)}{extra}".strip()


def _empty_note(g: ContinentGroup) -> str:
    return f"אין דילים מתאימים (מלונות 4–5 כוכבים, יציאה ב-{g.window_days} הימים הקרובים)."


def section_text(report: Optional[TravelReport]) -> list[str]:
    if report is None:
        return []
    lines = ["", "=== דילים לחופשה ==="]
    if report.status != "ok":
        lines.append(f"לא ניתן לקרוא את קובץ הדילים: {report.error}")
        return lines
    for g in report.groups:
        lines.append(f"-- {CONTINENT_HE.get(g.continent, g.continent)} ({len(g.deals)}) --")
        if not g.deals:
            lines.append("  " + _empty_note(g))
        for d in g.deals:
            lines.append(f"  {d.destination}, {d.country} | {_dates(d)} | {_price(d)} | {_hotel_line(d)}"
                         + (f" | {d.url}" if d.url else ""))
        for c in g.missing_required:
            lines.append(f"  אין כרגע דיל ל{'יפן' if c == 'Japan' else 'תאילנד'} בטווח התאריכים.")
    return lines


def section_html(report: Optional[TravelReport]) -> str:
    if report is None:
        return ""
    parts = ['<h2 style="margin:28px 0 8px;font-size:20px;">✈️ דילים לחופשה</h2>']
    if report.status != "ok":
        parts.append(f'<p style="color:#b42318;">לא ניתן לקרוא את קובץ הדילים: {escape(report.error or "")}</p>')
        return "\n".join(parts)
    for g in report.groups:
        title = escape(CONTINENT_HE.get(g.continent, g.continent))
        parts.append(f'<h3 style="margin:16px 0 6px;font-size:16px;">{title} '
                     f'<span style="color:#667085;font-weight:normal;">({len(g.deals)})</span></h3>')
        if not g.deals:
            parts.append(f'<p style="margin:0 0 8px;color:#667085;">{escape(_empty_note(g))}</p>')
        else:
            rows = []
            for d in g.deals:
                dest = escape(f"{d.destination}, {d.country}")
                if d.url:
                    dest = f'<a href="{escape(d.url, quote=True)}">{dest}</a>'
                rows.append(
                    "<tr>"
                    f'<td style="padding:4px 8px;">{dest}</td>'
                    f'<td style="padding:4px 8px;direction:ltr;">{escape(_dates(d))}</td>'
                    f'<td style="padding:4px 8px;direction:ltr;font-weight:bold;">{escape(_price(d))}</td>'
                    f'<td style="padding:4px 8px;">{escape(_hotel_line(d))}</td>'
                    "</tr>"
                )
            parts.append('<table style="border-collapse:collapse;width:100%;font-size:14px;">'
                         + "".join(rows) + "</table>")
        for c in g.missing_required:
            name = "יפן" if c == "Japan" else "תאילנד"
            parts.append(f'<p style="margin:4px 0;color:#b54708;">אין כרגע דיל ל{name} בטווח התאריכים.</p>')
    return "\n".join(parts)


def print_report(report: Optional[TravelReport]) -> None:
    for line in section_text(report):
        print(line)
