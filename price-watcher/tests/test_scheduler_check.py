"""Issues #7 (check loop / schedule / status), #9 (dry run, exit codes),
#44 (exit code when the email fails), #48 (reason in last-run.json),
#40 (last-run.json for no watches / dry run)."""
from __future__ import annotations

import plistlib
from pathlib import Path
import xml.etree.ElementTree as ET

import filelock
import pytest
from typer.testing import CliRunner

from core_helpers import (
    URL, FakeSMTP, fail, install_env, last_run, ok, script_fetch, state_for, watch,
    write_watchlist,
)
from price_watcher import email_sender, scheduler
from price_watcher.cli import app
from price_watcher.contracts import FetchStatus
from price_watcher.paths import lock_file, state_file

URL2 = "https://other.example/products/hat"


@pytest.fixture
def smtp(monkeypatch):
    install_env(monkeypatch, threshold=3)
    fake = FakeSMTP()
    monkeypatch.setattr(email_sender, "_send_via_smtp", fake)
    return fake


# ---- #7 check loop ---------------------------------------------------------

def test_check_cycle_first_seen_then_change_then_quiet(monkeypatch, smtp):
    write_watchlist([watch()])
    seq = {URL: [ok("100"), ok("100"), ok("90"), ok("90")]}
    script_fetch(monkeypatch, seq)
    assert scheduler.run_check() == 0 and smtp.sent == []
    assert scheduler.run_check() == 0 and smtp.sent == []
    assert scheduler.run_check() == 0 and len(smtp.sent) == 1
    assert smtp.sent[0].subject == "המחיר ירד: Shoe – מ-100 ל-90 ₪ (-10.0%)"
    assert scheduler.run_check() == 0 and len(smtp.sent) == 1


def test_one_failing_link_does_not_stop_the_others(monkeypatch, smtp):
    write_watchlist([watch(), watch(id="other", url=URL2, name="Hat")])

    def fake(url, selector=None, **kw):
        if url == URL:
            raise RuntimeError("parser exploded")
        return ok("50", final_url=URL2)

    script_fetch(monkeypatch, fake)
    assert scheduler.run_check() == 1
    assert state_for("other")["last_price"] == "50"
    lr = last_run()
    assert lr["failed"] == 1 and lr["checked"] == 2


def test_paused_watch_is_skipped(monkeypatch, smtp):
    write_watchlist([watch(paused=True)])
    script_fetch(monkeypatch, {URL: [ok("1")]})
    assert scheduler.run_check() == 0
    assert last_run()["no_watches"] is True


def test_random_delay_between_links(monkeypatch, smtp):
    monkeypatch.setenv("PW_DELAY_MIN_SEC", "2")
    monkeypatch.setenv("PW_DELAY_MAX_SEC", "8")
    sleeps = []
    monkeypatch.setattr(scheduler.time, "sleep", sleeps.append)
    write_watchlist([watch(), watch(id="b", url=URL2), watch(id="c", url=URL2 + "2")])
    script_fetch(monkeypatch, lambda url, selector=None, **k: ok("5", final_url=url))
    scheduler.run_check()
    assert len(sleeps) == 2 and all(2 <= s <= 8 for s in sleeps)


def test_lock_prevents_overlapping_runs(monkeypatch, smtp):
    write_watchlist([watch()])
    called = []
    script_fetch(monkeypatch, lambda *a, **k: called.append(1) or ok("1"))
    held = filelock.FileLock(str(lock_file()))
    with held:
        # filelock is re-entrant within a process for the same object only;
        # use a separate lock object like a second process would.
        other = filelock.FileLock(str(lock_file()), timeout=0)
        with pytest.raises(filelock.Timeout):
            other.acquire()
        monkeypatch.setattr(scheduler.filelock, "FileLock", lambda *a, **k: other)
        assert scheduler.run_check() == 0
    assert called == []


# ---- #44 email failure exit code ------------------------------------------

def test_email_failure_exits_non_zero_and_change_is_retried(monkeypatch, smtp):
    write_watchlist([watch()])
    script_fetch(monkeypatch, {URL: [ok("90"), ok("80"), ok("80")]})
    assert scheduler.run_check() == 0                    # first seen
    smtp.fail = True
    rc = scheduler.run_check()
    assert rc == scheduler.EXIT_EMAIL_FAILED != 0
    lr = last_run()
    assert lr["email_failed"] is True and lr["email_sent"] is False and lr["exit_code"] == rc
    assert state_for()["last_price"] == "90"             # change held back
    smtp.fail = False
    assert scheduler.run_check() == 0
    assert [m.subject for m in smtp.sent] == ["המחיר ירד: Shoe – מ-90 ל-80 ₪ (-11.1%)"]
    assert last_run()["email_failed"] is False
    assert state_for()["last_price"] == "80"


def test_missing_smtp_config_does_not_crash_check(monkeypatch):
    install_env(monkeypatch)
    monkeypatch.delenv("PW_SMTP_PASSWORD")
    write_watchlist([watch()])
    script_fetch(monkeypatch, {URL: [ok("90"), ok("80")]})
    assert scheduler.run_check() == 0
    assert scheduler.run_check() == scheduler.EXIT_EMAIL_FAILED


def test_status_shows_email_failure(monkeypatch, smtp, capsys):
    write_watchlist([watch()])
    script_fetch(monkeypatch, {URL: [ok("90"), ok("80")]})
    scheduler.run_check()
    smtp.fail = True
    scheduler.run_check()
    capsys.readouterr()
    scheduler.status()
    out = capsys.readouterr().out
    assert "could NOT be sent" in out and "exit code 3" in out


# ---- #8 / #48 threshold alert through the real loop ----------------------

def test_threshold_alert_once_and_reason_in_last_run(monkeypatch, smtp):
    write_watchlist([watch()])
    missing = fail(FetchStatus.NOT_FOUND, "Shopify variant 1 not found in product")
    script_fetch(monkeypatch, {URL: [ok("90")] + [missing] * 5 + [ok("90")]})
    rcs = [scheduler.run_check() for _ in range(7)]
    assert rcs == [0, 1, 1, 1, 1, 1, 0]
    subjects = [m.subject for m in smtp.sent]
    assert subjects == [
        "המעקב הפסיק לעבוד: Shoe – הדף לא נמצא (ייתכן שהמוצר הוסר)",
        "המעקב חזר לעבוד: Shoe",
    ]
    assert "Shopify variant 1 not found in product" in smtp.sent[0].text_body


def test_last_run_records_failure_detail(monkeypatch, smtp):
    write_watchlist([watch()])
    script_fetch(monkeypatch, {URL: [fail(FetchStatus.BLOCKED, "Blocked (HTTP 403)", 403)]})
    assert scheduler.run_check() == 1
    [ev] = last_run()["events"]
    assert ev["status"] == "blocked" and ev["http_status"] == 403
    assert ev["error_message"] == "Blocked (HTTP 403)"
    assert "HTTP 403" in ev["detail"] and ev["hint"]


# ---- #9 dry run ------------------------------------------------------------

def test_dry_run_writes_no_state_sends_nothing(monkeypatch, smtp, capsys):
    write_watchlist([watch()])
    script_fetch(monkeypatch, {URL: [ok("100")]})
    scheduler.run_check()                                # real run: first seen
    before = state_file().read_bytes()
    script_fetch(monkeypatch, {URL: [ok("80")]})
    capsys.readouterr()
    assert scheduler.run_check(dry_run=True) == 0
    out = capsys.readouterr().out
    assert smtp.sent == []
    assert state_file().read_bytes() == before
    assert "would send: המחיר ירד: Shoe" in out
    assert "100 ILS" in out and "80 ILS" in out          # previous and current
    assert "WOULD SEND EMAIL" in out
    lr = last_run()
    assert lr["dry_run"] is True and lr["previous_real_run"]["dry_run"] is False


def test_dry_run_unchanged_says_would_not_notify(monkeypatch, smtp, capsys):
    write_watchlist([watch()])
    script_fetch(monkeypatch, {URL: [ok("100")]})
    scheduler.run_check()
    capsys.readouterr()
    scheduler.run_check(dry_run=True)
    assert "would not notify" in capsys.readouterr().out


def test_dry_run_needs_no_smtp_settings(monkeypatch):
    for k in ("PW_SMTP_USER", "PW_SMTP_PASSWORD", "PW_EMAIL_FROM", "PW_EMAIL_TO"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PW_DELAY_MAX_SEC", "0")
    monkeypatch.setenv("PW_DELAY_MIN_SEC", "0")
    write_watchlist([watch()])
    script_fetch(monkeypatch, {URL: [ok("100")]})
    assert scheduler.run_check(dry_run=True) == 0
    assert not state_file().exists()


def test_dry_run_exit_code_on_failure(monkeypatch, smtp):
    write_watchlist([watch()])
    script_fetch(monkeypatch, {URL: [fail()]})
    assert scheduler.run_check(dry_run=True) == 1


def test_no_watches_writes_last_run(smtp, capsys):
    assert scheduler.run_check() == 0
    lr = last_run()
    assert lr["no_watches"] is True and lr["checked"] == 0
    capsys.readouterr()
    scheduler.status()
    assert "no active watches" in capsys.readouterr().out


def test_status_after_dry_run_shows_both(monkeypatch, smtp, capsys):
    write_watchlist([watch()])
    script_fetch(monkeypatch, {URL: [ok("100")]})
    scheduler.run_check()
    scheduler.run_check(dry_run=True)
    capsys.readouterr()
    scheduler.status()
    out = capsys.readouterr().out
    assert "dry run" in out and "Last real run" in out


# ---- CLI exit codes ----------------------------------------------------------

def test_cli_check_propagates_exit_code(monkeypatch, smtp):
    write_watchlist([watch()])
    script_fetch(monkeypatch, {URL: [fail()]})
    res = CliRunner().invoke(app, ["check"])
    assert res.exit_code == 1
    script_fetch(monkeypatch, {URL: [ok("5")]})
    assert CliRunner().invoke(app, ["check"]).exit_code == 0


def test_cli_test_email_failure_exit_code(monkeypatch, smtp):
    smtp.fail = True
    res = CliRunner().invoke(app, ["test-email"])
    assert res.exit_code == 1 and "Test email failed" in res.output


def test_cli_digest_propagates_exit_code(monkeypatch):
    from price_watcher import digest
    monkeypatch.setattr(digest, "run_digest", lambda dry_run=False: 1)
    assert CliRunner().invoke(app, ["digest"]).exit_code == 1


# ---- schedule definitions (offline) -------------------------------------

def test_launchd_plist(monkeypatch):
    data = scheduler.launchd_plist(scheduler._PLIST_LABEL, "check", 9, "")
    blob = plistlib.dumps(data)
    back = plistlib.loads(blob)
    assert back["ProgramArguments"][1:] == ["-m", "price_watcher", "check"]
    assert back["ProgramArguments"][0] == scheduler._venv_python()
    assert back["StartCalendarInterval"] == {"Hour": 9, "Minute": 0}
    assert back["WorkingDirectory"] == str(scheduler._repo_dir())
    assert back["StandardOutPath"].endswith("launchd.out.log")
    digest = scheduler.launchd_plist(scheduler._DIGEST_PLIST_LABEL, "digest", 9, "digest.")
    assert digest["StandardErrorPath"].endswith("launchd.digest.err.log")


def test_schtasks_xml(monkeypatch, tmp_path):
    venv = tmp_path / "A&B venv" / "Scripts"
    venv.mkdir(parents=True)
    (venv / "python.exe").write_text("")
    (venv / "pythonw.exe").write_text("")
    monkeypatch.setattr(scheduler, "_venv_python", lambda: str(venv / "python.exe"))
    xml = scheduler.schtasks_xml("Price Watcher daily check", "check", 9, "PT30M")
    root = ET.fromstring(xml.replace('encoding="UTF-16"', ""))  # parses -> valid XML
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.find("t:Settings/t:StartWhenAvailable", ns).text == "true"
    assert root.find("t:Triggers/t:CalendarTrigger/t:StartBoundary", ns).text.endswith("T09:00:00")
    assert root.find("t:Triggers/t:CalendarTrigger/t:ScheduleByDay/t:DaysInterval", ns).text == "1"
    cmd = root.find("t:Actions/t:Exec/t:Command", ns).text
    assert cmd.endswith("pythonw.exe") and "A&B venv" in cmd   # no console window, escaped
    assert root.find("t:Actions/t:Exec/t:Arguments", ns).text == "-m price_watcher check"


def test_install_schedule_on_windows_calls_schtasks(monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler.sys, "platform", "win32")
    monkeypatch.setattr(scheduler.subprocess, "run",
                        lambda args, **kw: calls.append(args) or type("R", (), {"returncode": 0, "stderr": ""})())
    scheduler.install_schedule()
    assert ["schtasks", "/delete", "/tn", "PriceWatcherHourly", "/f"] in calls
    create = [c for c in calls if c[1] == "/create"][0]
    assert create[-3:] == ["/tn", "PriceWatcherDailyCheck", "/f"]
    xml_file = create[3]
    assert Path(xml_file).read_text(encoding="utf-16").startswith("<?xml")


def test_install_schedule_on_mac_writes_plist(monkeypatch, tmp_path):
    calls = []
    plist = tmp_path / "LaunchAgents" / "x.plist"
    monkeypatch.setattr(scheduler.sys, "platform", "darwin")
    monkeypatch.setattr(scheduler, "_PLIST_PATH", plist)
    monkeypatch.setattr(scheduler.subprocess, "run",
                        lambda args, **kw: calls.append(args) or type("R", (), {"returncode": 0, "stderr": ""})())
    scheduler.install_schedule()
    assert plistlib.loads(plist.read_bytes())["ProgramArguments"][-1] == "check"
    assert calls[0][:2] == ["launchctl", "bootstrap"]
    scheduler.uninstall_schedule()
    assert not plist.exists()


def test_cron_line_for_other_platforms():
    assert scheduler.cron_line("check", 9).startswith("0 9 * * * ")
    assert scheduler.cron_line("check", 9).endswith("-m price_watcher check")
