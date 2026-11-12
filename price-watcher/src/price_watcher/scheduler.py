"""Scheduler + check runner for price-watcher (issue #7).

Public API
----------
run_check(dry_run=False) -> int    exit code, see EXIT_* below
install_schedule()       -> None
uninstall_schedule()     -> None
status()                 -> None
install_digest_schedule() / uninstall_digest_schedule()
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
from typing import Optional
from xml.sax.saxutils import escape as _xml_escape

import filelock
from rich.console import Console
from rich.markup import escape as _rich_escape
from rich.table import Table

from price_watcher import config as _config_mod
from price_watcher.contracts import (
    NOTIFIABLE_TYPES,
    ChangeEvent,
    ChangeType,
    FetchResult,
    FetchStatus,
    Watch,
)
from price_watcher.logging_setup import get as _get_logger
from price_watcher.paths import data_dir, lock_file, log_dir

_console = Console()
log = _get_logger(__name__)

# Exit codes of `check` (also what the OS scheduler records for the run).
EXIT_OK = 0
EXIT_LINK_FAILED = 1      # at least one link could not be fetched / read
EXIT_EMAIL_FAILED = 3     # a change email could not be sent (changes held back)

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
_DIGEST_HOUR = 9


def _repo_dir() -> Path:
    """Root of the price-watcher sub-project (two levels above this package)."""
    return Path(__file__).resolve().parents[2]


def _venv_python() -> str:
    """The interpreter of the project's own virtual environment."""
    return sys.executable


def last_run_path() -> Path:
    return data_dir() / "last-run.json"


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
    tmp.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _read_last_run() -> Optional[dict]:
    path = last_run_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# run_check
# ---------------------------------------------------------------------------

def run_check(dry_run: bool = False) -> int:
    """Fetch all active watches, detect changes, optionally send email.

    Returns EXIT_OK (0) when every link was read and any change email was
    sent, EXIT_LINK_FAILED (1) when at least one link failed, and
    EXIT_EMAIL_FAILED (3) when a change email could not be sent (the changes
    are then held back and retried on the next run).
    """
    # The check itself needs no SMTP settings; the sender loads them.
    check_cfg = _config_mod.load_check()

    try:
        lock = filelock.FileLock(str(lock_file()), timeout=0)
        lock.acquire()
    except filelock.Timeout:
        _console.print("[yellow]another check is running[/yellow]")
        log.info("Skipped: another check is running")
        return EXIT_OK

    try:
        return _run_check_locked(check_cfg, dry_run)
    finally:
        lock.release()


def _check_one(watch: Watch, check_cfg) -> ChangeEvent:
    """Fetch + evaluate one watch. Never raises: one bad link must not stop the others."""
    from price_watcher import fetcher as _fetcher
    from price_watcher import state as _state

    try:
        fetch_result = _fetcher.fetch(watch.url, watch.selector)
        return _state.evaluate(watch, fetch_result, check_cfg)
    except Exception as exc:  # defensive: fetch() promises not to raise
        log.exception("Checking %s failed", watch.url)
        fetch_result = FetchResult(
            status=FetchStatus.UNREACHABLE,
            final_url=watch.url,
            error_message=f"internal error: {type(exc).__name__}: {exc}",
        )
        return ChangeEvent(
            type=ChangeType.FAILED,
            watch=watch,
            fetch=fetch_result,
            reason=fetch_result.error_message,
        )


def _is_failed(event: ChangeEvent) -> bool:
    return event.fetch.status != FetchStatus.OK or event.type in (
        ChangeType.FAILED, ChangeType.THRESHOLD_FAILURE,
    )


def _run_check_locked(check_cfg, dry_run: bool) -> int:
    from price_watcher import email_sender as _email_sender
    from price_watcher import state as _state
    from price_watcher import watchlist as _watchlist

    started = datetime.now(tz=timezone.utc)
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

    previous = _state.load_state().get("watches", {})

    # Fetch + evaluate, with a small random delay between links.
    events: list[ChangeEvent] = []
    for idx, watch in enumerate(watches):
        if idx > 0:
            time.sleep(random.uniform(check_cfg.delay_min_sec, check_cfg.delay_max_sec))
        events.append(_check_one(watch, check_cfg))

    payload = _email_sender.build_payload(events)

    # Results table
    table = Table(show_header=True, header_style="bold")
    table.add_column("Status", width=6)
    table.add_column("Name")
    table.add_column("Previous")
    table.add_column("Current")
    table.add_column("Action")

    changed_count = 0
    for event in events:
        fetch = event.fetch
        ws = previous.get(event.watch.id, {})
        prev_cell = _price_text(ws.get("last_price"), ws.get("currency"))
        cur_cell = _price_text(fetch.price, fetch.currency or ws.get("currency"))
        if fetch.estimated_final_price is not None and fetch.promo is not None:
            cur_cell += f" (≈{fetch.estimated_final_price} est.)"
        notifies = event.type in NOTIFIABLE_TYPES
        if notifies:
            subject = _email_sender._subject_single(event)
            action = (f"would send: {subject}" if dry_run else f"email: {subject}")
        else:
            action = "would not notify" if dry_run else "no email"

        if _is_failed(event):
            status_cell = "[red]FAIL[/red]"
            cur_cell = fetch.error_message or fetch.status.value
            if not notifies:
                action += f" ({event.reason or 'failed'})"
        elif event.type == ChangeType.FIRST_SEEN:
            status_cell = "[green]OK[/green]"
            action += " — first seen"
        elif event.type == ChangeType.SANITY_DEFERRED:
            status_cell = "[yellow]OK[/yellow]"
            action += f" — deferred: {event.reason}"
        elif event.type == ChangeType.NO_CHANGE:
            status_cell = "[green]OK[/green]"
            if event.reason:
                action += f" — {event.reason}"
        else:
            status_cell = "[green]OK[/green]"
            if event.type in (ChangeType.PRICE_CHANGED, ChangeType.STOCK_CHANGED,
                              ChangeType.PROMO_CHANGED):
                changed_count += 1
        table.add_row(
            status_cell,
            _rich_escape(event.watch.name or event.watch.url),
            _rich_escape(prev_cell),
            _rich_escape(cur_cell),
            _rich_escape(action),
        )

    _console.print(table)

    checked = len(events)
    failed_count = sum(1 for e in events if _is_failed(e))
    any_failed = failed_count > 0

    email_sent: Optional[bool] = None
    if dry_run:
        if payload is not None:
            _console.print(f"[bold]WOULD SEND EMAIL[/bold] — Subject: {_rich_escape(payload.subject)}")
            preview = payload.text_body.splitlines()[:8]
            for line in preview:
                _console.print(f"  [dim]{_rich_escape(line)}[/dim]")
        else:
            _console.print("[dim]Would not send any email.[/dim]")
        _console.print("[dim]Dry run: nothing was sent and the state file was not changed.[/dim]")
    else:
        if payload is not None:
            email_sent = _email_sender.send_change_notification(events)
            if email_sent:
                _state.mark_notified(events)
            else:
                _console.print(
                    "[bold red]The change email could not be sent; the changes are held "
                    "back and will be retried on the next run.[/bold red]"
                )
                log.error(
                    "Change email not sent; %d notifiable change(s) held back for retry",
                    sum(1 for e in events if e.type in NOTIFIABLE_TYPES),
                )
        _state.apply_many(events, email_failed=(email_sent is False))

    email_failed = email_sent is False
    if email_failed:
        rc = EXIT_EMAIL_FAILED
    elif any_failed:
        rc = EXIT_LINK_FAILED
    else:
        rc = EXIT_OK

    summary_line = f"{checked} checked, {changed_count} changed, {failed_count} failed"
    if email_failed:
        summary_line += ", EMAIL NOT SENT"
    _console.print(f"\n[bold]{summary_line}[/bold]" + (" [dim](dry run)[/dim]" if dry_run else ""))
    log.info("Check finished%s: %s (exit %d)", " (dry run)" if dry_run else "", summary_line, rc)

    _write_last_run({
        "timestamp": started.isoformat(),
        "dry_run": dry_run,
        "no_watches": False,
        "checked": checked,
        "changed": changed_count,
        "failed": failed_count,
        "email_sent": email_sent,
        "email_failed": email_failed,
        "exit_code": rc,
        "events": [_event_summary(e) for e in events],
    })
    return rc


def _price_text(price, currency: Optional[str]) -> str:
    if price is None:
        return "—"
    return f"{price} {currency}" if currency else str(price)


def _event_summary(e: ChangeEvent) -> dict:
    out = {
        "watch_id": e.watch.id,
        "name": e.watch.name,
        "url": e.watch.url,
        "type": e.type.value,
        "status": e.fetch.status.value,
        "http_status": e.fetch.http_status,
        "error_message": e.fetch.error_message,
        "old_price": str(e.old_price) if e.old_price is not None else None,
        "new_price": str(e.new_price) if e.new_price is not None else None,
        "reason": e.reason,
    }
    if _is_failed(e):
        from price_watcher.email_sender import describe_failure
        info = describe_failure(e.fetch)
        out["detail"] = info["detail"]
        out["hint"] = info["hint"]
    return out


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
    """Task Scheduler XML for a daily `python -m price_watcher <command>`.

    StartWhenAvailable = run as soon as possible after a missed start
    (computer asleep or off at the scheduled time).
    """
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
    <StartWhenAvailable>true</StartWhenAvailable>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <ExecutionTimeLimit>{time_limit}</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions>
    <Exec>
      <Command>{_xml_escape(_windows_python())}</Command>
      <Arguments>-m price_watcher {_xml_escape(command)}</Arguments>
      <WorkingDirectory>{_xml_escape(str(_repo_dir()))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""


def cron_line(command: str, hour: int) -> str:
    return f"0 {hour} * * * {_venv_python()} -m price_watcher {command}"


# ---------------------------------------------------------------------------
# install / uninstall (generic)
# ---------------------------------------------------------------------------

def _install_launchd_job(plist_path: Path, plist_data: dict, what: str, hour: int) -> None:
    log_dir().mkdir(parents=True, exist_ok=True)

    # Idempotent: unload first if plist already exists
    if plist_path.exists():
        _bootout(plist_path)

    plist_path.parent.mkdir(parents=True, exist_ok=True)
    with plist_path.open("wb") as f:
        plistlib.dump(plist_data, f)
    _console.print(f"Wrote plist to {plist_path}")

    uid = os.getuid()
    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        # Fallback for older macOS
        result2 = subprocess.run(
            ["launchctl", "load", "-w", str(plist_path)],
            capture_output=True, text=True, check=False,
        )
        if result2.returncode != 0:
            _console.print(
                f"[red]launchctl bootstrap failed:[/red] {result.stderr.strip()}\n"
                f"[red]launchctl load -w also failed:[/red] {result2.stderr.strip()}"
            )
            return
    _console.print(f"[green]{what} installed (launchd, daily at {hour:02d}:00).[/green]")


def _bootout(plist_path: Path) -> None:
    """Best-effort unload/bootout of a launchd job."""
    uid = os.getuid()
    result = subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}", str(plist_path)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        subprocess.run(
            ["launchctl", "unload", str(plist_path)],
            capture_output=True, text=True, check=False,
        )


def _uninstall_launchd_job(plist_path: Path, what: str) -> None:
    if not plist_path.exists():
        _console.print(f"[yellow]{what}: plist not found — not installed.[/yellow]")
        return
    _bootout(plist_path)
    plist_path.unlink(missing_ok=True)
    _console.print(f"[green]{what} removed.[/green]")


def _install_schtask(task_name: str, xml_content: str, xml_file: str, what: str, hour: int) -> None:
    xml_path = data_dir() / xml_file
    xml_path.write_text(xml_content, encoding="utf-16")
    result = subprocess.run(
        ["schtasks", "/create", "/xml", str(xml_path), "/tn", task_name, "/f"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        _console.print(f"[red]schtasks /create failed:[/red] {result.stderr.strip()}")
        return
    _console.print(f"[green]{what} installed (Task Scheduler, daily at {hour:02d}:00).[/green]")


def _uninstall_schtask(task_name: str, what: str) -> None:
    result = subprocess.run(
        ["schtasks", "/delete", "/tn", task_name, "/f"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        _console.print(f"[red]schtasks /delete failed:[/red] {result.stderr.strip()}")
        return
    _console.print(f"[green]{what} removed.[/green]")


def _delete_legacy_schtask() -> None:
    """Remove the old hourly task if a previous version installed it."""
    subprocess.run(
        ["schtasks", "/delete", "/tn", _LEGACY_WIN_TASK_NAME, "/f"],
        capture_output=True, text=True, check=False,
    )


# ---------------------------------------------------------------------------
# Price check schedule
# ---------------------------------------------------------------------------

def install_schedule() -> None:
    """Install the daily (09:00) price-check job for the current OS."""
    if sys.platform == "darwin":
        _install_launchd_job(
            _PLIST_PATH,
            launchd_plist(_PLIST_LABEL, "check", _CHECK_HOUR, ""),
            "Schedule", _CHECK_HOUR,
        )
    elif sys.platform.startswith("win"):
        _delete_legacy_schtask()
        _install_schtask(
            _WIN_TASK_NAME,
            schtasks_xml("Price Watcher daily check", "check", _CHECK_HOUR, "PT30M"),
            "task-scheduler.xml", "Schedule", _CHECK_HOUR,
        )
    else:
        _console.print(
            "[yellow]Unsupported platform for automatic scheduling. "
            "Add a cron entry manually:[/yellow]\n"
            f"  {cron_line('check', _CHECK_HOUR)}"
        )


def uninstall_schedule() -> None:
    """Remove the daily price-check job."""
    if sys.platform == "darwin":
        _uninstall_launchd_job(_PLIST_PATH, "Schedule")
    elif sys.platform.startswith("win"):
        _delete_legacy_schtask()
        _uninstall_schtask(_WIN_TASK_NAME, "Schedule")
    else:
        _console.print("[yellow]No automatic schedule to remove on this platform.[/yellow]")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

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


def status() -> None:
    """Print schedule + last-run status."""
    if sys.platform == "darwin":
        installed = _launchd_installed()
    elif sys.platform.startswith("win"):
        installed = _schtasks_installed()
    else:
        installed = None  # unknown

    if installed is True:
        _console.print("[bold]Schedule:[/bold] [green]installed[/green]")
    elif installed is False:
        _console.print("[bold]Schedule:[/bold] [yellow]not installed[/yellow]")
    else:
        _console.print("[bold]Schedule:[/bold] [dim]unknown (unsupported platform)[/dim]")

    path = last_run_path()
    if not path.exists():
        _console.print("[bold]Last run:[/bold] No runs yet")
        return
    data = _read_last_run()
    if data is None:
        _console.print(f"[red]Could not parse {path}[/red]")
        return

    if data.get("dry_run"):
        _console.print(f"[bold]Last run:[/bold] {_run_line(data)} [yellow](dry run — nothing sent or saved)[/yellow]")
        real = data.get("previous_real_run")
        if real:
            _console.print(f"[bold]Last real run:[/bold] {_run_line(real)}")
            data = real
        else:
            data = None
    else:
        _console.print(f"[bold]Last run:[/bold] {_run_line(data)}")

    if not data:
        return
    if data.get("email_failed"):
        _console.print(
            "[bold red]Email:[/bold red] the change email could NOT be sent; the changes "
            "were held back and will be retried. Run `price-watcher test-email`."
        )
    elif data.get("email_sent"):
        _console.print("[bold]Email:[/bold] [green]sent[/green]")
    for ev in data.get("events", []):
        if ev.get("detail"):
            name = ev.get("name") or ev.get("url") or ev.get("watch_id")
            _console.print(f"  [red]FAIL[/red] {_rich_escape(str(name))}: {_rich_escape(ev['detail'])}")
            if ev.get("hint"):
                _console.print(f"       [dim]{_rich_escape(ev['hint'])}[/dim]")


def _launchd_installed() -> bool:
    result = subprocess.run(
        ["launchctl", "list"], capture_output=True, text=True, check=False,
    )
    return _PLIST_LABEL in result.stdout.split()


def _schtasks_installed() -> bool:
    result = subprocess.run(
        ["schtasks", "/query", "/tn", _WIN_TASK_NAME, "/v", "/fo", "list"],
        capture_output=True, text=True, check=False,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Daily digest schedule (9 AM local)
# ---------------------------------------------------------------------------

def install_digest_schedule() -> None:
    """Install the daily sales-digest job at 9 AM local time."""
    if sys.platform == "darwin":
        _install_launchd_job(
            _DIGEST_PLIST_PATH,
            launchd_plist(_DIGEST_PLIST_LABEL, "digest", _DIGEST_HOUR, "digest."),
            "Digest schedule", _DIGEST_HOUR,
        )
    elif sys.platform.startswith("win"):
        _install_schtask(
            _DIGEST_WIN_TASK_NAME,
            schtasks_xml("Price Watcher daily sales digest", "digest", _DIGEST_HOUR, "PT15M"),
            "task-scheduler-digest.xml", "Digest schedule", _DIGEST_HOUR,
        )
    else:
        _console.print(
            "[yellow]Unsupported platform for automatic scheduling. "
            "Add a cron entry manually:[/yellow]\n"
            f"  {cron_line('digest', _DIGEST_HOUR)}"
        )


def uninstall_digest_schedule() -> None:
    """Remove the daily digest job."""
    if sys.platform == "darwin":
        _uninstall_launchd_job(_DIGEST_PLIST_PATH, "Digest schedule")
    elif sys.platform.startswith("win"):
        _uninstall_schtask(_DIGEST_WIN_TASK_NAME, "Digest schedule")
    else:
        _console.print("[yellow]No automatic schedule to remove on this platform.[/yellow]")
