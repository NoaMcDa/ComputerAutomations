"""Hotel star rating + 4–5★ package filter for the vacation-deal scan (#52).

Every scanner is fed a canned response through httpx.MockTransport. No network.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest

from price_watcher import vacations as vac

D = Decimal


# --------------------------------------------------------------------------- helpers

def _serve(monkeypatch, routes: dict[str, object]) -> None:
    """Point every scanner's HTTP client at canned responses keyed by URL fragment."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for fragment, body in routes.items():
            if fragment in url:
                if isinstance(body, str):
                    return httpx.Response(200, text=body)
                return httpx.Response(200, json=body)
        return httpx.Response(404, text=f"no canned response for {url}")

    def client(**extra_headers):
        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True,
                            headers={**vac._HEADERS, **extra_headers})

    monkeypatch.setattr(vac, "_client", client)


# Dated inside the Europe window: scan_and_report drops undated deals (#53).
_SOON = date.today() + timedelta(days=10)
# A 4-night trip: scan_and_report keeps only 3-5 night trips.
_BACK = _SOON + timedelta(days=4)


def _pkg(stars, source="src", destination="Athens", price="300") -> vac.VacationDeal:
    return vac.VacationDeal(source=source, kind=vac.KIND_PACKAGE, destination=destination,
                            price=D(price), url="https://example.invalid/", stars=stars,
                            date_from=_SOON, date_to=_BACK)


def _flight(source="src", destination="Rome", price="150") -> vac.VacationDeal:
    return vac.VacationDeal(source=source, kind=vac.KIND_FLIGHT, destination=destination,
                            price=D(price), url="https://example.invalid/",
                            date_from=_SOON, date_to=_BACK)


# --------------------------------------------------------------------------- parsing

@pytest.mark.parametrize("raw, expected", [
    (4, D("4")), (5, D("5")), ("4", D("4")), ("4.5", D("4.5")), (4.5, D("4.5")),
    ("4,5", D("4.5")), ("5★", D("5")), ("4.5 ★", D("4.5")), ("4 כוכבים", D("4")),
    (D("4.50"), D("4.5")), ("5.0", D("5")), (" 3 ", D("3")),
])
def test_stars_parses_numbers_and_half_stars(raw, expected):
    assert vac._stars(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "  ", 0, "0", True, False, "abc", "★", "nan",
                                 "inf", -4, [4], {"stars": 4}])
def test_stars_unknown_values_are_none(raw):
    assert vac._stars(raw) is None


def test_hotel_line_shows_half_stars():
    assert vac._hotel_line("Hilton", "4.5") == "מלון Hilton (4.5★)"
    assert vac._hotel_line("Hilton", 5) == "מלון Hilton (5★)"
    assert vac._hotel_line("Hilton", 0) == "מלון Hilton"
    assert vac._hotel_line("Hilton") == "מלון Hilton"


# --------------------------------------------------------------------------- filter

@pytest.mark.parametrize("stars", [D("3"), D("3.5"), D("4"), D("4.5"), D("5"), "3", "4", 4.5, 5])
def test_three_to_five_star_packages_qualify(stars):
    assert vac._hotel_qualifies(_pkg(stars))


@pytest.mark.parametrize("stars", [D("2"), D("2.5"), D("2.9"), D("5.5"), D("6"), None, "garbage", 0])
def test_low_missing_or_garbage_star_packages_are_rejected(stars):
    assert not vac._hotel_qualifies(_pkg(stars))


@pytest.mark.parametrize("stars, three", [(D("3"), True), (D("3.5"), True), (D("4"), False),
                                          (D("5"), False), (None, False)])
def test_three_star_tier(stars, three):
    assert vac._is_three_star(_pkg(stars)) is three
    assert not vac._is_three_star(_flight())


def test_flights_pass_without_a_rating():
    flight = _flight()
    assert flight.stars is None
    assert vac._hotel_qualifies(flight)


# --------------------------------------------------------------------------- per source

def _issta_card(href: str, title: str, desc: str, price: int) -> str:
    return f"""
    <a href="{href}" itemprop="url">
      <span itemprop="name">{title}</span>
      <span itemprop="description">{desc}</span>
      <div class="deal-Price" itemprop="offers">
        <p class="deal-PricePrefix"><span>מחיר לאדם:</span>
          <strong itemprop="price" content="{price}"><small itemprop="priceCurrency" content="$">$</small>{price}</strong>
        </p>
      </div>
    </a>"""


def test_issta_reads_star_level_from_package_link(monkeypatch):
    pkg = "/loader?url=/dynamic-packages/results.aspx?fdate=15/11/2026&tdate=18/11/2026&code=ath&adt1=2&dealId=1&stars={}"
    html = "<html><body>" + "".join([
        _issta_card(pkg.format(4), "דיל רגע אחרון לאתונה", "מגוון מלונות לבחירה", 300),
        _issta_card(pkg.format(3), "דיל רגע אחרון ללרנקה", "מגוון מלונות לבחירה", 200),
        _issta_card("/loader?url=/flights/results.aspx?padt=1&fdate=13/11/2026&aport=pfo&stars=0",
                    "טיסות לפאפוס", "טיסות אל על", 99),
        _issta_card("/loader?url=/trips/details.aspx?did=1&roompax[0].adt=1",
                    "מאורגן: 5 ימים בפראג", "פניני צ'כיה", 900),
    ]) + "</body></html>"
    _serve(monkeypatch, {"issta.co.il/deals/last-call": html})

    deals = vac._scan_issta()

    by_dest = {d.destination: d for d in deals}
    assert by_dest["אתונה"].stars == D("4")
    assert by_dest["אתונה"].includes[-1].endswith("(4★)")
    assert by_dest["לרנקה"].stars == D("3")
    assert by_dest["פאפוס"].kind == vac.KIND_FLIGHT and by_dest["פאפוס"].stars is None
    tour = next(d for d in deals if "מאורגן" in d.destination)
    assert tour.kind == vac.KIND_PACKAGE and tour.stars is None


def test_eshet_stars(monkeypatch):
    promos = [
        {"DestinationName": "רומא", "HotelName": "Grand", "Stars": 5, "Price": {"PriceToShow": 500}},
        {"DestinationName": "אתונה", "HotelName": "Half", "Stars": "4.5", "Price": {"PriceToShow": 400}},
        {"DestinationName": "סופיה", "HotelName": "Basic", "Stars": 3, "Price": {"PriceToShow": 200}},
        {"DestinationName": "באקו", "HotelName": "Unknown", "Price": {"PriceToShow": 250}},
    ]
    data = {"props": {"initialState": {"general": {"promotionGroups": [{"Promotions": promos}]}}}}
    html = f'<html><script id="__NEXT_DATA__">{json.dumps(data)}</script></html>'
    _serve(monkeypatch, {"eshet.com/deals": html})

    by_dest = {d.destination: d for d in vac._scan_eshet()}

    assert by_dest["רומא"].stars == D("5") and by_dest["רומא"].hotel == "Grand (5★)"
    assert by_dest["אתונה"].stars == D("4.5") and by_dest["אתונה"].hotel == "Half (4.5★)"
    assert by_dest["סופיה"].stars == D("3")
    assert by_dest["באקו"].stars is None and by_dest["באקו"].hotel == "Unknown"


def test_flying_carpet_stars(monkeypatch):
    feed = [
        {"destinationName": {"he": "פראג"}, "hotelName": {"he": "Hotel A"}, "stars": 4,
         "discountedPrice": {"priceAdl": 450}},
        {"destinationName": {"he": "וינה"}, "hotelName": {"he": "Hotel B"}, "stars": "4.5",
         "discountedPrice": {"priceAdl": 550}},
        {"destinationName": {"he": "ורנה"}, "hotelName": {"he": "Hotel C"}, "stars": 0,
         "discountedPrice": {"priceAdl": 150}},
    ]
    _serve(monkeypatch, {"agencyContents/byHost": {"agencyCode": "FLY"},
                         "deal/inEffectHomeV3": feed})

    by_dest = {d.destination: d for d in vac._scan_flying_carpet()}

    assert by_dest["פראג"].stars == D("4") and by_dest["פראג"].hotel == "Hotel A (4★)"
    assert by_dest["וינה"].stars == D("4.5")
    assert by_dest["ורנה"].stars is None and by_dest["ורנה"].hotel == "Hotel C"


def test_gulliver_stars_packages_only(monkeypatch):
    deals = [
        {"Title": "רודוס, יוון", "UsdPrice": 600, "DealName": "Sea View", "HotelStars": "4.5",
         "DealTypeTitle": "חבילת נופש"},
        {"Title": "לרנקה, קפריסין", "UsdPrice": 300, "DealName": "Budget Inn", "HotelStars": "3",
         "DealTypeTitle": "חבילת נופש"},
        {"Title": "רומא, איטליה", "UsdPrice": 120, "DealName": "ישראייר", "HotelStars": "0",
         "DealTypeTitle": "טיסת צ'רטר"},
    ]
    body = {"PageData": {"HotDealsData": {"HotDealsGroups": [{"HotDeals": deals}]}}}
    _serve(monkeypatch, {"GulliverAPI.svc/InitPage": body})

    by_dest = {d.destination: d for d in vac._scan_gulliver()}

    assert by_dest["רודוס"].kind == vac.KIND_PACKAGE and by_dest["רודוס"].stars == D("4.5")
    assert by_dest["לרנקה"].stars == D("3")
    assert by_dest["רומא"].kind == vac.KIND_FLIGHT and by_dest["רומא"].stars is None


def test_hulyo_stars_from_hotel_rating(monkeypatch):
    packages = {
        "catalogItems": [
            {"productId": "p1", "destination": "בטומי", "sellingPrice": 400},
            {"productId": "p2", "destination": "טביליסי", "sellingPrice": 300},
            {"productId": "p3", "destination": "סופיה", "sellingPrice": 250},
        ],
        "productOverrides": {
            "p1": {"baseProductCode": "b1"}, "p2": {"baseProductCode": "b2"},
            "p3": {"baseProductCode": "b3"},
        },
        "baseProducts": {"b1": {"hotelCode": 11}, "b2": {"hotelCode": 22}, "b3": {"hotelCode": 33}},
        "hotels": {"11": {"name": "Sheraton", "rating": "4.5"},
                   "22": {"name": "Hostel", "rating": "3"},
                   "33": {"name": "Mystery"}},
    }
    flights = {"catalogItems": [{"productId": "f1", "destination": "אתונה", "sellingPrice": 99}]}
    _serve(monkeypatch, {"packages.json": packages, "flights-round-trip-v2.json": flights})

    by_dest = {d.destination: d for d in vac._scan_hulyo()}

    assert by_dest["בטומי"].stars == D("4.5")
    assert "מלון Sheraton (4.5★)" in by_dest["בטומי"].includes
    assert by_dest["טביליסי"].stars == D("3")
    assert by_dest["סופיה"].stars is None
    assert by_dest["אתונה"].kind == vac.KIND_FLIGHT and by_dest["אתונה"].stars is None


# --------------------------------------------------------------------------- scan_and_report

def test_scan_and_report_drops_packages_without_a_verified_3_to_5_star_hotel(monkeypatch):
    monkeypatch.delenv("PW_DEALS_DAYS", raising=False)
    monkeypatch.delenv("PW_DEALS_TOP", raising=False)
    site_a = [
        _pkg(D("5"), source="A", destination="Rome", price="500"),
        _pkg(D("4.5"), source="A", destination="Paris", price="450"),
        _pkg(D("3.5"), source="A", destination="Sofia", price="100"),  # 3.5★ passes
        _pkg(D("2.5"), source="A", destination="Prague", price="60"),  # 2.5★ doesn't
        _flight(source="A", destination="Rome", price="90"),
    ]
    site_b = [
        _pkg(D("4"), source="B", destination="Athens", price="300"),
        _pkg(None, source="B", destination="Baku", price="80"),        # no rating
        _pkg(D("2"), source="B", destination="Varna", price="70"),
    ]
    monkeypatch.setattr(vac, "_SOURCES", (("A", lambda: site_a), ("B", lambda: site_b)))

    report = vac.scan_and_report()

    assert report.status == "ok"
    assert {d.destination for d in report.packages} == {"Rome", "Paris", "Athens", "Sofia"}
    assert all(vac._MIN_STARS <= d.stars <= vac._MAX_STARS for d in report.packages)
    assert [d.destination for d in report.flights] == ["Rome"]
    assert report.dropped_low_star == 3
    counts = {s.name: s.count for s in report.sources}
    assert counts == {"A": 4, "B": 1}
    assert report.total_found == 5


# --------------------------------------------------------------------------- 3★ share

def _ranked_stars(deals, top_n, **kw):
    return [(d.destination, d.stars) for d in vac._rank(deals, top_n, **kw)]


def test_one_three_star_row_per_destination_at_most():
    deals = [
        _pkg(D("3"), source="A", destination="Rome", price="100"),
        _pkg(D("3.5"), source="B", destination="Rome", price="110"),  # second 3★: skipped
        _pkg(D("4"), source="C", destination="Rome", price="200"),
        _pkg(D("5"), source="D", destination="Rome", price="300"),
        _pkg(D("4"), source="E", destination="Rome", price="400"),    # 4th Rome row: over the cap
    ]
    assert _ranked_stars(deals, 15, max_three_star=15) == [
        ("Rome", D("3")), ("Rome", D("4")), ("Rome", D("5"))]


def test_same_source_can_offer_a_three_and_a_four_star_row():
    deals = [_pkg(D("3"), source="A", destination="Rome", price="100"),
             _pkg(D("4"), source="A", destination="Rome", price="200"),
             _pkg(D("4.5"), source="A", destination="Rome", price="250")]  # same source + tier as the 4★
    assert _ranked_stars(deals, 15) == [("Rome", D("3")), ("Rome", D("4"))]


def test_three_star_rows_are_capped_at_a_third_of_the_list():
    cities = ["Rome", "Paris", "Athens", "Sofia", "Prague", "Vienna"]
    three = [_pkg(D("3"), source="A", destination=c, price=str(100 + i)) for i, c in enumerate(cities)]
    four = [_pkg(D("4"), source="B", destination=c, price=str(300 + i)) for i, c in enumerate(cities)]
    ranked = vac._rank(three + four, 6)
    assert len(ranked) == 6
    assert sum(vac._is_three_star(d) for d in ranked) == 2  # 6 // 3
    # The cheapest 3★ rows win their slots; 4–5★ rows fill the rest.
    assert [d.destination for d in ranked if vac._is_three_star(d)] == ["Rome", "Paris"]


def test_four_row_continent_gets_one_three_star_row():
    deals = [_pkg(D("3"), source="A", destination=c, price=str(100 + i))
             for i, c in enumerate(["Bangkok", "Tokyo", "Dubai"])]
    deals += [_pkg(D("5"), source="B", destination=c, price=str(500 + i))
              for i, c in enumerate(["Bangkok", "Tokyo", "Dubai"])]
    ranked = vac._rank(deals, 4)
    assert sum(vac._is_three_star(d) for d in ranked) == 1


def test_asia_selection_keeps_the_three_star_share():
    def asia(stars, dest, country, price):
        d = _pkg(stars, destination=dest, price=price, source=dest)
        d.country = country
        return d

    ranked = [asia(D("3"), "טוקיו", "יפן", "100"), asia(D("3"), "בנגקוק", "תאילנד", "110"),
              asia(D("4"), "פוקט", "תאילנד", "300"), asia(D("4"), "דובאי", "", "310"),
              asia(D("5"), "סינגפור", "", "320")]
    chosen, missing = vac._select_asia(ranked, 4)
    assert missing == []
    assert sum(vac._is_three_star(d) for d in chosen) == 1
    # Japan only has a 3★ deal, so it takes the 3★ slot; Thailand uses its 4★ one.
    assert {d.destination for d in chosen} == {"טוקיו", "פוקט", "דובאי", "סינגפור"}


def test_scan_and_report_source_with_only_unrated_packages_keeps_its_flights(monkeypatch):
    monkeypatch.delenv("PW_DEALS_DAYS", raising=False)
    deals = [_pkg(None, destination="Budapest"), _flight(destination="Rome")]
    monkeypatch.setattr(vac, "_SOURCES", (("unrated", lambda: deals),))

    report = vac.scan_and_report()

    assert report.packages == []
    assert [d.destination for d in report.flights] == ["Rome"]
    assert report.dropped_low_star == 1
    assert report.sources[0].ok and report.sources[0].count == 1
