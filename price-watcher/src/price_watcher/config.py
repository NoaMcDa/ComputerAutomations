from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _env_dir() -> Path:
    """Return the directory containing the .env file (project root of price-watcher)."""
    return Path(__file__).resolve().parents[2]


load_dotenv(_env_dir() / ".env")


class ConfigError(RuntimeError):
    """Invalid or missing configuration (reported as a one-line CLI error)."""


def _get(name: str, default: str | None = None) -> str:
    val = os.environ.get(name, default)
    if val is None or (default is None and val == ""):
        raise ConfigError(f"Missing required env var: {name}")
    return val


def _get_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"Invalid value for {name}: {raw!r} (expected an integer)") from None


def _get_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"Invalid value for {name}: {raw!r} (expected a number)") from None


@dataclass(frozen=True)
class EmailConfig:
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    email_from: str
    email_to: str
    # Extra recipients who get ONLY the vacation-deals email (never the daily report).
    deals_email_to: tuple[str, ...] = ()


@dataclass(frozen=True)
class CheckConfig:
    failure_threshold: int
    sanity_jump_pct: float
    delay_min_sec: float
    delay_max_sec: float


@dataclass(frozen=True)
class AppConfig:
    email: EmailConfig
    check: CheckConfig
    log_level: str


def load_check() -> CheckConfig:
    """Load only the check settings (no SMTP values needed).

    Used by `check --dry-run` and by the check loop itself, so a missing or
    broken email setup never stops prices from being checked.
    """
    delay_min = max(0.0, _get_float("PW_DELAY_MIN_SEC", 2.0))
    delay_max = max(delay_min, _get_float("PW_DELAY_MAX_SEC", 8.0))
    return CheckConfig(
        failure_threshold=max(1, _get_int("PW_FAILURE_THRESHOLD", 6)),
        sanity_jump_pct=_get_float("PW_SANITY_JUMP_PCT", 90.0),
        delay_min_sec=delay_min,
        delay_max_sec=delay_max,
    )


def load(require_email: bool = True) -> AppConfig:
    """Load config from env + .env file. Raises ConfigError if invalid.

    With require_email=False the SMTP settings may be missing (they are then
    empty strings); used by dry runs that never send mail.
    """
    def _opt(name: str) -> str:
        return os.environ.get(name, "") if not require_email else _get(name)

    return AppConfig(
        email=EmailConfig(
            smtp_host=_get("PW_SMTP_HOST", "smtp.gmail.com"),
            smtp_port=_get_int("PW_SMTP_PORT", 587),
            smtp_user=_opt("PW_SMTP_USER"),
            smtp_password=_opt("PW_SMTP_PASSWORD"),
            email_from=_opt("PW_EMAIL_FROM"),
            email_to=_opt("PW_EMAIL_TO"),
            deals_email_to=tuple(
                addr.strip()
                for addr in os.environ.get("PW_DEALS_EMAIL_TO", "").split(",")
                if addr.strip()
            ),
        ),
        check=load_check(),
        log_level=os.environ.get("PW_LOG_LEVEL", "INFO"),
    )
