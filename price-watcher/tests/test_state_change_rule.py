"""Issue #5 (store prices, change rule) and #8 (failures, sanity guards)."""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from core_helpers import cfg, fail, ok, promo, watch
from price_watcher import state
from price_watcher.contracts import ChangeType, FetchStatus
from price_watcher.paths import state_file


def run(w, fetch, c=None):
    """evaluate + apply, like one check run whose email succeeded."""
    ev = state.evaluate(w, fetch, c or cfg())
    state.mark_notified([ev])
    state.apply_many([ev])
    return ev


# ---- #5 change rule -------------------------------------------------------

def test_first_observation_records_only():
    w = watch()
    ev = run(w, ok("100"))
    assert ev.type == ChangeType.FIRST_SEEN
    ws = state.load_state()["watches"]["shop-1"]
    assert ws["last_price"] == "100"
    assert ws["currency"] == "ILS"
    assert ws["last_check_time"] and ws["last_success_time"]
    assert ws["consecutive_failures"] == 0
    assert ws["history"] == [{"t": ws["history"][0]["t"], "price": "100"}]


def test_same_price_is_no_change_and_twice_sends_nothing():
    w = watch()
    run(w, ok("100"))
    assert run(w, ok("100")).type == ChangeType.NO_CHANGE
    assert run(w, ok("100")).type == ChangeType.NO_CHANGE


def test_price_change_reported_exactly_once():
    w = watch()
    run(w, ok("100"))
    ev = run(w, ok("90"))
    assert ev.type == ChangeType.PRICE_CHANGED
    assert ev.old_price == Decimal("100") and ev.new_price == Decimal("90")
    assert ev.diff == Decimal("-10") and ev.pct_change == pytest.approx(-10.0)
    assert run(w, ok("90")).type == ChangeType.NO_CHANGE
    hist = [h["price"] for h in state.load_state()["watches"]["shop-1"]["history"]]
    assert hist == ["100", "90"]


def test_decimal_comparison_19_90_equals_19_9():
    w = watch()
    run(w, ok("19.90"))
    assert run(w, ok("19.9")).type == ChangeType.NO_CHANGE


def test_failed_fetch_is_not_a_change_and_keeps_price():
    w = watch()
    run(w, ok("100"))
    ev = run(w, fail())
    assert ev.type == ChangeType.FAILED
    ws = state.load_state()["watches"]["shop-1"]
    assert ws["last_price"] == "100"
    assert ws["consecutive_failures"] == 1
    # back with the same price -> no change, failures reset
    assert run(w, ok("100")).type == ChangeType.NO_CHANGE
    assert state.load_state()["watches"]["shop-1"]["consecutive_failures"] == 0


def test_ok_fetch_without_price_is_a_failure_not_a_change():
    w = watch()
    run(w, ok("100"))
    ev = run(w, ok(None))
    assert ev.type == ChangeType.FAILED
    assert ev.fetch.status == FetchStatus.PRICE_NOT_FOUND


# ---- #5 optional per-link settings ---------------------------------------

def test_only_drops_ignores_rises_but_reports_drops():
    w = watch(notify_only_on_drop=True)
    run(w, ok("100"))
    assert run(w, ok("110")).type == ChangeType.NO_CHANGE
    # 105 is below the price seen but above what the user last heard (100)
    assert run(w, ok("105")).type == ChangeType.NO_CHANGE
    ev = run(w, ok("95"))
    assert ev.type == ChangeType.PRICE_CHANGED and ev.old_price == Decimal("100")


def test_min_change_pct_is_cumulative():
    w = watch(min_change_pct=5.0)
    run(w, ok("100"))
    for p in ("99", "98", "97", "96"):
        assert run(w, ok(p)).type == ChangeType.NO_CHANGE
    ev = run(w, ok("95"))
    assert ev.type == ChangeType.PRICE_CHANGED
    assert ev.old_price == Decimal("100") and ev.pct_change == pytest.approx(-5.0)
    # history still records every observed price
    hist = [h["price"] for h in state.load_state()["watches"]["shop-1"]["history"]]
    assert hist == ["100", "99", "98", "97", "96", "95"]


def test_min_change_abs():
    w = watch(min_change_abs=Decimal("10"))
    run(w, ok("100"))
    assert run(w, ok("95")).type == ChangeType.NO_CHANGE
    assert run(w, ok("90")).type == ChangeType.PRICE_CHANGED


def test_target_price():
    w = watch(target_price=Decimal("80"))
    run(w, ok("100"))
    assert run(w, ok("90")).type == ChangeType.NO_CHANGE
    ev = run(w, ok("79.90"))
    assert ev.type == ChangeType.PRICE_CHANGED and ev.old_price == Decimal("100")
    assert run(w, ok("85")).type == ChangeType.NO_CHANGE


# ---- #5 atomic writes / history ------------------------------------------

def test_atomic_write_leaves_no_temp_file_and_uses_replace(monkeypatch):
    calls = []
    import os
    real = os.replace
    monkeypatch.setattr(state.os, "replace", lambda a, b: (calls.append((a, b)), real(a, b)))
    run(watch(), ok("100"))
    assert calls and str(calls[-1][1]) == str(state_file())
    assert not state_file().with_suffix(".json.tmp").exists()
    json.loads(state_file().read_text(encoding="utf-8"))


def test_crash_during_write_keeps_old_state(monkeypatch):
    run(watch(), ok("100"))
    before = state_file().read_text(encoding="utf-8")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(state.json, "dump", boom)
    with pytest.raises(OSError):
        run(watch(), ok("90"))
    assert state_file().read_text(encoding="utf-8") == before


def test_corrupt_state_is_backed_up():
    state_file().write_text("{not json", encoding="utf-8")
    assert state.load_state()["watches"] == {}
    backups = list(state_file().parent.glob("state.json.corrupt-*"))
    assert len(backups) == 1 and backups[0].read_text() == "{not json"


def test_history_is_capped(monkeypatch):
    monkeypatch.setattr(state, "_HISTORY_CAP", 3)
    w = watch()
    for p in ("10", "11", "12", "13", "14"):
        run(w, ok(p), cfg(jump=1000))
    hist = [h["price"] for h in state.load_state()["watches"]["shop-1"]["history"]]
    assert hist == ["12", "13", "14"]


# ---- #8 failures ------------------------------------------------------------

def test_threshold_failure_once_then_recovery_once():
    w = watch()
    c = cfg(threshold=3)
    run(w, ok("100"), c)
    types = [run(w, fail(FetchStatus.BLOCKED, "Blocked (HTTP 403)", 403), c).type for _ in range(6)]
    assert types == [ChangeType.FAILED, ChangeType.FAILED, ChangeType.THRESHOLD_FAILURE,
                     ChangeType.FAILED, ChangeType.FAILED, ChangeType.FAILED]
    assert run(w, ok("100"), c).type == ChangeType.RECOVERED
    assert run(w, ok("100"), c).type == ChangeType.NO_CHANGE


def test_threshold_alert_retried_when_email_failed():
    w = watch()
    c = cfg(threshold=2)
    run(w, ok("100"), c)
    run(w, fail(), c)
    ev = state.evaluate(w, fail(), c)
    assert ev.type == ChangeType.THRESHOLD_FAILURE
    state.apply_many([ev], email_failed=True)          # email could not be sent
    assert state.evaluate(w, fail(), c).type == ChangeType.THRESHOLD_FAILURE


def test_link_that_never_worked_still_alerts():
    w = watch()
    c = cfg(threshold=2)
    assert run(w, fail(), c).type == ChangeType.FAILED
    assert run(w, fail(), c).type == ChangeType.THRESHOLD_FAILURE


def test_failure_details_stored_in_state():
    w = watch()
    run(w, ok("100"))
    run(w, fail(FetchStatus.NOT_FOUND, "404 Not Found", 404))
    err = state.load_state()["watches"]["shop-1"]["last_error"]
    assert err == {"status": "not_found", "http_status": 404, "message": "404 Not Found"}


def test_out_of_stock_and_back_in_stock_are_stock_events():
    w = watch()
    run(w, ok("100", in_stock=True))
    ev = run(w, ok("100", in_stock=False))
    assert ev.type == ChangeType.STOCK_CHANGED and ev.fetch.in_stock is False
    # out-of-stock page with no price: still a stock state, not a failure
    assert run(w, ok(None, in_stock=False)).type == ChangeType.NO_CHANGE
    ev = run(w, ok("100", in_stock=True))
    assert ev.type == ChangeType.STOCK_CHANGED and ev.fetch.in_stock is True


def test_price_zero_is_never_a_change():
    # Merge note: a price of 0 is a deferred reading that counts as a failed
    # fetch (it escalates to the failure alert), not a FAILED fetch result.
    w = watch()
    run(w, ok("100"))
    ev = run(w, ok("0"))
    assert ev.type == ChangeType.SANITY_DEFERRED
    assert "0" in ev.reason
    ws = state.load_state()["watches"]["shop-1"]
    assert ws["last_price"] == "100" and ws["consecutive_failures"] == 1


def test_currency_change_rechecked_then_accepted():
    # Merge note: a new currency is accepted once it persisted for
    # failure_threshold runs (here 2: deferred once, then accepted).
    w = watch()
    c = cfg(threshold=2)
    run(w, ok("100", currency="ILS"), c)
    ev = run(w, ok("30", currency="USD"), c)
    assert ev.type == ChangeType.SANITY_DEFERRED
    assert state.load_state()["watches"]["shop-1"]["last_price"] == "100"
    ev = run(w, ok("30", currency="USD"), c)
    assert ev.type == ChangeType.PRICE_CHANGED and ev.pct_change is None
    assert state.load_state()["watches"]["shop-1"]["currency"] == "USD"


def test_big_jump_deferred_once():
    w = watch()
    run(w, ok("100"))
    assert run(w, ok("5")).type == ChangeType.SANITY_DEFERRED
    assert state.load_state()["watches"]["shop-1"]["last_price"] == "100"
    assert run(w, ok("5")).type == ChangeType.PRICE_CHANGED


def test_big_jump_that_goes_away_is_forgotten():
    w = watch()
    run(w, ok("100"))
    assert run(w, ok("5")).type == ChangeType.SANITY_DEFERRED
    assert run(w, ok("100")).type == ChangeType.NO_CHANGE
    assert run(w, ok("5")).type == ChangeType.SANITY_DEFERRED


def test_redirect_to_home_page_is_not_found():
    w = watch()
    run(w, ok("100"))
    ev = run(w, ok("49", final_url="https://shop.example/"))
    assert ev.type == ChangeType.FAILED
    assert ev.fetch.status == FetchStatus.NOT_FOUND
    assert "home page" in ev.fetch.error_message


# ---- #11 promotions in state ---------------------------------------------

def test_promo_change_and_promotion_ended():
    w = watch()
    run(w, ok("189", promo=promo("20"), estimated_final_price=Decimal("151.20")))
    ev = run(w, ok("189", promo=promo("30", "VIP30"), estimated_final_price=Decimal("132.30")))
    assert ev.type == ChangeType.PROMO_CHANGED
    assert ev.old_promo.code == "VIP20" and ev.old_estimated_price == Decimal("151.20")
    ev = run(w, ok("189"))
    assert ev.type == ChangeType.PROMO_CHANGED           # promotion ended
    assert ev.fetch.promo is None and ev.old_promo.code == "VIP30"
    ws = state.load_state()["watches"]["shop-1"]
    assert ws["promo"] is None and ws["estimated_final_price"] is None
    assert run(w, ok("189")).type == ChangeType.NO_CHANGE  # not reported twice


def test_unreadable_or_tiered_banner_is_not_a_change():
    w = watch()
    run(w, ok("189", promo=promo("20"), estimated_final_price=Decimal("151.20")))
    unreadable = promo("0", "SPRING")
    assert run(w, ok("189", promo=unreadable, estimated_final_price=Decimal("189"))).type \
        == ChangeType.NO_CHANGE
    # the last readable promotion is kept, so coming back is not news either
    assert run(w, ok("189", promo=promo("20"), estimated_final_price=Decimal("151.20"))).type \
        == ChangeType.NO_CHANGE


def test_promo_off_switch_and_excluded_flag_strip_promo():
    for w in (watch(track_promo=False), watch(id="shop-2", excluded_from_promo=True)):
        ev = run(w, ok("189", promo=promo("20"), estimated_final_price=Decimal("151.20")))
        assert ev.fetch.promo is None and ev.fetch.estimated_final_price is None
        ev = run(w, ok("189", promo=promo("40"), estimated_final_price=Decimal("113.40")))
        assert ev.type == ChangeType.NO_CHANGE
