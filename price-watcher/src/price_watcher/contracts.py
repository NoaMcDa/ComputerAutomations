"""Interface contracts shared across all modules.

This file is the single source of truth for data shapes passed between the
watch-list, fetcher, state store, emailer, and scheduler. If you add a new field,
update this file first, then update the modules that produce or consume it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional, Protocol


# ---- Watch list ----

@dataclass
class Watch:
    """One URL being watched, as stored in the watch list."""
    id: str
    url: str
    name: Optional[str] = None
    selector: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    # Only notify when the price went down.
    notify_only_on_drop: bool = False
    # Ignore price changes smaller than this percentage / absolute amount.
    min_change_pct: Optional[float] = None
    min_change_abs: Optional[Decimal] = None
    # Only notify about a price change once the price is at or below this.
    target_price: Optional[Decimal] = None
    # Read site promotion banners (MyProtein) for this link. False = off.
    track_promo: bool = True
    # The item is excluded from site promotions: the banner's discount does
    # not apply to it, so no estimated final price is computed.
    excluded_from_promo: bool = False
    # Skipped by `check` while True.
    paused: bool = False


# ---- Fetcher ----

class FetchStatus(str, Enum):
    OK = "ok"
    UNREACHABLE = "unreachable"
    BLOCKED = "blocked"
    NOT_FOUND = "not_found"
    PRICE_NOT_FOUND = "price_not_found"


@dataclass
class PromoInfo:
    """A promotion read from a site banner (e.g. MyProtein)."""
    code: Optional[str]
    discount_pct: Decimal
    condition: Optional[str] = None
    source_text: Optional[str] = None


@dataclass
class FetchResult:
    """What the fetcher returns for one URL.

    Always returns a FetchResult, never raises; use status to distinguish outcomes.
    """
    status: FetchStatus
    final_url: str
    title: Optional[str] = None
    price: Optional[Decimal] = None
    currency: Optional[str] = None
    regular_price: Optional[Decimal] = None
    in_stock: Optional[bool] = None
    promo: Optional[PromoInfo] = None
    estimated_final_price: Optional[Decimal] = None
    http_status: Optional[int] = None
    error_message: Optional[str] = None
    raw_html_path: Optional[str] = None


# ---- Change detection ----

class ChangeType(str, Enum):
    FIRST_SEEN = "first_seen"
    NO_CHANGE = "no_change"
    PRICE_CHANGED = "price_changed"
    STOCK_CHANGED = "stock_changed"
    PROMO_CHANGED = "promo_changed"
    FAILED = "failed"
    THRESHOLD_FAILURE = "threshold_failure"
    RECOVERED = "recovered"
    SANITY_DEFERRED = "sanity_deferred"


# Change types that warrant an email.
NOTIFIABLE_TYPES = frozenset({
    ChangeType.PRICE_CHANGED,
    ChangeType.STOCK_CHANGED,
    ChangeType.PROMO_CHANGED,
    ChangeType.THRESHOLD_FAILURE,
    ChangeType.RECOVERED,
})


@dataclass
class ChangeEvent:
    """The outcome of comparing a new FetchResult against stored state."""
    type: ChangeType
    watch: Watch
    fetch: FetchResult
    old_price: Optional[Decimal] = None
    new_price: Optional[Decimal] = None
    pct_change: Optional[float] = None
    diff: Optional[Decimal] = None
    reason: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)
    # Secondary changes carried by the same event (e.g. a PRICE_CHANGED that
    # also went out of stock, or a RECOVERED whose price moved meanwhile).
    stock_changed: bool = False
    promo_changed: bool = False
    old_promo: Optional[PromoInfo] = None
    # The estimated final price stored before this run, so the email can say
    # what changed (e.g. "promotion ended").
    old_estimated_price: Optional[Decimal] = None
    # Set when the price is compared across a currency switch.
    old_currency: Optional[str] = None


# ---- Email ----

@dataclass
class EmailPayload:
    subject: str
    text_body: str
    html_body: str


# ---- Site-specific extractor plugin ----

class SiteExtractor(Protocol):
    """A plugin for a specific shop. Registered in `sites/__init__.py`.

    `applies_to` decides whether this extractor should run for a URL.
    `extract` is given the fetched HTML plus the generic-extractor's best guess,
    and returns an updated FetchResult (e.g. with promo info or a corrected price).
    """

    def applies_to(self, url: str) -> bool: ...

    def extract(self, html: str, url: str, base_result: FetchResult) -> FetchResult: ...
