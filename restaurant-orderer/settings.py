"""Configuration loading, validation, .env handling and target-date iteration."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterator, Mapping

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = HERE / "config.json"
DEFAULT_ENV_PATH = HERE / ".env"

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
VALID_MODES = ("notify_only", "auto_order")


class ConfigError(ValueError):
    """Raised for an invalid config.json."""


def _parse_hhmm(value: str, what: str) -> time:
    try:
        return datetime.strptime(str(value).strip(), "%H:%M").time()
    except ValueError as exc:
        raise ConfigError(f"{what}: expected HH:MM, got {value!r}") from exc


def _parse_date(value: str, what: str) -> date:
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ConfigError(f"{what}: expected YYYY-MM-DD, got {value!r}") from exc


@dataclass(frozen=True)
class TimeWindow:
    start: time
    end: time
    query_times: tuple[time, ...]

    def contains(self, t: time) -> bool:
        return self.start <= t <= self.end

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], base: "TimeWindow | None" = None, what: str = "time_window") -> "TimeWindow":
        start = _parse_hhmm(raw["start"], f"{what}.start") if "start" in raw else (base.start if base else time(0, 0))
        end = _parse_hhmm(raw["end"], f"{what}.end") if "end" in raw else (base.end if base else time(23, 59))
        if end < start:
            raise ConfigError(f"{what}: end {end:%H:%M} is before start {start:%H:%M}")
        if "query_times" in raw:
            qts = tuple(_parse_hhmm(q, f"{what}.query_times") for q in raw["query_times"])
        elif base is not None:
            qts = base.query_times
        else:
            qts = (start,)
        if not qts:
            raise ConfigError(f"{what}.query_times must not be empty")
        return cls(start=start, end=end, query_times=qts)


@dataclass(frozen=True)
class EmailSettings:
    frequency: str = "daily"
    suppress_empty: bool = True
    subject_prefix: str = "[Restaurant Orderer]"


@dataclass(frozen=True)
class HttpSettings:
    min_interval_seconds: float = 2.5
    jitter_seconds: float = 1.5
    timeout_seconds: float = 20.0
    max_retries: int = 3
    backoff_seconds: float = 4.0
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    )
    accept_language: str = "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7"


@dataclass(frozen=True)
class Config:
    restaurant_name: str
    start: date
    end: date
    days_filter: tuple[str, ...]
    weekend_days: tuple[str, ...]
    party_size: int
    mode: str
    time_window: TimeWindow
    time_window_overrides: Mapping[str, TimeWindow]
    email: EmailSettings
    http: HttpSettings
    platforms: Mapping[str, Mapping[str, Any]]
    state_dir: Path
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    # -- dates -------------------------------------------------------------
    def target_weekdays(self) -> set[int]:
        """Weekday numbers (Mon=0) selected by days_filter."""
        out: set[int] = set()
        for token in self.days_filter:
            if token == "weekend":
                out.update(WEEKDAYS.index(d) for d in self.weekend_days)
            elif token == "all":
                out.update(range(7))
            else:
                out.add(WEEKDAYS.index(token))
        return out

    def target_dates(self, today: date) -> list[date]:
        return list(iter_target_dates(self.start, self.end, self.target_weekdays(), today))

    def window_for(self, d: date) -> TimeWindow:
        return self.time_window_overrides.get(WEEKDAYS[d.weekday()], self.time_window)


def iter_target_dates(start: date, end: date, weekdays: set[int], today: date) -> Iterator[date]:
    """Yield dates in [start, end] (inclusive) on the given weekdays, skipping dates before ``today``."""
    d = max(start, today)
    while d <= end:
        if d.weekday() in weekdays:
            yield d
        d += timedelta(days=1)


def _validate_day_tokens(tokens: Any, what: str, allow_special: bool) -> tuple[str, ...]:
    if not isinstance(tokens, list) or not tokens:
        raise ConfigError(f"{what} must be a non-empty list")
    out = []
    for t in tokens:
        t = str(t).strip().lower()
        if t in WEEKDAYS or (allow_special and t in ("weekend", "all")):
            out.append(t)
        else:
            raise ConfigError(f"{what}: unknown day {t!r}")
    return tuple(out)


def parse_config(raw: Mapping[str, Any], base_dir: Path = HERE) -> Config:
    try:
        name = str(raw["restaurant_name"]).strip()
        dr = raw["date_range"]
        start = _parse_date(dr["start"], "date_range.start")
        end = _parse_date(dr["end"], "date_range.end")
    except KeyError as exc:
        raise ConfigError(f"missing required key: {exc}") from exc
    if not name:
        raise ConfigError("restaurant_name must not be empty")
    if end < start:
        raise ConfigError("date_range.end is before date_range.start")

    days_filter = _validate_day_tokens(raw.get("days_filter", ["weekend"]), "days_filter", True)
    weekend_days = _validate_day_tokens(raw.get("weekend_days", ["friday", "saturday"]), "weekend_days", False)

    party_size = raw.get("party_size", 2)
    if not isinstance(party_size, int) or isinstance(party_size, bool) or not 1 <= party_size <= 30:
        raise ConfigError("party_size must be an integer between 1 and 30")

    mode = raw.get("mode", "notify_only")
    if mode not in VALID_MODES:
        raise ConfigError(f"mode must be one of {VALID_MODES}, got {mode!r}")

    tw = TimeWindow.from_dict(raw.get("time_window", {"start": "18:00", "end": "22:30"}))
    overrides = {}
    for day, ov in (raw.get("time_window_overrides") or {}).items():
        day_l = day.strip().lower()
        if day_l not in WEEKDAYS:
            raise ConfigError(f"time_window_overrides: unknown day {day!r}")
        overrides[day_l] = TimeWindow.from_dict(ov, base=tw, what=f"time_window_overrides.{day_l}")

    em = raw.get("email_notifications") or {}
    email = EmailSettings(
        frequency=str(em.get("frequency", "daily")),
        suppress_empty=bool(em.get("suppress_empty", True)),
        subject_prefix=str(em.get("subject_prefix", "[Restaurant Orderer]")),
    )
    if email.frequency != "daily":
        raise ConfigError("email_notifications.frequency: only 'daily' is supported")

    hraw = raw.get("http") or {}
    known = HttpSettings.__dataclass_fields__.keys()
    http = HttpSettings(**{k: v for k, v in hraw.items() if k in known})

    state_dir = Path(raw.get("state_dir", ".state"))
    if not state_dir.is_absolute():
        state_dir = base_dir / state_dir

    return Config(
        restaurant_name=name,
        start=start,
        end=end,
        days_filter=days_filter,
        weekend_days=weekend_days,
        party_size=party_size,
        mode=mode,
        time_window=tw,
        time_window_overrides=overrides,
        email=email,
        http=http,
        platforms=raw.get("platforms") or {},
        state_dir=state_dir,
        raw=raw,
    )


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    path = Path(path)
    with path.open(encoding="utf-8") as fh:
        raw = json.load(fh)
    return parse_config(raw, base_dir=path.resolve().parent)


# -- .env ------------------------------------------------------------------

def load_dotenv(path: Path | str = DEFAULT_ENV_PATH, environ: dict[str, str] | None = None) -> None:
    """Minimal .env loader: KEY=VALUE lines; existing environment variables win."""
    environ = os.environ if environ is None else environ
    path = Path(path)
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        environ.setdefault(key, value)
