"""State store and change detection for Price Watcher.

Public API
----------
load_state()         -> dict
drop_watch(watch_id) -> None
evaluate(watch, fetch, cfg) -> ChangeEvent
apply(event)         -> None
apply_many(events)   -> None
mark_notified(events) -> None
"""

from __future__ import annotations

import dataclasses
import json
import os
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

def evaluate(watch: Watch, fetch: FetchResult, cfg: CheckConfig) -> ChangeEvent:
    """Decide the ChangeType for this fetch result against stored state.

    Does NOT write state. Call apply() or apply_many() afterward.
    """
    data = load_state()
    ws: dict = data["watches"].get(watch.id, {})
    now = datetime.now().astimezone()

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

        threshold = cfg.failure_threshold
        failure_notified = ws.get("failure_notified", False)

        if consecutive > threshold and not failure_notified:
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

    # A fetch that could not read the currency (e.g. a blocked Shopify page
    # rescued through its product JSON) keeps the watch's stored currency, so
    # emails still show the symbol.
    if fetch.currency is None and ws.get("currency"):
        fetch = dataclasses.replace(fetch, currency=ws["currency"])

    # Recovery
    if consecutive > 0 and failure_notified:
        return ChangeEvent(
            type=ChangeType.RECOVERED,
            watch=watch,
            fetch=fetch,
            reason=f"recovered after {consecutive} consecutive failures",
            timestamp=now,
        )

    stored_price_str = ws.get("last_price")

    # First seen
    if stored_price_str is None:
        return ChangeEvent(
            type=ChangeType.FIRST_SEEN,
            watch=watch,
            fetch=fetch,
            timestamp=now,
        )

    stored_price = Decimal(stored_price_str)
    new_price = fetch.price  # may be None; handled below

    # Currency sanity
    stored_currency = ws.get("currency")
    if (
        stored_currency
        and fetch.currency
        and fetch.currency != stored_currency
    ):
        return ChangeEvent(
            type=ChangeType.SANITY_DEFERRED,
            watch=watch,
            fetch=fetch,
            reason=f"currency changed from {stored_currency} to {fetch.currency}",
            timestamp=now,
        )

    # Price is 0 sanity
    if new_price is not None and new_price == Decimal("0"):
        return ChangeEvent(
            type=ChangeType.SANITY_DEFERRED,
            watch=watch,
            fetch=fetch,
            reason="price is 0",
            timestamp=now,
        )

    # Price change calculation
    if new_price is not None and stored_price != Decimal("0"):
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
        # (fall through to PRICE_CHANGED)

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

    # No change
    if (
        new_price == stored_price
        and not stock_changed
        and not promo_changed
    ):
        return ChangeEvent(
            type=ChangeType.NO_CHANGE,
            watch=watch,
            fetch=fetch,
            timestamp=now,
        )

    # Stock changed (price same)
    if stock_changed and new_price == stored_price and not promo_changed:
        return ChangeEvent(
            type=ChangeType.STOCK_CHANGED,
            watch=watch,
            fetch=fetch,
            timestamp=now,
        )

    # Promo changed (displayed price same, stock same)
    if promo_changed and new_price == stored_price and not stock_changed:
        return ChangeEvent(
            type=ChangeType.PROMO_CHANGED,
            watch=watch,
            fetch=fetch,
            timestamp=now,
        )

    # Price changed (or combined price+stock/promo change)
    diff = (new_price - stored_price) if (new_price is not None) else None
    event = ChangeEvent(
        type=ChangeType.PRICE_CHANGED,
        watch=watch,
        fetch=fetch,
        old_price=stored_price,
        new_price=new_price,
        pct_change=pct_change,
        diff=diff,
        timestamp=now,
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
    """Downgrade PRICE_CHANGED to NO_CHANGE if per-watch filters say so."""
    if event.type != ChangeType.PRICE_CHANGED:
        return event

    pct = event.pct_change  # signed float

    # Drop-only filter
    if watch.notify_only_on_drop and pct is not None and pct > 0:
        return ChangeEvent(
            type=ChangeType.NO_CHANGE,
            watch=event.watch,
            fetch=event.fetch,
            timestamp=event.timestamp,
            reason="price rose; notify_only_on_drop is set",
        )

    # Minimum change filter
    if (
        watch.min_change_pct is not None
        and pct is not None
        and abs(pct) < watch.min_change_pct
    ):
        return ChangeEvent(
            type=ChangeType.NO_CHANGE,
            watch=event.watch,
            fetch=event.fetch,
            timestamp=event.timestamp,
            reason=(
                f"change {abs(pct):.2f}% < min_change_pct "
                f"{watch.min_change_pct}%"
            ),
        )

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
        ws["consecutive_failures"] = ws.get("consecutive_failures", 0) + 1
        return ws

    if ct == ChangeType.SANITY_DEFERRED:
        # Currency sanity: do NOT update stored price
        if event.reason and "currency changed" in event.reason:
            return ws
        # Price-is-0 sanity: do not update
        if event.reason and "price is 0" in event.reason:
            return ws
        # Jump sanity: increment deferred count
        ws["sanity_deferred_count"] = ws.get("sanity_deferred_count", 0) + 1
        return ws

    # Successful fetch — reset failure counters
    ws["consecutive_failures"] = 0
    ws["failure_notified"] = False
    ws["last_success_time"] = now_iso

    if ct == ChangeType.RECOVERED:
        # Update price/stock/promo from the successful fetch but no history entry
        _update_price_fields(ws, fetch)
        return ws

    if ct == ChangeType.FIRST_SEEN:
        _update_price_fields(ws, fetch)
        _append_history(ws, fetch.price, now_iso)
        return ws

    if ct == ChangeType.NO_CHANGE:
        # Still update stock/promo/timestamps but not price history
        _update_price_fields(ws, fetch)
        # Reset sanity deferred count on a stable no-change
        ws["sanity_deferred_count"] = 0
        return ws

    if ct == ChangeType.STOCK_CHANGED:
        ws["in_stock"] = fetch.in_stock
        ws["last_success_time"] = now_iso
        ws["sanity_deferred_count"] = 0
        return ws

    if ct == ChangeType.PROMO_CHANGED:
        ws["promo"] = _promo_to_dict(fetch.promo)
        ws["sanity_deferred_count"] = 0
        return ws

    if ct == ChangeType.PRICE_CHANGED:
        _update_price_fields(ws, fetch)
        _append_history(ws, fetch.price, now_iso)
        ws["sanity_deferred_count"] = 0
        return ws

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
    if fetch.promo is not None:
        ws["promo"] = _promo_to_dict(fetch.promo)
    elif "promo" not in ws:
        ws["promo"] = None
    if fetch.estimated_final_price is not None:
        ws["estimated_final_price"] = str(fetch.estimated_final_price)


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


def apply_many(events: list[ChangeEvent]) -> None:
    """Apply multiple events atomically (one load, mutate all, one save)."""
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
    suppress future notifications.
    """
    if not events:
        return
    data = load_state()
    for event in events:
        ws = data["watches"].setdefault(event.watch.id, {})
        ws["last_notified_at"] = event.timestamp.astimezone().isoformat()
        if event.type == ChangeType.THRESHOLD_FAILURE:
            ws["failure_notified"] = True
    _save_state(data)
