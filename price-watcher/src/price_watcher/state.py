"""State store and change detection for Price Watcher.

Public API
----------
load_state()         -> dict
drop_watch(watch_id) -> None
evaluate(watch, fetch, cfg) -> ChangeEvent
apply(event)         -> None
apply_many(events, email_failed=False) -> None
mark_notified(events) -> None
effective_fetch(watch, fetch) -> FetchResult
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional
from urllib.parse import urlparse

from price_watcher.contracts import (
    ChangeEvent,
    ChangeType,
    NOTIFIABLE_TYPES,
    FetchResult,
    FetchStatus,
    PromoInfo,
    Watch,
)
from price_watcher.config import CheckConfig
from price_watcher.logging_setup import get as _get_logger
from price_watcher.paths import state_file

log = _get_logger(__name__)

_SCHEMA_VERSION = 1
_HISTORY_CAP = 500


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _decimal_or_none(value: Optional[str]) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _str_or_none(value: Optional[Decimal]) -> Optional[str]:
    if value is None:
        return None
    return str(value)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _promo_to_dict(promo: Optional[PromoInfo]) -> Optional[dict]:
    if promo is None:
        return None
    return {
        "code": promo.code,
        "discount_pct": str(promo.discount_pct),
        "condition": promo.condition,
    }


def _promo_from_dict(d: Optional[dict]) -> Optional[PromoInfo]:
    if d is None:
        return None
    return PromoInfo(
        code=d.get("code"),
        discount_pct=Decimal(d["discount_pct"]) if d.get("discount_pct") is not None else Decimal("0"),
        condition=d.get("condition"),
    )


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def _empty_state() -> dict:
    return {"schema_version": _SCHEMA_VERSION, "watches": {}}


def load_state() -> dict:
    """Load the raw state dict from disk. Returns a default if the file is missing.

    A corrupt file is copied aside to ``state.json.corrupt-<time>`` (so the
    price history is not silently lost on the next save) and an empty state
    is returned.
    """
    path = state_file()
    if not path.exists():
        return _empty_state()
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or not isinstance(data.get("watches", {}), dict):
            raise ValueError("unexpected structure")
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, OSError) as exc:
        backup = path.with_name(
            f"{path.name}.corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        )
        try:
            shutil.copy2(path, backup)
            log.error("State file %s is unreadable (%s); backed up to %s", path, exc, backup)
        except OSError:
            log.error("State file %s is unreadable (%s)", path, exc)
        return _empty_state()
    data.setdefault("watches", {})
    return data


def _save_state(data: dict) -> None:
    """Atomically write the state dict to disk (temp file + fsync + rename)."""
    path = state_file()
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def drop_watch(watch_id: str) -> None:
    """Remove the per-watch record when the watch is deleted from the watchlist."""
    data = load_state()
    data["watches"].pop(watch_id, None)
    _save_state(data)


# ---------------------------------------------------------------------------
# Change detection (pure-ish — reads state from disk, writes nothing)
# ---------------------------------------------------------------------------

def _redirected_to_home(watch_url: str, final_url: Optional[str]) -> bool:
    """True when a product link now lands on the shop's home page."""
    if not final_url:
        return False
    try:
        w, f = urlparse(watch_url), urlparse(final_url)
    except ValueError:
        return False
    return w.path.strip("/") != "" and f.path.strip("/") == ""


def effective_fetch(watch: Watch, fetch: FetchResult) -> FetchResult:
    """Apply per-watch options and sanity rules to a raw fetch result.

    - promotion tracking switched off / item excluded from promotions:
      drop the banner promo and the estimated final price;
    - a product page that now redirects to the home page: "not found";
    - a successful fetch with no price (and not "out of stock"), or a price
      of 0: "price not found" — an unusable reading is never a price change.
    """
    if (
        fetch.status in (FetchStatus.OK, FetchStatus.PRICE_NOT_FOUND)
        and _redirected_to_home(watch.url, fetch.final_url)
    ):
        return replace(
            fetch,
            status=FetchStatus.NOT_FOUND,
            error_message=(
                f"the product page now redirects to the home page ({fetch.final_url}); "
                "the product was probably removed"
            ),
        )
    if fetch.status != FetchStatus.OK:
        return fetch
    if (not watch.track_promo or watch.excluded_from_promo) and (
        fetch.promo is not None or fetch.estimated_final_price is not None
    ):
        fetch = replace(fetch, promo=None, estimated_final_price=None)
    if fetch.price is None and fetch.in_stock is not False:
        return replace(
            fetch,
            status=FetchStatus.PRICE_NOT_FOUND,
            error_message=fetch.error_message or "the page was read but no price was found",
        )
    if fetch.price is not None and fetch.price <= 0:
        return replace(
            fetch,
            status=FetchStatus.PRICE_NOT_FOUND,
            error_message=f"the page shows a price of {fetch.price}; treated as an unreadable price",
        )
    return fetch


def _promo_computable(promo: Optional[PromoInfo]) -> bool:
    """A promo with a usable percentage (not tiered "up to", not unreadable)."""
    return promo is not None and promo.discount_pct > 0


def evaluate(watch: Watch, fetch: FetchResult, cfg: CheckConfig) -> ChangeEvent:
    """Decide the ChangeType for this fetch result against stored state.

    Does NOT write state. Call apply() or apply_many() afterward.
    """
    data = load_state()
    ws: dict = data["watches"].get(watch.id, {})
    now = datetime.now().astimezone()
    fetch = effective_fetch(watch, fetch)

    # ------------------------------------------------------------------ #
    # Fetch failed (or the reading was unusable)                          #
    # ------------------------------------------------------------------ #
    if fetch.status != FetchStatus.OK:
        consecutive = int(ws.get("consecutive_failures", 0)) + 1
        threshold = max(1, cfg.failure_threshold)
        failure_notified = ws.get("failure_notified", False)

        if consecutive >= threshold and not failure_notified:
            return ChangeEvent(
                type=ChangeType.THRESHOLD_FAILURE,
                watch=watch,
                fetch=fetch,
                reason=(
                    f"האחזור נכשל {consecutive} פעמים ברצף "
                    f"(הסף: {threshold})"
                ),
                timestamp=now,
            )

        return ChangeEvent(
            type=ChangeType.FAILED,
            watch=watch,
            fetch=fetch,
            reason=(
                f"fetch failed with status {fetch.status.value} "
                f"(attempt {consecutive})"
                + (f": {fetch.error_message}" if fetch.error_message else "")
            ),
            timestamp=now,
        )

    # ------------------------------------------------------------------ #
    # Fetch succeeded                                                      #
    # ------------------------------------------------------------------ #
    consecutive = int(ws.get("consecutive_failures", 0))
    failure_notified = ws.get("failure_notified", False)
    stored_price = _decimal_or_none(ws.get("last_price"))
    stored_promo = _promo_from_dict(ws.get("promo"))
    stored_estimate = _decimal_or_none(ws.get("estimated_final_price"))

    # Recovery
    if consecutive > 0 and failure_notified:
        return ChangeEvent(
            type=ChangeType.RECOVERED,
            watch=watch,
            fetch=fetch,
            old_price=_reference_price(ws),
            new_price=fetch.price,
            reason=f"recovered after {consecutive} consecutive failures",
            timestamp=now,
        )

    # First seen
    if stored_price is None:
        return ChangeEvent(
            type=ChangeType.FIRST_SEEN,
            watch=watch,
            fetch=fetch,
            new_price=fetch.price,
            timestamp=now,
        )

    # Out of stock pages may show no price: compare as "price unchanged".
    new_price = fetch.price if fetch.price is not None else stored_price
    deferred_count = int(ws.get("sanity_deferred_count", 0))

    # Currency sanity: re-check on the next run before believing it.
    stored_currency = ws.get("currency")
    currency_changed = bool(
        stored_currency and fetch.currency and fetch.currency != stored_currency
    )
    if currency_changed and deferred_count < 1:
        return ChangeEvent(
            type=ChangeType.SANITY_DEFERRED,
            watch=watch,
            fetch=fetch,
            old_price=stored_price,
            new_price=new_price,
            reason=(
                f"currency changed from {stored_currency} to {fetch.currency}; "
                "re-checking on the next run"
            ),
            timestamp=now,
        )

    # Large jump sanity check (against the last observed price).
    if not currency_changed and stored_price != 0:
        jump_pct = float((new_price - stored_price) / stored_price * 100)
        if abs(jump_pct) > cfg.sanity_jump_pct and deferred_count < 1:
            return ChangeEvent(
                type=ChangeType.SANITY_DEFERRED,
                watch=watch,
                fetch=fetch,
                old_price=stored_price,
                new_price=new_price,
                pct_change=jump_pct,
                reason=(
                    f"price jump of {jump_pct:.1f}% exceeds sanity limit "
                    f"({cfg.sanity_jump_pct}%); deferring one cycle"
                ),
                timestamp=now,
            )
        # deferred_count >= 1: the spike persisted — treat it as real.

    # The price change is measured against the price the user last heard
    # about (the "reference"), so per-link filters work cumulatively: five
    # 1% drops add up to a 5% drop instead of each being ignored.
    ref_price = _reference_price(ws) or stored_price
    if currency_changed or ref_price == 0:
        pct_change: Optional[float] = None
    else:
        pct_change = float((new_price - ref_price) / ref_price * 100)

    # Stock comparison
    stored_in_stock = ws.get("in_stock")  # None means unknown
    stock_changed = (
        fetch.in_stock is not None
        and stored_in_stock is not None
        and fetch.in_stock != stored_in_stock
    )

    # Promo comparison. A tiered / unreadable banner (no usable percentage)
    # is never a change by itself: it must not look like "promotion ended"
    # (which would read as a price rise).
    if fetch.promo is not None and not _promo_computable(fetch.promo):
        promo_changed = False
    else:
        promo_changed = _promos_differ(
            stored_promo if _promo_computable(stored_promo) else None,
            fetch.promo,
        )

    price_changed = new_price != ref_price or currency_changed

    common = dict(
        watch=watch,
        fetch=fetch,
        timestamp=now,
        old_promo=stored_promo,
        old_estimated_price=stored_estimate,
    )

    if not price_changed and not stock_changed and not promo_changed:
        return ChangeEvent(type=ChangeType.NO_CHANGE, new_price=new_price, **common)

    if not price_changed:
        # Stock and/or promotion changed at an unchanged price. When both
        # changed it is reported as a stock change; the email shows the
        # current promotion too.
        return ChangeEvent(
            type=ChangeType.STOCK_CHANGED if stock_changed else ChangeType.PROMO_CHANGED,
            old_price=ref_price,
            new_price=new_price,
            **common,
        )

    # Price changed (possibly together with stock / promo)
    event = ChangeEvent(
        type=ChangeType.PRICE_CHANGED,
        old_price=ref_price,
        new_price=new_price,
        pct_change=pct_change,
        diff=new_price - ref_price,
        reason=(
            f"currency changed from {stored_currency} to {fetch.currency}"
            if currency_changed else None
        ),
        **common,
    )
    filtered = _apply_filters(event, watch)
    if filtered.type == ChangeType.NO_CHANGE and (stock_changed or promo_changed):
        # The price move is filtered out, but the stock / promo change is
        # still news on its own.
        return replace(
            filtered,
            type=ChangeType.STOCK_CHANGED if stock_changed else ChangeType.PROMO_CHANGED,
        )
    return filtered


def _reference_price(ws: dict) -> Optional[Decimal]:
    """The price the user last heard about (falls back to the last price)."""
    ref = _decimal_or_none(ws.get("reference_price"))
    return ref if ref is not None else _decimal_or_none(ws.get("last_price"))


def _promos_differ(a: Optional[PromoInfo], b: Optional[PromoInfo]) -> bool:
    if a is None and b is None:
        return False
    if a is None or b is None:
        return True
    return a.code != b.code or a.discount_pct != b.discount_pct


def _apply_filters(event: ChangeEvent, watch: Watch) -> ChangeEvent:
    """Downgrade PRICE_CHANGED to NO_CHANGE if per-watch filters say so."""
    if event.type != ChangeType.PRICE_CHANGED:
        return event

    def _quiet(reason: str) -> ChangeEvent:
        return replace(
            event,
            type=ChangeType.NO_CHANGE,
            pct_change=None,
            diff=None,
            reason=reason,
        )

    pct = event.pct_change  # signed float
    diff = event.diff

    # Drop-only filter
    if watch.notify_only_on_drop and diff is not None and diff > 0:
        return _quiet("price rose; notify_only_on_drop is set")

    # Target price: only notify once the price is at or below the target.
    if (
        watch.target_price is not None
        and event.new_price is not None
        and event.new_price > watch.target_price
    ):
        return _quiet(
            f"price {event.new_price} is above the target price {watch.target_price}"
        )

    # Minimum change filters
    if (
        watch.min_change_pct is not None
        and pct is not None
        and abs(pct) < watch.min_change_pct
    ):
        return _quiet(
            f"change {abs(pct):.2f}% < min_change_pct {watch.min_change_pct}%"
        )
    if (
        watch.min_change_abs is not None
        and diff is not None
        and abs(diff) < watch.min_change_abs
    ):
        return _quiet(f"change {abs(diff)} < min_change_abs {watch.min_change_abs}")

    return event


# ---------------------------------------------------------------------------
# State mutation helpers
# ---------------------------------------------------------------------------

def _apply_event_to_ws(ws: dict, event: ChangeEvent) -> dict:
    """Return an updated copy of the per-watch state dict based on the event."""
    ws = dict(ws)  # shallow copy
    fetch = event.fetch
    now_iso = event.timestamp.astimezone().isoformat()

    ws["last_check_time"] = now_iso

    ct = event.type

    if ct == ChangeType.FAILED or ct == ChangeType.THRESHOLD_FAILURE:
        ws["consecutive_failures"] = int(ws.get("consecutive_failures", 0)) + 1
        ws["last_error"] = {
            "status": fetch.status.value,
            "http_status": fetch.http_status,
            "message": fetch.error_message,
        }
        return ws

    if ct == ChangeType.SANITY_DEFERRED:
        # Keep the stored price; count the deferral so a reading that
        # persists on the next run is believed.
        ws["sanity_deferred_count"] = int(ws.get("sanity_deferred_count", 0)) + 1
        return ws

    # Successful fetch — reset failure / sanity counters
    ws["consecutive_failures"] = 0
    ws["failure_notified"] = False
    ws["last_success_time"] = now_iso
    ws["sanity_deferred_count"] = 0
    ws.pop("last_error", None)

    price_moved = (
        fetch.price is not None
        and _decimal_or_none(ws.get("last_price")) != fetch.price
    )
    _update_price_fields(ws, fetch)
    if price_moved:
        _append_history(ws, fetch.price, now_iso)

    # The reference price is what the user was last told about. It moves on
    # first sight, on a notified price change and on recovery; a change that
    # a per-link filter held back leaves it alone.
    if ct in (ChangeType.FIRST_SEEN, ChangeType.PRICE_CHANGED, ChangeType.RECOVERED):
        if fetch.price is not None:
            ws["reference_price"] = str(fetch.price)
    elif "reference_price" not in ws and event.old_price is not None:
        ws["reference_price"] = str(event.old_price)
    return ws


def _update_price_fields(ws: dict, fetch: FetchResult) -> None:
    """Mutate ws in-place with current fetch price/stock/promo data."""
    if fetch.price is not None:
        ws["last_price"] = str(fetch.price)
    if fetch.currency is not None:
        ws["currency"] = fetch.currency
    if fetch.regular_price is not None:
        ws["regular_price"] = str(fetch.regular_price)
    if fetch.in_stock is not None:
        ws["in_stock"] = fetch.in_stock
    # A tiered / unreadable banner keeps the last readable promotion, so a
    # flapping banner does not produce "ended" / "started" emails.
    if fetch.promo is not None and not _promo_computable(fetch.promo):
        if _promo_from_dict(ws.get("promo")) is None:
            ws["promo"] = _promo_to_dict(fetch.promo)
        return
    ws["promo"] = _promo_to_dict(fetch.promo)
    ws["estimated_final_price"] = _str_or_none(fetch.estimated_final_price)


def _append_history(ws: dict, price: Optional[Decimal], iso_time: str) -> None:
    if price is None:
        return
    history: list = ws.setdefault("history", [])
    history.append({"t": iso_time, "price": str(price)})
    if len(history) > _HISTORY_CAP:
        del history[: len(history) - _HISTORY_CAP]


# ---------------------------------------------------------------------------
# Public write API
# ---------------------------------------------------------------------------

def apply(event: ChangeEvent) -> None:
    """Write updated state for a single event (atomic)."""
    data = load_state()
    ws = data["watches"].get(event.watch.id, {})
    data["watches"][event.watch.id] = _apply_event_to_ws(ws, event)
    _save_state(data)


def apply_many(events: list[ChangeEvent], *, email_failed: bool = False) -> None:
    """Apply multiple events atomically (one load, mutate all, one save).

    With ``email_failed=True`` the events that should have been emailed are
    held back (their state is left as it was), so the next run detects the
    same change again and retries the email.
    """
    if email_failed:
        events = [e for e in events if e.type not in NOTIFIABLE_TYPES]
    if not events:
        return
    data = load_state()
    for event in events:
        ws = data["watches"].get(event.watch.id, {})
        data["watches"][event.watch.id] = _apply_event_to_ws(ws, event)
    _save_state(data)


def mark_notified(events: list[ChangeEvent]) -> None:
    """Stamp last_notified_at and set failure_notified on THRESHOLD_FAILURE events.

    Called only after a successful email send so that email failures do not
    suppress future notifications. Events that were not emailed are ignored.
    """
    events = [e for e in events if e.type in NOTIFIABLE_TYPES]
    if not events:
        return
    data = load_state()
    for event in events:
        ws = data["watches"].setdefault(event.watch.id, {})
        ws["last_notified_at"] = event.timestamp.astimezone().isoformat()
        if event.type == ChangeType.THRESHOLD_FAILURE:
            ws["failure_notified"] = True
    _save_state(data)
