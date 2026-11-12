"""Vacation deals grouped by continent (issue #53).

Europe: departures today..today+50, up to PW_DEALS_TOP deals.
Other continents: departures today..today+365, PW_DEALS_OTHER_TOP (4) each.
Asia: always includes Japan and Thailand when they exist in the window.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

import pytest

from price_watcher import continents as C
from price_watcher import email_sender
from price_watcher import vacations as V

TODAY = date.today()


@pytest.fixture(autouse=True)
def _clean_deal_env(monkeypatch):
    for var in ("PW_DEALS_DAYS", "PW_DEALS_TOP", "PW_DEALS_OTHER_TOP",
                "PW_DEALS_EUROPE_DAYS", "PW_DEALS_OTHER_DAYS"):
        monkeypatch.delenv(var, raising=False)


def _deal(destination: str, days: int | None, price: int, *, kind: str = V.KIND_PACKAGE,
          country: str = "", source: str = "אתר") -> V.VacationDeal:
    return V.VacationDeal(
        source=source, kind=kind, destination=destination, price=Decimal(price),
        url="https://example.invalid/deal", country=country,
        date_from=None if days is None else TODAY + timedelta(days=days),
        date_to=None if days is None else TODAY + timedelta(days=days + 5),
    )


def _scan(monkeypatch, deals: list[V.VacationDeal]) -> V.VacationReport:
    monkeypatch.setattr(V, "_SOURCES", (("אתר", lambda: list(deals)),))
    return V.scan_and_report()


def _group(groups: list[V.ContinentGroup], continent: str) -> V.ContinentGroup:
    return next(g for g in groups if g.continent == continent)


def _dests(deals) -> list[str]:
    return [d.destination for d in deals]


# ---------------------------------------------------------------- classification

@pytest.mark.parametrize("destination,country,code,continent", [
    # Hebrew city names as the sites write them
    ("בנגקוק", "", "TH", C.ASIA),
    ("פוקט", "", "TH", C.ASIA),
    ("קו סמוי", "", "TH", C.ASIA),
    ("טוקיו", "", "JP", C.ASIA),
    ("אוסקה", "", "JP", C.ASIA),
    ("קיוטו", "", "JP", C.ASIA),
    ("דובאי", "איחוד האמירויות", "AE", C.ASIA),
    ("אבו דאבי", "", "AE", C.ASIA),
    ("סמרקנד", "", "UZ", C.ASIA),
    ("ניו יורק", "", "US", C.NORTH_AMERICA),
    ("לוס אנגלס", 'ארה"ב', "US", C.NORTH_AMERICA),
    ("בואנוס איירס", "ארגנטינה", "AR", C.SOUTH_AMERICA),
    ("איי סיישל", "אייסיישל", "SC", C.AFRICA),
    ("האי מאהה", "", "SC", C.AFRICA),
    ("זנזיבר", "", "TZ", C.AFRICA),
    ("מאוריציוס", "", "MU", C.AFRICA),
    ("שארם א-שייח", "", "EG", C.AFRICA),
    ("באקו", "אזרבייגן", "AZ", C.EUROPE),
    ("באקו", "אזרביג'ן", "AZ", C.EUROPE),
    ("בטומי", "גיאורגיה", "GE", C.EUROPE),
    ("טביליסי", "", "GE", C.EUROPE),
    ("לרנקה", "", "CY", C.EUROPE),
    ("איסטנבול", "", "TR", C.EUROPE),
    ("רובניימי", "פינלנד", "FI", C.EUROPE),
    # Titles with extra words / separators
    ("סיציליה (פלרמו)", "איטליה", "IT", C.EUROPE),
    ("פלרמו-סיציליה", "", "IT", C.EUROPE),
    ("כרתים הרקליון", "", "GR", C.EUROPE),
    ("דורס | אלבניה", "", "AL", C.EUROPE),
    ("טורונטו - ON", "", "CA", C.NORTH_AMERICA),
    ("מאורגן: 5 ימים בפראג", "", "CZ", C.EUROPE),
    # English city names
    ("Bangkok", "", "TH", C.ASIA),
    ("Koh Samui", "", "TH", C.ASIA),
    ("Tokyo", "", "JP", C.ASIA),
    ("New York", "", "US", C.NORTH_AMERICA),
    ("Sydney", "", "AU", C.OCEANIA),
    ("Zanzibar", "", "TZ", C.AFRICA),
    # The country decides when the city is unknown: English, ISO-2, Hebrew
    ("Somewhere", "Japan", "JP", C.ASIA),
    ("Somewhere", "th", "TH", C.ASIA),
    ("Somewhere", "תאילנד", "TH", C.ASIA),
    ("Somewhere", "UK", "GB", C.EUROPE),
    ("Somewhere", "Egypt", "EG", C.AFRICA),
    ("Somewhere", "Hungary", "HU", C.EUROPE),
    # Israel is classified, as domestic
    ("אילת", "ישראל", "IL", C.DOMESTIC),
])
def test_classify(destination, country, code, continent):
    assert C.classify(destination, country) == (code, continent)


@pytest.mark.parametrize("destination,country", [("אטלנטיס", ""), ("Atlantis", "Nowhere"), ("", "")])
def test_classify_unknown(destination, country):
    assert C.classify(destination, country) is None


def test_country_names():
    assert C.country_name("JP") == "Japan"
    assert C.country_name_he("Thailand") == "תאילנד"
    assert C.country_name_he("JP") == "יפן"


# ---------------------------------------------------------------- windows

def test_europe_day_50_included_day_51_excluded(monkeypatch):
    report = _scan(monkeypatch, [_deal("פראג", 50, 300), _deal("רומא", 51, 200)])
    europe = _group(report.package_groups, C.EUROPE)
    assert _dests(europe.deals) == ["פראג"]
    assert europe.window_days == 50


def test_europe_today_included_past_excluded(monkeypatch):
    report = _scan(monkeypatch, [_deal("פראג", 0, 300), _deal("רומא", -1, 200)])
    assert _dests(report.packages) == ["פראג"]


def test_other_continent_day_365_included_day_366_excluded(monkeypatch):
    report = _scan(monkeypatch, [
        _deal("ניו יורק", 365, 900, kind=V.KIND_FLIGHT),
        _deal("מיאמי", 366, 500, kind=V.KIND_FLIGHT),
    ])
    na = _group(report.flight_groups, C.NORTH_AMERICA)
    assert _dests(na.deals) == ["ניו יורק"]
    assert na.window_days == 365


def test_undated_deals_are_excluded(monkeypatch):
    report = _scan(monkeypatch, [_deal("פראג", None, 100), _deal("רומא", 10, 200)])
    assert _dests(report.packages) == ["רומא"]


def test_window_env_overrides(monkeypatch):
    monkeypatch.setenv("PW_DEALS_EUROPE_DAYS", "10")
    monkeypatch.setenv("PW_DEALS_OTHER_DAYS", "100")
    report = _scan(monkeypatch, [
        _deal("פראג", 10, 300), _deal("רומא", 11, 200),
        _deal("ניו יורק", 100, 900), _deal("מיאמי", 101, 500),
    ])
    assert _dests(report.packages) == ["פראג", "ניו יורק"]


def test_global_days_cap_still_applies(monkeypatch):
    monkeypatch.setenv("PW_DEALS_DAYS", "30")
    report = _scan(monkeypatch, [
        _deal("פראג", 30, 300), _deal("רומא", 40, 200),
        _deal("ניו יורק", 20, 900), _deal("מיאמי", 200, 500),
    ])
    assert _dests(report.packages) == ["פראג", "ניו יורק"]
    assert report.days_ahead == 30
    assert _group(report.package_groups, C.EUROPE).window_days == 30
    assert _group(report.package_groups, C.ASIA).window_days == 30


# ---------------------------------------------------------------- quotas

_EUROPE_CITIES = [
    "פראג", "רומא", "אתונה", "בודפשט", "ברלין", "ברצלונה", "לונדון", "פריז", "וינה", "ורשה",
    "קרקוב", "לרנקה", "פאפוס", "סופיה", "בוקרשט", "בטומי", "טביליסי", "באקו", "מילאנו", "ליסבון",
]


def test_europe_keeps_exactly_top_n(monkeypatch):
    deals = [_deal(city, 5 + i, 100 + i) for i, city in enumerate(_EUROPE_CITIES)]
    report = _scan(monkeypatch, deals)
    europe = _group(report.package_groups, C.EUROPE)
    assert len(europe.deals) == 15 == europe.limit
    assert _dests(europe.deals) == _EUROPE_CITIES[:15]  # cheapest first


def test_europe_top_n_from_env(monkeypatch):
    monkeypatch.setenv("PW_DEALS_TOP", "7")
    deals = [_deal(city, 5, 100 + i) for i, city in enumerate(_EUROPE_CITIES)]
    report = _scan(monkeypatch, deals)
    assert len(_group(report.package_groups, C.EUROPE).deals) == 7


def test_non_europe_keeps_exactly_four(monkeypatch):
    cities = ["ניו יורק", "מיאמי", "לוס אנג'לס", "טורונטו", "קנקון", "לאס וגאס"]
    africa = ["זנזיבר", "איי סיישל", "מאוריציוס", "מרקש", "קייפטאון"]
    report = _scan(monkeypatch, [_deal(c, 100, 500 + i) for i, c in enumerate(cities)]
                   + [_deal(c, 200, 700 + i) for i, c in enumerate(africa)])
    na = _group(report.package_groups, C.NORTH_AMERICA)
    assert len(na.deals) == 4 == na.limit
    assert _dests(na.deals) == cities[:4]
    assert len(_group(report.package_groups, C.AFRICA).deals) == 4


def test_fewer_than_four_shows_what_there_is(monkeypatch):
    report = _scan(monkeypatch, [_deal("סידני", 100, 1500)])
    assert _dests(_group(report.package_groups, C.OCEANIA).deals) == ["סידני"]


def test_rank_rules_apply_within_a_continent(monkeypatch):
    """One row per (source, destination), at most 3 per destination."""
    deals = [_deal("פראג", 5, 300, source="א"), _deal("פראג", 6, 200, source="א")]
    deals += [_deal("רומא", 5, 100 + i, source=s) for i, s in enumerate("בגדה")]
    report = _scan(monkeypatch, deals)
    europe = _group(report.package_groups, C.EUROPE)
    assert [(d.destination, d.price) for d in europe.deals if d.destination == "פראג"] == [
        ("פראג", Decimal(200))]
    assert _dests(europe.deals).count("רומא") == 3


# ---------------------------------------------------------------- Asia

_VIETNAM = ["האנוי", "הו צ'י מין", "דה נאנג", "פו קוק", "נה טראנג"]


def test_asia_includes_japan_and_thailand_over_cheaper_vietnam(monkeypatch):
    deals = [_deal(c, 30, 100 + 10 * i) for i, c in enumerate(_VIETNAM)]
    deals += [_deal("טוקיו", 60, 900), _deal("בנגקוק", 90, 700)]
    report = _scan(monkeypatch, deals)
    asia = _group(report.package_groups, C.ASIA)
    assert len(asia.deals) == 4
    assert _dests(asia.deals) == ["האנוי", "הו צ'י מין", "בנגקוק", "טוקיו"]  # price order
    assert asia.missing_required == []


def test_asia_reserves_only_the_cheapest_of_each(monkeypatch):
    deals = [_deal(c, 30, 100 + 10 * i) for i, c in enumerate(_VIETNAM)]
    deals += [_deal("טוקיו", 60, 900), _deal("אוסקה", 60, 950), _deal("פוקט", 90, 800)]
    report = _scan(monkeypatch, deals)
    assert _dests(_group(report.package_groups, C.ASIA).deals) == [
        "האנוי", "הו צ'י מין", "פוקט", "טוקיו"]


def test_asia_required_countries_come_from_the_site_country_too(monkeypatch):
    deals = [_deal(c, 30, 100 + 10 * i) for i, c in enumerate(_VIETNAM)]
    deals += [_deal("Unknown resort", 60, 900, country="Japan"),
              _deal("Another resort", 60, 990, country="תאילנד")]
    asia = _group(_scan(monkeypatch, deals).package_groups, C.ASIA)
    assert {"Unknown resort", "Another resort"} <= set(_dests(asia.deals))


def test_asia_missing_thailand_is_recorded(monkeypatch):
    deals = [_deal(c, 30, 100 + 10 * i) for i, c in enumerate(_VIETNAM)]
    deals += [_deal("טוקיו", 60, 900), _deal("בנגקוק", 400, 300)]  # Bangkok past the window
    asia = _group(_scan(monkeypatch, deals).package_groups, C.ASIA)
    assert asia.missing_required == ["Thailand"]
    assert "טוקיו" in _dests(asia.deals) and len(asia.deals) == 4


def test_missing_thailand_is_noted_in_email_text_and_html(monkeypatch):
    deals = [_deal(c, 30, 100 + 10 * i) for i, c in enumerate(_VIETNAM)]
    deals += [_deal("טוקיו", 60, 900), _deal("פראג", 5, 200)]
    payload = email_sender.build_deals_payload(_scan(monkeypatch, deals))
    note = "לא נמצא היום דיל לתאילנד ביציאה ב-365 הימים הקרובים"
    assert note in payload.text_body
    assert note in payload.html_body
    assert "יפן" not in payload.text_body.split("לא נמצא היום")[1].split("\n")[0]


def test_missing_japan_and_thailand_with_no_asia_deals_is_noted(monkeypatch):
    report = _scan(monkeypatch, [_deal("פראג", 5, 200)])
    asia = _group(report.package_groups, C.ASIA)
    assert asia.deals == [] and asia.missing_required == ["Japan", "Thailand"]
    payload = email_sender.build_deals_payload(report)
    note = "לא נמצאו היום דילים ליפן ולתאילנד ביציאה ב-365 הימים הקרובים"
    # Both kinds have no Asia deals: the note is shown for each.
    assert payload.text_body.count(note) == 2
    assert payload.html_body.count(note) == 2


# ---------------------------------------------------------------- unmapped / domestic

def test_unmapped_deals_are_dropped_and_counted(monkeypatch):
    report = _scan(monkeypatch, [_deal("אטלנטיס", 5, 50), _deal("פראג", 5, 200)])
    assert report.unmapped_count == 1
    assert report.unmapped == ["אתר: אטלנטיס"]
    assert _dests(report.packages) == ["פראג"]
    payload = email_sender.build_deals_payload(report)
    assert "דיל אחד ליעד שלא זוהתה היבשת שלו לא מוצג" in payload.text_body
    assert "דיל אחד ליעד שלא זוהתה היבשת שלו לא מוצג" in payload.html_body


def test_domestic_deals_are_dropped_but_not_unmapped(monkeypatch):
    report = _scan(monkeypatch, [_deal("אילת", 5, 30, kind=V.KIND_FLIGHT, country="ישראל"),
                                 _deal("פראג", 5, 200, kind=V.KIND_FLIGHT)])
    assert report.unmapped_count == 0
    assert _dests(report.flights) == ["פראג"]


# ---------------------------------------------------------------- report shape

def test_flat_lists_follow_continent_order(monkeypatch):
    report = _scan(monkeypatch, [
        _deal("סידני", 100, 50), _deal("ניו יורק", 100, 60), _deal("בנגקוק", 100, 70),
        _deal("פראג", 5, 900), _deal("זנזיבר", 100, 80), _deal("בואנוס איירס", 100, 90),
        _deal("רומא", 5, 10, kind=V.KIND_FLIGHT), _deal("טוקיו", 100, 5, kind=V.KIND_FLIGHT),
    ])
    assert [g.continent for g in report.package_groups] == list(C.CONTINENT_ORDER)
    assert _dests(report.packages) == [
        "פראג", "בנגקוק", "ניו יורק", "בואנוס איירס", "זנזיבר", "סידני"]
    assert _dests(report.flights) == ["רומא", "טוקיו"]
    assert [d.continent for d in report.flights] == [C.EUROPE, C.ASIA]
    assert report.deals == report.packages + report.flights


def test_subject_blurb_uses_the_cheapest_deal_not_the_first(monkeypatch):
    report = _scan(monkeypatch, [_deal("פראג", 5, 900), _deal("בנגקוק", 100, 300)])
    payload = email_sender.build_deals_payload(report)
    assert "חבילות מ-$300" in payload.subject


# ---------------------------------------------------------------- email layout

def test_email_renders_continent_sections(monkeypatch):
    report = _scan(monkeypatch, [
        _deal("פראג", 5, 200), _deal("טוקיו", 100, 900), _deal("בנגקוק", 100, 700),
        _deal("רומא", 5, 100, kind=V.KIND_FLIGHT),
    ])
    payload = email_sender.build_deals_payload(report)
    html, text = payload.html_body, payload.text_body
    # Shared stylesheet in the head, classes on the rows
    assert "<style>" in html.split("</head>")[0] and ".vt td" in html
    assert 'class="vt"' in html
    # Continent headings, in order, in both bodies
    for body in (html, text):
        assert body.index("אירופה") < body.index("אסיה")
        assert "דיל אחד · יציאה ב-50 הימים הקרובים" in body
        assert "2 דילים · יציאה ב-365 הימים הקרובים" in body
    # Empty continents get one short line, no empty table
    assert "אין היום דילים ל: צפון אמריקה, דרום אמריקה, אפריקה, אוקיאניה" in text
    assert "אין היום דילים ל: צפון אמריקה, דרום אמריקה, אפריקה, אוקיאניה" in html
    assert html.count('<table class="vt"') == 2  # one per kind
    # Rows carry no inline styles
    rows = html.split('<table class="vt"')[1]
    assert 'style="' not in rows.split("</table>")[0]


def test_daily_email_includes_vacation_styles(monkeypatch):
    from types import SimpleNamespace

    vac = _scan(monkeypatch, [_deal("פראג", 5, 200)])
    report = SimpleNamespace(site_digests=[], creatine=None, sims4=None, supermarkets=None,
                             vacations=vac, marketplace=None)
    payload = email_sender.build_daily_payload(report)
    assert ".vt td" in payload.html_body.split("</head>")[0]
    assert "אירופה" in payload.text_body and "פראג" in payload.html_body


def test_report_without_groups_still_renders():
    """Callers that build a VacationReport by hand (flat lists only) keep working."""
    from types import SimpleNamespace

    vac = SimpleNamespace(status="ok", packages=[_deal("פראג", 5, 200)], flights=[],
                          sources=[], total_found=1, days_ahead=0, error=None)
    vac.deals = vac.packages
    payload = email_sender.build_deals_payload(vac)
    assert "פראג" in payload.html_body and "פראג" in payload.text_body


# ---------------------------------------------------------------- lastminute request

def test_lastminute_asks_each_continent_with_its_window(monkeypatch):
    captured = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return []

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, params=None):
            captured["request"] = json.loads(params["request"])
            return _Resp()

    monkeypatch.setattr(V, "_client", lambda **kw: _Client())
    assert V._scan_lastminute() == []
    req = captured["request"]
    for kind in ("flight", "dynamic"):
        boxes = req[kind]
        assert len(boxes) == 5
        europe, japan, thailand, asia, other = boxes
        assert "PRG" in europe["ArrivalCodes"] and "DXB" not in europe["ArrivalCodes"]
        assert "SEZ" not in europe["ArrivalCodes"]
        assert europe["MaxFromDate"].startswith((TODAY + timedelta(days=50)).isoformat())
        # Japan and Thailand get their own boxes so the Asia rule can be met
        assert {"NRT", "KIX"} <= set(japan["ArrivalCodes"])
        assert {"BKK", "HKT", "USM"} <= set(thailand["ArrivalCodes"])
        assert {"DXB", "AUH", "TAS"} <= set(asia["ArrivalCodes"])
        assert {"JFK", "SEZ", "ZNZ", "MRU"} <= set(other["ArrivalCodes"])
        for box in (japan, thailand, asia, other):
            assert box["MaxFromDate"].startswith((TODAY + timedelta(days=365)).isoformat())
