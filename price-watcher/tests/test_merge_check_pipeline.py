"""Edge cases ported from main (PR #56) into the check pipeline during the merge."""
from __future__ import annotations

import io
import xml.etree.ElementTree as ET

from rich.console import Console

from core_helpers import URL, FakeSMTP, cfg, fail, install_env, ok, script_fetch, watch, write_watchlist
from price_watcher import email_sender, scheduler, state
from price_watcher.contracts import ChangeType, FetchStatus


def _run(w, fetch, c):
    ev = state.evaluate(w, fetch, c)
    state.mark_notified([ev])
    state.apply_many([ev])
    return ev


def test_link_that_never_worked_alerts_then_recovers_once():
    w, c = watch(), cfg(threshold=2)
    assert _run(w, fail(), c).type == ChangeType.FAILED
    assert _run(w, fail(), c).type == ChangeType.THRESHOLD_FAILURE
    ev = _run(w, ok("100"), c)
    assert ev.type == ChangeType.RECOVERED and ev.old_price is None
    assert email_sender.build_payload([ev]).subject.startswith("המעקב חזר לעבוד")
    assert _run(w, ok("100"), c).type == ChangeType.NO_CHANGE
    assert state.load_state()["watches"]["shop-1"]["baseline_price"] == "100"


def test_results_table_does_not_eat_brackets_in_names(monkeypatch):
    install_env(monkeypatch)
    monkeypatch.setattr(email_sender, "_send_via_smtp", FakeSMTP())
    out = io.StringIO()
    monkeypatch.setattr(scheduler, "_console", Console(file=out, width=200))
    write_watchlist([watch(name="Shoe [red]sale[/red]")])
    script_fetch(monkeypatch, {URL: [fail(FetchStatus.BLOCKED, "Blocked [x]", 403)]})
    assert scheduler.run_check() == 1
    text = out.getvalue()
    assert "Shoe [red]sale[/red]" in text and "Blocked [x]" in text


def test_status_lists_failure_detail_and_hint(monkeypatch):
    install_env(monkeypatch)
    monkeypatch.setattr(email_sender, "_send_via_smtp", FakeSMTP())
    write_watchlist([watch()])
    script_fetch(monkeypatch, {URL: [fail(FetchStatus.NOT_FOUND, "404 Not Found", 404)]})
    scheduler.run_check()
    out = io.StringIO()
    monkeypatch.setattr(scheduler, "_console", Console(file=out, width=300))
    scheduler.status()
    text = out.getvalue()
    assert "FAIL" in text and "HTTP 404" in text and "exit code 1" in text


def test_task_xml_ignores_a_second_instance(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "_venv_python", lambda: str(tmp_path / "python.exe"))
    xml = scheduler.schtasks_xml("Price Watcher daily check", "check", 9, "PT30M")
    root = ET.fromstring(xml.replace('encoding="UTF-16"', ""))
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.find("t:Settings/t:MultipleInstancesPolicy", ns).text == "IgnoreNew"
