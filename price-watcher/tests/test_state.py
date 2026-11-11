"""Change detection + state persistence (state.evaluate / apply_many / mark_notified)."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from price_watcher import state
from price_watcher.config import CheckConfig
from price_watcher.contracts import (
    ChangeType,
    FetchResult,
    FetchStatus,
    PromoInfo,
    Watch,
)
from price_watcher.paths import state_file

CFG = CheckConfig(failure_threshold=3, sanity_jump_pct=90.0, delay_min_sec=0, delay_max_sec=0)
URL = "https://x.example/p"


def ok(price, currency="ILS", in_stock=True, promo=None, **kw) -> FetchResult:
    return FetchResult(
        status=FetchStatus.OK,
        final_url=URL,
        price=Decimal(price),
        currency=currency,
        in_stock=in_stock,
        promo=promo,
        **kw,
    )


def bad() -> FetchResult:
    return FetchResult(status=FetchStatus.UNREACHABLE, final_url=URL, error_message="boom")


def step(watch: Watch, fetch: FetchResult):
    ev = state.evaluate(watch, fetch, CFG)
    state.apply_many([ev])
    return ev


def ws(watch_id: str = "w1") -> dict:
    return state.load_state()["watches"][watch_id]


@pytest.fixture
def watch() -> Watch:
    return Watch(id="w1", url=URL)


# --------------------------------------------------------------------- #27

def test_stock_and_promo_change_at_same_price_is_not_a_price_change(watch):
    step(watch, ok("100"))
    ev = step(watch, ok("100", in_stock=False, promo=PromoInfo(code="X10", discount_pct=Decimal(10))))
    assert ev.type == ChangeType.STOCK_CHANGED
    assert ev.stock_changed and ev.promo_changed
    assert ev.pct_change is None and ev.old_price is None
    # both parts recorded, so the next run is quiet
    assert step(watch, ok("100", in_stock=False, promo=PromoInfo(code="X10", discount_pct=Decimal(10)))).type == ChangeType.NO_CHANGE


def test_price_change_carries_stock_change(watch):
    step(watch, ok("100"))
    ev = step(watch, ok("80", in_stock=False))
    assert ev.type == ChangeType.PRICE_CHANGED
    assert ev.stock_changed and not ev.promo_changed


def test_min_change_filter_keeps_stock_change(watch):
    watch.min_change_pct = 5.0
    step(watch, ok("100"))
    ev = step(watch, ok("99", in_stock=False))
    assert ev.type == ChangeType.STOCK_CHANGED
    assert ev.pct_change is None


# --------------------------------------------------------------------- #28

def test_currency_switch_is_counted_then_accepted(watch):
    step(watch, ok("100", currency="ILS"))
    for _ in range(CFG.failure_threshold - 1):
        assert step(watch, ok("30", currency="USD")).type == ChangeType.SANITY_DEFERRED
    assert ws()["currency_hold_count"] == CFG.failure_threshold - 1
    ev = step(watch, ok("30", currency="USD"))
    assert ev.type == ChangeType.PRICE_CHANGED
    assert ev.old_currency == "ILS" and ev.old_price == Decimal("100") and ev.new_price == Decimal("30")
    assert ev.pct_change is None
    assert ws()["currency"] == "USD" and ws()["currency_hold_count"] == 0
    assert step(watch, ok("30", currency="USD")).type == ChangeType.NO_CHANGE


def test_currency_hold_resets_when_currency_returns(watch):
    step(watch, ok("100", currency="ILS"))
    step(watch, ok("30", currency="USD"))
    assert step(watch, ok("100", currency="ILS")).type == ChangeType.NO_CHANGE
    assert ws()["currency_hold_count"] == 0


def test_price_zero_escalates_to_threshold_failure_then_recovers(watch):
    step(watch, ok("100"))
    types = [step(watch, ok("0")).type for _ in range(CFG.failure_threshold)]
    assert types == [ChangeType.SANITY_DEFERRED] * (CFG.failure_threshold - 1) + [ChangeType.THRESHOLD_FAILURE]
    assert ws()["last_price"] == "100"
    ev = state.evaluate(watch, ok("0"), CFG)
    state.mark_notified([ev])  # pretend the alert was emailed
    state.apply_many([ev])
    assert step(watch, ok("0")).type == ChangeType.SANITY_DEFERRED  # alerted once only
    assert step(watch, ok("100")).type == ChangeType.RECOVERED


# --------------------------------------------------------------------- #29

def test_min_change_pct_measured_against_last_reported_price(watch):
    watch.min_change_pct = 1.0
    step(watch, ok("100"))
    ev1 = step(watch, ok("99.10"))
    assert ev1.type == ChangeType.NO_CHANGE
    ev2 = step(watch, ok("98.21"))
    assert ev2.type == ChangeType.PRICE_CHANGED
    assert ev2.old_price == Decimal("100")
    assert ws()["baseline_price"] == "98.21"
    # observed prices are still recorded
    assert [h["price"] for h in ws()["history"]] == ["100", "99.10", "98.21"]
    assert ws()["last_price"] == "98.21"


def test_baseline_creep_fixed_for_legacy_state_without_baseline(watch):
    state_file().write_text(json.dumps({
        "schema_version": 1,
        "watches": {"w1": {
            "last_price": "100", "currency": "ILS", "in_stock": True, "promo": None,
            "consecutive_failures": 0, "failure_notified": False,
            "last_success_time": "2026-11-01T09:00:00+02:00",
            "history": [{"t": "2026-11-01T09:00:00+02:00", "price": "100"}],
        }},
    }), encoding="utf-8")
    watch.min_change_pct = 1.0
    assert step(watch, ok("99.5")).type == ChangeType.NO_CHANGE
    ev = step(watch, ok("99"))
    assert ev.type == ChangeType.PRICE_CHANGED and ev.old_price == Decimal("100")


def test_notify_only_on_drop_measures_against_last_reported_price(watch):
    watch.notify_only_on_drop = True
    step(watch, ok("100"))
    assert step(watch, ok("120")).type == ChangeType.NO_CHANGE
    assert step(watch, ok("110")).type == ChangeType.NO_CHANGE  # still above 100
    ev = step(watch, ok("95"))
    assert ev.type == ChangeType.PRICE_CHANGED and ev.old_price == Decimal("100")


# --------------------------------------------------------------------- #30

def test_threshold_failure_fires_on_the_threshold_th_failure(watch):
    step(watch, ok("100"))
    types = [step(watch, bad()).type for _ in range(CFG.failure_threshold)]
    assert types[-1] == ChangeType.THRESHOLD_FAILURE
    assert all(t == ChangeType.FAILED for t in types[:-1])


def test_recovery_carries_price_change_and_history(watch):
    step(watch, ok("100"))
    for _ in range(CFG.failure_threshold - 1):
        step(watch, bad())
    ev = state.evaluate(watch, bad(), CFG)
    assert ev.type == ChangeType.THRESHOLD_FAILURE
    state.mark_notified([ev])
    state.apply_many([ev])

    rec = step(watch, ok("60", in_stock=False))
    assert rec.type == ChangeType.RECOVERED
    assert rec.old_price == Decimal("100") and rec.new_price == Decimal("60")
    assert rec.pct_change == pytest.approx(-40.0)
    assert rec.stock_changed
    assert ws()["history"][-1]["price"] == "60"
    assert ws()["consecutive_failures"] == 0 and ws()["failure_notified"] is False
    assert step(watch, ok("60", in_stock=False)).type == ChangeType.NO_CHANGE


# --------------------------------------------------------------------- #31

def test_removed_promo_is_cleared_from_state(watch):
    promo = PromoInfo(code="X10", discount_pct=Decimal(10))
    step(watch, ok("100", promo=promo, regular_price=Decimal("120"), estimated_final_price=Decimal("90")))
    ev = step(watch, ok("90"))
    assert ev.type == ChangeType.PRICE_CHANGED and ev.promo_changed
    assert ws()["promo"] is None
    assert ws()["regular_price"] is None and ws()["estimated_final_price"] is None
    assert step(watch, ok("90")).type == ChangeType.NO_CHANGE


def test_mark_notified_stamps_only_notified_watches():
    a, b = Watch(id="a", url="https://a.example/p"), Watch(id="b", url="https://b.example/p")
    state.apply_many([state.evaluate(a, ok("100"), CFG), state.evaluate(b, ok("50"), CFG)])
    ev_a = state.evaluate(a, ok("80"), CFG)
    ev_b = state.evaluate(b, ok("50"), CFG)
    assert ev_a.type == ChangeType.PRICE_CHANGED and ev_b.type == ChangeType.NO_CHANGE
    state.mark_notified([ev_a, ev_b])
    assert "last_notified_at" in ws("a")
    assert "last_notified_at" not in ws("b")


# --------------------------------------------------------------------- #12

def test_unsent_notification_is_not_applied(watch):
    step(watch, ok("100"))
    ev = state.evaluate(watch, ok("80"), CFG)
    state.apply_many([ev], notification_sent=False)
    assert ws()["last_price"] == "100"
    again = state.evaluate(watch, ok("80"), CFG)
    assert again.type == ChangeType.PRICE_CHANGED and again.old_price == Decimal("100")
