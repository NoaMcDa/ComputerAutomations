"""Flight price watching for El Al and Arkia (issue #10).

A flight watch is one pinned flight (airline, route, date, the flight the user
picked from a list) plus a window of +-15 days around the pinned date on the
same route. Each run:

1. checks the pinned flight (matched by flight number, then by departure time
   within a tolerance; otherwise "flight no longer listed");
2. checks the window (one fare-calendar request when the source offers one,
   otherwise one search per date, at most every ``window_interval_hours``);
3. stores the price per date and decides whether to email, using the rules
   in :func:`evaluate`. One combined email per watch per run.

Everything in this module except the two airline adapters at the bottom is
pure logic and covered by unit tests with a fake source. The adapters
(:class:`ElAlSource`, :class:`ArkiaSource`) use Playwright with URLs and
parsing that are UNVERIFIED against the live sites (no internet access while
this was written). When they cannot read a page they return a ``blocked`` /
``unsupported`` status instead of raising. See ``docs/flights.md``.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html import escape
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from price_watcher import logging_setup
from price_watcher.contracts import EmailPayload
from price_watcher.paths import data_dir

log = logging_setup.get(__name__)

WINDOW_RADIUS_DAYS = 15
DEFAULT_TIME_TOLERANCE_MIN = 60
DEFAULT_WINDOW_INTERVAL_HOURS = 6
DEFAULT_MIN_CHANGE_AMOUNT = Decimal("30")   # threshold is ON by default for flights
DEFAULT_MIN_CHANGE_PCT = Decimal("3")
FAILURE_ALERT_AFTER = 3                     # consecutive fetch problems before one alert
NEXT_CHEAPEST_COUNT = 3

# Day / source statuses
OK = "ok"
NO_FLIGHTS = "no_flights"       # no flight on the route that day (a state, not an error)
SOLD_OUT = "sold_out"           # flights exist but none bookable (a state, not an error)
BLOCKED = "blocked"             # bot protection / CAPTCHA / HTTP 403
UNSUPPORTED = "unsupported"     # adapter could not read the page (unverified parser) / no browser
ERROR = "error"
NOT_LISTED = "not_listed"       # pinned flight only: the picked flight is not in the results

FETCH_PROBLEMS = {BLOCKED, UNSUPPORTED, ERROR}
DEFINITIVE = {OK, NO_FLIGHTS, SOLD_OUT, NOT_LISTED}

_STATUS_HE = {
    OK: "תקין",
    NO_FLIGHTS: "אין טיסות ביום זה",
    SOLD_OUT: "אזלו המקומות",
    BLOCKED: "אתר חברת התעופה חסם את הבדיקה",
    UNSUPPORTED: "לא ניתן לקרוא את אתר חברת התעופה",
    ERROR: "שגיאה",
    NOT_LISTED: "הטיסה כבר לא מופיעה",
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FlightIdentity:
    """How the pinned flight is recognised on later checks (never typed by the user)."""
    flight_number: Optional[str]
    departure_time: str  # "HH:MM", local time at origin


@dataclass
class FlightWatch:
    id: str
    airline: str          # "elal" / "arkia"
    origin: str           # IATA, e.g. "TLV"
    destination: str
    pinned_date: date
    identity: FlightIdentity
    passengers: int = 1
    return_date: Optional[date] = None   # round trip only when stated; never moves
    min_change_amount: Decimal = DEFAULT_MIN_CHANGE_AMOUNT
    min_change_pct: Decimal = DEFAULT_MIN_CHANGE_PCT
    active: bool = True
    created_at: str = ""

    @property
    def label(self) -> str:
        num = self.identity.flight_number or self.identity.departure_time
        trip = f" ↩ {self.return_date.isoformat()}" if self.return_date else ""
        return (f"{AIRLINE_NAMES.get(self.airline, self.airline)} {num} "
                f"{self.origin}→{self.destination} {self.pinned_date.isoformat()}{trip}")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": "flight",
            "airline": self.airline,
            "origin": self.origin,
            "destination": self.destination,
            "pinned_date": self.pinned_date.isoformat(),
            "flight_number": self.identity.flight_number,
            "departure_time": self.identity.departure_time,
            "passengers": self.passengers,
            "return_date": self.return_date.isoformat() if self.return_date else None,
            "min_change_amount": str(self.min_change_amount),
            "min_change_pct": str(self.min_change_pct),
            "active": self.active,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FlightWatch":
        return cls(
            id=d["id"],
            airline=d["airline"],
            origin=d["origin"],
            destination=d["destination"],
            pinned_date=date.fromisoformat(d["pinned_date"]),
            identity=FlightIdentity(d.get("flight_number"), d["departure_time"]),
            passengers=int(d.get("passengers", 1)),
            return_date=date.fromisoformat(d["return_date"]) if d.get("return_date") else None,
            min_change_amount=Decimal(d.get("min_change_amount") or DEFAULT_MIN_CHANGE_AMOUNT),
            min_change_pct=Decimal(d.get("min_change_pct") or DEFAULT_MIN_CHANGE_PCT),
            active=bool(d.get("active", True)),
            created_at=d.get("created_at", ""),
        )


@dataclass(frozen=True)
class FlightOption:
    """One flight in a day's results. ``price`` = cheapest total fare for all passengers."""
    flight_number: Optional[str]
    departure_time: str          # "HH:MM"
    arrival_time: Optional[str]
    price: Optional[Decimal]
    currency: str = "ILS"
    sold_out: bool = False


@dataclass
class DayResult:
    day: date
    status: str
    options: list[FlightOption] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def cheapest(self) -> Optional[Decimal]:
        prices = [o.price for o in self.options if o.price is not None and not o.sold_out]
        return min(prices) if prices else None

    @property
    def day_status(self) -> str:
        """Collapse to ok / no_flights / sold_out (or the fetch problem)."""
        if self.status != OK:
            return self.status
        if not self.options:
            return NO_FLIGHTS
        return OK if self.cheapest is not None else SOLD_OUT


@dataclass
class CalendarResult:
    """Low-fare calendar: cheapest price per day (None = no flights / sold out)."""
    status: str
    prices: dict[date, Optional[Decimal]] = field(default_factory=dict)
    currency: str = "ILS"
    error: Optional[str] = None


class AirlineSource(ABC):
    """Pluggable data source for one airline."""

    code: str = ""
    name: str = ""

    @abstractmethod
    def search_day(self, origin: str, destination: str, day: date, passengers: int,
                   return_date: Optional[date] = None) -> DayResult:
        """All flights on one date. Must not raise; report problems as a status."""

    def fare_calendar(self, origin: str, destination: str, start: date, end: date,
                      passengers: int, return_date: Optional[date] = None) -> Optional[CalendarResult]:
        """Cheapest price per day for a range in one request, or None when the
        source has no calendar view (the window is then searched day by day)."""
        return None


# ---------------------------------------------------------------------------
# Window logic
# ---------------------------------------------------------------------------

def is_expired(watch: FlightWatch, today: date) -> bool:
    """The watch stops once the pinned date has passed."""
    return watch.pinned_date < today


def window_dates(pinned: date, today: date, radius: int = WINDOW_RADIUS_DAYS) -> list[date]:
    """Dates from pinned-radius to pinned+radius, skipping past dates.
    Empty once the pinned date itself has passed."""
    if pinned < today:
        return []
    start = max(pinned - timedelta(days=radius), today)
    end = pinned + timedelta(days=radius)
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


# ---------------------------------------------------------------------------
# Flight matching
# ---------------------------------------------------------------------------

def normalize_flight_number(num: Optional[str]) -> Optional[str]:
    """'LY 001' / 'ly1' / 'LY-0001' -> 'LY1'; 'IZ 321' -> 'IZ321'."""
    if not num:
        return None
    s = re.sub(r"[\s\-]", "", str(num)).upper()
    if s.isdigit():
        return str(int(s))
    m = re.fullmatch(r"([A-Z]{2,3}|[A-Z]\d|\d[A-Z])(\d{1,4})", s)
    if not m:
        return s or None
    return f"{m.group(1)}{int(m.group(2))}"


def time_to_minutes(hhmm: str) -> Optional[int]:
    m = re.search(r"(\d{1,2}):(\d{2})", hhmm or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return h * 60 + mi


def _time_diff(a: int, b: int) -> int:
    d = abs(a - b)
    return min(d, 1440 - d)


@dataclass(frozen=True)
class MatchResult:
    option: Optional[FlightOption]
    method: str   # "flight_number" / "departure_time" / "not_listed"


def match_flight(identity: FlightIdentity, options: list[FlightOption],
                 tolerance_minutes: int = DEFAULT_TIME_TOLERANCE_MIN) -> MatchResult:
    """Flight number first; then the closest departure time within tolerance."""
    want = normalize_flight_number(identity.flight_number)
    if want:
        for o in options:
            if normalize_flight_number(o.flight_number) == want:
                return MatchResult(o, "flight_number")
    target = time_to_minutes(identity.departure_time)
    if target is not None:
        best: Optional[tuple[int, FlightOption]] = None
        for o in options:
            t = time_to_minutes(o.departure_time)
            if t is None:
                continue
            diff = _time_diff(t, target)
            if diff <= tolerance_minutes and (best is None or diff < best[0]):
                best = (diff, o)
        if best:
            return MatchResult(best[1], "departure_time")
    return MatchResult(None, NOT_LISTED)


# ---------------------------------------------------------------------------
# Evaluation / notification rules (pure)
# ---------------------------------------------------------------------------

@dataclass
class PinnedSnapshot:
    status: str          # ok / not_listed / sold_out / no_flights / blocked / unsupported / error
    price: Optional[Decimal] = None
    currency: str = "ILS"
    method: Optional[str] = None
    option: Optional[FlightOption] = None
    error: Optional[str] = None


def snapshot_pinned(watch: FlightWatch, result: DayResult,
                    tolerance_minutes: int = DEFAULT_TIME_TOLERANCE_MIN) -> PinnedSnapshot:
    if result.status in FETCH_PROBLEMS:
        return PinnedSnapshot(result.status, error=result.error)
    if not result.options:
        return PinnedSnapshot(NO_FLIGHTS)
    m = match_flight(watch.identity, result.options, tolerance_minutes)
    if m.option is None:
        return PinnedSnapshot(NOT_LISTED, method=NOT_LISTED)
    if m.option.sold_out or m.option.price is None:
        return PinnedSnapshot(SOLD_OUT, method=m.method, option=m.option)
    return PinnedSnapshot(OK, m.option.price, m.option.currency, m.method, m.option)


def change_threshold(watch: FlightWatch, reference: Decimal) -> Decimal:
    """A change counts only when it is at least this big (both ₪ and % floors)."""
    return max(watch.min_change_amount, reference * watch.min_change_pct / 100)


@dataclass
class FlightEvaluation:
    watch: FlightWatch
    notify: bool = False
    reasons: list[str] = field(default_factory=list)
    pinned: Optional[PinnedSnapshot] = None
    prev_pinned_price: Optional[Decimal] = None
    prev_pinned_status: Optional[str] = None
    cheapest: Optional[tuple[date, Decimal]] = None
    prev_cheapest: Optional[tuple[date, Decimal]] = None
    next_cheapest: list[tuple[date, Decimal]] = field(default_factory=list)
    cheaper_than_pinned: list[tuple[date, Decimal]] = field(default_factory=list)
    expired: bool = False
    new_state: dict = field(default_factory=dict)


def _dec(v: Any) -> Optional[Decimal]:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def _money(v: Optional[Decimal], currency: str = "ILS") -> str:
    if v is None:
        return "—"
    sym = {"ILS": "₪", "USD": "$", "EUR": "€", "GBP": "£"}.get(currency, currency + " ")
    v = v.quantize(Decimal("1")) if v == v.to_integral_value() else v.quantize(Decimal("0.01"))
    return f"{sym}{v:,}"


def evaluate(watch: FlightWatch, prev: dict, pinned_result: Optional[DayResult],
             window: Optional[dict[date, DayResult]], today: date, now: datetime,
             tolerance_minutes: int = DEFAULT_TIME_TOLERANCE_MIN) -> FlightEvaluation:
    """Apply the notification rules. ``prev`` is this watch's stored state.

    Rules (one combined email per watch per run):
    * pinned flight: its price changed by at least the threshold
      (``max(min_change_amount, min_change_pct% of the old price)``);
    * pinned flight changed state between ok / not listed / sold out /
      no flights (reported as such, never as a price change);
    * window: the cheapest date changed (different date at a different price,
      or same date moving by at least the threshold);
    * window: a date (other than the pinned one) became cheaper than the
      pinned flight by at least the threshold;
    * after FAILURE_ALERT_AFTER consecutive fetch problems, one alert.
    The first run only records a baseline. ``window=None`` means the window
    was not re-checked this run; stored per-date prices are reused.
    """
    ev = FlightEvaluation(watch=watch)
    state = json.loads(json.dumps(prev or {}))
    ev.prev_pinned_status = (state.get("pinned") or {}).get("status")
    ev.prev_pinned_price = _dec((state.get("pinned") or {}).get("price"))

    if is_expired(watch, today):
        ev.expired = True
        state["expired_at"] = now.isoformat()
        ev.new_state = state
        return ev

    # The baseline exists once the pinned flight has had one definitive reading.
    first_run = ev.prev_pinned_status not in DEFINITIVE
    reasons: list[str] = []

    # ---- pinned flight -------------------------------------------------
    snap = snapshot_pinned(watch, pinned_result, tolerance_minutes) if pinned_result else None
    ev.pinned = snap
    pinned_state = dict(state.get("pinned") or {})
    if snap is not None and snap.status in FETCH_PROBLEMS:
        fails = int(state.get("consecutive_failures", 0)) + 1
        state["consecutive_failures"] = fails
        state["last_error"] = f"{snap.status}: {snap.error or ''}".strip()
        if fails == FAILURE_ALERT_AFTER:
            reasons.append(f"הבדיקה נכשלה {fails} פעמים ברציפות: {_STATUS_HE[snap.status]}"
                           + (f" ({snap.error})" if snap.error else ""))
    elif snap is not None:
        state["consecutive_failures"] = 0
        state.pop("last_error", None)
        if not first_run and ev.prev_pinned_status in DEFINITIVE and snap.status != ev.prev_pinned_status:
            if snap.status == OK:
                reasons.append(f"הטיסה זמינה שוב: {_money(snap.price, snap.currency)}")
            else:
                reasons.append(_STATUS_HE[snap.status])
        elif (snap.status == OK and ev.prev_pinned_status == OK
              and ev.prev_pinned_price is not None and snap.price is not None):
            delta = snap.price - ev.prev_pinned_price
            if abs(delta) >= change_threshold(watch, ev.prev_pinned_price):
                word = "ירד" if delta < 0 else "עלה"
                reasons.append(f"מחיר הטיסה {word}: {_money(ev.prev_pinned_price, snap.currency)} → "
                               f"{_money(snap.price, snap.currency)}")
        pinned_state = {
            "status": snap.status,
            "price": str(snap.price) if snap.price is not None else None,
            "currency": snap.currency,
            "method": snap.method,
            "checked_at": now.isoformat(),
        }
        if snap.option is not None:
            pinned_state["seen_flight_number"] = snap.option.flight_number
            pinned_state["seen_departure_time"] = snap.option.departure_time
    if pinned_state:
        state["pinned"] = pinned_state

    # ---- per-date prices -----------------------------------------------
    valid = {d.isoformat() for d in window_dates(watch.pinned_date, today)}
    dates: dict[str, dict] = {k: v for k, v in (state.get("dates") or {}).items() if k in valid}
    updates: dict[date, DayResult] = dict(window or {})
    if pinned_result is not None:
        updates[watch.pinned_date] = pinned_result
    for d, res in updates.items():
        key = d.isoformat()
        if key not in valid or res.status in FETCH_PROBLEMS:
            continue  # keep the last known value for that date
        dates[key] = {
            "status": res.day_status,
            "price": str(res.cheapest) if res.cheapest is not None else None,
            "checked_at": now.isoformat(),
        }
    state["dates"] = dict(sorted(dates.items()))
    if window is not None:
        state["last_window_check"] = now.isoformat()

    priced = sorted(
        ((date.fromisoformat(k), _dec(v.get("price"))) for k, v in dates.items()
         if v.get("status") == OK and _dec(v.get("price")) is not None),
        key=lambda t: (t[1], t[0]),
    )
    if priced:
        ev.cheapest = priced[0]
        ev.next_cheapest = priced[1:1 + NEXT_CHEAPEST_COUNT]

    prev_wc = state.get("window_cheapest")
    if prev_wc and prev_wc.get("date") and _dec(prev_wc.get("price")) is not None:
        ev.prev_cheapest = (date.fromisoformat(prev_wc["date"]), _dec(prev_wc["price"]))
    if ev.cheapest and ev.prev_cheapest and not first_run:
        (nd, np_), (od, op) = ev.cheapest, ev.prev_cheapest
        moved_date = nd != od and np_ != op
        moved_price = abs(np_ - op) >= change_threshold(watch, op)
        if moved_date or moved_price:
            reasons.append(f"התאריך הזול ביותר בחלון השתנה: {od.strftime('%d/%m')} {_money(op)} → "
                           f"{nd.strftime('%d/%m')} {_money(np_)}")
    if ev.cheapest:
        state["window_cheapest"] = {"date": ev.cheapest[0].isoformat(), "price": str(ev.cheapest[1])}

    # ---- dates cheaper than the pinned flight -------------------------
    pinned_price = snap.price if snap is not None and snap.status == OK else None
    if pinned_price is None and ev.prev_pinned_status == OK and (snap is None or snap.status in FETCH_PROBLEMS):
        pinned_price = ev.prev_pinned_price
    cheaper: list[tuple[date, Decimal]] = []
    if pinned_price is not None:
        thr = change_threshold(watch, pinned_price)
        cheaper = [(d, p) for d, p in priced if d != watch.pinned_date and pinned_price - p >= thr]
    ev.cheaper_than_pinned = cheaper
    prev_cheaper = set(state.get("cheaper_than_pinned") or [])
    newly = [(d, p) for d, p in cheaper if d.isoformat() not in prev_cheaper]
    if newly and not first_run:
        listed = ", ".join(f"{d.strftime('%d/%m')} {_money(p)}" for d, p in newly[:5])
        reasons.append(f"תאריכים זולים יותר מהטיסה שלך: {listed}")
    state["cheaper_than_pinned"] = [d.isoformat() for d, _ in cheaper]

    ev.reasons = reasons
    ev.notify = bool(reasons)
    ev.new_state = state
    return ev


def window_due(prev: dict, now: datetime, interval_hours: float = DEFAULT_WINDOW_INTERVAL_HOURS) -> bool:
    last = (prev or {}).get("last_window_check")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    if last_dt.tzinfo is None and now.tzinfo is not None:
        last_dt = last_dt.replace(tzinfo=now.tzinfo)
    return now - last_dt >= timedelta(hours=interval_hours)


def check_watch(watch: FlightWatch, source: AirlineSource, prev: dict, today: date, now: datetime,
                window_interval_hours: float = DEFAULT_WINDOW_INTERVAL_HOURS,
                tolerance_minutes: int = DEFAULT_TIME_TOLERANCE_MIN) -> FlightEvaluation:
    """Fetch what is due for one watch and evaluate it. Never raises on source errors."""
    if is_expired(watch, today):
        return evaluate(watch, prev, None, None, today, now, tolerance_minutes)

    def _safe_day(d: date) -> DayResult:
        try:
            return source.search_day(watch.origin, watch.destination, d, watch.passengers, watch.return_date)
        except Exception as exc:  # an adapter must never break the run
            return DayResult(d, ERROR, error=f"{type(exc).__name__}: {exc}"[:200])

    pinned_result = _safe_day(watch.pinned_date)
    window: Optional[dict[date, DayResult]] = None
    if window_due(prev, now, window_interval_hours) and pinned_result.status not in (BLOCKED,):
        dates = [d for d in window_dates(watch.pinned_date, today) if d != watch.pinned_date]
        window = {}
        cal = None
        if dates:
            try:
                cal = source.fare_calendar(watch.origin, watch.destination, dates[0], dates[-1],
                                           watch.passengers, watch.return_date)
            except Exception as exc:
                log.warning("Fare calendar failed: %s", exc)
                cal = None
        if cal is not None and cal.status == OK:
            for d in dates:
                if d in cal.prices:
                    p = cal.prices[d]
                    window[d] = DayResult(d, OK, [FlightOption(None, "", None, p, cal.currency)] if p is not None else [])
        else:
            for d in dates:
                res = _safe_day(d)
                window[d] = res
                if res.status == BLOCKED:
                    break  # stop hammering a site that already blocked us
    return evaluate(watch, prev, pinned_result, window, today, now, tolerance_minutes)


# ---------------------------------------------------------------------------
# Email (one per watch)
# ---------------------------------------------------------------------------

def build_email(ev: FlightEvaluation) -> EmailPayload:
    w = ev.watch
    cur = ev.pinned.currency if ev.pinned else "ILS"
    subject = f"טיסה: {w.label} — " + "; ".join(ev.reasons)[:140]
    pinned_line = "—"
    if ev.pinned is not None:
        pinned_line = (_money(ev.pinned.price, cur) if ev.pinned.status == OK
                       else _STATUS_HE.get(ev.pinned.status, ev.pinned.status))
    lines = [w.label, "=" * 50, ""]
    lines += [f"• {r}" for r in ev.reasons]
    lines += ["", f"מחיר הטיסה שבחרת: {pinned_line}"]
    if ev.cheapest:
        lines.append(f"התאריך הזול ביותר בחלון (±{WINDOW_RADIUS_DAYS} יום): "
                     f"{ev.cheapest[0].strftime('%d/%m/%Y')} — {_money(ev.cheapest[1], cur)}")
    if ev.next_cheapest:
        lines.append("הבאים בתור: " + ", ".join(f"{d.strftime('%d/%m')} {_money(p, cur)}" for d, p in ev.next_cheapest))
    lines += ["", f"נוסעים: {w.passengers}" + (f" · חזור: {w.return_date.strftime('%d/%m/%Y')}" if w.return_date else " · כיוון אחד"),
              "", "— מעקב מחירים"]
    text = "\n".join(lines)

    rows = "".join(f"<li>{escape(r)}</li>" for r in ev.reasons)
    nxt = "".join(f'<li><span dir="ltr">{d.strftime("%d/%m")}</span> — <span dir="ltr">{escape(_money(p, cur))}</span></li>'
                  for d, p in ev.next_cheapest)
    html = (
        '<!DOCTYPE html><html dir="rtl" lang="he"><head><meta charset="utf-8"></head>'
        '<body dir="rtl" style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Arial,sans-serif;'
        'color:#111827;text-align:right">'
        f'<h2 style="font-size:18px">{escape(w.label)}</h2>'
        f'<ul>{rows}</ul>'
        f'<p>מחיר הטיסה שבחרת: <b dir="ltr">{escape(pinned_line)}</b></p>'
        + (f'<p>התאריך הזול ביותר בחלון: <b dir="ltr">{ev.cheapest[0].strftime("%d/%m/%Y")}</b> — '
           f'<b dir="ltr">{escape(_money(ev.cheapest[1], cur))}</b></p>' if ev.cheapest else "")
        + (f'<p>הבאים בתור:</p><ul>{nxt}</ul>' if nxt else "")
        + '<p style="color:#6b7280;font-size:12px">— מעקב מחירים</p></body></html>'
    )
    return EmailPayload(subject=subject, text_body=text, html_body=html)


# ---------------------------------------------------------------------------
# Storage: flight watches + per-watch state (atomic JSON files)
# ---------------------------------------------------------------------------

def watches_file() -> Path:
    return data_dir() / "flight_watches.json"


def state_file() -> Path:
    return data_dir() / "flights_state.json"


def _read_json(p: Path, default: dict) -> dict:
    if not p.exists():
        return default
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else default
    except (OSError, json.JSONDecodeError):
        log.warning("%s unreadable; using defaults", p.name)
        return default


def _write_json(p: Path, data: dict) -> None:
    tmp = p.with_name(p.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)


def load_watches() -> list[FlightWatch]:
    data = _read_json(watches_file(), {"schema_version": 1, "watches": []})
    out = []
    for d in data.get("watches", []):
        try:
            out.append(FlightWatch.from_dict(d))
        except (KeyError, ValueError, InvalidOperation) as exc:
            log.warning("Skipping malformed flight watch %r: %s", d.get("id"), exc)
    return out


def save_watches(watches: list[FlightWatch]) -> None:
    _write_json(watches_file(), {"schema_version": 1, "watches": [w.to_dict() for w in watches]})


def load_state() -> dict:
    data = _read_json(state_file(), {"schema_version": 1, "watches": {}})
    data.setdefault("watches", {})
    return data


def save_state(data: dict) -> None:
    _write_json(state_file(), data)


def remove_watch(watch_id: str) -> bool:
    watches = load_watches()
    keep = [w for w in watches if w.id != watch_id]
    if len(keep) == len(watches):
        return False
    save_watches(keep)
    st = load_state()
    st["watches"].pop(watch_id, None)
    save_state(st)
    return True


# ---------------------------------------------------------------------------
# Adding a watch (pick-from-a-list)
# ---------------------------------------------------------------------------

class FlightSearchError(RuntimeError):
    pass


def bookable_options(result: DayResult) -> list[FlightOption]:
    return sorted(result.options, key=lambda o: (time_to_minutes(o.departure_time) or 0))


def add_flight(source: AirlineSource, origin: str, destination: str, day: date,
               choose: Callable[[list[FlightOption]], int], passengers: int = 1,
               return_date: Optional[date] = None,
               min_change_amount: Decimal = DEFAULT_MIN_CHANGE_AMOUNT,
               today: Optional[date] = None) -> FlightWatch:
    """Load the day's flights, let ``choose`` pick one (0-based index), store the watch."""
    today = today or date.today()
    if day < today:
        raise FlightSearchError("התאריך כבר עבר")
    if return_date is not None and return_date < day:
        raise FlightSearchError("תאריך החזור מוקדם מתאריך היציאה")
    if passengers < 1:
        raise FlightSearchError("מספר הנוסעים חייב להיות לפחות 1")
    origin, destination = origin.strip().upper(), destination.strip().upper()
    result = source.search_day(origin, destination, day, passengers, return_date)
    if result.status != OK:
        raise FlightSearchError(f"{_STATUS_HE.get(result.status, result.status)}"
                                + (f": {result.error}" if result.error else ""))
    options = bookable_options(result)
    if not options:
        raise FlightSearchError(_STATUS_HE[NO_FLIGHTS])
    idx = choose(options)
    if not 0 <= idx < len(options):
        raise FlightSearchError("בחירה לא תקינה")
    picked = options[idx]
    watch = FlightWatch(
        id=uuid.uuid4().hex[:8],
        airline=source.code,
        origin=origin,
        destination=destination,
        pinned_date=day,
        identity=FlightIdentity(normalize_flight_number(picked.flight_number), picked.departure_time),
        passengers=passengers,
        return_date=return_date,
        min_change_amount=min_change_amount,
        created_at=datetime.now().astimezone().isoformat(),
    )
    watches = load_watches()
    watches.append(watch)
    save_watches(watches)
    return watch


# ---------------------------------------------------------------------------
# Run all flight watches
# ---------------------------------------------------------------------------

def run_flight_checks(dry_run: bool = False,
                      source_for: Optional[Callable[[str], AirlineSource]] = None,
                      send: Optional[Callable[[EmailPayload], bool]] = None,
                      today: Optional[date] = None, now: Optional[datetime] = None,
                      echo: Callable[[str], None] = print) -> int:
    """Check every active flight watch. Returns 0, or 1 if an email failed to send."""
    source_for = source_for or get_source
    send = send or _send_email
    now = now or datetime.now().astimezone()
    today = today or now.date()
    watches = load_watches()
    st = load_state()
    rc = 0
    changed_watches = False
    for w in watches:
        if not w.active:
            continue
        prev = st["watches"].get(w.id, {})
        ev = check_watch(w, source_for(w.airline), prev, today, now)
        if ev.expired:
            echo(f"{w.label}: the pinned date has passed; watch stopped")
            w.active = False
            changed_watches = True
            st["watches"][w.id] = ev.new_state
            continue
        status = ev.pinned.status if ev.pinned else "?"
        echo(f"{w.label}: pinned {status} {_money(ev.pinned.price) if ev.pinned and ev.pinned.price else ''}".rstrip())
        if ev.notify:
            payload = build_email(ev)
            if dry_run:
                echo(f"  WOULD SEND: {payload.subject}")
                continue
            if not send(payload):
                rc = 1
                continue  # keep old state so the change is reported again next run
        if not dry_run:
            st["watches"][w.id] = ev.new_state
    if not dry_run:
        save_state(st)
        if changed_watches:
            save_watches(watches)
    return rc


def _send_email(payload: EmailPayload) -> bool:
    import smtplib

    from price_watcher import config as _config
    from price_watcher.email_sender import _send_via_smtp
    try:
        _send_via_smtp(payload, _config.load().email)
        return True
    except (smtplib.SMTPException, OSError, RuntimeError) as exc:
        log.error("Flight email send failed: %s", type(exc).__name__)
        return False


# ---------------------------------------------------------------------------
# Airline adapters (Playwright) — URLs and parsing UNVERIFIED live
# ---------------------------------------------------------------------------

AIRLINE_NAMES = {"elal": "אל על", "arkia": "ארקיע"}

_BOT_MARKERS = (
    "captcha", "px-captcha", "_incapsula_resource", "incapsula incident",
    "access denied", "request unsuccessful", "cf-chl", "attention required",
    "are you a robot", "verify you are human", "pardon our interruption",
)
_NO_FLIGHT_MARKERS = (
    "no flights", "no available flights", "לא נמצאו טיסות", "אין טיסות",
)

_FN_KEYS = ("flightNumber", "flight_number", "marketingFlightNumber", "flightNo", "FlightNumber")
_DEP_KEYS = ("departureTime", "departure_time", "departureDateTime", "departure", "DepartureTime", "std")
_ARR_KEYS = ("arrivalTime", "arrival_time", "arrivalDateTime", "arrival", "ArrivalTime", "sta")
_PRICE_KEYS = ("totalPrice", "total_price", "price", "lowestPrice", "amount", "TotalPrice", "fare")
_CARRIER_KEYS = ("carrierCode", "marketingCarrier", "airlineCode", "carrier")


def detect_bot_wall(html: str, http_status: Optional[int] = None) -> bool:
    if http_status in (403, 429):
        return True
    low = (html or "").lower()
    return any(m in low for m in _BOT_MARKERS)


def _scalar_price(v: Any) -> tuple[Optional[Decimal], Optional[str]]:
    if isinstance(v, dict):
        cur = v.get("currency") or v.get("currencyCode")
        for k in ("total", "amount", "value", "totalAmount"):
            if k in v:
                p, _ = _scalar_price(v[k])
                return p, cur
        return None, cur
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return Decimal(str(v)), None
    if isinstance(v, str):
        m = re.search(r"\d[\d,]*(?:\.\d+)?", v)
        if m:
            try:
                return Decimal(m.group(0).replace(",", "")), None
            except InvalidOperation:
                return None, None
    return None, None


def _hhmm(v: Any) -> Optional[str]:
    if not isinstance(v, str):
        return None
    m = re.search(r"(?:T|\b)(\d{1,2}):(\d{2})", v)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def options_from_payloads(payloads: Iterable[Any], day: Optional[date] = None,
                          default_currency: str = "ILS") -> list[FlightOption]:
    """Generic walker over captured JSON API responses: any object with a
    flight number, a departure time and a price is taken as one flight.
    The real field names of El Al / Arkia's APIs are UNVERIFIED; the key lists
    above are educated guesses and the first thing to fix after the survey."""
    found: dict[tuple[Optional[str], str], FlightOption] = {}
    stack = list(payloads)
    while stack:
        cur = stack.pop()
        if isinstance(cur, list):
            stack.extend(cur)
            continue
        if not isinstance(cur, dict):
            continue
        stack.extend(cur.values())
        fn = next((cur[k] for k in _FN_KEYS if cur.get(k) not in (None, "")), None)
        dep_raw = next((cur[k] for k in _DEP_KEYS if isinstance(cur.get(k), str)), None)
        dep = _hhmm(dep_raw)
        if fn is None or dep is None:
            continue
        if day is not None and isinstance(dep_raw, str) and re.match(r"\d{4}-\d{2}-\d{2}", dep_raw):
            if dep_raw[:10] != day.isoformat():
                continue
        price, cur_code = None, None
        for k in _PRICE_KEYS:
            if k in cur:
                price, cur_code = _scalar_price(cur[k])
                if price is not None:
                    break
        carrier = next((str(cur[k]) for k in _CARRIER_KEYS if isinstance(cur.get(k), str)), "")
        number = str(fn)
        if carrier and not number.upper().startswith(carrier.upper()):
            number = carrier + number
        sold_out = bool(cur.get("soldOut") or cur.get("sold_out") or cur.get("isSoldOut"))
        opt = FlightOption(
            flight_number=normalize_flight_number(number),
            departure_time=dep,
            arrival_time=_hhmm(next((cur[k] for k in _ARR_KEYS if isinstance(cur.get(k), str)), None)),
            price=None if sold_out else price,
            currency=str(cur.get("currency") or cur_code or default_currency),
            sold_out=sold_out or price is None,
        )
        key = (opt.flight_number, opt.departure_time)
        old = found.get(key)
        if old is None or (opt.price is not None and (old.price is None or opt.price < old.price)):
            found[key] = opt  # keep the cheapest fare per flight
    return list(found.values())


class PlaywrightAirlineSource(AirlineSource):
    """Load the airline's results page in headless Chromium, capture its JSON
    API responses and read flights from them. UNVERIFIED live."""

    currency = "ILS"

    def search_url(self, origin: str, destination: str, day: date, passengers: int,
                   return_date: Optional[date]) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def search_day(self, origin: str, destination: str, day: date, passengers: int,
                   return_date: Optional[date] = None) -> DayResult:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return DayResult(day, UNSUPPORTED, error="Playwright is not installed")
        url = self.search_url(origin, destination, day, passengers, return_date)
        payloads: list[Any] = []

        def _on_response(resp: Any) -> None:
            try:
                if "json" in (resp.headers.get("content-type") or "") and len(payloads) < 60:
                    payloads.append(resp.json())
            except Exception:
                pass

        try:
            with sync_playwright() as pw:
                try:
                    browser = pw.chromium.launch(headless=True,
                                                 args=["--disable-blink-features=AutomationControlled"])
                except Exception as exc:
                    return DayResult(day, UNSUPPORTED, error=f"Chromium not available: {type(exc).__name__}")
                try:
                    page = browser.new_context(locale="he-IL", viewport={"width": 1366, "height": 900}).new_page()
                    page.on("response", _on_response)
                    resp = page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=30_000)
                    except Exception:
                        pass
                    html = page.content()
                    status = resp.status if resp else None
                finally:
                    browser.close()
        except Exception as exc:
            return DayResult(day, ERROR, error=f"{type(exc).__name__}: {exc}"[:200])
        return self.interpret(day, html, payloads, status)

    def interpret(self, day: date, html: str, payloads: list[Any], http_status: Optional[int]) -> DayResult:
        """Pure: turn a loaded page + captured JSON into a DayResult."""
        if detect_bot_wall(html, http_status):
            return DayResult(day, BLOCKED, error=f"{self.name}: bot protection / CAPTCHA"
                             + (f" (HTTP {http_status})" if http_status else ""))
        options = options_from_payloads(payloads, day, self.currency)
        if options:
            return DayResult(day, OK, options)
        low = (html or "").lower()
        if any(m in low for m in _NO_FLIGHT_MARKERS):
            return DayResult(day, OK, [])  # -> no_flights state
        return DayResult(day, UNSUPPORTED,
                         error=f"{self.name}: results not recognised (adapter unverified, see docs/flights.md)")


class ElAlSource(PlaywrightAirlineSource):
    code = "elal"
    name = "El Al"

    def search_url(self, origin, destination, day, passengers, return_date):
        # UNVERIFIED: guessed deep-link shape for El Al's booking engine.
        params = (f"journeyType={'RT' if return_date else 'OW'}&origin={origin}&destination={destination}"
                  f"&departureDate={day.isoformat()}&adults={passengers}")
        if return_date:
            params += f"&returnDate={return_date.isoformat()}"
        return f"https://www.elal.com/heb/booking/flights?{params}"


class ArkiaSource(PlaywrightAirlineSource):
    code = "arkia"
    name = "Arkia"

    def search_url(self, origin, destination, day, passengers, return_date):
        # UNVERIFIED: guessed deep-link shape for Arkia's booking engine.
        params = (f"from={origin}&to={destination}&depart={day.isoformat()}&adults={passengers}"
                  + (f"&return={return_date.isoformat()}" if return_date else "&oneway=true"))
        return f"https://www.arkia.co.il/he/flights-results?{params}"


_SOURCES: dict[str, type[AirlineSource]] = {"elal": ElAlSource, "arkia": ArkiaSource}


def get_source(code: str) -> AirlineSource:
    try:
        return _SOURCES[code.lower()]()
    except KeyError:
        raise ValueError(f"Unknown airline {code!r}; choose one of: {', '.join(_SOURCES)}") from None
