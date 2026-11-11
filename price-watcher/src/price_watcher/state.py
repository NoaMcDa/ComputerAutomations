"""State store and change detection for Price Watcher.

Public API
----------
load_state()         -> dict
drop_watch(watch_id) -> None
evaluate(watch, fetch, cfg) -> ChangeEvent
apply(event)         -> None
apply_many(events, notification_sent=True) -> None
mark_notified(events) -> None
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from price_watcher.contracts import (
    ChangeEvent,
    ChangeType,
    FetchResult,
    FetchStatus,
    PromoInfo,
    Watch,
)
from price_watcher.config import CheckConfig
from price_watcher.paths import state_file

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

def load_state() -> dict:
    """Load the raw state dict from disk. Returns a default if the file is missing."""
    path = state_file()
    if not path.exists():
        return {"schema_version": _SCHEMA_VERSION, "watches": {}}
    with path.open("r", encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return {"schema_version": _SCHEMA_VERSION, "watches": {}}
    if "watches" not in data:
        data["watches"] = {}
    return data


def _save_state(data: dict) -> None:
    """Atomically write the state dict to disk."""
    path = state_file()
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def drop_watch(watch_id: str) -> None:
    """Remove the per-watch record when the watch is deleted from the watchlist."""
    data = load_state()
    data["watches"].pop(watch_id, None)
    _save_state(data)


# ---------------------------------------------------------------------------
# Change detection (pure-ish — reads state from disk, writes nothing)
# ---------------------------------------------------------------------------

def _baseline_price(ws: dict) -> Optional[Decimal]:
    """Reference price for change detection: the price last reported to the user
    (or first seen). Older state files have no ``baseline_price``; there the
    last observed price is the baseline."""
    raw = ws.get("baseline_price")
    if raw is None:
        raw = ws.get("last_price")
    return _decimal_or_none(raw)


def evaluate(watch: Watch, fetch: FetchResult, cfg: CheckConfig) -> ChangeEvent:
    """Decide the ChangeType for this fetch result against stored state.

    Does NOT write state. Call apply() or apply_many() afterward.

    Price, stock and promo changes are detected independently; one event
    carries all of them (``type`` is the most important one, the others are
    flagged via ``stock_changed`` / ``promo_changed``).
    """
    data = load_state()
    ws: dict = data["watches"].get(watch.id, {})
    now = datetime.now().astimezone()
    threshold = cfg.failure_threshold

    # ------------------------------------------------------------------ #
    # Fetch failed                                                         #
    # ------------------------------------------------------------------ #
    if fetch.status != FetchStatus.OK:
        consecutive = ws.get("consecutive_failures", 0) + 1
        has_prior_success = ws.get("last_success_time") is not None

        if not has_prior_success:
            return ChangeEvent(
                type=ChangeType.FAILED,
                watch=watch,
                fetch=fetch,
                reason=f"fetch failed with status {fetch.status}; no prior success",
                timestamp=now,
            )

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
            reason=f"fetch failed with status {fetch.status} (attempt {consecutive})",
            timestamp=now,
        )

    # ------------------------------------------------------------------ #
    # Fetch succeeded                                                      #
    # ------------------------------------------------------------------ #
    consecutive = ws.get("consecutive_failures", 0)
    failure_notified = ws.get("failure_notified", False)
    recovering = consecutive > 0 and failure_notified

    stored_price = _baseline_price(ws)

    # First seen
    if stored_price is None:
        return ChangeEvent(
            type=ChangeType.FIRST_SEEN,
            watch=watch,
            fetch=fetch,
            timestamp=now,
        )

    new_price = fetch.price  # may be None; handled below

    # Price is 0: the watch is effectively broken. Counted like a failed
    # fetch, so it escalates to THRESHOLD_FAILURE after `threshold` runs.
    if new_price is not None and new_price == Decimal("0"):
        attempt = consecutive + 1
        if attempt >= threshold and not failure_notified:
            return ChangeEvent(
                type=ChangeType.THRESHOLD_FAILURE,
                watch=watch,
                fetch=fetch,
                reason=(
                    f"המחיר הוצג כ-0 ב-{attempt} בדיקות ברצף "
                    f"(הסף: {threshold})"
                ),
                timestamp=now,
            )
        return ChangeEvent(
            type=ChangeType.SANITY_DEFERRED,
            watch=watch,
            fetch=fetch,
            reason=f"price is 0 (counted as failure {attempt} of {threshold})",
            timestamp=now,
        )

    # Currency sanity: defer while the switch is fresh; once the new currency
    # has persisted for `threshold` consecutive runs, accept it as the new
    # baseline and report it as a price change across currencies.
    stored_currency = ws.get("currency")
    currency_switched = bool(
        stored_currency
        and fetch.currency
        and fetch.currency != stored_currency
    )
    if currency_switched:
        held = ws.get("currency_hold_count", 0) + 1
        if held < threshold:
            return ChangeEvent(
                type=ChangeType.SANITY_DEFERRED,
                watch=watch,
                fetch=fetch,
                reason=(
                    f"currency changed from {stored_currency} to {fetch.currency} "
                    f"(run {held} of {threshold} before accepting)"
                ),
                timestamp=now,
            )

    # Price change calculation (not comparable across currencies)
    if new_price is not None and stored_price != Decimal("0") and not currency_switched:
        pct_change = float((new_price - stored_price) / stored_price * 100)
    else:
        pct_change = None

    # Large jump sanity check
    if pct_change is not None and abs(pct_change) > cfg.sanity_jump_pct:
        deferred_count = ws.get("sanity_deferred_count", 0)
        if deferred_count < 1:
            return ChangeEvent(
                type=ChangeType.SANITY_DEFERRED,
                watch=watch,
                fetch=fetch,
                reason=(
                    f"price jump of {pct_change:.1f}% exceeds sanity limit "
                    f"({cfg.sanity_jump_pct}%); deferring one cycle"
                ),
                timestamp=now,
            )
        # deferred_count >= 1: spike persisted — treat as real price change below

    # Stock comparison
    stored_in_stock = ws.get("in_stock")  # None means unknown
    stock_changed = (
        fetch.in_stock is not None
        and stored_in_stock is not None
        and fetch.in_stock != stored_in_stock
    )

    # Promo comparison
    stored_promo = _promo_from_dict(ws.get("promo"))
    promo_changed = _promos_differ(stored_promo, fetch.promo)

    price_changed = new_price is not None and (
        new_price != stored_price or currency_switched
    )

    if price_changed:
        ctype = ChangeType.PRICE_CHANGED
    elif stock_changed:
        ctype = ChangeType.STOCK_CHANGED
    elif promo_changed:
        ctype = ChangeType.PROMO_CHANGED
    else:
        ctype = ChangeType.NO_CHANGE

    event = ChangeEvent(
        type=ctype,
        watch=watch,
        fetch=fetch,
        old_price=stored_price if price_changed else None,
        new_price=new_price if price_changed else None,
        pct_change=pct_change if price_changed else None,
        diff=(new_price - stored_price) if price_changed and not currency_switched else None,
        reason=(
            f"currency changed from {stored_currency} to {fetch.currency}"
            if currency_switched else None
        ),
        timestamp=now,
        stock_changed=stock_changed,
        promo_changed=promo_changed,
        old_promo=stored_promo if promo_changed else None,
        old_currency=stored_currency if currency_switched else None,
    )

    # Recovery: the "working again" email also carries whatever changed
    # during the outage (per-watch filters do not apply to it).
    if recovering:
        return replace(
            event,
            type=ChangeType.RECOVERED,
            reason=f"recovered after {consecutive} consecutive failures",
        )

    # Apply per-watch filters
    event = _apply_filters(event, watch)
    return event


def _promos_differ(a: Optional[PromoInfo], b: Optional[PromoInfo]) -> bool:
    if a is None and b is None:
        return False
    if a is None or b is None:
        return True
    return a.code != b.code or a.discount_pct != b.discount_pct


def _apply_filters(event: ChangeEvent, watch: Watch) -> ChangeEvent:
    """Suppress the price part of PRICE_CHANGED if per-watch filters say so.

    The baseline price is not moved by a suppressed change (see
    _apply_event_to_ws), so small steps accumulate against the last reported
    price instead of creeping. This also holds for notify_only_on_drop: a rise
    does not become the new reference, later drops are measured against the
    last reported price. A stock/promo change in the same event is kept.
    """
    if event.type != ChangeType.PRICE_CHANGED:
        return event

    pct = event.pct_change  # signed float
    reason: Optional[str] = None

    # Drop-only filter
    if watch.notify_only_on_drop and pct is not None and pct > 0:
        reason = "price rose; notify_only_on_drop is set"

    # Minimum change filter
    elif (
        watch.min_change_pct is not None
        and pct is not None
        and abs(pct) < watch.min_change_pct
    ):
        reason = (
            f"change {abs(pct):.2f}% < min_change_pct "
            f"{watch.min_change_pct}%"
        )

    if reason is None:
        return event

    if event.stock_changed:
        new_type = ChangeType.STOCK_CHANGED
    elif event.promo_changed:
        new_type = ChangeType.PROMO_CHANGED
    else:
        new_type = ChangeType.NO_CHANGE
    return replace(
        event,
        type=new_type,
        old_price=None,
        new_price=None,
        pct_change=None,
        diff=None,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# State mutation helpers
# ---------------------------------------------------------------------------

# Change types that warrant an email (mirrors email_sender._NOTIFIABLE).
_NOTIFIABLE_TYPES = {
    ChangeType.PRICE_CHANGED,
    ChangeType.STOCK_CHANGED,
    ChangeType.PROMO_CHANGED,
    ChangeType.THRESHOLD_FAILURE,
    ChangeType.RECOVERED,
}

# Changes whose state update is held back when their email could not be sent,
# so the same change is detected (and emailed) again on the next run.
# THRESHOLD_FAILURE is still applied (failure counting); it is re-raised
# because failure_notified only gets set by mark_notified().
_HOLD_IF_UNSENT = {
    ChangeType.PRICE_CHANGED,
    ChangeType.STOCK_CHANGED,
    ChangeType.PROMO_CHANGED,
    ChangeType.RECOVERED,
}


def _apply_event_to_ws(ws: dict, event: ChangeEvent) -> dict:
    """Return an updated copy of the per-watch state dict based on the event."""
    ws = dict(ws)  # shallow copy
    fetch = event.fetch
    now_iso = event.timestamp.astimezone().isoformat()

    ws["last_check_time"] = now_iso

    ct = event.type

    if ct == ChangeType.FAILED or ct == ChangeType.THRESHOLD_FAILURE:
        ws["consecutive_failures"] = ws.get("consecutive_failures", 0) + 1
        return ws

    if ct == ChangeType.SANITY_DEFERRED:
        reason = event.reason or ""
        # Currency sanity: do NOT update stored price; count the hold
        if reason.startswith("currency changed"):
            ws["currency_hold_count"] = ws.get("currency_hold_count", 0) + 1
            return ws
        # Price-is-0 sanity: counts as a failed fetch
        if reason.startswith("price is 0"):
            ws["consecutive_failures"] = ws.get("consecutive_failures", 0) + 1
            return ws
        # Jump sanity: increment deferred count
        ws["sanity_deferred_count"] = ws.get("sanity_deferred_count", 0) + 1
        return ws

    # Successful fetch — reset failure / sanity counters
    ws["consecutive_failures"] = 0
    ws["failure_notified"] = False
    ws["last_success_time"] = now_iso
    ws["sanity_deferred_count"] = 0
    ws["currency_hold_count"] = 0

    previous_price = _decimal_or_none(ws.get("last_price"))
    if "baseline_price" not in ws and ws.get("last_price") is not None:
        # Older state file: pin the baseline before last_price moves.
        ws["baseline_price"] = ws["last_price"]

    _update_price_fields(ws, fetch)

    # History records every observed price change, reported or not.
    if ct == ChangeType.FIRST_SEEN or (
        fetch.price is not None and fetch.price != previous_price
    ):
        _append_history(ws, fetch.price, now_iso)

    # The baseline only moves when the price was reported (or first seen).
    if ct in (ChangeType.FIRST_SEEN, ChangeType.PRICE_CHANGED, ChangeType.RECOVERED):
        if ws.get("last_price") is not None:
            ws["baseline_price"] = ws["last_price"]

    return ws


def _update_price_fields(ws: dict, fetch: FetchResult) -> None:
    """Mutate ws in-place with current fetch price/stock/promo data.

    promo / regular_price / estimated_final_price describe the current page,
    so they are overwritten even when absent (a removed promo is cleared).
    """
    if fetch.price is not None:
        ws["last_price"] = str(fetch.price)
    if fetch.currency is not None:
        ws["currency"] = fetch.currency
    ws["regular_price"] = _str_or_none(fetch.regular_price)
    if fetch.in_stock is not None:
        ws["in_stock"] = fetch.in_stock
    ws["promo"] = _promo_to_dict(fetch.promo)
    ws["estimated_final_price"] = _str_or_none(fetch.estimated_final_price)


def _append_history(ws: dict, price: Optional[Decimal], iso_time: str) -> None:
    if price is None:
        return
    history: list = ws.setdefault("history", [])
    history.append({"t": iso_time, "price": str(price)})
    if len(history) > _HISTORY_CAP:
        del history[0]


# ---------------------------------------------------------------------------
# Public write API
# ---------------------------------------------------------------------------

def apply(event: ChangeEvent) -> None:
    """Write updated state for a single event (atomic)."""
    data = load_state()
    ws = data["watches"].get(event.watch.id, {})
    data["watches"][event.watch.id] = _apply_event_to_ws(ws, event)
    _save_state(data)


def apply_many(events: list[ChangeEvent], *, notification_sent: bool = True) -> None:
    """Apply multiple events atomically (one load, mutate all, one save).

    Pass ``notification_sent=False`` when the change email failed: notifiable
    changes are then not written (only ``last_check_time`` is bumped), so they
    are detected and emailed again on the next run.
    """
    if not events:
        return
    data = load_state()
    for event in events:
        ws = data["watches"].get(event.watch.id, {})
        if not notification_sent and event.type in _HOLD_IF_UNSENT:
            ws = dict(ws)
            ws["last_check_time"] = event.timestamp.astimezone().isoformat()
            data["watches"][event.watch.id] = ws
            continue
        data["watches"][event.watch.id] = _apply_event_to_ws(ws, event)
    _save_state(data)


def mark_notified(events: list[ChangeEvent]) -> None:
    """Stamp last_notified_at and set failure_notified on THRESHOLD_FAILURE events.

    Called only after a successful email send so that email failures do not
    suppress future notifications. Only events that were part of the email
    (notifiable types) are stamped.
    """
    events = [e for e in events if e.type in _NOTIFIABLE_TYPES]
    if not events:
        return
    data = load_state()
    for event in events:
        ws = data["watches"].setdefault(event.watch.id, {})
        ws["last_notified_at"] = event.timestamp.astimezone().isoformat()
        if event.type == ChangeType.THRESHOLD_FAILURE:
            ws["failure_notified"] = True
    _save_state(data)
