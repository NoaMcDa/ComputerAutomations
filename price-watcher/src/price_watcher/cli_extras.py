"""CLI commands for flight watches (#10) and the Marketplace preview (#54).

Registered onto the main Typer app by one hook call in ``cli.py`` so the
commands live next to their features instead of in the shared CLI file.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

console = Console()


def _parse_date(value: Optional[str], name: str) -> Optional[date]:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise typer.BadParameter(f"{name} must be YYYY-MM-DD, got {value!r}") from None


def _print_options(options) -> None:
    from price_watcher.flights import _money

    table = Table(show_header=True, header_style="bold")
    for col in ("#", "Departs", "Arrives", "Flight", "Price"):
        table.add_column(col)
    for i, o in enumerate(options, 1):
        price = "sold out" if o.sold_out or o.price is None else _money(o.price, o.currency)
        table.add_row(str(i), o.departure_time, o.arrival_time or "—", o.flight_number or "—", price)
    console.print(table)


def register(app: typer.Typer) -> None:
    @app.command(name="add-flight")
    def add_flight_cmd(
        airline: str = typer.Option(..., "--airline", help="elal or arkia"),
        origin: str = typer.Option(..., "--from", help="Origin airport (IATA), e.g. TLV"),
        destination: str = typer.Option(..., "--to", help="Destination airport (IATA)"),
        day: str = typer.Option(..., "--date", help="Pinned flight date, YYYY-MM-DD"),
        passengers: int = typer.Option(1, "--passengers", min=1, help="Number of passengers"),
        return_date: Optional[str] = typer.Option(None, "--return", help="Stated return date (round trip only)"),
        min_change: float = typer.Option(30.0, "--min-change", help="Minimum price change (₪) that triggers an email"),
    ) -> None:
        """Watch a flight: load that day's flights, pick one from a numbered list."""
        from price_watcher import flights

        try:
            source = flights.get_source(airline)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from None

        def _choose(options) -> int:
            _print_options(options)
            n = typer.prompt(f"Pick a flight (1-{len(options)})", type=int)
            return n - 1

        console.print(f"Loading {source.name} flights {origin.upper()}→{destination.upper()} on {day} ...")
        try:
            watch = flights.add_flight(
                source, origin, destination, _parse_date(day, "--date"), _choose,
                passengers=passengers, return_date=_parse_date(return_date, "--return"),
                min_change_amount=Decimal(str(min_change)),
            )
        except flights.FlightSearchError as exc:
            console.print(f"[red]Could not add the flight:[/red] {exc}")
            raise typer.Exit(1)
        console.print(f"[green]Watching[/green] {watch.label}  (id {watch.id})")

    @app.command(name="list-flights")
    def list_flights_cmd() -> None:
        """List flight watches with the last known prices."""
        from price_watcher import flights

        watches = flights.load_watches()
        if not watches:
            console.print("No flight watches. Add one with: price-watcher add-flight")
            return
        st = flights.load_state()["watches"]
        table = Table(show_header=True, header_style="bold")
        for col in ("id", "Flight", "Pinned", "Cheapest in window", "Active"):
            table.add_column(col)
        for w in watches:
            s = st.get(w.id, {})
            p = s.get("pinned") or {}
            pinned = (flights._money(Decimal(p["price"]), p.get("currency", "ILS")) if p.get("price")
                      else p.get("status", "—"))
            wc = s.get("window_cheapest") or {}
            cheapest = f"{wc['date']} {flights._money(Decimal(wc['price']))}" if wc else "—"
            table.add_row(w.id, w.label, pinned, cheapest, "yes" if w.active else "ended")
        console.print(table)

    @app.command(name="remove-flight")
    def remove_flight_cmd(watch_id: str = typer.Argument(..., help="Flight watch id")) -> None:
        """Remove a flight watch."""
        from price_watcher import flights

        if not flights.remove_watch(watch_id):
            console.print(f"[red]No flight watch with id {watch_id}[/red]")
            raise typer.Exit(1)
        console.print(f"Removed flight watch {watch_id}")

    @app.command(name="check-flights")
    def check_flights_cmd(
        dry_run: bool = typer.Option(False, "--dry-run", help="Check and print, but do not email or save state"),
    ) -> None:
        """Check every flight watch; email per watch when the rules fire."""
        from price_watcher import flights

        raise typer.Exit(flights.run_flight_checks(dry_run=dry_run, echo=console.print))

    @app.command(name="marketplace-preview")
    def marketplace_preview_cmd() -> None:
        """Run the Facebook Marketplace scan and print the email section (no email, no state change)."""
        from price_watcher import marketplace

        report = marketplace.scan_and_report()
        marketplace.print_report(report)
