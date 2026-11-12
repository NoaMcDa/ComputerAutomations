"""Secret Flights deals use their own 90-day departure window."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from price_watcher import continents as C
from price_watcher import email_sender
from price_watcher import vacations as V

TODAY = date.today()
SECRET = V._SECRET_FLIGHTS


@pytest.fixture(autouse=True)
def _clean_deal_env(monkeypatch):
    for var in ("PW_DEALS_DAYS", "PW_DEALS_TOP", "PW_DEALS_OTHER_TOP", "PW_DEALS_EUROPE_DAYS",
                "PW_DEALS_OTHER_DAYS", "PW_DEALS_SECRET_FLIGHTS_DAYS",
                "PW_DEALS_LEAD_DAYS", "PW_DEALS_MIN_NIGHTS", "PW_DEALS_MAX_NIGHTS"):
        monkeypatch.delenv(var, raising=False)


def _flight(destination: str, days: int, price: int, source: str) -> V.VacationDeal:
    return V.VacationDeal(
        source=source, kind=V.KIND_FLIGHT, destination=destination, price=Decimal(price),
        url="https://example.invalid/deal",
        date_from=TODAY + timedelta(days=days), date_to=TODAY + timedelta(days=days + 4),
    )


def _europe_flights(monkeypatch, deals) -> list[tuple[str, int]]:
    by_source: dict[str, list[V.VacationDeal]] = {}
    for d in deals:
        by_source.setdefault(d.source, []).append(d)
    monkeypatch.setattr(V, "_SOURCES", tuple(
        (name, (lambda ds=ds: list(ds))) for name, ds in by_source.items()))
    report = V.scan_and_report()
    group = next(g for g in report.flight_groups if g.continent == C.EUROPE)
    return [(d.source, (d.date_from - TODAY).days) for d in group.deals]


def test_secret_flights_day_90_in_day_91_out(monkeypatch):
    shown = _europe_flights(monkeypatch, [
        _flight("רומא", 90, 100, SECRET),
        _flight("אתונה", 91, 90, SECRET),
    ])
    assert shown == [(SECRET, 90)]


def test_other_sites_keep_the_50_day_europe_window(monkeypatch):
    shown = _europe_flights(monkeypatch, [
        _flight("רומא", 50, 100, "טרווליסט"),
        _flight("אתונה", 60, 90, "טרווליסט"),
        _flight("ורשה", 60, 95, SECRET),
    ])
    assert sorted(shown) == [(SECRET, 60), ("טרווליסט", 50)]


def test_secret_flights_window_from_env_and_global_cap(monkeypatch):
    monkeypatch.setenv("PW_DEALS_SECRET_FLIGHTS_DAYS", "120")
    assert _europe_flights(monkeypatch, [_flight("רומא", 110, 100, SECRET)]) == [(SECRET, 110)]
    monkeypatch.setenv("PW_DEALS_DAYS", "30")
    assert _europe_flights(monkeypatch, [_flight("רומא", 40, 100, SECRET)]) == []


def test_email_states_the_secret_flights_window(monkeypatch):
    _europe_flights(monkeypatch, [_flight("רומא", 70, 100, SECRET)])
    report = V.scan_and_report()
    payload = email_sender.build_deals_payload(report)
    assert f"{SECRET}: יציאה ב-90 הימים הקרובים" in payload.html_body
