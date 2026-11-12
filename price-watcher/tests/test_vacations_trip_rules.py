"""Trip rules for vacation deals (owner's request).

Length: 3-5 nights inclusive (PW_DEALS_MIN_NIGHTS / PW_DEALS_MAX_NIGHTS), for
packages and flights; unknown length is left out and counted.
Lead time: departures from today + PW_DEALS_LEAD_DAYS (2) on; the far end of
every window is unchanged.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from price_watcher import continents as C
from price_watcher import vacations as V

TODAY = date.today()
SECRET = V._SECRET_FLIGHTS


@pytest.fixture(autouse=True)
def _clean_deal_env(monkeypatch):
    for var in ("PW_DEALS_DAYS", "PW_DEALS_TOP", "PW_DEALS_OTHER_TOP",
                "PW_DEALS_EUROPE_DAYS", "PW_DEALS_OTHER_DAYS", "PW_DEALS_SECRET_FLIGHTS_DAYS",
                "PW_DEALS_LEAD_DAYS", "PW_DEALS_MIN_NIGHTS", "PW_DEALS_MAX_NIGHTS"):
        monkeypatch.delenv(var, raising=False)


def _deal(destination: str, days: int, nights: int | None, price: int = 300, *,
          kind: str = V.KIND_PACKAGE, source: str = "אתר",
          nights_field: int | None = None) -> V.VacationDeal:
    """A deal departing today + `days`, returning `nights` later (None = no return date)."""
    start = TODAY + timedelta(days=days)
    return V.VacationDeal(
        source=source, kind=kind, destination=destination, price=Decimal(price),
        url="https://example.invalid/deal",
        date_from=start,
        date_to=None if nights is None else start + timedelta(days=nights),
        nights=nights_field,
        stars=Decimal(4) if kind == V.KIND_PACKAGE else None,
    )


def _scan(monkeypatch, deals: list[V.VacationDeal]) -> V.VacationReport:
    by_source: dict[str, list[V.VacationDeal]] = {}
    for d in deals:
        by_source.setdefault(d.source, []).append(d)
    monkeypatch.setattr(V, "_SOURCES", tuple(
        (name, (lambda ds=ds: list(ds))) for name, ds in by_source.items()))
    return V.scan_and_report()


def _dests(deals) -> list[str]:
    return [d.destination for d in deals]


def _group(groups, continent):
    return next(g for g in groups if g.continent == continent)


# ---------------------------------------------------------------- trip length

def test_packages_two_nights_out_three_and_five_in_six_out(monkeypatch):
    report = _scan(monkeypatch, [
        _deal("פראג", 10, 2, 100), _deal("רומא", 10, 3, 200),
        _deal("אתונה", 10, 5, 300), _deal("ברלין", 10, 6, 400),
    ])
    assert sorted(_dests(report.packages)) == sorted(["רומא", "אתונה"])
    assert report.dropped_length == 2


def test_flights_follow_the_same_length_rule(monkeypatch):
    report = _scan(monkeypatch, [
        _deal("פראג", 10, 2, 100, kind=V.KIND_FLIGHT), _deal("רומא", 10, 3, 200, kind=V.KIND_FLIGHT),
        _deal("אתונה", 10, 5, 300, kind=V.KIND_FLIGHT), _deal("ברלין", 10, 6, 400, kind=V.KIND_FLIGHT),
    ])
    assert _dests(report.flights) == ["רומא", "אתונה"]
    assert report.dropped_length == 2


def test_unknown_length_is_dropped_and_counted(monkeypatch):
    report = _scan(monkeypatch, [
        _deal("פראג", 10, None, 100),                       # no return date, no nights
        _deal("רומא", 10, None, 200, kind=V.KIND_FLIGHT),   # one-way flight
        _deal("אתונה", 10, 4, 300),
    ])
    assert _dests(report.packages) == ["אתונה"] and report.flights == []
    assert report.dropped_length == 2


def test_stated_nights_count_even_without_a_return_date(monkeypatch):
    report = _scan(monkeypatch, [
        _deal("פראג", 10, None, 100, nights_field=4),
        _deal("רומא", 10, None, 200, nights_field=7),
    ])
    assert _dests(report.packages) == ["פראג"]
    assert report.dropped_length == 1


def test_source_counts_reflect_the_length_rule(monkeypatch):
    report = _scan(monkeypatch, [
        _deal("פראג", 10, 4, source="א"), _deal("רומא", 10, 1, source="א"),
        _deal("אתונה", 10, 8, source="ב"),
    ])
    assert {s.name: s.count for s in report.sources} == {"א": 1, "ב": 0}
    assert report.total_found == 1
    assert report.dropped_length == 2


def test_length_counted_apart_from_low_star(monkeypatch):
    short_rated = _deal("פראג", 10, 2, 100)
    long_unrated = _deal("רומא", 10, 9, 200)
    long_unrated.stars = None
    ok_unrated = _deal("אתונה", 10, 4, 300)
    ok_unrated.stars = None
    report = _scan(monkeypatch, [short_rated, long_unrated, ok_unrated, _deal("ברלין", 10, 4, 400)])
    assert _dests(report.packages) == ["ברלין"]
    assert report.dropped_length == 2
    assert report.dropped_low_star == 1


def test_length_env_overrides(monkeypatch):
    monkeypatch.setenv("PW_DEALS_MIN_NIGHTS", "1")
    monkeypatch.setenv("PW_DEALS_MAX_NIGHTS", "2")
    report = _scan(monkeypatch, [
        _deal("פראג", 10, 1, 100), _deal("רומא", 10, 2, 200), _deal("אתונה", 10, 3, 300),
    ])
    assert _dests(report.packages) == ["פראג", "רומא"]

    monkeypatch.setenv("PW_DEALS_MIN_NIGHTS", "6")
    monkeypatch.setenv("PW_DEALS_MAX_NIGHTS", "7")
    report = _scan(monkeypatch, [
        _deal("פראג", 10, 5, 100), _deal("רומא", 10, 6, 200),
        _deal("אתונה", 10, 7, 300), _deal("ברלין", 10, 8, 400),
    ])
    assert _dests(report.packages) == ["רומא", "אתונה"]


def test_length_defaults():
    assert (V._min_nights(), V._max_nights()) == (3, 5)


def test_bad_length_env_values_fall_back(monkeypatch):
    monkeypatch.setenv("PW_DEALS_MIN_NIGHTS", "abc")
    monkeypatch.setenv("PW_DEALS_MAX_NIGHTS", "")
    assert (V._min_nights(), V._max_nights()) == (3, 5)
    monkeypatch.setenv("PW_DEALS_MIN_NIGHTS", "4")
    monkeypatch.setenv("PW_DEALS_MAX_NIGHTS", "2")  # below the minimum: raised to it
    assert (V._min_nights(), V._max_nights()) == (4, 4)


# ---------------------------------------------------------------- lead time

def test_departure_today_and_tomorrow_out_day_two_in(monkeypatch):
    report = _scan(monkeypatch, [
        _deal("פראג", 0, 4, 100), _deal("רומא", 1, 4, 200), _deal("אתונה", 2, 4, 300),
        _deal("ברלין", 1, 4, 50, kind=V.KIND_FLIGHT), _deal("וינה", 2, 4, 60, kind=V.KIND_FLIGHT),
    ])
    assert _dests(report.packages) == ["אתונה"]
    assert _dests(report.flights) == ["וינה"]
    assert {s.name: s.count for s in report.sources} == {"אתר": 2}
    # Out-of-window deals are not length drops.
    assert report.dropped_length == 0


def test_lead_days_env_override(monkeypatch):
    monkeypatch.setenv("PW_DEALS_LEAD_DAYS", "0")
    report = _scan(monkeypatch, [_deal("פראג", 0, 4, 100), _deal("רומא", -1, 4, 200)])
    assert _dests(report.packages) == ["פראג"]

    monkeypatch.setenv("PW_DEALS_LEAD_DAYS", "5")
    report = _scan(monkeypatch, [_deal("פראג", 4, 4, 100), _deal("רומא", 5, 4, 200)])
    assert _dests(report.packages) == ["רומא"]


def test_lead_days_default():
    assert V._lead_days() == 2
    assert V._first_departure(TODAY) == TODAY + timedelta(days=2)


# ---------------------------------------------------------------- far end unchanged

def test_europe_far_end_unchanged(monkeypatch):
    report = _scan(monkeypatch, [_deal("פראג", 50, 4, 300), _deal("רומא", 51, 4, 200)])
    europe = _group(report.package_groups, C.EUROPE)
    assert _dests(europe.deals) == ["פראג"]
    assert europe.window_days == 50


def test_other_continent_and_secret_flights_far_end_unchanged(monkeypatch):
    report = _scan(monkeypatch, [
        _deal("ניו יורק", 365, 5, 900, kind=V.KIND_FLIGHT),
        _deal("מיאמי", 366, 5, 500, kind=V.KIND_FLIGHT),
        _deal("רומא", 90, 3, 100, kind=V.KIND_FLIGHT, source=SECRET),
        _deal("אתונה", 91, 3, 90, kind=V.KIND_FLIGHT, source=SECRET),
    ])
    assert _dests(_group(report.flight_groups, C.NORTH_AMERICA).deals) == ["ניו יורק"]
    assert _dests(_group(report.flight_groups, C.EUROPE).deals) == ["רומא"]


def test_lead_days_do_not_shift_the_far_end(monkeypatch):
    monkeypatch.setenv("PW_DEALS_LEAD_DAYS", "10")
    report = _scan(monkeypatch, [_deal("פראג", 50, 4, 300), _deal("רומא", 51, 4, 200),
                                 _deal("אתונה", 9, 4, 100)])
    assert _dests(report.packages) == ["פראג"]


# ---------------------------------------------------------------- _continent_groups on its own

def test_continent_groups_apply_both_rules():
    deals = [_deal("פראג", 10, 2, 100), _deal("רומא", 1, 4, 200),
             _deal("אתונה", 10, None, 250), _deal("ברלין", 2, 5, 300)]
    for d in deals:
        d.continent = C.EUROPE
    europe = _group(V._continent_groups(deals, TODAY), C.EUROPE)
    assert _dests(europe.deals) == ["ברלין"]
