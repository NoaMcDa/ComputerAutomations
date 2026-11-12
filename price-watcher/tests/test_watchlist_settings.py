"""Issue #40: watchlist.load_all robustness; per-link settings CLI (add flags / set)."""
from __future__ import annotations

import json
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from core_helpers import URL, ok, watch, write_watchlist
from price_watcher import fetcher, watchlist
from price_watcher.cli import app, parse_min_change
from price_watcher.paths import watchlist_file


# ---- load_all robustness ---------------------------------------------------

def test_corrupt_file_is_backed_up_and_treated_as_empty():
    watchlist_file().write_text("{oops", encoding="utf-8")
    assert watchlist.load_all() == []
    backups = list(watchlist_file().parent.glob("watchlist.json.corrupt-*"))
    assert len(backups) == 1 and backups[0].read_text() == "{oops"


def test_wrong_structure_is_treated_as_corrupt():
    watchlist_file().write_text("[1, 2]", encoding="utf-8")
    assert watchlist.load_all() == []


def test_unknown_keys_are_ignored_and_bad_records_skipped():
    good = {"id": "a", "url": URL, "name": "Shoe", "created_at": "2026-01-01T00:00:00",
            "future_field": 1, "target_price": "80"}
    payload = {"schema_version": 1, "watches": [
        good,
        {"id": "b"},                               # no url
        "not a record",
        {"id": "c", "url": URL, "target_price": "abc"},
        {"id": "d", "url": URL, "created_at": None},
    ]}
    watchlist_file().write_text(json.dumps(payload), encoding="utf-8")
    ws = watchlist.load_all()
    assert [w.id for w in ws] == ["a", "d"]
    assert ws[0].target_price == Decimal("80")


def test_old_records_without_new_fields_load_with_defaults():
    payload = {"watches": [{"id": "a", "url": URL, "name": None, "selector": None,
                            "created_at": "2026-01-01T00:00:00",
                            "notify_only_on_drop": False, "min_change_pct": None,
                            "target_price": None, "excluded_from_promo": False, "paused": False}]}
    watchlist_file().write_text(json.dumps(payload), encoding="utf-8")
    [w] = watchlist.load_all()
    assert w.track_promo is True and w.min_change_abs is None


def test_round_trip_of_all_settings():
    w = watch(notify_only_on_drop=True, min_change_pct=5.0, min_change_abs=Decimal("10"),
              target_price=Decimal("79.90"), track_promo=False, excluded_from_promo=True,
              paused=True)
    write_watchlist([w])
    assert watchlist.load_all() == [w]


# ---- CLI: add flags and set ------------------------------------------------

def test_parse_min_change():
    assert parse_min_change("5%") == {"min_change_pct": 5.0}
    assert parse_min_change("10") == {"min_change_abs": "10"}
    assert parse_min_change(None) == {}
    import typer
    with pytest.raises(typer.BadParameter):
        parse_min_change("lots")


def test_add_with_flags(monkeypatch):
    monkeypatch.setattr(fetcher, "fetch", lambda url, selector=None, **k: ok("100"))
    res = CliRunner().invoke(app, [
        "add", URL, "--name", "Shoe", "--only-drops", "--min-change", "5%",
        "--target", "79.90", "--no-promo", "--excluded-from-promo",
    ])
    assert res.exit_code == 0, res.output
    [w] = watchlist.load_all()
    assert w.notify_only_on_drop and w.min_change_pct == 5.0
    assert w.target_price == Decimal("79.90")
    assert w.track_promo is False and w.excluded_from_promo is True


def test_add_rejects_bad_option_before_fetching(monkeypatch):
    called = []
    monkeypatch.setattr(fetcher, "fetch", lambda *a, **k: called.append(1) or ok("1"))
    res = CliRunner().invoke(app, ["add", URL, "--target", "cheap"])
    assert res.exit_code == 1 and called == []


def test_set_changes_and_clears_settings():
    write_watchlist([watch(id="w1")])
    r = CliRunner().invoke(app, ["set", "w1", "--min-change", "10", "--target", "50",
                                 "--pause", "--only-drops"])
    assert r.exit_code == 0, r.output
    [w] = watchlist.load_all()
    assert w.min_change_abs == Decimal("10") and w.min_change_pct is None
    assert w.target_price == Decimal("50") and w.paused and w.notify_only_on_drop
    r = CliRunner().invoke(app, ["set", URL, "--min-change", "3%", "--target", "none",
                                 "--resume", "--all-changes", "--no-promo"])
    assert r.exit_code == 0, r.output
    [w] = watchlist.load_all()
    assert w.min_change_pct == 3.0 and w.min_change_abs is None
    assert w.target_price is None and not w.paused and not w.notify_only_on_drop
    assert w.track_promo is False
    r = CliRunner().invoke(app, ["set", URL, "--min-change", "none"])
    [w] = watchlist.load_all()
    assert w.min_change_pct is None and w.min_change_abs is None


def test_set_unknown_watch_and_no_options():
    write_watchlist([watch(id="w1")])
    assert CliRunner().invoke(app, ["set", "nope", "--pause"]).exit_code == 1
    assert CliRunner().invoke(app, ["set", "w1"]).exit_code != 0


def test_check_survives_corrupt_watchlist(monkeypatch):
    from price_watcher import scheduler
    watchlist_file().write_text("garbage", encoding="utf-8")
    assert scheduler.run_check(dry_run=True) == 0
