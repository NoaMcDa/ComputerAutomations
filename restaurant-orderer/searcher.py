"""Availability search for Ontopo and Tabit behind one adapter interface.

IMPORTANT: the platform endpoints, request bodies and response shapes used here
are based on the public web clients as best understood at development time and
have NOT been verified live (no network access while this was written). All
URLs, paths and venue identifiers live in ``config.json`` so they can be fixed
without code changes; the parsers are deliberately tolerant and report
``unknown_response`` instead of crashing when the shape differs.
See ``docs/research.md``.
"""

from __future__ import annotations

import logging
import random
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Protocol

from settings import Config, HttpSettings, TimeWindow

log = logging.getLogger("restaurant_orderer.searcher")

# Result status values ------------------------------------------------------
OK = "ok"                        # request succeeded, slots (possibly zero) parsed
NO_AVAILABILITY = "no_availability"  # request succeeded, platform says nothing open
BLOCKED = "blocked"              # 401/403/429/captcha: stop hammering this platform
ERROR = "error"                  # network failure / 5xx after retries / other 4xx
UNKNOWN_RESPONSE = "unknown_response"  # 2xx but the payload shape is not recognised
NOT_CONFIGURED = "not_configured"  # venue id missing in config.json
SKIPPED = "skipped"              # not queried (platform already blocked this run)

_CAPTCHA_MARKERS = ("captcha", "cf-chl", "challenge-platform", "attention required", "access denied")


@dataclass(frozen=True, order=True)
class Slot:
    date: date
    time: time
    area: str
    platform: str
    deep_link: str = field(default="", compare=False)
    area_id: str = field(default="", compare=False)

    def key(self) -> tuple:
        return (self.platform, self.date, self.time, self.area)


@dataclass
class DayResult:
    platform: str
    date: date
    status: str
    slots: list[Slot] = field(default_factory=list)
    message: str = ""


@dataclass
class ScanReport:
    restaurant: str
    party_size: int
    results: list[DayResult] = field(default_factory=list)

    @property
    def slots(self) -> list[Slot]:
        seen: dict[tuple, Slot] = {}
        for r in self.results:
            for s in r.slots:
                seen.setdefault(s.key(), s)
        return sorted(seen.values())

    def status_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.results:
            out[r.status] = out.get(r.status, 0) + 1
        return out

    def problems(self) -> list[DayResult]:
        return [r for r in self.results if r.status not in (OK, NO_AVAILABILITY)]


# HTTP layer ----------------------------------------------------------------

class Transport(Protocol):
    def request(self, method: str, url: str, *, headers: Mapping[str, str], params: Mapping[str, Any] | None,
                json: Any, timeout: float) -> Any:  # returns a requests.Response-like object
        ...


class RequestsTransport:
    """Thin wrapper around a ``requests.Session`` (imported lazily)."""

    def __init__(self) -> None:
        import requests  # noqa: PLC0415 - optional at import time so tests need no network stack

        self._requests = requests
        self._session = requests.Session()

    def request(self, method, url, *, headers, params, json, timeout):
        return self._session.request(method, url, headers=dict(headers), params=params, json=json, timeout=timeout)


class NetworkError(Exception):
    pass


@dataclass
class HttpResult:
    kind: str  # "ok" | "blocked" | "http_error" | "network_error" | "not_json"
    status_code: int = 0
    data: Any = None
    message: str = ""


class HttpClient:
    """Rate-limited HTTP client with retries and realistic browser headers."""

    def __init__(self, settings: HttpSettings, transport: Transport | None = None,
                 sleep: Callable[[float], None] = _time.sleep, clock: Callable[[], float] = _time.monotonic,
                 rng: random.Random | None = None) -> None:
        self.settings = settings
        self._transport = transport
        self._sleep = sleep
        self._clock = clock
        self._rng = rng or random.Random()
        self._last_request: float | None = None

    @property
    def transport(self) -> Transport:
        if self._transport is None:
            self._transport = RequestsTransport()
        return self._transport

    def base_headers(self, origin: str | None = None) -> dict[str, str]:
        h = {
            "User-Agent": self.settings.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": self.settings.accept_language,
        }
        if origin:
            h["Origin"] = origin
            h["Referer"] = origin.rstrip("/") + "/"
        return h

    def _throttle(self) -> None:
        if self._last_request is not None:
            wait = self.settings.min_interval_seconds + self._rng.uniform(0, self.settings.jitter_seconds)
            elapsed = self._clock() - self._last_request
            if elapsed < wait:
                self._sleep(wait - elapsed)
        self._last_request = self._clock()

    def request(self, method: str, url: str, *, headers: Mapping[str, str] | None = None,
                params: Mapping[str, Any] | None = None, json: Any = None) -> HttpResult:
        attempts = max(1, int(self.settings.max_retries) + 1)
        last = HttpResult(kind="network_error", message="no attempt made")
        for attempt in range(attempts):
            if attempt:
                self._sleep(self.settings.backoff_seconds * (2 ** (attempt - 1)))
            self._throttle()
            try:
                resp = self.transport.request(method, url, headers=headers or {}, params=params, json=json,
                                              timeout=self.settings.timeout_seconds)
            except Exception as exc:  # requests.RequestException, socket errors, ...
                last = HttpResult(kind="network_error", message=f"{type(exc).__name__}: {exc}")
                log.warning("%s %s failed (attempt %d/%d): %s", method, url, attempt + 1, attempts, last.message)
                continue
            last = _classify(resp)
            if last.kind == "ok":
                return last
            code = last.status_code
            if code == 429 or code >= 500:
                retry_after = _retry_after(resp)
                if retry_after and attempt + 1 < attempts:
                    self._sleep(min(retry_after, 60.0))
                log.warning("%s %s -> HTTP %s (attempt %d/%d)", method, url, code, attempt + 1, attempts)
                continue
            return last  # other 4xx, 401/403 and non-JSON are not retried
        if last.status_code == 429:
            last.kind = "blocked"
        return last


def _retry_after(resp: Any) -> float | None:
    try:
        return float((getattr(resp, "headers", None) or {}).get("Retry-After"))
    except (TypeError, ValueError):
        return None


def _classify(resp: Any) -> HttpResult:
    code = int(getattr(resp, "status_code", 0) or 0)
    text = getattr(resp, "text", "") or ""
    data = None
    try:
        data = resp.json()
    except Exception:
        data = None
    if code in (401, 403, 429):
        return HttpResult(kind="blocked", status_code=code, data=data, message=f"HTTP {code}")
    if data is None:
        low = text[:4000].lower()
        if any(m in low for m in _CAPTCHA_MARKERS):
            return HttpResult(kind="blocked", status_code=code, message=f"HTTP {code}: bot-protection page")
        if 200 <= code < 300:
            return HttpResult(kind="not_json", status_code=code, message=f"HTTP {code}: response is not JSON")
        return HttpResult(kind="http_error", status_code=code, message=f"HTTP {code}")
    if 200 <= code < 300:
        return HttpResult(kind="ok", status_code=code, data=data)
    return HttpResult(kind="http_error", status_code=code, data=data, message=f"HTTP {code}")


# Adapters ------------------------------------------------------------------

@dataclass(frozen=True)
class BookingRequirements:
    otp_required: bool
    card_required: bool
    notes: str = ""

    @property
    def human_free(self) -> bool:
        return not (self.otp_required or self.card_required)


class PlatformAdapter:
    """Interface every platform implements."""

    name = "base"

    def __init__(self, platform_cfg: Mapping[str, Any], http: HttpClient, party_size: int) -> None:
        self.cfg = dict(platform_cfg)
        self.http = http
        self.party_size = party_size
        self.blocked = False

    # --- configuration ---
    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True))

    def is_configured(self) -> bool:
        raise NotImplementedError

    def not_configured_message(self) -> str:
        return f"{self.name}: venue id missing in config.json"

    # --- search ---
    def check_day(self, day: date, window: TimeWindow) -> DayResult:
        raise NotImplementedError

    def deep_link(self, day: date, at: time | None = None) -> str:
        at = at or time(20, 0)
        fields = {
            "date": day.isoformat(),
            "date_compact": day.strftime("%Y%m%d"),
            "time": at.strftime("%H:%M"),
            "time_compact": at.strftime("%H%M"),
            "party_size": self.party_size,
            **{k: v for k, v in self.cfg.items() if isinstance(v, (str, int))},
        }
        template = self.cfg.get("deep_link_template", "")
        try:
            return template.format(**fields) if template else ""
        except (KeyError, IndexError, ValueError):
            log.warning("%s: bad deep_link_template %r", self.name, template)
            return ""

    # --- booking ---
    def booking_requirements(self) -> BookingRequirements:
        # Conservative default: assume a human is needed.
        return BookingRequirements(otp_required=True, card_required=False, notes="unknown platform")

    def book(self, slot: Slot, contact: Mapping[str, str]) -> Mapping[str, Any]:
        raise NotImplementedError(f"{self.name}: programmatic booking is not implemented")


def _hhmm_from_compact(value: Any) -> time | None:
    s = str(value or "").strip().replace(":", "")
    if len(s) == 3:
        s = "0" + s
    if len(s) != 4 or not s.isdigit():
        return None
    try:
        return time(int(s[:2]), int(s[2:]))
    except ValueError:
        return None


class OntopoAdapter(PlatformAdapter):
    """Ontopo (ontopo.com). UNVERIFIED endpoint shapes - see docs/research.md.

    Assumed flow of the web widget:
      1. POST {base_url}{auth_endpoint} -> {"jwt_token": "..."}; sent back as header ``token``.
      2. POST {base_url}{availability_endpoint} with
         {"slug": venue_slug, "locale": "he",
          "criteria": {"size": "2", "date": "YYYYMMDD", "time": "HHMM"}}
         -> {"areas": [{"id", "name", "options": [{"time": "HHMM", "method": "seat"|"standby"|...}]}],
             "recommended": [...]}
    Only ``method == "seat"`` counts as bookable (``standby`` is a wait-list).
    """

    name = "ontopo"
    BOOKABLE_METHODS = ("seat",)

    def __init__(self, platform_cfg, http, party_size):
        super().__init__(platform_cfg, http, party_size)
        self._token: str | None = None

    def is_configured(self) -> bool:
        return bool(str(self.cfg.get("venue_slug", "")).strip())

    def not_configured_message(self) -> str:
        return "ontopo: platforms.ontopo.venue_slug is empty in config.json"

    def _url(self, key: str) -> str:
        return str(self.cfg.get("base_url", "https://ontopo.com")).rstrip("/") + str(self.cfg.get(key, ""))

    def _headers(self) -> dict[str, str]:
        h = self.http.base_headers(origin=str(self.cfg.get("base_url", "https://ontopo.com")))
        h["Content-Type"] = "application/json"
        if self._token:
            h["token"] = self._token
        return h

    def _ensure_token(self, force: bool = False) -> None:
        if (self._token and not force) or not self.cfg.get("auth_endpoint"):
            return
        res = self.http.request("POST", self._url("auth_endpoint"), headers=self._headers(), json={})
        if res.kind == "ok" and isinstance(res.data, dict):
            self._token = res.data.get("jwt_token") or res.data.get("token")
        if not self._token:
            log.info("ontopo: anonymous login gave no token (%s); continuing without it", res.message or res.kind)

    def check_day(self, day: date, window: TimeWindow) -> DayResult:
        self._ensure_token()
        slots: list[Slot] = []
        statuses: list[str] = []
        messages: list[str] = []
        for qt in window.query_times:
            body = {
                "slug": self.cfg.get("venue_slug"),
                "locale": self.cfg.get("locale", "he"),
                "criteria": {"size": str(self.party_size), "date": day.strftime("%Y%m%d"), "time": qt.strftime("%H%M")},
            }
            res = self.http.request("POST", self._url("availability_endpoint"), headers=self._headers(), json=body)
            if res.kind == "blocked" and res.status_code == 401 and self.cfg.get("auth_endpoint"):
                self._ensure_token(force=True)
                res = self.http.request("POST", self._url("availability_endpoint"), headers=self._headers(), json=body)
            status, found, msg = self._to_status(res, day)
            statuses.append(status)
            if msg:
                messages.append(msg)
            slots.extend(s for s in found if window.contains(s.time))
            if status == BLOCKED:
                self.blocked = True
                break
        return DayResult(self.name, day, _merge_status(statuses, bool(slots)), slots, "; ".join(dict.fromkeys(messages)))

    def _to_status(self, res: HttpResult, day: date) -> tuple[str, list[Slot], str]:
        if res.kind == "blocked":
            return BLOCKED, [], res.message
        if res.kind == "network_error" or (res.kind == "http_error" and res.data is None):
            return ERROR, [], res.message
        if res.kind == "not_json":
            return UNKNOWN_RESPONSE, [], res.message
        if res.kind == "http_error":
            return ERROR, [], res.message
        try:
            slots = self.parse(res.data, day)
        except UnknownShape as exc:
            return UNKNOWN_RESPONSE, [], f"ontopo: {exc}"
        return (OK if slots else NO_AVAILABILITY), slots, ""

    def parse(self, data: Any, day: date) -> list[Slot]:
        if not isinstance(data, dict) or not ({"areas", "recommended"} & data.keys()):
            raise UnknownShape("expected an object with 'areas' or 'recommended'")
        slots: list[Slot] = []
        areas = data.get("areas") or []
        if not isinstance(areas, list):
            raise UnknownShape("'areas' is not a list")
        for area in areas:
            if not isinstance(area, dict):
                continue
            area_name = str(area.get("name") or area.get("id") or "")
            for opt in area.get("options") or []:
                if not isinstance(opt, dict) or opt.get("method") not in self.BOOKABLE_METHODS:
                    continue
                t = _hhmm_from_compact(opt.get("time"))
                if t:
                    slots.append(Slot(day, t, area_name, self.name, self.deep_link(day, t), str(area.get("id", ""))))
        if not areas:  # fall back to the flat "recommended" list
            for opt in data.get("recommended") or []:
                if isinstance(opt, dict) and opt.get("method") in self.BOOKABLE_METHODS:
                    t = _hhmm_from_compact(opt.get("time"))
                    if t:
                        slots.append(Slot(day, t, str(opt.get("area") or ""), self.name, self.deep_link(day, t)))
        return slots

    def booking_requirements(self) -> BookingRequirements:
        return BookingRequirements(
            otp_required=True, card_required=False,
            notes="Ontopo checkout verifies the diner's phone number by SMS code; many venues also "
                  "require a credit-card guarantee. Unverified live - see docs/research.md.",
        )


class TabitAdapter(PlatformAdapter):
    """Tabit (tabitisrael.co.il / tgm-api.tabit.cloud). UNVERIFIED - see docs/research.md.

    Assumed flow of the web client: POST {base_url}{availability_endpoint} with
      {"organization": org_id, "type": "future_reservation", "standby_reservation": false,
       "seats_count": 2, "reserved_from": "<UTC ISO>", "preference": "<area or omitted>"}
    - success -> {"reservation": {"_id", "reserved_from", "preference"}} (a short temporary hold,
      which we release again via DELETE {release_endpoint});
    - unavailable (often HTTP 409) -> {"alternative_results": [{"reserved_from"|"time", "preference"|"area"}]}.
    """

    name = "tabit"

    def __init__(self, platform_cfg, http, party_size, tz: Any = None):
        super().__init__(platform_cfg, http, party_size)
        self.tz = tz or _local_tz()

    def is_configured(self) -> bool:
        return bool(str(self.cfg.get("organization_id", "")).strip())

    def not_configured_message(self) -> str:
        return "tabit: platforms.tabit.organization_id is empty in config.json"

    def _url(self, key: str, **kw: Any) -> str:
        return str(self.cfg.get("base_url", "")).rstrip("/") + str(self.cfg.get(key, "")).format(**kw)

    def _headers(self) -> dict[str, str]:
        h = self.http.base_headers(origin="https://tabitisrael.co.il")
        h["Content-Type"] = "application/json"
        return h

    def check_day(self, day: date, window: TimeWindow) -> DayResult:
        slots: list[Slot] = []
        statuses: list[str] = []
        messages: list[str] = []
        for qt in window.query_times:
            local_dt = datetime.combine(day, qt, tzinfo=self.tz)
            body: dict[str, Any] = {
                "organization": self.cfg.get("organization_id"),
                "type": "future_reservation",
                "standby_reservation": False,
                "seats_count": self.party_size,
                "reserved_from": local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            }
            if self.cfg.get("preference"):
                body["preference"] = self.cfg["preference"]
            res = self.http.request("POST", self._url("availability_endpoint"), headers=self._headers(), json=body)
            if res.kind == "blocked":
                statuses.append(BLOCKED)
                messages.append(res.message)
                self.blocked = True
                break
            if res.kind in ("network_error", "not_json") or (res.kind == "http_error" and res.data is None):
                statuses.append(ERROR if res.kind != "not_json" else UNKNOWN_RESPONSE)
                messages.append(res.message)
                continue
            try:
                found = self.parse(res.data, day)
            except UnknownShape as exc:
                statuses.append(UNKNOWN_RESPONSE if res.kind == "ok" else ERROR)
                messages.append(f"tabit: {exc}" if res.kind == "ok" else res.message)
                continue
            self._release_hold(res.data)
            statuses.append(OK if found else NO_AVAILABILITY)
            slots.extend(s for s in found if window.contains(s.time))
        return DayResult(self.name, day, _merge_status(statuses, bool(slots)), slots, "; ".join(dict.fromkeys(messages)))

    def _release_hold(self, data: Any) -> None:
        res_obj = data.get("reservation") if isinstance(data, dict) else None
        rid = res_obj.get("_id") if isinstance(res_obj, dict) else None
        if rid and self.cfg.get("release_endpoint"):
            r = self.http.request("DELETE", self._url("release_endpoint", reservation_id=rid), headers=self._headers())
            if r.kind != "ok":
                log.info("tabit: could not release temporary hold %s (%s); it expires on its own", rid, r.message)

    def _to_local(self, value: Any, day: date) -> datetime | None:
        if not value:
            return None
        s = str(value)
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            t = _hhmm_from_compact(s)
            return datetime.combine(day, t, tzinfo=self.tz) if t else None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.tz)
        return dt.astimezone(self.tz)

    def _slot(self, entry: Mapping[str, Any], day: date, area: str | None = None) -> Slot | None:
        when = self._to_local(entry.get("reserved_from") or entry.get("time") or entry.get("timestamp"), day)
        if when is None or when.date() != day:
            return None
        t = when.time().replace(second=0, microsecond=0, tzinfo=None)
        area_name = str(area or entry.get("preference") or entry.get("area") or entry.get("name") or "")
        return Slot(day, t, area_name, self.name, self.deep_link(day, t))

    def parse(self, data: Any, day: date) -> list[Slot]:
        if not isinstance(data, dict) or not ({"reservation", "alternative_results"} & data.keys()):
            raise UnknownShape("expected an object with 'reservation' or 'alternative_results'")
        slots: list[Slot] = []
        res_obj = data.get("reservation")
        if isinstance(res_obj, dict):
            s = self._slot(res_obj, day)
            if s:
                slots.append(s)
        alts = data.get("alternative_results") or []
        if isinstance(alts, dict):  # {area: [entries]}
            for area, entries in alts.items():
                for e in entries or []:
                    if isinstance(e, dict) and (s := self._slot(e, day, area)):
                        slots.append(s)
        elif isinstance(alts, list):
            for e in alts:
                if isinstance(e, dict) and (s := self._slot(e, day)):
                    slots.append(s)
        else:
            raise UnknownShape("'alternative_results' is neither list nor object")
        return slots

    def booking_requirements(self) -> BookingRequirements:
        return BookingRequirements(
            otp_required=True, card_required=False,
            notes="Tabit's online reservation flow confirms the diner's phone by SMS code, and some "
                  "venues add a credit-card guarantee. Unverified live - see docs/research.md.",
        )


class UnknownShape(ValueError):
    pass


def _local_tz():
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        return ZoneInfo("Asia/Jerusalem")
    except Exception:  # missing tzdata (e.g. Windows without the tzdata package)
        log.warning("zoneinfo 'Asia/Jerusalem' unavailable; assuming UTC+2 (Israel winter time)")
        return timezone(timedelta(hours=2), "IST")


def _merge_status(statuses: list[str], have_slots: bool) -> str:
    if have_slots:
        return OK
    for s in (BLOCKED, UNKNOWN_RESPONSE, ERROR):
        if s in statuses:
            # a mix of success and failure still counts as the failure, so it is visible
            return s
    return NO_AVAILABILITY if statuses else ERROR


ADAPTERS: dict[str, type[PlatformAdapter]] = {"ontopo": OntopoAdapter, "tabit": TabitAdapter}


def build_adapters(config: Config, http: HttpClient | None = None) -> list[PlatformAdapter]:
    http = http or HttpClient(config.http)
    out = []
    for name, pcfg in config.platforms.items():
        cls = ADAPTERS.get(name)
        if cls is None:
            log.warning("unknown platform %r in config.json - ignored", name)
            continue
        adapter = cls(pcfg, http, config.party_size)
        if adapter.enabled:
            out.append(adapter)
    return out


def scan(config: Config, adapters: Iterable[PlatformAdapter], today: date) -> ScanReport:
    """Query every adapter for every target date; never raises for platform problems."""
    report = ScanReport(config.restaurant_name, config.party_size)
    dates = config.target_dates(today)
    for adapter in adapters:
        if not adapter.is_configured():
            report.results.append(DayResult(adapter.name, today, NOT_CONFIGURED, message=adapter.not_configured_message()))
            continue
        for d in dates:
            if adapter.blocked:
                report.results.append(DayResult(adapter.name, d, SKIPPED, message="skipped: platform blocked earlier in this run"))
                continue
            try:
                result = adapter.check_day(d, config.window_for(d))
            except Exception as exc:  # defensive: an adapter bug must not kill the daily run
                log.exception("%s: unexpected error on %s", adapter.name, d)
                result = DayResult(adapter.name, d, ERROR, message=f"unexpected {type(exc).__name__}: {exc}")
            report.results.append(result)
    return report
