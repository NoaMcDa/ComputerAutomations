"""Luggage state of every vacation deal, per site (VacationDeal.luggage / luggage_extra).

Payloads are small copies of the live shapes (Nov 2026), fed through
httpx.MockTransport. No network.
"""
from __future__ import annotations

import json

import httpx
import pytest

from price_watcher import vacations as vac

SUITCASE, CARRY_ON, NONE, UNKNOWN = (
    vac.LUGGAGE_SUITCASE, vac.LUGGAGE_CARRY_ON, vac.LUGGAGE_NONE, vac.LUGGAGE_UNKNOWN,
)


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


def _no_baggage_text(includes: list[str]) -> bool:
    return not any(w in x for x in includes for w in ("מזוודה", "טרולי", "כבודה"))


# --------------------------------------------------------------------------- shared rule

@pytest.mark.parametrize("legs, expected", [
    ([SUITCASE, SUITCASE], SUITCASE),
    ([CARRY_ON, CARRY_ON], CARRY_ON),
    ([NONE, NONE], NONE),
    ([SUITCASE, CARRY_ON], CARRY_ON),   # only what holds on every leg
    ([SUITCASE, NONE], NONE),
    ([SUITCASE, None], UNKNOWN),        # a leg the site doesn't state
    ([], UNKNOWN),
])
def test_trip_luggage_takes_what_every_leg_has(legs, expected):
    assert vac._trip_luggage(legs) == expected


# --------------------------------------------------------------------------- Hulyo

def _hulyo_product(*legs: tuple[str, str]) -> dict:
    return {"legRefs": [{"baggageAllowance": {"baggage": b, "trolley": t}, "segmentIds": ["x"]}
                        for b, t in legs]}


IN, OUT = "include", "notInclude"


@pytest.mark.parametrize("legs, expected", [
    ([(IN, IN), (IN, IN)], SUITCASE),
    ([(IN, OUT), (IN, OUT)], SUITCASE),       # suitcase without trolley still a suitcase
    ([(OUT, IN), (OUT, IN)], CARRY_ON),
    ([(OUT, OUT), (OUT, OUT)], NONE),
    ([(IN, IN), (OUT, IN)], CARRY_ON),        # suitcase only one way
    ([(IN, IN), (OUT, OUT)], NONE),           # nothing on the way back
    ([(OUT, IN), (OUT, OUT)], NONE),
    ([(IN, IN), ("", "")], UNKNOWN),          # a leg without a stated allowance
])
def test_hulyo_leg_combinations(legs, expected):
    assert vac._hulyo_luggage(_hulyo_product(*legs)) == expected


def test_hulyo_without_legs_is_unknown():
    assert vac._hulyo_luggage({}) == UNKNOWN
    assert vac._hulyo_luggage({"legRefs": [{"segmentIds": ["x"]}]}) == UNKNOWN


def test_hulyo_scan_sets_luggage_on_packages_and_flights(monkeypatch):
    packages = {
        "catalogItems": [
            {"productId": "p1", "destination": "בטומי", "sellingPrice": 400},
            {"productId": "p2", "destination": "טביליסי", "sellingPrice": 300},
        ],
        "productOverrides": {
            "p1": {"baseProductCode": "b1", **_hulyo_product((IN, IN), (IN, IN))},
            "p2": {"baseProductCode": "b1", **_hulyo_product((OUT, OUT), (OUT, OUT))},
        },
        "baseProducts": {"b1": {"hotelCode": 11, "boardBaseDesc": "לינה בלבד"}},
        "hotels": {"11": {"name": "Sheraton", "rating": "4"}},
    }
    flights = {
        "catalogItems": [{"productId": "f1", "destination": "אתונה", "sellingPrice": 99},
                         {"productId": "f2", "destination": "רומא", "sellingPrice": 89}],
        "productOverrides": {"f1": _hulyo_product((OUT, IN), (OUT, IN)), "f2": {}},
    }
    _serve(monkeypatch, {"packages.json": packages, "flights-round-trip-v2.json": flights})

    by_dest = {d.destination: d for d in vac._scan_hulyo()}

    assert by_dest["בטומי"].luggage == SUITCASE and by_dest["בטומי"].has_luggage
    assert by_dest["טביליסי"].luggage == NONE and not by_dest["טביליסי"].has_luggage
    assert by_dest["אתונה"].luggage == CARRY_ON and by_dest["אתונה"].has_luggage
    assert by_dest["רומא"].luggage == UNKNOWN
    assert all(d.luggage_extra == "" for d in by_dest.values())
    # The baggage text left `includes`; the rest stays.
    assert by_dest["בטומי"].includes == ["טיסה הלוך ושוב", "מלון Sheraton (4★)", "לינה בלבד"]
    assert _no_baggage_text(by_dest["טביליסי"].includes)


# --------------------------------------------------------------------------- Flying Carpet

def _fc_flight(suitcase="20", trolley="7", hand="5") -> dict:
    return {"FL_Max_Weight": suitcase, "FL_Max_Weight_Hand": hand, "FL_Max_Weight_Trolley": trolley}


def _fc_deal(out_prices=(None, None), back_prices=(None, None), flights=None, **extra) -> dict:
    """out/back_prices = (carryOnBaggage, checkinBaggage) the site charges for that direction."""
    return {
        "roundTripFlight": {"FlightDetail": [_fc_flight(), _fc_flight()] if flights is None else flights},
        "chargedBaggagePrices": [
            {"returningLeg": True, "carryOnBaggage": back_prices[0], "checkinBaggage": back_prices[1]},
            {"returningLeg": False, "carryOnBaggage": out_prices[0], "checkinBaggage": out_prices[1]},
        ],
        "currenySymbol": "$",
        **extra,
    }


@pytest.mark.parametrize("deal, expected, extra", [
    # Live: Fly Dubai / Arkia charters, nothing charged, 20 kg + 7 kg allowed.
    (_fc_deal(), SUITCASE, ""),
    # Live: Israir to Budapest / Prague, trolley $25 and suitcase $45 each way.
    (_fc_deal((25, 45), (25, 45)), NONE, "טרולי $25, מזוודה $45 לכיוון"),
    # Trolley in the price, suitcase paid.
    (_fc_deal((None, 45), (None, 45)), CARRY_ON, "מזוודה $45 לכיוון"),
    # Different prices per direction.
    (_fc_deal((25, 45), (30, 50)), NONE, "הלוך: טרולי $25, מזוודה $45 · חזור: טרולי $30, מזוודה $50"),
    # Suitcase paid only on the way back: the trolley is all that holds both ways.
    (_fc_deal((None, None), (None, 40)), CARRY_ON, "חזור: מזוודה $40"),
    # Nothing charged but the flight allows no luggage at all (a handbag is not a carry-on).
    (_fc_deal(flights=[_fc_flight("0", "0"), _fc_flight("0", "0")]), NONE, ""),
    # No suitcase allowed, trolley free.
    (_fc_deal(flights=[_fc_flight("0", "8"), _fc_flight("0", "8")]), CARRY_ON, ""),
    # No flight details and nothing charged: the site doesn't say.
    (_fc_deal(flights=[]), UNKNOWN, ""),
    # No flight details but both items charged both ways: none included.
    (_fc_deal((25, 45), (25, 45), flights=[]), NONE, "טרולי $25, מזוודה $45 לכיוון"),
])
def test_flying_luggage_from_charged_prices_and_weights(deal, expected, extra):
    assert vac._flying_luggage(deal) == (expected, extra)


def test_flying_luggage_without_any_baggage_data_is_unknown():
    assert vac._flying_luggage({}) == (UNKNOWN, "")


def test_flying_carpet_scan_sets_luggage_and_drops_baggage_text(monkeypatch):
    base = {"stars": 4, "discountedPrice": {"priceAdl": 450}, "boardBasisName": {"he": "לינה וארוחת בוקר"},
            "subjectName": {"he": "Flt+Htl Without Transfers"}}
    feed = [
        {**base, **_fc_deal((25, 45), (25, 45)), "destinationName": {"he": "בודפשט"},
         "hotelName": {"he": "Soho"}},
        {**base, **_fc_deal(), "destinationName": {"he": "דובאי"}, "hotelName": {"he": "Crowne"}},
    ]
    _serve(monkeypatch, {"agencyContents/byHost": {"agencyCode": "FLY"},
                         "deal/inEffectHomeV3": feed})

    by_dest = {d.destination: d for d in vac._scan_flying_carpet()}

    bud = by_dest["בודפשט"]
    assert (bud.luggage, bud.luggage_extra) == (NONE, "טרולי $25, מזוודה $45 לכיוון")
    assert bud.includes == ["טיסה הלוך ושוב", "מלון Soho (4★)", "לינה וארוחת בוקר", "ללא העברות"]
    dxb = by_dest["דובאי"]
    assert (dxb.luggage, dxb.luggage_extra) == (SUITCASE, "")
    assert _no_baggage_text(dxb.includes)


# --------------------------------------------------------------------------- sites that don't say

def test_sites_without_luggage_data_stay_unknown(monkeypatch):
    issta_card = (
        '<a href="/loader?url=/dynamic-packages/results.aspx?fdate=14/11/2026&tdate=17/11/2026'
        '&code=lca&adt1=2&stars=4"><span itemprop="name">דיל רגע אחרון ללרנקה</span>'
        '<span itemprop="description">מגוון מלונות</span><div class="deal-Price">'
        '<strong itemprop="price" content="188"><small itemprop="priceCurrency" content="$">$</small>'
        '</strong></div></a>'
    )
    eshet_promo = {"DestinationName": "בטומי", "HotelName": "White Sails", "Stars": 5,
                   "Price": {"PriceToShow": 479}, "PromotionalNotes": [], "MarketingNote": "",
                   "Conditions": {"TotalNights": 3}, "Capacity": {"Adults": 2}}
    eshet_data = {"props": {"initialState": {"general": {
        "promotionGroups": [{"Promotions": [eshet_promo]}]}}}}
    gulliver = {"PageData": {"HotDealsData": {"HotDealsGroups": [{"HotDeals": [
        {"Title": "לרנקה, קפריסין", "UsdPrice": 173, "DealName": "Tus Airways",
         "HotelStars": 0, "DealTypeTitle": "טיסת צ'רטר", "DatesTitle": "13.12.26 - 09.12.26"}]}]}}}
    travelist = {"resources": [{"title": "לרנקה", "subTitle": "קפריסין", "date": "14.02.27 - 18.02.27",
                                "price": 60, "currency": "$", "location": {"href": "/flightsResults"}}]}
    secret = [{"destination_city": "אילת", "destination_country": "ישראל", "price_usd": 49,
               "date_from": "2027-01-21", "date_to": "2027-01-31", "direct": True,
               "airline_name": "אייר חיפה"}]
    _serve(monkeypatch, {
        "issta.co.il/deals/last-call": f"<html><body>{issta_card}</body></html>",
        "eshet.com/deals": f'<html><script id="__NEXT_DATA__">{json.dumps(eshet_data)}</script></html>',
        "GulliverAPI.svc/InitPage": gulliver,
        "flights-deals.json": travelist,
        "get-api-data": secret,
    })

    for scan in (vac._scan_issta, vac._scan_eshet, vac._scan_gulliver,
                 vac._scan_travelist, vac._scan_secret_flights):
        deals = scan()
        assert deals, scan.__name__
        for d in deals:
            assert (d.luggage, d.luggage_extra) == (UNKNOWN, ""), scan.__name__
            assert not d.has_luggage


# ---- Flying Carpet deal link (the API's selectedPackageUrl opens an empty page)

def test_flying_deal_url_uses_the_site_card_format():
    from urllib.parse import parse_qs, urlparse
    from price_watcher import vacations as V
    d = {"categoryId": "vacation_pack", "departureDate": "2026-11-16", "arrivalDate": "2026-11-19",
         "flightDestinationCode": "BUD", "hotelId": 53968, "packageId": 235512,
         "id": "6af137ae3458d82ee1211448", "adult": 2, "child": 1, "infant": 0,
         "selectedPackageUrl": "/product-page?packId=235512&laId=53968"}
    url = V._flying_deal_url("https://www.flying.co.il", d)
    parts = urlparse(url)
    assert parts.path == "/product-page"
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert q == {"packCateg": "vacation_pack", "packageDates": "2026-11-16,2026-11-19",
                 "hotelSupplier": "FlyingCarpet", "flightDestCode": "BUD", "hotelCode": "53968",
                 "paxByRoom": "2,1,0", "packageIds": "235512",
                 "dealId": "6af137ae3458d82ee1211448"}
    assert "packId=" not in url


def test_flying_deal_url_falls_back_when_fields_missing():
    from price_watcher import vacations as V
    d = {"selectedPackageUrl": "/product-page?packId=1&laId=2", "departureDate": "2026-11-16"}
    assert V._flying_deal_url("https://www.flying.co.il", d) == \
        "https://www.flying.co.il/product-page?packId=1&laId=2"
