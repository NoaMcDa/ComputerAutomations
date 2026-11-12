"""Holiday Finder "secret deals" reader."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import httpx
import pytest

from price_watcher import vacations as V

BASE = "https://www.holidayfinder.co.il"


def _item(price=686, luggage=False, trolley=False, board="BB", adult=2, rating=4, **offer_extra):
    stickers = {"boardType": board, "direct": True}
    if luggage is not None:
        stickers["luggageIncluded"] = luggage
    if trolley is not None:
        stickers["carryOnTrolleyIncluded"] = trolley
    return {
        "destinationData": {"name": "אתונה", "atlas_top": "יוון", "atlas_top_en": "Greece"},
        "offer": {
            "hfOfferId": "m4d46h1c20o241126i271126st1", "outboundDate": "24/11/2026",
            "inboundDate": "27/11/2026", "price": price, "currency": "USD", "duration": 3,
            "capacity": {"adult": adult, "child": 0},
            "packageDeeplinkUrl": f"http://www.holidayfinder.co.il/offer/9221645?bc=x:recommended:BB&adult=2&airports[]=TLV",
            "stickers": stickers, **offer_extra,
        },
        "hotel": {"name": "Athens Lodge", "rating": rating, "roomName": "Premium Room"},
        "flight": {"company_name": "Wizzair"},
    }


def test_offer_fields():
    d = V._hf_offer(_item(), BASE)
    assert d.source == V._HOLIDAY_FINDER and d.kind == V.KIND_PACKAGE
    assert d.destination == "אתונה" and d.country == "יוון"
    assert d.total == Decimal(686) and d.price == Decimal(343) and d.adults == 2
    assert (d.date_from, d.date_to, d.nights) == (date(2026, 11, 24), date(2026, 11, 27), 3)
    assert d.stars == Decimal(4) and d.hotel == "Athens Lodge (4★)"
    assert d.includes == ["טיסה הלוך ושוב", "מלון Athens Lodge (4★)", "לינה וארוחת בוקר", "Premium Room"]
    assert d.note == "לינה וארוחת בוקר · טיסה ישירה · Wizzair"
    assert d.url.startswith("https://www.holidayfinder.co.il/offer/9221645?")


@pytest.mark.parametrize("luggage, trolley, expected", [
    (True, False, V.LUGGAGE_SUITCASE),
    (False, True, V.LUGGAGE_CARRY_ON),
    (False, False, V.LUGGAGE_NONE),
    (None, None, V.LUGGAGE_UNKNOWN),
    (False, None, V.LUGGAGE_UNKNOWN),
])
def test_luggage_from_stickers(luggage, trolley, expected):
    assert V._hf_offer(_item(luggage=luggage, trolley=trolley), BASE).luggage == expected


def test_odd_price_split_and_missing_fields():
    assert V._hf_offer(_item(price=811), BASE).price == Decimal("405.50")
    assert V._hf_offer(_item(price=None), BASE) is None
    no_link = _item(packageDeeplinkUrl="")
    assert V._hf_offer(no_link, BASE).url == f"{BASE}/in/the-secret-deals"


def _mock(monkeypatch, lists, offers_for):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/partner-data"):
            return httpx.Response(200, json={"lists": [{"hfOfferIds": ids} for ids in lists]})
        sent = json.loads(request.read())["data"]
        ids = [o["id"] for o in sent["hfOffers"]]
        calls.append((ids, sent["limit"], sent["offset"]))
        return offers_for(ids)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(V, "_client", lambda **h: httpx.Client(transport=transport, headers=h))
    return calls


def _offers(*items):
    return httpx.Response(200, json={"data": {"offers": list(items)}})


def test_scan_prices_each_list_separately(monkeypatch):
    one = _item()
    two = _item(price=900)
    two["offer"]["hfOfferId"] = "other"
    calls = _mock(monkeypatch, [["a", "b"], ["c"]],
                  lambda ids: _offers(one, _item(price=None)) if ids == ["a", "b"] else _offers(one, two))
    deals = V._scan_holiday_finder()
    assert [ids for ids, _, _ in calls] == [["a", "b"], ["c"]]
    assert all(limit >= len(ids) and offset == 0 for ids, limit, offset in calls)
    # The same offer in two lists is shown once.
    assert sorted(d.total for d in deals) == [Decimal(686), Decimal(900)]


def test_a_failing_list_is_skipped(monkeypatch):
    _mock(monkeypatch, [["a"], ["b"]],
          lambda ids: httpx.Response(500) if ids == ["a"] else _offers(_item()))
    assert len(V._scan_holiday_finder()) == 1


def test_source_fails_when_every_list_fails(monkeypatch):
    _mock(monkeypatch, [["a"], ["b"]], lambda ids: httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        V._scan_holiday_finder()


def test_holiday_finder_is_a_source():
    assert (V._HOLIDAY_FINDER, V._scan_holiday_finder) in V._SOURCES
