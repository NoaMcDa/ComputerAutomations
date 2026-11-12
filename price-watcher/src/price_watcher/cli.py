from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Optional

import typer
from rich.console import Console

from price_watcher import __version__
from price_watcher.logging_setup import configure as configure_logging

app = typer.Typer(
    name="price-watcher",
    help="Watch product URLs and email on price change.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _fail(message: str, code: int = 2) -> "typer.Exit":
    typer.echo(f"Error: {message}", err=True)
    return typer.Exit(code)


@contextmanager
def _friendly_errors():
    """Turn configuration errors into a one-line message + non-zero exit."""
    from price_watcher.config import ConfigError
    try:
        yield
    except ConfigError as exc:
        raise _fail(str(exc)) from None


@app.callback()
def _root(
    log_level: str = typer.Option(
        None, "--log-level",
        help="DEBUG, INFO, WARNING, ERROR (default: $PW_LOG_LEVEL, else INFO)",
    ),
) -> None:
    from price_watcher import config as _config  # loads .env
    level = log_level or os.environ.get("PW_LOG_LEVEL") or "INFO"
    try:
        configure_logging(level)
    except ValueError as exc:
        raise _fail(str(exc)) from None


@app.command()
def version() -> None:
    """Print the installed Price Watcher version."""
    console.print(f"price-watcher {__version__}")


def parse_min_change(value: Optional[str]) -> dict:
    """"5%" -> {"min_change_pct": 5.0}; "10" -> {"min_change_abs": "10"}."""
    if value is None:
        return {}
    text = value.strip()
    try:
        if text.endswith("%"):
            pct = float(text[:-1].strip())
            if pct < 0:
                raise ValueError
            return {"min_change_pct": pct}
        amount = float(text)
        if amount < 0:
            raise ValueError
        return {"min_change_abs": text}
    except ValueError:
        raise typer.BadParameter(
            f"{value!r}: use a percentage like 5% or an amount like 10",
            param_hint="--min-change",
        )


_MIN_CHANGE_HELP = "Ignore smaller price changes: a percentage (5%) or an amount (10)"
_TARGET_HELP = "Only notify about a price change once the price is at or below this"
_DROPS_HELP = "Only notify when the price goes down"
_PROMO_HELP = "Read the site's promotion banner (MyProtein) and estimate the final price"
_EXCLUDED_HELP = "The item is excluded from site promotions: no estimated final price"


@app.command()
def add(
    url: str = typer.Argument(..., help="Product URL to watch"),
    name: str = typer.Option(None, "--name", help="Friendly label"),
    selector: str = typer.Option(None, "--selector", help="CSS selector for the price, used if auto-detection fails"),
    only_drops: bool = typer.Option(False, "--only-drops", help=_DROPS_HELP),
    min_change: Optional[str] = typer.Option(None, "--min-change", help=_MIN_CHANGE_HELP),
    target: Optional[str] = typer.Option(None, "--target", help=_TARGET_HELP),
    promo: bool = typer.Option(True, "--promo/--no-promo", help=_PROMO_HELP),
    excluded_from_promo: bool = typer.Option(False, "--excluded-from-promo", help=_EXCLUDED_HELP),
) -> None:
    """Add a URL to the watch list (exit code 1 if it is already watched)."""
    from price_watcher.watchlist import add as _add

    options: dict = {}
    if only_drops:
        options["notify_only_on_drop"] = True
    options.update(parse_min_change(min_change))
    if target is not None:
        options["target_price"] = target
    if not promo:
        options["track_promo"] = False
    if excluded_from_promo:
        options["excluded_from_promo"] = True
    if not _add(url=url, name=name, selector=selector, **options):
        raise typer.Exit(1)


@app.command(name="set")
def set_cmd(
    target: str = typer.Argument(..., help="URL or id of the watch to change"),
    name: Optional[str] = typer.Option(None, "--name", help="Friendly label"),
    selector: Optional[str] = typer.Option(None, "--selector", help="CSS selector for the price"),
    only_drops: Optional[bool] = typer.Option(None, "--only-drops/--all-changes", help=_DROPS_HELP),
    min_change: Optional[str] = typer.Option(
        None, "--min-change", help=_MIN_CHANGE_HELP + "; 'none' removes it",
    ),
    target_price: Optional[str] = typer.Option(
        None, "--target", help=_TARGET_HELP + "; 'none' removes it",
    ),
    promo: Optional[bool] = typer.Option(None, "--promo/--no-promo", help=_PROMO_HELP),
    excluded_from_promo: Optional[bool] = typer.Option(
        None, "--excluded-from-promo/--not-excluded-from-promo", help=_EXCLUDED_HELP,
    ),
    paused: Optional[bool] = typer.Option(None, "--pause/--resume", help="Skip this link in `check`"),
) -> None:
    """Change the settings of a watched URL (notification filters, promo, pause)."""
    from price_watcher.watchlist import set_options

    options: dict = {
        "name": name,
        "selector": selector,
        "notify_only_on_drop": only_drops,
        "track_promo": promo,
        "excluded_from_promo": excluded_from_promo,
        "paused": paused,
    }
    clear: list[str] = []
    if min_change is not None:
        clear += ["min_change_pct", "min_change_abs"]
        if min_change.strip().lower() != "none":
            parsed = parse_min_change(min_change)
            clear = [k for k in clear if k not in parsed]
            options.update(parsed)
    if target_price is not None:
        if target_price.strip().lower() == "none":
            clear.append("target_price")
        else:
            options["target_price"] = target_price
    if all(v is None for v in options.values()) and not clear:
        raise typer.BadParameter("nothing to change; pass at least one option")
    watch = set_options(target, clear=clear, **options)
    console.print(f"[green]Updated:[/green] {watch.name or watch.url}")
    console.print(_describe_settings(watch))


def _describe_settings(w) -> str:
    parts = []
    if w.paused:
        parts.append("paused")
    if w.notify_only_on_drop:
        parts.append("drops only")
    if w.min_change_pct is not None:
        parts.append(f"min change {w.min_change_pct:g}%")
    if w.min_change_abs is not None:
        parts.append(f"min change {w.min_change_abs}")
    if w.target_price is not None:
        parts.append(f"target ≤ {w.target_price}")
    if not w.track_promo:
        parts.append("promotion banner off")
    if w.excluded_from_promo:
        parts.append("excluded from promotions")
    return "  settings: " + (", ".join(parts) if parts else "defaults (notify on every change)")


@app.command(name="list")
def list_cmd() -> None:
    """List every watched URL with its last price and last check time."""
    from price_watcher.watchlist import list_watches
    list_watches()


@app.command()
def remove(target: str = typer.Argument(..., help="URL or id to remove")) -> None:
    """Remove a URL from the watch list."""
    from price_watcher.watchlist import remove as _remove
    _remove(target)


@app.command()
def check(
    dry_run: bool = typer.Option(False, "--dry-run", help="Run the full pipeline but do not send email or write state"),
) -> None:
    """Fetch, compare, notify and save state for every watched URL.

    Exit code: 0 = all links read (and any change email sent), 1 = at least
    one link could not be fetched or read, 2 = configuration error (a real
    run needs the SMTP settings), 3 = a change email could not be sent (the
    changes are retried on the next run).
    """
    from price_watcher import config as _config
    from price_watcher.scheduler import run_check
    with _friendly_errors():
        if not dry_run:
            _config.load()  # a real run needs SMTP settings: fail fast, one line
        rc = run_check(dry_run=dry_run)
    raise typer.Exit(rc)


@app.command(name="test-email")
def test_email() -> None:
    """Send a sample email to verify the SMTP setup."""
    from price_watcher.email_sender import send_test_email
    with _friendly_errors():
        try:
            send_test_email()
        except Exception as exc:
            from price_watcher.config import ConfigError
            if isinstance(exc, ConfigError):
                raise
            raise _fail(f"Test email failed: {type(exc).__name__}: {exc}", 1) from None
    console.print("[green]Test email sent.[/green] Check your inbox (and spam folder).")


@app.command(name="install-schedule")
def install_schedule() -> None:
    """Install the OS-level daily (09:00) schedule (launchd on macOS, Task Scheduler on Windows)."""
    from price_watcher.scheduler import install_schedule as _install
    _install()


@app.command(name="uninstall-schedule")
def uninstall_schedule() -> None:
    """Remove the OS-level schedule."""
    from price_watcher.scheduler import uninstall_schedule as _uninstall
    _uninstall()


@app.command()
def status() -> None:
    """Show whether the schedule is installed and when the last run happened."""
    from price_watcher.scheduler import status as _status
    _status()


@app.command()
def digest(
    dry_run: bool = typer.Option(False, "--dry-run", help="Collect + print, but do not email"),
    test: bool = typer.Option(False, "--test", help="Email only PW_EMAIL_TO (skip the deals-only recipients) and save nothing"),
) -> None:
    """Send the daily report (site promos, creatine, Sims 4, meat, vacation deals)."""
    from price_watcher.digest import run_digest
    with _friendly_errors():
        rc = run_digest(dry_run=dry_run, test=test)
    raise typer.Exit(rc)


@app.command()
def deals(
    dry_run: bool = typer.Option(False, "--dry-run", help="Collect + print, but do not email"),
    test: bool = typer.Option(False, "--test", help="Send the deals-only email to PW_EMAIL_TO instead of PW_DEALS_EMAIL_TO"),
    days: int = typer.Option(None, "--days", help="Only deals departing within this many days (overrides PW_DEALS_DAYS; 0 = no limit)"),
) -> None:
    """Scan vacation deals and send the deals-only email to PW_DEALS_EMAIL_TO."""
    from price_watcher.digest import run_deals
    with _friendly_errors():
        rc = run_deals(dry_run=dry_run, test=test, days=days)
    raise typer.Exit(rc)


@app.command(name="install-digest-schedule")
def install_digest_schedule() -> None:
    """Install the daily digest job at 9 AM local time."""
    from price_watcher.scheduler import install_digest_schedule as _install
    _install()


@app.command(name="uninstall-digest-schedule")
def uninstall_digest_schedule() -> None:
    """Remove the daily digest job."""
    from price_watcher.scheduler import uninstall_digest_schedule as _uninstall
    _uninstall()


# --- hook: flight watches (#10) + Marketplace preview (#54), see cli_extras.py ---
from price_watcher.cli_extras import register as _register_extras  # noqa: E402

_register_extras(app)


if __name__ == "__main__":
    app()
