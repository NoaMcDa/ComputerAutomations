from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import tempfile
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import unquote_plus, urlparse, urlunparse

from rich.console import Console
from rich.table import Table

from price_watcher.contracts import Watch
from price_watcher.paths import watchlist_file

console = Console()

_SCHEMA_VERSION = 1
# Known tracking / attribution params that never identify a product; everything
# else in the query string is kept.
_TRACKING_PARAMS = {
    "gclid", "gclsrc", "dclid", "gbraid", "wbraid", "fbclid", "msclkid", "srsltid",
    "yclid", "twclid", "ttclid", "igshid", "ref", "ref_", "ref_src", "mc_cid", "mc_eid",
    "_ga", "_gl", "_hsenc", "_hsmi", "mkt_tok",
}


def _is_tracking_param(key: str) -> bool:
    k = unquote_plus(key).strip().lower()
    return k.startswith("utm_") or k in _TRACKING_PARAMS


def _normalise_url(raw: str) -> str:
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Invalid URL (must have scheme and host): {raw!r}")
    # Keep the remaining query pairs verbatim (order and encoding preserved).
    pairs = [p for p in parsed.query.split("&") if p and not _is_tracking_param(p.split("=", 1)[0])]
    new_query = "&".join(pairs)
    # Strip fragment; preserve path with non-ASCII chars as-is (no re-encoding)
    return urlunparse(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.params, new_query, "")
    )


def _make_id(normalised_url: str) -> str:
    host = urlparse(normalised_url).netloc.lower().removeprefix("www.")
    short = hashlib.sha1(normalised_url.encode()).hexdigest()[:8]
    return f"{host}-{short}"


def _same_url(stored: str, normalised: str) -> bool:
    try:
        return _normalise_url(stored) == normalised
    except ValueError:
        return stored == normalised


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

    if any(w.id == watch_id or _same_url(w.url, normalised) for w in watches):
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
    if result.price is None:
        price_str = "unknown"
    elif result.currency:
        price_str = f"{result.price} {result.currency}"
    else:
        price_str = f"{result.price}"
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

    def _matches(w: Watch) -> bool:
        return (
            w.id == target_id
            or w.id == target
            or w.url == normalised
            or (normalised is not None and _same_url(w.url, normalised))
        )

    removed_ids = [w.id for w in watches if _matches(w)]
    watches = [w for w in watches if not _matches(w)]

    if not removed_ids:
        console.print(f"[yellow]Not found:[/yellow] {target!r}")
        raise SystemExit(1)

    save_all(watches)

    try:
        from price_watcher.state import drop_watch
        for rid in removed_ids:
            drop_watch(rid)
    except Exception:
        pass

    console.print(f"[green]Removed:[/green] {target}")
