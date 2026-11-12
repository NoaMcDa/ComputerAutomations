"""Vacation deal scanner (packages + flights from Israel).

Reads the deal listings that Israeli travel sites publish on their own deal
pages and merges them into one ranked list:

  איסתא            issta.co.il/deals/last-call        (page HTML)
  אשת טורס         eshet.com/deals/                   (data embedded in the page)
  השטיח המעופף     flying.co.il home-page package feed
  גוליבר           gulliver.co.il home-page hot deals
  חוליו            hulyo.co.il catalogue files
  לאסט מינט        lastminute.co.il deal feed
  טרווליסט         travelist.co.il flight-deal file
  טיסות סודיות     secretflights.co.il deal feed

Every source is plain HTTP, runs independently, and a failing source never
sinks the others. Prices are per person as each site states them.

The report keeps, for packages and for flights separately, the cheapest
`PW_DEALS_TOP` deals (default 15): one deal per source and destination, and
at most `_MAX_PER_DESTINATION` rows for the same destination.
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

from price_watcher import logging_setup

log = logging_setup.get(__name__)
console = Console()

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "he,en;q=0.8,en-US;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
}
_TIMEOUT = 45

KIND_PACKAGE = "package"
KIND_FLIGHT = "flight"

_MAX_PER_DESTINATION = 3
_CURRENCY_CODES = {"$": "USD", "₪": "ILS", "€": "EUR", "£": "GBP"}


def _window_days() -> int:
    """Only keep deals departing within this many days. 0 (default) = no limit."""
    try:
        return max(0, int(os.environ.get("PW_DEALS_DAYS", "0")))
    except ValueError:
        return 0


def _top_n() -> int:
    try:
        return max(1, int(os.environ.get("PW_DEALS_TOP", "15")))
    except ValueError:
        return 15


# ---- Data model ----

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
class VacationReport:
    status: str = "ok"              # "ok" / "fetch_failed"
    packages: list[VacationDeal] = field(default_factory=list)
    flights: list[VacationDeal] = field(default_factory=list)
    sources: list[SourceStatus] = field(default_factory=list)
    total_found: int = 0            # before dedupe / top-N cut
    days_ahead: int = 0             # departure window applied (0 = none)
    error: Optional[str] = None

    @property
    def deals(self) -> list[VacationDeal]:
        return self.packages + self.flights


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


def _hotel_line(name: object, stars: object = 0) -> str:
    name = _clean(name)
    if not name:
        return ""
    n = _int(stars)
    return f"מלון {name}" + (f" ({n}★)" if n else "")


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
                ("מלון: " + _clean(desc_el.get_text())) if desc_el else "מלון",
            ),
            adults=_int((inner.get("adt1") or inner.get("padt") or [0])[0]),
            children=_int((inner.get("chd1") or inner.get("pchd") or [0])[0]),
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
            stars = p.get("Stars") or 0
            hotel = _clean(p.get("HotelName") or p.get("SubTitle"))
            capacity = p.get("Capacity") or {}
            out.append(VacationDeal(
                source="אשת טורס", kind=KIND_PACKAGE, destination=destination, price=price,
                currency=_currency(price_info.get("CurrencySymbole")),
                url=_abs_url(base, p.get("PromoLink")),
                date_from=_date(p.get("StartDate")), date_to=_date(p.get("EndDate")),
                nights=(p.get("Conditions") or {}).get("TotalNights") or None,
                hotel=hotel + (f" ({stars}★)" if hotel and stars else ""),
                note=_clean(p.get("BoardHebrew")),
                includes=_includes(
                    _FLIGHT_INCLUDED, _hotel_line(hotel, stars), p.get("BoardHebrew"),
                    p.get("MarketingNote"),
                ),
                adults=_int(capacity.get("Adults")), children=_int(capacity.get("Children")),
                total_price=_dec(price_info.get("PriceAfterDiscount")),
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
        stars = d.get("stars") or 0
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
            hotel=hotel + (f" ({stars}★)" if hotel and stars else ""),
            note=_clean(d.get("priceRemark")),
            includes=_includes(
                _FLIGHT_INCLUDED, _hotel_line(hotel, stars), _he(d.get("boardBasisName")),
                _he(d.get("roomTypeName")), transfers, baggage,
            ),
            adults=_int(d.get("adult")), children=_int(d.get("child")),
            total_price=_dec((d.get("discountedPrice") or {}).get("price")),
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
            is_package = bool(
                d.get("HotelId") or d.get("HotelStars")
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
                ))
    return out


_LASTMINUTE_EUROPE = [
    "BER", "MAD", "BCN", "BUS", "ZRH", "VCE", "SOF", "GVA", "ROM", "CHQ", "SKG", "ZTH",
    "JTR", "AMS", "ATH", "CFU", "KGS", "HER", "JMK", "RHO", "PRG", "PAR", "DXB", "LON",
    "LCA", "LIS", "TBS", "TIV", "BOJ", "BAK", "PAS", "PFO", "VIE", "MIL", "OTP", "WAW",
    "BUD", "SEZ",
]


def _scan_lastminute() -> list[VacationDeal]:
    base = "https://www.lastminute.co.il/"
    today = date.today()
    window = {
        "FromDate": f"{today.isoformat()}T00:00:00",
        "MaxFromDate": f"{(today + timedelta(days=_window_days() or 240)).isoformat()}T00:00:00",
    }
    common = {
        "DealBoxType": 1, "MinPrice": 0, "DynamicDate": 0, "MaxNumberOfDeals": 30,
        "DepartureCodes": ["TLV"], "ArrivalCodes": _LASTMINUTE_EUROPE,
        "FlightDirectType": 1, "AirlineCodes": [], "FlightCabin": [], **window,
    }
    request = {
        "flight": [{**common, "Title": "טיסות", "MaxPrice": 600, "ProductType": 0, "DataSorce": 0}],
        "dynamic": [{**common, "Title": "חופשות", "MaxPrice": 900, "ProductType": 1,
                     "DataSorce": 1, "StarsRating": 0, "MinNights": 2, "MaxNights": 30,
                     "HotelName": ""}],
        "flightToAnywhere": [],
    }
    with _client(Accept="application/json", Referer=base) as c:
        r = c.get(f"{base}api/Deals/GetDeals",
                  params={"request": json.dumps(request, ensure_ascii=False)})
    r.raise_for_status()
    out: list[VacationDeal] = []
    for group in r.json() or []:
        for d in group.get("Deals") or []:
            price = _dec(d.get("PriceActual"))
            title = _clean(d.get("Title"))
            if price is None or price <= 0 or not title:
                continue
            is_package = d.get("ProductType") == 1
            destination = re.sub(r"^(?:טיסות|טיסה\s*\+\s*מלון)\s+ל", "", title, count=1) or title
            out.append(VacationDeal(
                source="לאסט מינט", kind=KIND_PACKAGE if is_package else KIND_FLIGHT,
                destination=destination, price=price.quantize(Decimal("1")),
                currency=_currency(d.get("Currency")), url=_abs_url(base, d.get("Url")),
                date_from=_date(d.get("DateFrom")), date_to=_date(d.get("DateTo")),
                note=" · ".join(x for x in (
                    _clean(d.get("SubTitle")), _clean(d.get("PricePrefix")),
                ) if x),
                includes=_includes(_FLIGHT_INCLUDED, "מלון", d.get("SubTitle")) if is_package else [],
                adults=_int(d.get("Adults")) if is_package else 0,
                children=(_int(d.get("Childs")) + _int(d.get("Youth"))) if is_package else 0,
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
    ("לאסט מינט", _scan_lastminute),
    ("טרווליסט", _scan_travelist),
    ("טיסות סודיות", _scan_secret_flights),
)


# ---- Orchestrator ----

def _rank(deals: list[VacationDeal], top_n: int) -> list[VacationDeal]:
    """Cheapest first: one row per (source, destination), max 3 per destination."""
    cheapest: dict[tuple[str, str], VacationDeal] = {}
    for d in deals:
        key = (d.source, d.destination)
        if key not in cheapest or d.price < cheapest[key].price:
            cheapest[key] = d
    # Non-dollar prices can't be compared without a rate, so they sort last.
    ordered = sorted(cheapest.values(), key=lambda d: (d.currency != "USD", d.price))
    per_destination: dict[str, int] = {}
    out: list[VacationDeal] = []
    for d in ordered:
        if per_destination.get(d.destination, 0) >= _MAX_PER_DESTINATION:
            continue
        per_destination[d.destination] = per_destination.get(d.destination, 0) + 1
        out.append(d)
        if len(out) >= top_n:
            break
    return out


def scan_and_report() -> VacationReport:
    """Scan every source in parallel and return the ranked report."""
    log.info("Running vacation deal scan across %d sites", len(_SOURCES))
    today = date.today()
    days = _window_days()
    last_day = today + timedelta(days=days) if days else None
    report = VacationReport(days_ahead=days)

    def _in_window(d: VacationDeal) -> bool:
        if d.date_from is None:
            return last_day is None  # undated deals can't be placed in a window
        return d.date_from >= today and (last_day is None or d.date_from <= last_day)

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
            if error:
                log.warning("Vacation scan: %s failed: %s", name, error)
            else:
                log.info("Vacation scan: %s: %d deals", name, len(live))
            report.sources.append(SourceStatus(name, ok=error is None, count=len(live), error=error))
            found.extend(live)

    report.total_found = len(found)
    top_n = _top_n()
    report.packages = _rank([d for d in found if d.kind == KIND_PACKAGE], top_n)
    report.flights = _rank([d for d in found if d.kind == KIND_FLIGHT], top_n)
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
    for title, deals in (("Packages", report.packages), ("Flights", report.flights)):
        table = Table(title=title, show_header=True, header_style="bold")
        for col in ("#", "Price", "Destination", "Dates", "Nights", "Source", "Hotel / note"):
            table.add_column(col)
        for i, d in enumerate(deals, 1):
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
