"""Scheduler + check runner for price-watcher (issue #7).

Public API
----------
run_check(dry_run=False) -> int
install_schedule()       -> None
uninstall_schedule()     -> None
status()                 -> None
"""

from __future__ import annotations

import json
import os
import plistlib
import random
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import filelock
from rich.console import Console
from rich.table import Table

from price_watcher import config as _config_mod
from price_watcher.contracts import ChangeEvent, ChangeType, FetchStatus
from price_watcher.paths import data_dir, lock_file, log_dir

_console = Console()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PLIST_LABEL = "com.noada.price-watcher"
_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{_PLIST_LABEL}.plist"
_WIN_TASK_NAME = "PriceWatcherHourly"


def _repo_dir() -> Path:
    """Root of the price-watcher sub-project (two levels above this package)."""
    return Path(__file__).resolve().parents[2]


def _venv_python() -> str:
    return sys.executable


# ---------------------------------------------------------------------------
# run_check
# ---------------------------------------------------------------------------

def run_check(dry_run: bool = False) -> int:  # noqa: C901  (complexity OK here)
    """Fetch all active watches, detect changes, optionally send email.

    Returns 0 if every fetch succeeded, 1 if at least one failed.
    """
    # 1. Load config
    cfg = _config_mod.load()

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

    # 3. Load & filter watches
    watches = [w for w in _watchlist.load_all() if not w.paused]

    if not watches:
        _console.print("[dim]No active watches.[/dim]")
        return 0

    # 4. Fetch + evaluate
    events: list[ChangeEvent] = []
    for idx, watch in enumerate(watches):
        if idx > 0:
            delay = random.uniform(cfg.check.delay_min_sec, cfg.check.delay_max_sec)
            import time
            time.sleep(delay)

        fetch_result = _fetcher.fetch(watch.url, watch.selector)
        event = _state.evaluate(watch, fetch_result, cfg.check)
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

        def _price_cell() -> str:
            if fetch.price is not None:
                cur = fetch.currency or ""
                return f"{fetch.price}{(' ' + cur) if cur else ''}"
            return "—"

        if failed:
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
            old_str = str(event.old_price) if event.old_price is not None else "—"
            new_str = str(event.new_price) if event.new_price is not None else "—"
            info_cell = f"{old_str} → {new_str}"
            action_cell = "would send" if dry_run else "sending"

        table.add_row(status_cell, watch_name, info_cell, action_cell)

    _console.print(table)

    # 6. Build email payload
    payload = _email_sender.build_payload(events)

    if dry_run:
        if payload is not None:
            _console.print(
                f"[bold]WOULD SEND EMAIL[/bold] — Subject: {payload.subject}"
            )
        return 1 if any_failed else 0

    # Real run: send + persist
    if payload is not None:
        sent = _email_sender.send_change_notification(events)
        if sent:
            _state.mark_notified(events)

    _state.apply_many(events)

    # Summary line
    checked = len(events)
    failed_count = sum(
        1 for e in events
        if e.fetch.status != FetchStatus.OK or e.type == ChangeType.FAILED
    )
    _console.print(
        f"\n[bold]{checked} checked, {changed_count} changed, {failed_count} failed[/bold]"
    )

    # Write last-run.json
    brief_events = []
    for e in events:
        brief_events.append({
            "watch_id": e.watch.id,
            "name": e.watch.name,
            "type": e.type.value,
            "old_price": str(e.old_price) if e.old_price is not None else None,
            "new_price": str(e.new_price) if e.new_price is not None else None,
            "reason": e.reason,
        })

    last_run = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "checked": checked,
        "changed": changed_count,
        "failed": failed_count,
        "events": brief_events,
    }
    last_run_path = data_dir() / "last-run.json"
    last_run_path.write_text(json.dumps(last_run, indent=2))

    return 1 if any_failed else 0


# ---------------------------------------------------------------------------
# install_schedule
# ---------------------------------------------------------------------------

def install_schedule() -> None:
    """Install the hourly scheduler job for the current OS."""
    if sys.platform == "darwin":
        _install_launchd()
    elif sys.platform.startswith("win"):
        _install_schtasks()
    else:
        _console.print(
            "[yellow]Unsupported platform for automatic scheduling. "
            "Add a cron entry manually:[/yellow]\n"
            f"  0 * * * * {_venv_python()} -m price_watcher check"
        )


def _install_launchd() -> None:
    log_dir().mkdir(parents=True, exist_ok=True)

    plist_data = {
        "Label": _PLIST_LABEL,
        "ProgramArguments": [_venv_python(), "-m", "price_watcher", "check"],
        "WorkingDirectory": str(_repo_dir()),
        "StartInterval": 3600,
        "RunAtLoad": True,
        "StandardOutPath": str(log_dir() / "launchd.out.log"),
        "StandardErrorPath": str(log_dir() / "launchd.err.log"),
    }

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

    _console.print("[green]Schedule installed (launchd, hourly).[/green]")


def _install_schtasks() -> None:
    python_path = _venv_python()
    # Prefer pythonw.exe to suppress console window
    pythonw = Path(python_path).parent / "pythonw.exe"
    if pythonw.exists():
        python_path = str(pythonw)

    # Build XML for StartWhenAvailable support
    xml_content = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Price Watcher hourly check</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition>
        <Interval>PT1H</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>2024-01-01T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Settings>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions>
    <Exec>
      <Command>{python_path}</Command>
      <Arguments>-m price_watcher check</Arguments>
      <WorkingDirectory>{_repo_dir()}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""

    xml_path = data_dir() / "task-scheduler.xml"
    xml_path.write_text(xml_content, encoding="utf-16")

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

    _console.print("[green]Schedule installed (Task Scheduler, hourly).[/green]")


# ---------------------------------------------------------------------------
# uninstall_schedule
# ---------------------------------------------------------------------------

def uninstall_schedule() -> None:
    """Remove the hourly scheduler job."""
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


def _uninstall_schtasks() -> None:
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

    # --- last run ---
    last_run_path = data_dir() / "last-run.json"
    if not last_run_path.exists():
        _console.print("[bold]Last run:[/bold] No runs yet")
        return

    try:
        data = json.loads(last_run_path.read_text())
        ts_raw = data.get("timestamp", "")
        checked = data.get("checked", "?")
        changed = data.get("changed", "?")
        failed = data.get("failed", "?")

        # Pretty-print the timestamp
        try:
            dt = datetime.fromisoformat(ts_raw)
            ts_display = dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        except (ValueError, TypeError):
            ts_display = ts_raw

        _console.print(
            f"[bold]Last run:[/bold] {ts_display} — "
            f"{checked} checked, {changed} changed, {failed} failed"
        )
    except (json.JSONDecodeError, KeyError) as exc:
        _console.print(f"[red]Could not parse last-run.json:[/red] {exc}")


def _launchd_installed() -> bool:
    result = subprocess.run(
        ["launchctl", "list"],
        capture_output=True,
        text=True,
        check=False,
    )
    return _PLIST_LABEL in result.stdout


def _schtasks_installed() -> bool:
    result = subprocess.run(
        ["schtasks", "/query", "/tn", _WIN_TASK_NAME, "/v", "/fo", "list"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0
