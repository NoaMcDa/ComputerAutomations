from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import tempfile
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from rich.console import Console
from rich.table import Table

from price_watcher.contracts import Watch
from price_watcher.paths import watchlist_file

console = Console()

_SCHEMA_VERSION = 1
_KEEP_PARAMS = {"variant", "size", "sku", "color", "colour", "flavour", "pid", "product_id", "id"}


def _normalise_url(raw: str) -> str:
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Invalid URL (must have scheme and host): {raw!r}")
    qs = parse_qs(parsed.query, keep_blank_values=True)
    kept = {k: v for k, v in qs.items() if k in _KEEP_PARAMS}
    new_query = urlencode(kept, doseq=True)
    # Strip fragment; preserve path with non-ASCII chars as-is (no re-encoding)
    normalised = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, ""))
    return normalised


def _make_id(normalised_url: str) -> str:
    host = urlparse(normalised_url).netloc.lstrip("www.")
    short = hashlib.sha1(normalised_url.encode()).hexdigest()[:8]
    return f"{host}-{short}"


class _Encoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return str(obj)
        return super().default(obj)


def load_all() -> list[Watch]:
    path = watchlist_file()
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    watches = []
    for rec in data.get("watches", []):
        if "created_at" in rec and rec["created_at"]:
            rec["created_at"] = datetime.fromisoformat(rec["created_at"])
        if "target_price" in rec and rec["target_price"] is not None:
            rec["target_price"] = Decimal(rec["target_price"])
        watches.append(Watch(**rec))
    return watches


def save_all(watches: list[Watch]) -> None:
    path = watchlist_file()
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "watches": [dataclasses.asdict(w) for w in watches],
    }
    text = json.dumps(payload, indent=2, cls=_Encoder, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".watchlist-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def add(url: str, name: str | None = None, selector: str | None = None) -> None:
    try:
        normalised = _normalise_url(url)
    except ValueError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1)

    watch_id = _make_id(normalised)
    watches = load_all()

    if any(w.id == watch_id for w in watches):
        console.print(f"[yellow]Already watching:[/yellow] {normalised}")
        return

    from price_watcher.contracts import FetchStatus
    from price_watcher.fetcher import fetch

    result = fetch(normalised, selector=selector)

    if result.status != FetchStatus.OK:
        msg = result.error_message or result.status.value
        console.print(f"[red]Could not read price:[/red] {msg}")
        if selector is None:
            console.print("Tip: try again with [bold]--selector[/bold] to specify a CSS selector for the price element.")
        raise SystemExit(1)

    label = name or normalised
    price_str = f"{result.price} {result.currency}" if result.price is not None else "unknown"
    console.print(f"Added: {label} — current price: {price_str}")

    watch = Watch(
        id=watch_id,
        url=normalised,
        name=name,
        selector=selector,
        created_at=datetime.now(),
    )
    watches.append(watch)
    save_all(watches)


def list_watches() -> None:
    watches = load_all()

    state_map: dict[str, dict] = {}
    try:
        from price_watcher.state import load_state
        raw = load_state()
        state_map = raw.get("watches", {})
    except Exception:
        pass

    table = Table(show_header=True, header_style="bold")
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("URL")
    table.add_column("Last Price")
    table.add_column("Last Check")

    for w in watches:
        wstate = state_map.get(w.id, {})
        price = wstate.get("last_price") or ""
        currency = wstate.get("currency") or ""
        last_price = f"{price} {currency}".strip() if price else "—"
        last_check = wstate.get("last_check_time") or "—"
        table.add_row(w.id, w.name or "", w.url, last_price, last_check)

    if not watches:
        console.print("No watches yet. Use [bold]price-watcher add <url>[/bold] to get started.")
    else:
        console.print(table)


def remove(target: str) -> None:
    watches = load_all()

    try:
        normalised = _normalise_url(target)
        target_id = _make_id(normalised)
    except ValueError:
        target_id = target
        normalised = None

    before = len(watches)
    watches = [
        w for w in watches
        if w.id != target_id and w.id != target and w.url != normalised
    ]

    if len(watches) == before:
        console.print(f"[yellow]Not found:[/yellow] {target!r}")
        raise SystemExit(1)

    save_all(watches)

    removed_id = target_id if target_id else target
    try:
        from price_watcher.state import drop_watch
        drop_watch(removed_id)
    except Exception:
        pass

    console.print(f"[green]Removed:[/green] {target}")
