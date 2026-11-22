"""Couples-only packages and the per-night package price in the deals email."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from price_watcher import email_sender as E
from price_watcher import vacations as V

TODAY = date.today()


@pytest.fixture(autouse=True)
def _couples_on(monkeypatch):
    monkeypatch.setenv("PW_DEALS_COUPLES_ONLY", "1")
    for var in ("PW_DEALS_DAYS", "PW_DEALS_LEAD_DAYS", "PW_DEALS_MIN_NIGHTS", "PW_DEALS_MAX_NIGHTS"):
        monkeypatch.delenv(var, raising=False)


def _pkg(dest: str, price: int, adults: int, children: int = 0, nights: int = 3,
         kind: str = V.KIND_PACKAGE, luggage: str = V.LUGGAGE_CARRY_ON) -> V.VacationDeal:
    start = TODAY + timedelta(days=10)
    return V.VacationDeal(
        source="אתר", kind=kind, destination=dest, price=Decimal(price),
        url="https://example.invalid/deal", date_from=start,
        date_to=start + timedelta(days=nights),
        stars=Decimal(4) if kind == V.KIND_PACKAGE else None,
        adults=adults, children=children, luggage=luggage,
    )


def _scan(monkeypatch, deals):
    monkeypatch.setattr(V, "_SOURCES", (("אתר", lambda: list(deals)),))
    return V.scan_and_report()


def test_only_packages_for_two_adults_are_kept(monkeypatch):
    report = _scan(monkeypatch, [
        _pkg("רומא", 186, 2),
        _pkg("אתונה", 150, 2, 2),           # family
        _pkg("פראג", 140, 1),               # single
        _pkg("בודפשט", 120, 0),             # party not stated
        _pkg("וינה", 90, 0, kind=V.KIND_FLIGHT),  # flights are unaffected
    ])
    assert [d.destination for d in report.packages] == ["רומא"]
    assert [d.destination for d in report.flights] == ["וינה"]
    assert report.dropped_party == 3


def test_rule_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("PW_DEALS_COUPLES_ONLY", "0")
    report = _scan(monkeypatch, [_pkg("רומא", 186, 2), _pkg("אתונה", 150, 2, 2)])
    assert len(report.packages) == 2 and report.dropped_party == 0


def test_package_row_shows_couple_price_per_night_and_package_price():
    rows = "".join(E._vacation_rows_html([_pkg("רומא", 186, 2)], is_packages=True))
    assert "$124" in rows and "ללילה לזוג" in rows   # $372 for two / 3 nights
    assert "$372" in rows                               # package price stays
    assert "$186" not in rows                           # no per-person price
    assert "$62" not in rows                            # no per-person-per-night


def test_flight_row_keeps_price_per_person():
    rows = "".join(E._vacation_rows_html([_pkg("רומא", 90, 0, kind=V.KIND_FLIGHT)], is_packages=False))
    assert "$90" in rows and "ללילה" not in rows


def test_package_marks_use_package_price():
    deals = [_pkg("רומא", 200, 2, nights=5), _pkg("אתונה", 150, 2, nights=3)]
    marks = E._deal_badges(deals, is_packages=True)
    assert E._BADGE_PACKAGE in marks[1]                 # $300 < $400
    assert E._BADGE_VALUE in marks[0]                   # $80/night < $100/night
    assert not any(E._BADGE_CHEAPEST in m for m in marks.values())


def test_header_and_subject(monkeypatch):
    report = _scan(monkeypatch, [_pkg("רומא", 186, 2), _pkg("לונדון", 90, 0, kind=V.KIND_FLIGHT)])
    html = E._vacations_section_html(report)
    assert "מחיר ללילה" in html and "מחיר החבילה" in html
    payload = E.build_deals_payload(report)
    assert "חבילות לזוג מ-$372" in payload.subject
    assert "$124 ללילה" in payload.text_body
