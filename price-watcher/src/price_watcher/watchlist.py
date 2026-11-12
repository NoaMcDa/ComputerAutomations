from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlparse, urlunparse

from rich.console import Console
from rich.table import Table

from price_watcher.contracts import Watch
from price_watcher.logging_setup import get as _get_logger
from price_watcher.paths import watchlist_file

console = Console()
log = _get_logger(__name__)

_SCHEMA_VERSION = 1
# Query parameters that identify the product / variant; everything else
# (utm_*, fbclid, gclid, ref, ...) is tracking and is dropped.
_KEEP_PARAMS = {
    "variant", "variation", "size", "sku", "color", "colour", "flavour",
    "pid", "product_id", "id",
}

# A run of percent-encoded bytes >= 0x80, i.e. an encoded non-ASCII character.
_PCT_NON_ASCII = re.compile(r"(?:%[89A-Fa-f][0-9A-Fa-f])+")


def _readable_path(path: str) -> str:
    """Decode percent-encoded non-ASCII (e.g. Hebrew) characters in a path.

    "/%D7%A1%D7%99%D7%99%D7%9C" -> "/סייל". Encoded ASCII such as %2F or %20
    is left alone, so the path still means the same thing. The fetcher
    percent-encodes again for the request.
    """
    def _decode(m: re.Match) -> str:
        try:
            return unquote(m.group(0), errors="strict")
        except UnicodeDecodeError:
            return m.group(0)
    return _PCT_NON_ASCII.sub(_decode, path)


def _normalise_url(raw: str) -> str:
    parsed = urlparse(raw.strip())
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"Invalid URL (must be http(s) with a host): {raw!r}")
    qs = parse_qs(parsed.query, keep_blank_values=True)
    kept = {k: v for k, v in qs.items() if k in _KEEP_PARAMS}
    new_query = urlencode(kept, doseq=True)
    # Strip fragment; keep the path readable (non-ASCII shown as characters)
    normalised = urlunparse(
        (parsed.scheme, parsed.netloc, _readable_path(parsed.path), parsed.params, new_query, "")
    )
    return normalised


def _same_url(stored: str, normalised: str | None) -> bool:
    if normalised is None:
        return False
    try:
        return _normalise_url(stored) == normalised
    except ValueError:
        return stored == normalised


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


_WATCH_FIELDS = {f.name for f in dataclasses.fields(Watch)}
_DECIMAL_FIELDS = ("target_price", "min_change_abs")


def _watch_from_record(rec: Any) -> Watch:
    """Build a Watch from one stored record. Raises ValueError if unusable."""
    if not isinstance(rec, dict):
        raise ValueError(f"record is a {type(rec).__name__}, not an object")
    if not rec.get("id") or not rec.get("url"):
        raise ValueError("record has no id or url")
    unknown = set(rec) - _WATCH_FIELDS
    if unknown:
        log.warning(
            "Watch %s: ignoring unknown field(s) %s in %s",
            rec.get("id"), ", ".join(sorted(unknown)), watchlist_file(),
        )
    rec = {k: v for k, v in rec.items() if k in _WATCH_FIELDS}
    if rec.get("created_at"):
        rec["created_at"] = datetime.fromisoformat(str(rec["created_at"]))
    else:
        rec.pop("created_at", None)
    for key in _DECIMAL_FIELDS:
        if rec.get(key) is not None:
            try:
                rec[key] = Decimal(str(rec[key]))
            except InvalidOperation as exc:
                raise ValueError(f"{key} {rec[key]!r} is not a number") from exc
    return Watch(**rec)


def _backup_corrupt(path, exc: Exception) -> None:
    backup = path.with_name(f"{path.name}.corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    try:
        shutil.copy2(path, backup)
        log.error("Watch list %s is unreadable (%s); backed up to %s", path, exc, backup)
        console.print(
            f"[red]The watch list file is corrupt ({exc}).[/red] "
            f"A copy was saved to {backup}."
        )
    except OSError:
        log.error("Watch list %s is unreadable (%s)", path, exc)


def load_all() -> list[Watch]:
    """Load the watch list.

    Never raises on bad data: a corrupt file is backed up and treated as
    empty; a record with unknown keys keeps its known fields; an unusable
    record is logged and skipped. One bad entry must not take down `check`,
    `list` or `digest`.
    """
    path = watchlist_file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        records = data.get("watches", []) if isinstance(data, dict) else None
        if not isinstance(records, list):
            raise ValueError("no 'watches' list")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        _backup_corrupt(path, exc)
        return []
    watches = []
    for i, rec in enumerate(records):
        try:
            watches.append(_watch_from_record(rec))
        except (TypeError, ValueError) as exc:
            log.error("Skipping watch #%d in %s: %s", i + 1, path, exc)
            console.print(f"[yellow]Skipping an unreadable watch entry (#{i + 1}): {exc}[/yellow]")
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


def add(url: str, name: str | None = None, selector: str | None = None, **options: Any) -> None:
    """Add a watch. *options* are extra Watch settings (see set_options)."""
    try:
        normalised = _normalise_url(url)
        opts = _check_options(options)
    except ValueError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1)

    watch_id = _make_id(normalised)
    watches = load_all()

    if any(w.id == watch_id or _same_url(w.url, normalised) for w in watches):
        console.print(f"[yellow]Already watching:[/yellow] {normalised}")
        raise SystemExit(1)

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
        **opts,
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

    removed = [
        w for w in watches
        if w.id in (target_id, target) or _same_url(w.url, normalised)
    ]

    if not removed:
        console.print(f"[yellow]Not found:[/yellow] {target!r}")
        raise SystemExit(1)

    watches = [w for w in watches if w not in removed]
    save_all(watches)

    try:
        from price_watcher.state import drop_watch
        for w in removed:
            drop_watch(w.id)
    except Exception:
        pass

    console.print(f"[green]Removed:[/green] {target}")


# ---------------------------------------------------------------------------
# Per-watch notification settings
# ---------------------------------------------------------------------------

# Settings that `add` / `set` may change (everything except identity fields).
SETTABLE_OPTIONS = (
    "name", "selector", "notify_only_on_drop", "min_change_pct", "min_change_abs",
    "target_price", "track_promo", "excluded_from_promo", "paused",
)


def _check_options(options: dict) -> dict:
    """Validate and normalise per-watch settings; raises ValueError."""
    unknown = set(options) - set(SETTABLE_OPTIONS)
    if unknown:
        raise ValueError(f"unknown watch option(s): {', '.join(sorted(unknown))}")
    out = dict(options)
    for key in ("target_price", "min_change_abs"):
        if out.get(key) is not None:
            try:
                value = Decimal(str(out[key]))
            except InvalidOperation as exc:
                raise ValueError(f"{key}: {out[key]!r} is not a number") from exc
            if value < 0:
                raise ValueError(f"{key} must not be negative")
            out[key] = value
    if out.get("min_change_pct") is not None:
        out["min_change_pct"] = float(out["min_change_pct"])
        if out["min_change_pct"] < 0:
            raise ValueError("min_change_pct must not be negative")
    return out


def find(watches: list[Watch], target: str) -> Watch | None:
    """Find a watch by id or (normalised) URL."""
    try:
        normalised = _normalise_url(target)
        target_id = _make_id(normalised)
    except ValueError:
        normalised, target_id = None, target
    for w in watches:
        if w.id in (target, target_id) or (normalised and w.url == normalised):
            return w
    return None


def set_options(target: str, **options: Any) -> Watch:
    """Change settings of an existing watch (by URL or id). Returns the new Watch.

    Options left as None are not changed; to clear a numeric setting pass
    the CLI's "none" (handled by the CLI as ``clear=[...]``).
    """
    clear = options.pop("clear", None) or []
    watches = load_all()
    watch = find(watches, target)
    if watch is None:
        console.print(f"[yellow]Not found:[/yellow] {target!r}")
        raise SystemExit(1)
    try:
        changes = _check_options({k: v for k, v in options.items() if v is not None})
        for key in clear:
            if key not in SETTABLE_OPTIONS:
                raise ValueError(f"unknown watch option: {key}")
            changes[key] = None
    except ValueError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1)
    updated = dataclasses.replace(watch, **changes)
    save_all([updated if w.id == watch.id else w for w in watches])
    return updated
