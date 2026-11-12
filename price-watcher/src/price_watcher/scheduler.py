"""Scheduler + check runner for price-watcher (issue #7).

Public API
----------
run_check(dry_run=False) -> int    exit code, see EXIT_* below
install_schedule()       -> None
uninstall_schedule()     -> None
status()                 -> None
install_digest_schedule() / uninstall_digest_schedule()
launchd_plist() / schtasks_xml() / cron_line()   schedule definitions (pure)
"""

from __future__ import annotations

import json
import os
import plistlib
import random
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape
from typing import Optional

import filelock
from rich.console import Console
from rich.markup import escape as _rich_escape
from rich.table import Table

from price_watcher import config as _config_mod
from price_watcher.contracts import NOTIFIABLE_TYPES, ChangeEvent, ChangeType, FetchStatus
from price_watcher.paths import data_dir, lock_file, log_dir

_console = Console()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PLIST_LABEL = "com.noada.price-watcher"
_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{_PLIST_LABEL}.plist"
_WIN_TASK_NAME = "PriceWatcherDailyCheck"
# Name used while the check ran hourly; removed on install/uninstall.
_LEGACY_WIN_TASK_NAME = "PriceWatcherHourly"
# Local time of the daily price check (same as the daily digest).
_CHECK_HOUR = 9

_DIGEST_PLIST_LABEL = "com.noada.price-watcher.digest"
_DIGEST_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{_DIGEST_PLIST_LABEL}.plist"
_DIGEST_WIN_TASK_NAME = "PriceWatcherDailyDigest"


def _repo_dir() -> Path:
    """Root of the price-watcher sub-project (two levels above this package)."""
    return Path(__file__).resolve().parents[2]


def _venv_python() -> str:
    return sys.executable


def last_run_path() -> Path:
    return data_dir() / "last-run.json"


def _read_last_run() -> Optional[dict]:
    path = last_run_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_last_run(summary: dict) -> None:
    """Write last-run.json atomically.

    A dry run keeps the previous real run under ``previous_real_run`` so
    ``status`` can still say when the scheduled check last ran.
    """
    path = last_run_path()
    if summary.get("dry_run"):
        prev = _read_last_run()
        if prev is not None:
            real = prev if not prev.get("dry_run") else prev.get("previous_real_run")
            if real:
                summary["previous_real_run"] = real
    tmp = path.with_suffix(".json.tmp")
    # ASCII-only JSON: readable whatever encoding a reader assumes.
    tmp.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# run_check
# ---------------------------------------------------------------------------

# Exit codes of `check` (also what the OS scheduler records for the run).
EXIT_OK = 0
EXIT_LINK_FAILED = 1      # at least one link could not be fetched / read
EXIT_EMAIL_FAILED = 3     # a change email could not be sent (changes held back)


def run_check(dry_run: bool = False) -> int:  # noqa: C901  (complexity OK here)
    """Fetch all active watches, detect changes, optionally send email.

    Returns 0 if every fetch succeeded, 1 if at least one failed, and 3 if
    there was a change email to send and sending it failed (takes precedence,
    so a broken SMTP setup is visible to the OS scheduler).

    The check itself needs no SMTP settings: a missing / broken email setup
    makes the change email fail (exit 3, changes held back) instead of
    stopping prices from being checked. The `check` CLI command still
    refuses a real run without SMTP settings (one-line config error).
    """
    # 1. Load config
    cfg = _config_mod.load(require_email=False)

    # 2. Acquire process lock
    try:
        _lock = filelock.FileLock(str(lock_file()), timeout=0)
        _lock.acquire()
    except filelock.Timeout:
        _console.print("[yellow]another check is running[/yellow]")
        return 0

    try:
        return _run_check_locked(cfg, dry_run)
    finally:
        _lock.release()


def _run_check_locked(cfg, dry_run: bool) -> int:
    # Lazy imports
    from price_watcher import watchlist as _watchlist
    from price_watcher import fetcher as _fetcher
    from price_watcher import state as _state
    from price_watcher import email_sender as _email_sender
    from price_watcher.contracts import FetchResult
    from price_watcher.logging_setup import get as _get_logger

    _log = _get_logger(__name__)

    started = datetime.now(tz=timezone.utc)

    # 3. Load & filter watches
    watches = [w for w in _watchlist.load_all() if not w.paused]

    if not watches:
        _console.print("[dim]No active watches.[/dim]")
        _write_last_run({
            "timestamp": started.isoformat(),
            "dry_run": dry_run,
            "no_watches": True,
            "checked": 0, "changed": 0, "failed": 0,
            "email_sent": None, "email_failed": False,
            "exit_code": EXIT_OK,
            "events": [],
        })
        return EXIT_OK

    # 4. Fetch + evaluate
    events: list[ChangeEvent] = []
    for idx, watch in enumerate(watches):
        if idx > 0:
            delay = random.uniform(cfg.check.delay_min_sec, cfg.check.delay_max_sec)
            time.sleep(delay)

        # One misbehaving watch must not abort the whole run: an exception is
        # treated as a failed fetch for that watch only.
        try:
            fetch_result = _fetcher.fetch(watch.url, watch.selector)
        except Exception as exc:  # noqa: BLE001
            _log.exception("fetch raised for %s", watch.url)
            fetch_result = FetchResult(
                status=FetchStatus.UNREACHABLE,
                final_url=watch.url,
                error_message=f"fetch crashed: {type(exc).__name__}: {exc}",
            )
        try:
            event = _state.evaluate(watch, fetch_result, cfg.check)
        except Exception as exc:  # noqa: BLE001
            _log.exception("evaluate raised for %s", watch.url)
            event = ChangeEvent(
                type=ChangeType.FAILED,
                watch=watch,
                fetch=FetchResult(
                    status=FetchStatus.UNREACHABLE,
                    final_url=watch.url,
                    error_message=f"evaluate crashed: {type(exc).__name__}: {exc}",
                ),
                reason="evaluate crashed",
            )
        events.append(event)

    # 5. Build results table
    table = Table(show_header=True, header_style="bold")
    table.add_column("Status", width=4)
    table.add_column("Name")
    table.add_column("Price / Info")
    table.add_column("Action")

    any_failed = False
    changed_count = 0

    for event in events:
        watch_name = event.watch.name or event.watch.url
        fetch = event.fetch
        ok = fetch.status == FetchStatus.OK
        failed = not ok or event.type == ChangeType.FAILED
        send_word = "would send" if dry_run else "sending"
        # A dry run says what it would email (the subject) or that it would not.
        subject = (
            f": {_email_sender._subject_single(event)}"
            if dry_run and event.type in NOTIFIABLE_TYPES else ""
        )

        def _price_cell() -> str:
            if fetch.price is not None:
                cur = fetch.currency or ""
                return f"{fetch.price}{(' ' + cur) if cur else ''}"
            return "—"

        def _change_cell() -> str:
            parts = []
            if event.old_price is not None or event.new_price is not None:
                old_str = str(event.old_price) if event.old_price is not None else "—"
                new_str = str(event.new_price) if event.new_price is not None else "—"
                parts.append(f"{old_str} → {new_str}")
            else:
                parts.append(_price_cell())
            if event.stock_changed:
                parts.append("in stock" if fetch.in_stock else "out of stock")
            if event.promo_changed:
                parts.append("promo changed" if fetch.promo else "promo ended")
            return " — ".join(parts)

        if event.type == ChangeType.THRESHOLD_FAILURE:
            any_failed = True
            status_cell = "[red]FAIL[/red]"
            info_cell = event.reason or fetch.error_message or fetch.status.value
            action_cell = f"{send_word} failure alert{subject}"
        elif event.type == ChangeType.RECOVERED:
            if event.old_price is not None or event.stock_changed or event.promo_changed:
                changed_count += 1
            status_cell = "[green]OK[/green]"
            info_cell = _change_cell()
            action_cell = f"recovered ({send_word}){subject}"
        elif failed:
            any_failed = True
            reason = fetch.error_message or fetch.status.value
            status_cell = "[red]FAIL[/red]"
            info_cell = reason
            action_cell = "no email"
        elif event.type == ChangeType.FIRST_SEEN:
            status_cell = "[green]OK[/green]"
            info_cell = _price_cell()
            action_cell = "first seen (no email)"
        elif event.type == ChangeType.NO_CHANGE:
            status_cell = "[green]OK[/green]"
            info_cell = _price_cell()
            action_cell = "no change"
        elif event.type == ChangeType.SANITY_DEFERRED:
            status_cell = "[yellow]OK[/yellow]"
            info_cell = f"{_price_cell()} — {event.reason or 'deferred'}"
            action_cell = "deferred (no email)"
        else:
            changed_count += 1
            status_cell = "[green]OK[/green]"
            info_cell = _change_cell()
            action_cell = f"{send_word}{subject}"

        if dry_run and event.type not in NOTIFIABLE_TYPES:
            action_cell = f"would not notify ({action_cell})"

        # Names, URLs and error texts may contain [brackets]: not markup.
        table.add_row(
            status_cell,
            _rich_escape(watch_name),
            _rich_escape(info_cell),
            _rich_escape(action_cell),
        )

    _console.print(table)

    # 6. Build email payload
    payload = _email_sender.build_payload(events)

    sent = True
    email_sent: Optional[bool] = None
    if dry_run:
        if payload is not None:
            _console.print(
                f"[bold]WOULD SEND EMAIL[/bold] — Subject: {_rich_escape(payload.subject)}"
            )
            for line in payload.text_body.splitlines()[:8]:
                _console.print(f"  [dim]{_rich_escape(line)}[/dim]")
        else:
            _console.print("[dim]Would not send any email.[/dim]")
        _console.print("[dim]Dry run: nothing was sent and the state file was not changed.[/dim]")
    else:
        # Real run: send + persist
        if payload is not None:
            sent = _email_sender.send_change_notification(events)
            email_sent = sent
            if sent:
                _state.mark_notified(events)
            else:
                _console.print(
                    "[yellow]Email failed — changes not recorded; they will be "
                    "retried on the next run.[/yellow]"
                )

        # If the email failed, notifiable changes are not written to state so the
        # next run detects and sends them again.
        _state.apply_many(events, notification_sent=sent)
    email_failed = email_sent is False

    # Summary line
    checked = len(events)
    failed_count = sum(
        1 for e in events
        if e.fetch.status != FetchStatus.OK
        or e.type in (ChangeType.FAILED, ChangeType.THRESHOLD_FAILURE)
    )
    if email_failed:
        rc = EXIT_EMAIL_FAILED
    elif any_failed:
        rc = EXIT_LINK_FAILED
    else:
        rc = EXIT_OK
    summary_line = f"{checked} checked, {changed_count} changed, {failed_count} failed"
    if email_failed:
        summary_line += ", EMAIL NOT SENT"
    _console.print(
        f"\n[bold]{summary_line}[/bold]" + (" [dim](dry run)[/dim]" if dry_run else "")
    )
    _log.info("Check finished%s: %s (exit %d)", " (dry run)" if dry_run else "", summary_line, rc)

    # Write last-run.json (a dry run keeps the last real run inside it)
    brief_events = []
    for e in events:
        info = _email_sender.describe_failure(e.fetch)
        brief_events.append({
            "watch_id": e.watch.id,
            "name": e.watch.name,
            "url": e.watch.url,
            "type": e.type.value,
            "old_price": str(e.old_price) if e.old_price is not None else None,
            "new_price": str(e.new_price) if e.new_price is not None else None,
            "reason": e.reason,
            "status": e.fetch.status.value,
            "fetch_status": e.fetch.status.value,
            "http_status": e.fetch.http_status,
            "error_message": e.fetch.error_message,
            "detail": info["detail"],
            "hint": info["hint"],
        })

    _write_last_run({
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "dry_run": dry_run,
        "no_watches": False,
        "checked": checked,
        "changed": changed_count,
        "failed": failed_count,
        "email_sent": email_sent,
        "email_failed": email_failed,
        "exit_code": rc,
        "events": brief_events,
    })

    if email_failed:
        _log.error("change email could not be sent; changes will be retried next run")
    return rc


# ---------------------------------------------------------------------------
# install_schedule
# ---------------------------------------------------------------------------

def install_schedule() -> None:
    """Install the daily (09:00) price-check job for the current OS."""
    if sys.platform == "darwin":
        _install_launchd()
    elif sys.platform.startswith("win"):
        _install_schtasks()
    else:
        _console.print(
            "[yellow]Unsupported platform for automatic scheduling. "
            "Add a cron entry manually:[/yellow]\n"
            f"  {cron_line('check', _CHECK_HOUR)}"
        )


# ---------------------------------------------------------------------------
# Schedule definitions (pure; tested offline)
# ---------------------------------------------------------------------------

def launchd_plist(label: str, command: str, hour: int, log_name: str) -> dict:
    """The launchd agent definition for `python -m price_watcher <command>`.

    launchd runs a StartCalendarInterval job that was missed while the Mac
    slept as soon as it wakes, so no extra catch-up setting is needed.
    """
    return {
        "Label": label,
        "ProgramArguments": [_venv_python(), "-m", "price_watcher", command],
        "WorkingDirectory": str(_repo_dir()),
        "StartCalendarInterval": {"Hour": hour, "Minute": 0},
        "RunAtLoad": False,
        "StandardOutPath": str(log_dir() / f"launchd.{log_name}out.log"),
        "StandardErrorPath": str(log_dir() / f"launchd.{log_name}err.log"),
    }


def _windows_python() -> str:
    """pythonw.exe next to the venv's python.exe, so no console window pops up."""
    python_path = _venv_python()
    pythonw = Path(python_path).parent / "pythonw.exe"
    if pythonw.exists():
        return str(pythonw)
    return python_path


def schtasks_xml(description: str, command: str, hour: int, time_limit: str) -> str:
    """Task Scheduler XML for a daily `python -m price_watcher <command>`."""
    return _task_xml(description, hour, time_limit, _windows_python(), command)


def cron_line(command: str, hour: int) -> str:
    return f"0 {hour} * * * {_venv_python()} -m price_watcher {command}"


def _install_launchd() -> None:
    log_dir().mkdir(parents=True, exist_ok=True)

    plist_data = launchd_plist(_PLIST_LABEL, "check", _CHECK_HOUR, "")

    # Idempotent: unload first if plist already exists
    if _PLIST_PATH.exists():
        _bootout_launchd()

    _PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _PLIST_PATH.open("wb") as f:
        plistlib.dump(plist_data, f)

    _console.print(f"Wrote plist to {_PLIST_PATH}")

    uid = os.getuid()
    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(_PLIST_PATH)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        # Fallback for older macOS
        result2 = subprocess.run(
            ["launchctl", "load", "-w", str(_PLIST_PATH)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result2.returncode != 0:
            _console.print(
                f"[red]launchctl bootstrap failed:[/red] {result.stderr.strip()}\n"
                f"[red]launchctl load -w also failed:[/red] {result2.stderr.strip()}"
            )
            return

    _console.print(f"[green]Schedule installed (launchd, daily at {_CHECK_HOUR:02d}:00).[/green]")


def _task_xml(description: str, hour: int, limit: str, python_path: str, subcommand: str) -> str:
    """Task Scheduler XML. Values are XML-escaped; battery settings allow
    running on battery (laptops) and not being killed when unplugged.

    StartWhenAvailable = run as soon as possible after a missed start
    (computer asleep or off at the scheduled time); a second start while one
    is still running is ignored."""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{_xml_escape(description)}</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2024-01-01T{hour:02d}:00:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <ExecutionTimeLimit>{limit}</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions>
    <Exec>
      <Command>{_xml_escape(python_path)}</Command>
      <Arguments>-m price_watcher {_xml_escape(subcommand)}</Arguments>
      <WorkingDirectory>{_xml_escape(str(_repo_dir()))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""


def _install_schtasks() -> None:
    python_path = _venv_python()
    # Prefer pythonw.exe to suppress console window
    pythonw = Path(python_path).parent / "pythonw.exe"
    if pythonw.exists():
        python_path = str(pythonw)

    xml_content = _task_xml("Price Watcher daily check", _CHECK_HOUR, "PT30M", python_path, "check")

    xml_path = data_dir() / "task-scheduler.xml"
    xml_path.write_text(xml_content, encoding="utf-16")

    _delete_legacy_schtask()

    result = subprocess.run(
        ["schtasks", "/create", "/xml", str(xml_path),
         "/tn", _WIN_TASK_NAME, "/f"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        _console.print(f"[red]schtasks /create failed:[/red] {result.stderr.strip()}")
        return

    _console.print(f"[green]Schedule installed (Task Scheduler, daily at {_CHECK_HOUR:02d}:00).[/green]")


# ---------------------------------------------------------------------------
# uninstall_schedule
# ---------------------------------------------------------------------------

def uninstall_schedule() -> None:
    """Remove the daily price-check job."""
    if sys.platform == "darwin":
        _uninstall_launchd()
    elif sys.platform.startswith("win"):
        _uninstall_schtasks()
    else:
        _console.print("[yellow]No automatic schedule to remove on this platform.[/yellow]")


def _bootout_launchd() -> None:
    """Best-effort unload/bootout of the launchd job."""
    uid = os.getuid()
    result = subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}", str(_PLIST_PATH)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        subprocess.run(
            ["launchctl", "unload", str(_PLIST_PATH)],
            capture_output=True,
            text=True,
            check=False,
        )


def _uninstall_launchd() -> None:
    if not _PLIST_PATH.exists():
        _console.print("[yellow]Plist not found — schedule may not be installed.[/yellow]")
        return

    _bootout_launchd()

    _PLIST_PATH.unlink(missing_ok=True)
    _console.print("[green]Schedule removed.[/green]")


def _delete_legacy_schtask() -> None:
    """Remove the old hourly task if a previous version installed it."""
    subprocess.run(
        ["schtasks", "/delete", "/tn", _LEGACY_WIN_TASK_NAME, "/f"],
        capture_output=True,
        text=True,
        check=False,
    )


def _uninstall_schtasks() -> None:
    _delete_legacy_schtask()
    result = subprocess.run(
        ["schtasks", "/delete", "/tn", _WIN_TASK_NAME, "/f"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        _console.print(f"[red]schtasks /delete failed:[/red] {result.stderr.strip()}")
        return
    _console.print("[green]Schedule removed.[/green]")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def status() -> None:
    """Print schedule + last-run status."""
    # --- schedule installed? ---
    if sys.platform == "darwin":
        installed = _launchd_installed()
        digest_installed = _launchd_installed(_DIGEST_PLIST_LABEL)
    elif sys.platform.startswith("win"):
        installed = _schtasks_installed()
        digest_installed = _schtasks_installed(_DIGEST_WIN_TASK_NAME)
    else:
        installed = None  # unknown
        digest_installed = None

    if installed is True:
        _console.print("[bold]Schedule:[/bold] [green]installed[/green]")
    elif installed is False:
        _console.print("[bold]Schedule:[/bold] [yellow]not installed[/yellow]")
    else:
        _console.print("[bold]Schedule:[/bold] [dim]unknown (unsupported platform)[/dim]")

    if digest_installed is True:
        _console.print("[bold]Digest schedule:[/bold] [green]installed[/green]")
    elif digest_installed is False:
        _console.print("[bold]Digest schedule:[/bold] [yellow]not installed[/yellow]")
    else:
        _console.print("[bold]Digest schedule:[/bold] [dim]unknown (unsupported platform)[/dim]")

    # --- last run ---
    path = last_run_path()
    if not path.exists():
        _console.print("[bold]Last run:[/bold] No runs yet")
        return
    data = _read_last_run()
    if data is None:
        _console.print(f"[red]Could not parse {path}[/red]")
        return

    if data.get("dry_run"):
        _console.print(
            f"[bold]Last run:[/bold] {_run_line(data)} "
            "[yellow](dry run — nothing sent or saved)[/yellow]"
        )
        real = data.get("previous_real_run")
        if real:
            _console.print(f"[bold]Last real run:[/bold] {_run_line(real)}")
        data = real
    else:
        _console.print(f"[bold]Last run:[/bold] {_run_line(data)}")

    if not data:
        return
    if data.get("email_failed"):
        _console.print(
            "[bold red]Email failed:[/bold red] the last run's change email could "
            "NOT be sent; changes are held and will be retried. "
            "Check the SMTP settings (run `test-email`)."
        )
    elif data.get("email_sent"):
        _console.print("[bold]Email:[/bold] [green]sent[/green]")
    for ev in data.get("events", []):
        if ev.get("detail") and ev.get("type") in ("failed", "threshold_failure"):
            name = ev.get("name") or ev.get("url") or ev.get("watch_id")
            _console.print(f"  [red]FAIL[/red] {_rich_escape(str(name))}: {_rich_escape(ev['detail'])}")
            if ev.get("hint"):
                _console.print(f"       [dim]{_rich_escape(ev['hint'])}[/dim]")


def _format_ts(ts_raw: str) -> str:
    try:
        return datetime.fromisoformat(ts_raw).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    except (ValueError, TypeError):
        return str(ts_raw)


def _run_line(data: dict) -> str:
    if data.get("no_watches"):
        what = "no active watches"
    else:
        what = (
            f"{data.get('checked', '?')} checked, {data.get('changed', '?')} changed, "
            f"{data.get('failed', '?')} failed"
        )
    if "exit_code" in data:
        what += f", exit code {data['exit_code']}"
    return f"{_format_ts(data.get('timestamp', ''))} — {what}"


def _launchd_installed(label: str = _PLIST_LABEL) -> bool:
    result = subprocess.run(
        ["launchctl", "list"],
        capture_output=True,
        text=True,
        check=False,
    )
    # Exact match on the label column (the digest label has the check label as a prefix).
    return any(
        line.split()[-1] == label for line in result.stdout.splitlines() if line.split()
    )


def _schtasks_installed(task_name: str = _WIN_TASK_NAME) -> bool:
    result = subprocess.run(
        ["schtasks", "/query", "/tn", task_name, "/v", "/fo", "list"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Daily digest schedule (9 AM local)
# ---------------------------------------------------------------------------

def install_digest_schedule() -> None:
    """Install the daily sales-digest job at 9 AM local time."""
    if sys.platform == "darwin":
        _install_digest_launchd()
    elif sys.platform.startswith("win"):
        _install_digest_schtasks()
    else:
        _console.print(
            "[yellow]Unsupported platform for automatic scheduling. "
            "Add a cron entry manually:[/yellow]\n"
            f"  {cron_line('digest', 9)}"
        )


def uninstall_digest_schedule() -> None:
    """Remove the daily digest job."""
    if sys.platform == "darwin":
        _uninstall_digest_launchd()
    elif sys.platform.startswith("win"):
        _uninstall_digest_schtasks()
    else:
        _console.print("[yellow]No automatic schedule to remove on this platform.[/yellow]")


def _install_digest_launchd() -> None:
    log_dir().mkdir(parents=True, exist_ok=True)

    plist_data = launchd_plist(_DIGEST_PLIST_LABEL, "digest", 9, "digest.")

    if _DIGEST_PLIST_PATH.exists():
        _bootout_digest_launchd()

    _DIGEST_PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _DIGEST_PLIST_PATH.open("wb") as f:
        plistlib.dump(plist_data, f)

    _console.print(f"Wrote plist to {_DIGEST_PLIST_PATH}")

    uid = os.getuid()
    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(_DIGEST_PLIST_PATH)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        result2 = subprocess.run(
            ["launchctl", "load", "-w", str(_DIGEST_PLIST_PATH)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result2.returncode != 0:
            _console.print(
                f"[red]launchctl bootstrap failed:[/red] {result.stderr.strip()}\n"
                f"[red]launchctl load -w also failed:[/red] {result2.stderr.strip()}"
            )
            return

    _console.print("[green]Digest schedule installed (launchd, daily at 09:00).[/green]")


def _install_digest_schtasks() -> None:
    python_path = _venv_python()
    pythonw = Path(python_path).parent / "pythonw.exe"
    if pythonw.exists():
        python_path = str(pythonw)

    xml_content = _task_xml("Price Watcher daily sales digest", 9, "PT15M", python_path, "digest")

    xml_path = data_dir() / "task-scheduler-digest.xml"
    xml_path.write_text(xml_content, encoding="utf-16")

    result = subprocess.run(
        ["schtasks", "/create", "/xml", str(xml_path),
         "/tn", _DIGEST_WIN_TASK_NAME, "/f"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        _console.print(f"[red]schtasks /create failed:[/red] {result.stderr.strip()}")
        return

    _console.print("[green]Digest schedule installed (Task Scheduler, daily at 09:00).[/green]")


def _bootout_digest_launchd() -> None:
    uid = os.getuid()
    result = subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}", str(_DIGEST_PLIST_PATH)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        subprocess.run(
            ["launchctl", "unload", str(_DIGEST_PLIST_PATH)],
            capture_output=True, text=True, check=False,
        )


def _uninstall_digest_launchd() -> None:
    if not _DIGEST_PLIST_PATH.exists():
        _console.print("[yellow]Digest plist not found — not installed.[/yellow]")
        return
    _bootout_digest_launchd()
    _DIGEST_PLIST_PATH.unlink(missing_ok=True)
    _console.print("[green]Digest schedule removed.[/green]")


def _uninstall_digest_schtasks() -> None:
    result = subprocess.run(
        ["schtasks", "/delete", "/tn", _DIGEST_WIN_TASK_NAME, "/f"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        _console.print(f"[red]schtasks /delete failed:[/red] {result.stderr.strip()}")
        return
    _console.print("[green]Digest schedule removed.[/green]")
