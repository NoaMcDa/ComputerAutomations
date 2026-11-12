"""Vacation deal scanner (packages + flights from Israel).

Reads the deal listings that Israeli travel sites publish on their own deal
pages and merges them into one ranked list:

  איסתא            issta.co.il/deals/last-call        (page HTML)
  אשת טורס         eshet.com/deals/                   (data embedded in the page)
  השטיח המעופף     flying.co.il home-page package feed
  גוליבר           gulliver.co.il home-page hot deals
  חוליו            hulyo.co.il catalogue files
  טרווליסט         travelist.co.il flight-deal file
  טיסות סודיות     secretflights.co.il deal feed

Every source is plain HTTP, runs independently, and a failing source never
sinks the others. Prices are per person as each site states them.

For packages and for flights separately, the report groups the deals by
continent (see `continents.py`) in this order: Europe, Asia, North America,
South America, Africa, Oceania. Within a continent it ranks cheapest first,
one deal per source and destination and at most `_MAX_PER_DESTINATION` rows
for the same destination, and keeps:

  Europe       departures today + PW_DEALS_LEAD_DAYS (2) ..
               today + PW_DEALS_EUROPE_DAYS (50), up to PW_DEALS_TOP deals (15)
  elsewhere    departures today + PW_DEALS_LEAD_DAYS (2) ..
               today + PW_DEALS_OTHER_DAYS (365),
               PW_DEALS_OTHER_TOP deals per continent (4)
  Asia         its deals include the cheapest Japan and the cheapest
               Thailand deal whenever one exists in the window; a missing
               one is recorded on the group so the email can say so.

Packages need a hotel rated 3 to 5 stars; unrated packages are left out.
3★ hotels are limited to a third of the rows: at most one of a
destination's (up to) three rows, and at most a third of each continent's
package rows. The other rows are 4–5★.

Lastminute (lastminute.co.il) was dropped as a source on the owner's
request: its packages carry no star rating and its reviews are poor.

Secret Flights deals use their own window instead of the continent's:
departures today + PW_DEALS_LEAD_DAYS .. today + PW_DEALS_SECRET_FLIGHTS_DAYS
(90), since the site publishes deals 2-4 months ahead.

Trip rules, for packages and flights alike (owner's request):

  length       PW_DEALS_MIN_NIGHTS (3) .. PW_DEALS_MAX_NIGHTS (5) nights,
               inclusive (`VacationDeal.nights_or_computed`); a deal whose
               length isn't known is left out. Dropped deals are counted in
               `VacationReport.dropped_length`.
  lead time    the first allowed departure is today + PW_DEALS_LEAD_DAYS (2),
               i.e. at least 48 hours from now. Only the near end of each
               window moves; the far end stays today + the window's days.

Undated deals can't be placed in a window and are left out, as are deals
whose destination can't be placed on a continent (logged and counted).
PW_DEALS_DAYS, when set, caps every window.
"""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional
from urllib.parse import parse_qs, quote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from rich.console import Console
from rich.table import Table

from price_watcher import continents as _continents
from price_watcher import logging_setup
from price_watcher.http_headers import USER_AGENT

log = logging_setup.get(__name__)
console = Console()

_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "he,en;q=0.8,en-US;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
}
_TIMEOUT = 45

KIND_PACKAGE = "package"
KIND_FLIGHT = "flight"

_MAX_PER_DESTINATION = 3
_CURRENCY_CODES = {"$": "USD", "₪": "ILS", "€": "EUR", "£": "GBP"}


def _env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _window_days() -> int:
    """Global cap on the departure window (PW_DEALS_DAYS). 0 (default) = none."""
    return _env_int("PW_DEALS_DAYS", 0, 0)


def _top_n() -> int:
    """How many Europe deals to keep per list (PW_DEALS_TOP)."""
    return _env_int("PW_DEALS_TOP", 15, 1)


def _other_top() -> int:
    """How many deals to keep per non-Europe continent (PW_DEALS_OTHER_TOP)."""
    return _env_int("PW_DEALS_OTHER_TOP", 4, 1)


def _capped(days: int) -> int:
    cap = _window_days()
    return min(days, cap) if cap else days


def _europe_days() -> int:
    """Europe departure window in days (PW_DEALS_EUROPE_DAYS), capped by PW_DEALS_DAYS."""
    return _capped(_env_int("PW_DEALS_EUROPE_DAYS", 50, 0))


def _other_days() -> int:
    """Departure window of every other continent (PW_DEALS_OTHER_DAYS), capped by PW_DEALS_DAYS."""
    return _capped(_env_int("PW_DEALS_OTHER_DAYS", 365, 0))


def _continent_days(continent: str) -> int:
    return _europe_days() if continent == _continents.EUROPE else _other_days()


# Sites whose deals use their own departure window instead of the continent's:
# source name -> (env var, default days). Secret Flights publishes deals 2-4
# months ahead, so the 50-day Europe window would drop all of them.
_SECRET_FLIGHTS = "טיסות סודיות"
_SOURCE_WINDOWS: dict[str, tuple[str, int]] = {
    _SECRET_FLIGHTS: ("PW_DEALS_SECRET_FLIGHTS_DAYS", 90),
}


def _source_days(source: str) -> Optional[int]:
    """The site's own departure window (capped by PW_DEALS_DAYS), or None."""
    if source not in _SOURCE_WINDOWS:
        return None
    name, default = _SOURCE_WINDOWS[source]
    return _capped(_env_int(name, default, 0))


def _deal_days(d: "VacationDeal", continent: str) -> int:
    own = _source_days(d.source)
    return own if own is not None else _continent_days(continent)


def source_windows() -> dict[str, int]:
    """Site -> its own departure window in days (for the email's summary line)."""
    return {s: _source_days(s) for s in _SOURCE_WINDOWS}


def _lead_days() -> int:
    """Days from today to the first allowed departure (PW_DEALS_LEAD_DAYS, default 2 = 48 hours)."""
    return _env_int("PW_DEALS_LEAD_DAYS", 2, 0)


def _first_departure(today: date) -> date:
    """Earliest departure date a deal may have. Windows still end at today + their days."""
    return today + timedelta(days=_lead_days())


def _min_nights() -> int:
    """Shortest trip kept, in nights (PW_DEALS_MIN_NIGHTS, default 3)."""
    return _env_int("PW_DEALS_MIN_NIGHTS", 3, 1)


def _max_nights() -> int:
    """Longest trip kept, in nights (PW_DEALS_MAX_NIGHTS, default 5); never below the minimum."""
    return _env_int("PW_DEALS_MAX_NIGHTS", 5, _min_nights())


def _length_ok(d: "VacationDeal") -> bool:
    """True when the trip is MIN..MAX nights long (inclusive). Unknown length -> False."""
    nights = d.nights_or_computed
    return nights is not None and _min_nights() <= nights <= _max_nights()


def _continent_limit(continent: str) -> int:
    return _top_n() if continent == _continents.EUROPE else _other_top()


# Asia must show at least one deal for each of these (ISO-2) when one exists.
_ASIA_REQUIRED = ("JP", "TH")


# ---- Data model ----

# Luggage states of a deal (VacationDeal.luggage).
LUGGAGE_SUITCASE = "suitcase"   # checked suitcase included (a carry-on may be too)
LUGGAGE_CARRY_ON = "carry_on"   # only a carry-on / trolley included
LUGGAGE_NONE = "none"           # the site says no luggage is included
LUGGAGE_UNKNOWN = "unknown"     # the site doesn't say

@dataclass
class VacationDeal:
    source: str                     # site display name (Hebrew)
    kind: str                       # KIND_PACKAGE / KIND_FLIGHT
    destination: str
    price: Decimal                  # per person
    url: str
    currency: str = "USD"
    country: str = ""
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    nights: Optional[int] = None
    hotel: str = ""
    note: str = ""                  # board basis, airline, "direct", price remark…
    includes: list[str] = field(default_factory=list)  # what the package contains, as the site states it
    adults: int = 0                 # party the site priced the deal for (0 = not stated)
    children: int = 0
    total_price: Optional[Decimal] = None  # whole-party price when the site gives it
    continent: str = ""             # continents.EUROPE / ASIA / ... (set by scan_and_report)

    # Hotel star rating as the site states it (4, 4.5, 5…); None when not given (#52).
    stars: Optional[Decimal] = None

    # Luggage as the site states it: LUGGAGE_SUITCASE / LUGGAGE_CARRY_ON /
    # LUGGAGE_NONE, or LUGGAGE_UNKNOWN when the site doesn't say.
    luggage: str = LUGGAGE_UNKNOWN
    # Add-on price per person the site quotes when luggage is not included
    # (e.g. "trolley $25 per leg"), as free text; "" when not given.
    luggage_extra: str = ""

    @property
    def has_luggage(self) -> bool:
        """True when at least a carry-on is included (a trolley is enough)."""
        return self.luggage in (LUGGAGE_SUITCASE, LUGGAGE_CARRY_ON)

    @property
    def party_size(self) -> int:
        return self.adults + self.children

    @property
    def total(self) -> Optional[Decimal]:
        """Price of the whole package for the party the site priced it for."""
        if self.total_price is not None:
            return self.total_price
        if self.party_size:
            return _dec(self.price * self.party_size)
        return None

    @property
    def nights_or_computed(self) -> Optional[int]:
        if self.nights:
            return self.nights
        if self.date_from and self.date_to:
            return (self.date_to - self.date_from).days or None
        return None


@dataclass
class SourceStatus:
    name: str
    ok: bool
    count: int = 0
    error: Optional[str] = None


@dataclass
class ContinentGroup:
    """The deals of one kind (packages or flights) shown for one continent."""
    continent: str                  # continents.EUROPE / ASIA / ...
    deals: list[VacationDeal] = field(default_factory=list)
    limit: int = 0                  # most deals this continent may show
    window_days: int = 0            # departures from today to today + window_days
    missing_required: list[str] = field(default_factory=list)  # e.g. ["Thailand"]: none in the window

    @property
    def title_he(self) -> str:
        return _continents.CONTINENT_HE.get(self.continent, self.continent)


@dataclass
class VacationReport:
    status: str = "ok"              # "ok" / "fetch_failed"
    packages: list[VacationDeal] = field(default_factory=list)  # every package shown, in display order
    flights: list[VacationDeal] = field(default_factory=list)   # every flight shown, in display order
    sources: list[SourceStatus] = field(default_factory=list)
    total_found: int = 0            # before dedupe / top-N cut
    days_ahead: int = 0             # global departure cap PW_DEALS_DAYS (0 = none)
    error: Optional[str] = None
    package_groups: list[ContinentGroup] = field(default_factory=list)  # by continent, display order
    flight_groups: list[ContinentGroup] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)  # "source: destination (country)" left out

    # Packages dropped because the hotel isn't a verified 3–5★ (#52).
    dropped_low_star: int = 0
    # Deals dropped because the trip isn't PW_DEALS_MIN_NIGHTS..MAX_NIGHTS
    # nights long (or its length isn't known).
    dropped_length: int = 0

    @property
    def deals(self) -> list[VacationDeal]:
        return self.packages + self.flights

    @property
    def unmapped_count(self) -> int:
        return len(self.unmapped)


# ---- Small parsing helpers ----

def _dec(value: object) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return d.quantize(Decimal("1")) if d == d.to_integral_value() else d.quantize(Decimal("0.01"))


def _currency(symbol_or_code: object) -> str:
    s = str(symbol_or_code or "").strip()
    return _CURRENCY_CODES.get(s, s.upper() or "USD")


_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%d.%m.%y", "%d/%m/%y")


def _date(value: object) -> Optional[date]:
    """Parse the date formats these sites use; ignores any time-of-day part."""
    if not value:
        return None
    s = str(value).strip()
    s = re.split(r"[T ]", s, maxsplit=1)[0]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _date_range(text: object) -> tuple[Optional[date], Optional[date]]:
    """'14.02.27 - 18.02.27' -> (from, to), whichever order it was written in."""
    found = [_date(m) for m in re.findall(r"\d{1,2}[./]\d{1,2}[./]\d{2,4}", str(text or ""))]
    found = sorted(d for d in found if d)
    if not found:
        return None, None
    return found[0], (found[-1] if len(found) > 1 else None)


def _clean(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _int(value: object) -> int:
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return 0


_FLIGHT_INCLUDED = "טיסה הלוך ושוב"


def _stars(value: object) -> Optional[Decimal]:
    """Hotel star rating as a Decimal: 4, '4.5', '4,5', '5★', '4 כוכבים'.

    None when the value is missing, zero (= "not rated" on these sites) or not
    a number, so an unknown rating can never pass as a good one.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        raw = str(value)
    elif isinstance(value, str):
        tokens = value.replace("★", " ").replace("*", " ").replace(",", ".").split()
        raw = tokens[0] if tokens else ""
    else:
        return None
    try:
        d = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite() or d <= 0:
        return None
    return d.normalize()  # '4.50' -> 4.5, '5.0' -> 5


def _star_label(stars: Optional[Decimal]) -> str:
    """' (4★)' / ' (4.5★)', or '' when the rating is unknown."""
    return f" ({stars:f}★)" if stars else ""


_MIN_STARS = Decimal("3")
_MAX_STARS = Decimal("5")
_UPPER_TIER_STARS = Decimal("4")  # 4–5★; 3–3.5★ is the capped lower tier


def _hotel_qualifies(deal: VacationDeal) -> bool:
    """Packages need a verified 3–5★ hotel (3, 3.5 … 5 pass); flights always pass.

    A missing or unreadable rating fails: an unknown hotel is never shown.
    """
    if deal.kind != KIND_PACKAGE:
        return True
    stars = _stars(deal.stars)
    return stars is not None and _MIN_STARS <= stars <= _MAX_STARS


def _is_three_star(deal: VacationDeal) -> bool:
    """A package whose hotel is in the 3★ tier (3 or 3.5 stars)."""
    if deal.kind != KIND_PACKAGE:
        return False
    stars = _stars(deal.stars)
    return stars is not None and stars < _UPPER_TIER_STARS


def _three_star_cap(rows: int) -> int:
    """How many of `rows` may be 3★: a third, rounded down."""
    return rows // 3


def _hotel_line(name: object, stars: object = None) -> str:
    name = _clean(name)
    if not name:
        return ""
    return f"מלון {name}" + _star_label(_stars(stars))


def _includes(*parts: object) -> list[str]:
    """Drop empty entries and duplicates, keep order."""
    out: list[str] = []
    for part in parts:
        text = _clean(part)
        if text and text not in out:
            out.append(text)
    return out


def _abs_url(base: str, href: object) -> str:
    """Join + percent-encode (some sites put brackets / spaces / Hebrew in links)."""
    return quote(urljoin(base, str(href or "")), safe=":/?&=%#+,;@~")


def _client(**extra_headers: str) -> httpx.Client:
    return httpx.Client(
        follow_redirects=True, timeout=_TIMEOUT, headers={**_HEADERS, **extra_headers},
    )


# ---- Sources ----

def _scan_issta() -> list[VacationDeal]:
    base = "https://www.issta.co.il"
    with _client() as c:
        r = c.get(f"{base}/deals/last-call")
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    out: list[VacationDeal] = []
    for price_box in soup.select(".deal-Price"):
        card = price_box.find_parent("a")
        price_el = price_box.select_one('[itemprop="price"]')
        if card is None or price_el is None:
            continue
        price = _dec(price_el.get("content"))
        if price is None:
            continue
        cur_el = price_box.select_one('[itemprop="priceCurrency"]')
        title_el = card.select_one('[itemprop="name"]')
        title = _clean(title_el.get_text() if title_el else "")
        href = card.get("href", "")
        # The last-call page mixes packages with flight-only deals.
        is_flight = "/flights/" in href or title.startswith("טיסות")
        destination = re.sub(r"^(?:דיל(?:\s+\S+)*?|טיסות)\s+ל", "", title, count=1) or title
        inner = parse_qs(urlparse(href.replace("/loader?url=", "", 1)).query)
        desc_el = card.select_one('[itemprop="description"]')
        prefix_el = price_box.select_one(".deal-PricePrefix span")
        # The card shows no rating; the package link carries the hotel star
        # level the deal was priced at ("…&stars=3"). Organised tours have none.
        stars = None if is_flight else _stars((inner.get("stars") or [None])[0])
        out.append(VacationDeal(
            source="איסתא", kind=KIND_FLIGHT if is_flight else KIND_PACKAGE,
            destination=destination, price=price,
            currency=_currency(cur_el.get("content") if cur_el else "$"),
            url=_abs_url(base, href),
            date_from=_date((inner.get("fdate") or [""])[0]),
            date_to=_date((inner.get("tdate") or [""])[0]),
            hotel=_clean(desc_el.get_text() if desc_el else ""),
            note=_clean(prefix_el.get_text() if prefix_el else "").rstrip(":"),
            includes=[] if is_flight else _includes(
                _FLIGHT_INCLUDED,
                ("מלון: " + _clean(desc_el.get_text()) if desc_el else "מלון") + _star_label(stars),
            ),
            adults=_int((inner.get("adt1") or inner.get("padt") or [0])[0]),
            children=_int((inner.get("chd1") or inner.get("pchd") or [0])[0]),
            stars=stars,
        ))
    return out


def _scan_eshet() -> list[VacationDeal]:
    base = "https://www.eshet.com"
    with _client() as c:
        r = c.get(f"{base}/deals/")
    r.raise_for_status()
    script = BeautifulSoup(r.text, "lxml").find("script", id="__NEXT_DATA__")
    if script is None or not script.string:
        raise ValueError("deal data not found in page")
    groups = json.loads(script.string)["props"]["initialState"]["general"]["promotionGroups"]
    out: list[VacationDeal] = []
    for group in groups or []:
        for p in group.get("Promotions") or []:
            price_info = p.get("Price") or {}
            price = _dec(price_info.get("PriceToShow") or price_info.get("AvgPriceAfterDiscount"))
            destination = _clean(p.get("DestinationName"))
            if price is None or not destination:
                continue
            stars = _stars(p.get("Stars"))
            hotel = _clean(p.get("HotelName") or p.get("SubTitle"))
            capacity = p.get("Capacity") or {}
            out.append(VacationDeal(
                source="אשת טורס", kind=KIND_PACKAGE, destination=destination, price=price,
                currency=_currency(price_info.get("CurrencySymbole")),
                url=_abs_url(base, p.get("PromoLink")),
                date_from=_date(p.get("StartDate")), date_to=_date(p.get("EndDate")),
                nights=(p.get("Conditions") or {}).get("TotalNights") or None,
                hotel=hotel + (_star_label(stars) if hotel else ""),
                note=_clean(p.get("BoardHebrew")),
                includes=_includes(
                    _FLIGHT_INCLUDED, _hotel_line(hotel, stars), p.get("BoardHebrew"),
                    p.get("MarketingNote"),
                ),
                adults=_int(capacity.get("Adults")), children=_int(capacity.get("Children")),
                total_price=_dec(price_info.get("PriceAfterDiscount")),
                stars=stars,
            ))
    return out


def _scan_flying_carpet() -> list[VacationDeal]:
    base = "https://www.flying.co.il"
    api = "https://apipck.flyingisr.com/api"
    with _client(Accept="application/json", Referer=f"{base}/") as c:
        # The site first asks which agency a host belongs to, then sends that
        # id with every data call.
        host = c.get(f"{api}/agencyContents/byHost/www.flying.co.il", params={"langCode": "HEB"})
        host.raise_for_status()
        info = host.json()
        agency = info.get("agencyCode") or info.get("code")
        if not agency:
            raise ValueError("agency id not found")
        r = c.get(
            f"{api}/deal/inEffectHomeV3",
            params={"dealType": "vacation_pack", "moreDeals": "false",
                    "diversifyByDest": "true", "langCode": "HEB"},
            headers={"agency": str(agency)},
        )
    r.raise_for_status()

    def _he(value: object) -> str:
        return _clean(value.get("he") if isinstance(value, dict) else value)

    out: list[VacationDeal] = []
    for d in r.json() or []:
        price = _dec((d.get("discountedPrice") or {}).get("priceAdl")
                     or (d.get("originalPrice") or {}).get("priceAdl"))
        destination = _he(d.get("destinationName"))
        if price is None or not destination:
            continue
        stars = _stars(d.get("stars"))
        hotel = _he(d.get("hotelName"))
        transfers = _clean(d.get("transfers"))
        if transfers:
            transfers = f"העברות: {transfers}"
        elif "without transfers" in _he(d.get("subjectName")).lower():
            transfers = "ללא העברות"
        bag = next((b for b in d.get("chargedBaggagePrices") or []
                    if b.get("carryOnBaggage") or b.get("checkinBaggage")), None)
        baggage = ""
        if bag:
            baggage = "כבודה בתוספת תשלום לכיוון: " + ", ".join(x for x in (
                f"טרולי ${bag['carryOnBaggage']}" if bag.get("carryOnBaggage") else "",
                f"מזוודה ${bag['checkinBaggage']}" if bag.get("checkinBaggage") else "",
            ) if x)
        out.append(VacationDeal(
            source="השטיח המעופף", kind=KIND_PACKAGE, destination=destination, price=price,
            currency=_currency(d.get("currenySymbol")),
            url=_abs_url(base, d.get("selectedPackageUrl")),
            country=_he(d.get("destinationCountryName")).title(),
            date_from=_date(d.get("departureDate")), date_to=_date(d.get("arrivalDate")),
            nights=d.get("nights") or None,
            hotel=hotel + (_star_label(stars) if hotel else ""),
            note=_clean(d.get("priceRemark")),
            includes=_includes(
                _FLIGHT_INCLUDED, _hotel_line(hotel, stars), _he(d.get("boardBasisName")),
                _he(d.get("roomTypeName")), transfers, baggage,
            ),
            adults=_int(d.get("adult")), children=_int(d.get("child")),
            total_price=_dec((d.get("discountedPrice") or {}).get("price")),
            stars=stars,
        ))
    return out


def _scan_gulliver() -> list[VacationDeal]:
    base = "https://www.gulliver.co.il"
    body = {
        "DestCode": None, "OriginCode": None, "PageId": -1, "DestCode2": None,
        "IsCountry": False, "IsSEO": False, "ModuleTypeId": -1,
        "Url": quote(f"{base}/", safe=""), "FetchHeaderData": False,
        "FetchFooterData": False, "FetchPageData": True, "BaseUrl": base,
        "IsRegion": False, "HotelId": -1, "StructureKey": None, "Patterns": None,
        "RuleId": -1, "GulliverExperimentId": None, "GulliverExperimentVariation": None,
        "IsForce": False,
    }
    with _client(Accept="application/json", Referer=f"{base}/") as c:
        r = c.post(f"{base}/Services/GulliverAPI.svc/InitPage", json=body)
    r.raise_for_status()
    groups = ((r.json().get("PageData") or {}).get("HotDealsData") or {}).get("HotDealsGroups") or []
    out: list[VacationDeal] = []
    for group in groups:
        for d in group.get("HotDeals") or []:
            price = _dec(d.get("UsdPrice"))
            title = _clean(d.get("Title"))
            if price is None or not title:
                continue
            destination, _, country = title.partition(",")
            deal_type = _clean(d.get("DealTypeTitle"))
            stars = _stars(d.get("HotelStars"))  # charter flights send the string "0"
            is_package = bool(
                d.get("HotelId") or stars
                or "חביל" in deal_type or "מלון" in deal_type
            )
            d_from, d_to = _date_range(d.get("DatesTitle"))
            out.append(VacationDeal(
                source="גוליבר", kind=KIND_PACKAGE if is_package else KIND_FLIGHT,
                destination=destination.strip(), price=price, currency="USD",
                url=_abs_url(base, d.get("TargetUrl")), country=country.strip(),
                date_from=d_from or _date(d.get("StartDate")), date_to=d_to,
                hotel=_clean(d.get("DealName")) if is_package else "",
                note=" · ".join(x for x in (
                    _clean(d.get("DealTypeTitle")),
                    "" if is_package else _clean(d.get("DealName")),
                    _clean(d.get("BoardDescription")),
                ) if x),
                includes=_includes(
                    _FLIGHT_INCLUDED, _hotel_line(d.get("DealName"), d.get("HotelStars")),
                    d.get("BoardDescription"),
                ) if is_package else [],
                adults=_int(d.get("NumOfAdults")), children=_int(d.get("NumOfChildren")),
                stars=stars if is_package else None,
            ))
    return out


def _hulyo_baggage(product: dict) -> str:
    """'מזוודה לא כלולה · טרולי כלול' — only what holds on every flight leg."""
    legs = [leg.get("baggageAllowance") or {} for leg in product.get("legRefs") or []]
    parts = []
    for key, label, suffix in (("baggage", "מזוודה", "ה"), ("trolley", "טרולי", "")):
        values = {leg.get(key) for leg in legs}
        if values == {"include"}:
            parts.append(f"{label} כלול{suffix}")
        elif values == {"notInclude"}:
            parts.append(f"{label} לא כלול{suffix}")
    return " · ".join(parts)


def _scan_hulyo() -> list[VacationDeal]:
    base = "https://www.hulyo.co.il"
    out: list[VacationDeal] = []
    with _client(Accept="application/json", Referer=f"{base}/") as c:
        for fname, kind in (("packages.json", KIND_PACKAGE),
                            ("flights-round-trip-v2.json", KIND_FLIGHT)):
            r = c.get(f"{base}/dynamic/client/{fname}")
            r.raise_for_status()
            data = r.json()
            products = data.get("productOverrides") or {}
            base_products = data.get("baseProducts") or {}
            hotels = data.get("hotels") or {}
            # catalogItems = the cheapest product of every destination.
            for item in data.get("catalogItems") or []:
                price = _dec(item.get("sellingPrice"))
                destination = _clean(item.get("destination"))
                if price is None or not destination:
                    continue
                product = products.get(item.get("productId")) or {}
                base_product = base_products.get(product.get("baseProductCode")) or {}
                hotel = hotels.get(str(base_product.get("hotelCode"))) or {}
                selling = product.get("sellingPrice") or {}
                board = _clean(product.get("boardBaseDesc") or base_product.get("boardBaseDesc"))
                pax = str(base_product.get("paxComplect") or "").upper()  # "AA" = two adults
                room = _clean(product.get("roomType") or base_product.get("roomType")).title()
                out.append(VacationDeal(
                    source="חוליו", kind=kind, destination=destination, price=price,
                    currency=_currency(selling.get("currency") or "USD"),
                    url=f"{base}/", country=_clean(item.get("countryName")),
                    date_from=_date(product.get("fromDate") or item.get("fromDate")),
                    date_to=_date(product.get("toDate")),
                    nights=product.get("nights") or base_product.get("nights") or None,
                    hotel=_clean(hotel.get("name")) if kind == KIND_PACKAGE else "",
                    note=board,
                    includes=_includes(
                        _FLIGHT_INCLUDED, _hotel_line(hotel.get("name"), hotel.get("rating")),
                        board, room, _hulyo_baggage(product),
                    ) if kind == KIND_PACKAGE else [],
                    adults=pax.count("A") if kind == KIND_PACKAGE else 0,
                    children=pax.count("C") if kind == KIND_PACKAGE else 0,
                    stars=_stars(hotel.get("rating")) if kind == KIND_PACKAGE else None,
                ))
    return out


def _scan_travelist() -> list[VacationDeal]:
    base = "https://www.travelist.co.il"
    with _client(Accept="application/json", Referer=f"{base}/") as c:
        r = c.get(f"{base}/config-data/client/flights-deals.json")
    r.raise_for_status()
    out: list[VacationDeal] = []
    for d in r.json().get("resources") or []:
        price = _dec(d.get("price"))
        destination = _clean(d.get("title"))
        if price is None or not destination:
            continue
        d_from, d_to = _date_range(d.get("date"))
        out.append(VacationDeal(
            source="טרווליסט", kind=KIND_FLIGHT, destination=destination, price=price,
            currency=_currency(d.get("currency")),
            url=_abs_url(base, (d.get("location") or {}).get("href")),
            country=_clean(d.get("subTitle")), date_from=d_from, date_to=d_to,
        ))
    return out


def _scan_secret_flights() -> list[VacationDeal]:
    base = "https://secretflights.co.il"
    params = {
        "params": "?origin=TLV&destination=&affid=8&cheapest=true&language=he&currency",
        "type": "get_deals",
    }
    with _client(Accept="application/json", Referer=f"{base}/") as c:
        r = c.get(f"{base}/wp-json/onix/v1/get-api-data", params=params)
    r.raise_for_status()
    out: list[VacationDeal] = []
    for d in r.json() or []:
        price = _dec(d.get("price_usd") or d.get("price"))
        destination = _clean(d.get("destination_city"))
        if price is None or not destination:
            continue
        out.append(VacationDeal(
            source="טיסות סודיות", kind=KIND_FLIGHT, destination=destination, price=price,
            currency="USD", url=str(d.get("fly_url") or f"{base}/"),
            country=_clean(d.get("destination_country")),
            date_from=_date(d.get("date_from")), date_to=_date(d.get("date_to")),
            nights=d.get("nights") or None,
            note=" · ".join(x for x in (
                "טיסה ישירה" if d.get("direct") else "", _clean(d.get("airline_name")),
            ) if x),
        ))
    return out


_SOURCES: tuple[tuple[str, Callable[[], list[VacationDeal]]], ...] = (
    ("איסתא", _scan_issta),
    ("אשת טורס", _scan_eshet),
    ("השטיח המעופף", _scan_flying_carpet),
    ("גוליבר", _scan_gulliver),
    ("חוליו", _scan_hulyo),
    ("טרווליסט", _scan_travelist),
    ("טיסות סודיות", _scan_secret_flights),
)


# ---- Orchestrator ----

def _rank(deals: list[VacationDeal], top_n: int,
          max_three_star: Optional[int] = None) -> list[VacationDeal]:
    """Cheapest first: one row per (source, destination, hotel tier), max 3 per destination.

    3★ packages are held to a third: at most one of a destination's three
    rows, and at most `max_three_star` rows overall (default: a third of
    `top_n`). A skipped 3★ row leaves its place to the next 4–5★ one.
    """
    if max_three_star is None:
        max_three_star = _three_star_cap(top_n)
    cheapest: dict[tuple[str, str, bool], VacationDeal] = {}
    for d in deals:
        key = (d.source, d.destination, _is_three_star(d))
        if key not in cheapest or d.price < cheapest[key].price:
            cheapest[key] = d
    # Non-dollar prices can't be compared without a rate, so they sort last.
    ordered = sorted(cheapest.values(), key=lambda d: (d.currency != "USD", d.price))
    per_destination: dict[str, int] = {}
    three_per_destination: dict[str, int] = {}
    three_total = 0
    out: list[VacationDeal] = []
    for d in ordered:
        if per_destination.get(d.destination, 0) >= _MAX_PER_DESTINATION:
            continue
        if _is_three_star(d):
            if (three_total >= max_three_star
                    or three_per_destination.get(d.destination, 0)
                    >= _three_star_cap(_MAX_PER_DESTINATION)):
                continue
            three_total += 1
            three_per_destination[d.destination] = three_per_destination.get(d.destination, 0) + 1
        per_destination[d.destination] = per_destination.get(d.destination, 0) + 1
        out.append(d)
        if len(out) >= top_n:
            break
    return out


def _deal_label(d: VacationDeal) -> str:
    return f"{d.source}: {d.destination}" + (f" ({d.country})" if d.country else "")


def _country_code(d: VacationDeal) -> str:
    hit = _continents.classify(d.destination, d.country)
    return hit[0] if hit else ""


def _classify(deals: list[VacationDeal]) -> tuple[list[VacationDeal], list[str]]:
    """Set each deal's continent. Returns (placed deals, labels of unplaced deals).

    Domestic deals (Israel, e.g. a flight to Eilat) are not trips abroad and
    are dropped without counting as unplaced.
    """
    placed: list[VacationDeal] = []
    unmapped: list[str] = []
    for d in deals:
        hit = _continents.classify(d.destination, d.country)
        if hit is None:
            unmapped.append(_deal_label(d))
            log.warning("Vacation scan: no continent for %s", _deal_label(d))
            continue
        if hit[1] == _continents.DOMESTIC:
            log.info("Vacation scan: domestic deal left out: %s", _deal_label(d))
            continue
        d.continent = hit[1]
        placed.append(d)
    return placed, unmapped


def _select_asia(ranked: list[VacationDeal], limit: int) -> tuple[list[VacationDeal], list[str]]:
    """Up to `limit` deals from `ranked` (already in display order), reserving a
    slot for the cheapest Japan and the cheapest Thailand deal. Returns
    (chosen, English names of required countries with no deal)."""
    chosen: list[VacationDeal] = []
    missing: list[str] = []
    three_cap = _three_star_cap(limit)

    def _fits(d: VacationDeal) -> bool:
        """False for a 3★ row once a third of the list is already 3★."""
        return not _is_three_star(d) or sum(map(_is_three_star, chosen)) < three_cap

    for code in _ASIA_REQUIRED:
        in_country = [d for d in ranked if _country_code(d) == code]
        if not in_country:
            missing.append(_continents.country_name(code))
            continue
        # Prefer one that keeps 3★ within a third; if the country only has
        # 3★ left, the Japan/Thailand rule wins over the 3★ share.
        pick = next((d for d in in_country if _fits(d)), in_country[0])
        if len(chosen) < limit:
            chosen.append(pick)
    for d in ranked:
        if len(chosen) >= limit:
            break
        if not any(c is d for c in chosen) and _fits(d):
            chosen.append(d)
    order = {id(d): i for i, d in enumerate(ranked)}
    return sorted(chosen, key=lambda d: order[id(d)]), missing


def _continent_groups(deals: list[VacationDeal], today: date) -> list[ContinentGroup]:
    """One kind's deals (continent already set) -> a group per continent, in order."""
    groups: list[ContinentGroup] = []
    first = _first_departure(today)
    for continent in _continents.CONTINENT_ORDER:
        days = _continent_days(continent)
        limit = _continent_limit(continent)
        candidates = [
            d for d in deals
            if d.continent == continent and d.date_from is not None and _length_ok(d)
            and first <= d.date_from <= today + timedelta(days=_deal_days(d, continent))
        ]
        group = ContinentGroup(continent=continent, limit=limit, window_days=days)
        if continent == _continents.ASIA:
            # Rank everything; _select_asia applies the 3★ share to its 4 rows.
            ranked = _rank(candidates, len(candidates) or 1, max_three_star=len(candidates))
            group.deals, group.missing_required = _select_asia(ranked, limit)
        else:
            group.deals = _rank(candidates, limit)
        groups.append(group)
    return groups


def scan_and_report() -> VacationReport:
    """Scan every source in parallel and return the report, grouped by continent."""
    log.info("Running vacation deal scan across %d sites", len(_SOURCES))
    today = date.today()
    days = _window_days()
    # The longest continent or site window (each is already capped by PW_DEALS_DAYS).
    last_day = today + timedelta(days=max(
        _europe_days(), _other_days(), *source_windows().values()))
    # Departures start PW_DEALS_LEAD_DAYS (48 hours) out; the far end is unchanged.
    first_day = _first_departure(today)
    report = VacationReport(days_ahead=days)

    def _in_window(d: VacationDeal) -> bool:
        # Undated deals can't be placed in a window.
        return d.date_from is not None and first_day <= d.date_from <= last_day

    def _one(source: tuple[str, Callable[[], list[VacationDeal]]]):
        name, fn = source
        try:
            return name, fn(), None
        except Exception as exc:  # one bad site must not sink the scan
            return name, [], f"{type(exc).__name__}: {exc}"[:160]

    found: list[VacationDeal] = []
    with ThreadPoolExecutor(max_workers=len(_SOURCES)) as pool:
        for name, deals, error in pool.map(_one, _SOURCES):
            live = [d for d in deals if d.price > 0 and _in_window(d)]
            sized = [d for d in live if _length_ok(d)]
            if len(sized) < len(live):
                log.info("Vacation scan: %s: dropped %d of %d deals (trip not %d-%d nights)",
                         name, len(live) - len(sized), len(live), _min_nights(), _max_nights())
                report.dropped_length += len(live) - len(sized)
            live = sized
            kept = [d for d in live if _hotel_qualifies(d)]
            if len(kept) < len(live):
                log.info("Vacation scan: %s: dropped %d of %d packages (hotel not a verified 3–5★)",
                         name, len(live) - len(kept),
                         sum(1 for d in live if d.kind == KIND_PACKAGE))
                report.dropped_low_star += len(live) - len(kept)
            live = kept
            if error:
                log.warning("Vacation scan: %s failed: %s", name, error)
            else:
                log.info("Vacation scan: %s: %d deals", name, len(live))
            report.sources.append(SourceStatus(name, ok=error is None, count=len(live), error=error))
            found.extend(live)

    report.total_found = len(found)
    placed, report.unmapped = _classify(found)
    if report.unmapped:
        log.warning("Vacation scan: %d deal(s) with no continent left out", len(report.unmapped))
    report.package_groups = _continent_groups([d for d in placed if d.kind == KIND_PACKAGE], today)
    report.flight_groups = _continent_groups([d for d in placed if d.kind == KIND_FLIGHT], today)
    report.packages = [d for g in report.package_groups for d in g.deals]
    report.flights = [d for g in report.flight_groups for d in g.deals]
    if not found:
        report.status = "fetch_failed"
        report.error = "לא נמצאו דילים באף אתר" + (f" ל-{days} הימים הקרובים" if days else "")
    return report


# ---- Terminal output ----

def print_report(report: VacationReport) -> None:
    for s in report.sources:
        mark = "[green]ok[/green]" if s.ok else f"[red]failed[/red] {s.error or ''}"
        console.print(f"  [dim]{s.name}: {s.count} deals[/dim] {mark}")
    if report.status != "ok":
        console.print(f"[red]Vacation scan failed:[/red] {report.error or ''}")
        return
    if report.unmapped:
        console.print(f"  [yellow]{len(report.unmapped)} deal(s) with no continent left out:[/yellow] "
                      + "; ".join(report.unmapped[:10]))
    for kind, groups in (("Packages", report.package_groups), ("Flights", report.flight_groups)):
        for g in groups:
            title = (f"{kind} — {g.continent} ({len(g.deals)}/{g.limit}, "
                     f"departing within {g.window_days} days)")
            if g.missing_required:
                title += f" — no {' / '.join(g.missing_required)} deal"
            if not g.deals:
                console.print(f"[dim]{title}: none[/dim]")
                continue
            table = Table(title=title, show_header=True, header_style="bold")
            for col in ("#", "Price", "Destination", "Dates", "Nights", "Source", "Hotel / note"):
                table.add_column(col)
            for i, d in enumerate(g.deals, 1):
                dates = (
                    f"{d.date_from:%d/%m}" + (f"-{d.date_to:%d/%m}" if d.date_to else "")
                    if d.date_from else "—"
                )
                table.add_row(
                    str(i), f"{d.price} {d.currency}",
                    d.destination + (f", {d.country}" if d.country else ""),
                    dates, str(d.nights_or_computed or "—"), d.source,
                    " · ".join(x for x in (d.hotel, d.note) if x)[:60],
                )
            console.print(table)
