"""Luggage line in the deals email and the luggage-aware "הכי משתלם" mark."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from price_watcher import email_sender as E
from price_watcher import vacations as V

TODAY = date.today()


def _pkg(price: int, nights: int, luggage: str = V.LUGGAGE_UNKNOWN, extra: str = "",
         dest: str = "רומא") -> V.VacationDeal:
    start = TODAY + timedelta(days=10)
    return V.VacationDeal(
        source="אתר", kind=V.KIND_PACKAGE, destination=dest, price=Decimal(price),
        url="https://example.invalid/deal", date_from=start,
        date_to=start + timedelta(days=nights), stars=Decimal(4),
        luggage=luggage, luggage_extra=extra, adults=2,
    )


def _value_row(deals) -> int | None:
    marks = E._deal_badges(deals, is_packages=True)
    hits = [i for i, bs in marks.items() if E._BADGE_VALUE in bs]
    return hits[0] if hits else None


def test_luggage_line_for_each_state():
    assert E._luggage_line(_pkg(1, 3, V.LUGGAGE_SUITCASE)) == "כבודה: מזוודה כלולה"
    assert E._luggage_line(_pkg(1, 3, V.LUGGAGE_CARRY_ON)) == "כבודה: טרולי כלול"
    assert E._luggage_line(_pkg(1, 3, V.LUGGAGE_NONE)) == "ללא כבודה"
    assert E._luggage_line(_pkg(1, 3, V.LUGGAGE_NONE, "טרולי $25 לכיוון")) == "ללא כבודה (טרולי $25 לכיוון)"
    assert E._luggage_line(_pkg(1, 3)) == "כבודה: לא צוין באתר"


def test_carry_on_counts_as_luggage():
    assert _pkg(1, 3, V.LUGGAGE_CARRY_ON).has_luggage
    assert _pkg(1, 3, V.LUGGAGE_SUITCASE).has_luggage
    assert not _pkg(1, 3, V.LUGGAGE_NONE).has_luggage
    assert not _pkg(1, 3).has_luggage


def test_best_value_skips_cheaper_rows_without_luggage():
    deals = [
        _pkg(150, 5, V.LUGGAGE_NONE),       # $30/night, no luggage
        _pkg(160, 5),                        # $32/night, not stated
        _pkg(200, 5, V.LUGGAGE_CARRY_ON),    # $40/night, carry-on -> wins
        _pkg(240, 5, V.LUGGAGE_SUITCASE),    # $48/night
    ]
    assert _value_row(deals) == 2


def test_no_best_value_when_no_row_has_luggage():
    deals = [_pkg(150, 5, V.LUGGAGE_NONE), _pkg(160, 4)]
    assert _value_row(deals) is None
    # The cheapest-package mark is unaffected by luggage.
    marks = E._deal_badges(deals, is_packages=True)
    assert E._BADGE_PACKAGE in marks[0]


def test_email_shows_luggage_and_no_value_note():
    deals = [_pkg(150, 5, V.LUGGAGE_NONE, dest="רומא"), _pkg(160, 4, dest="אתונה")]
    rows = "".join(E._vacation_rows_html(deals, is_packages=True))
    assert "ללא כבודה" in rows and "כבודה: לא צוין באתר" in rows
    flights = [V.VacationDeal(source="אתר", kind=V.KIND_FLIGHT, destination="רומא",
                              price=Decimal(90), url="https://example.invalid/f",
                              luggage=V.LUGGAGE_CARRY_ON)]
    assert "כבודה: טרולי כלול" in "".join(E._vacation_rows_html(flights, is_packages=False))


def test_every_row_has_a_deal_button():
    deals = [_pkg(150, 5, dest="רומא"), _pkg(160, 4, dest="אתונה")]
    rows = E._vacation_rows_html(deals, is_packages=True)
    for row in rows:
        assert 'class="go" href="https://example.invalid/deal"' in row
        assert E._DEAL_LINK_LABEL in row
    assert f"background:{E._DEAL_BUTTON};color:#ffffff" in E._VAC_CSS
    # Rows are no longer tinted by their marks; only zebra striping is left.
    assert not any('class="r' in r for r in rows)
