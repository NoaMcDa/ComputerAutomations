from __future__ import annotations

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


@app.callback()
def _root(
    log_level: str = typer.Option("INFO", "--log-level", help="DEBUG, INFO, WARNING, ERROR"),
) -> None:
    configure_logging(log_level)


@app.command()
def version() -> None:
    """Print the installed Price Watcher version."""
    console.print(f"price-watcher {__version__}")


@app.command()
def add(
    url: str = typer.Argument(..., help="Product URL to watch"),
    name: str = typer.Option(None, "--name", help="Friendly label"),
    selector: str = typer.Option(None, "--selector", help="CSS selector for the price, used if auto-detection fails"),
) -> None:
    """Add a URL to the watch list."""
    from price_watcher.watchlist import add as _add
    _add(url=url, name=name, selector=selector)


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
    """Fetch, compare, notify and save state for every watched URL."""
    from price_watcher.scheduler import run_check
    run_check(dry_run=dry_run)


@app.command(name="test-email")
def test_email() -> None:
    """Send a sample email to verify the SMTP setup."""
    from price_watcher.email_sender import send_test_email
    send_test_email()


@app.command(name="install-schedule")
def install_schedule() -> None:
    """Install the OS-level hourly schedule (launchd on macOS, Task Scheduler on Windows)."""
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


if __name__ == "__main__":
    app()
