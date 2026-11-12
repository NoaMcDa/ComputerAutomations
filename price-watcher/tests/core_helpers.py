"""Helpers for the check / state / email tests (no network, no SMTP)."""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Callable, Iterable, Optional

from price_watcher.config import CheckConfig
from price_watcher.contracts import FetchResult, FetchStatus, PromoInfo, Watch

URL = "https://shop.example/products/shoe"


def cfg(threshold: int = 3, jump: float = 90.0) -> CheckConfig:
    return CheckConfig(
        failure_threshold=threshold, sanity_jump_pct=jump, delay_min_sec=0, delay_max_sec=0,
    )


def watch(**kw) -> Watch:
    base = dict(id="shop-1", url=URL, name="Shoe", created_at=datetime(2026, 1, 1))
    base.update(kw)
    return Watch(**base)


def ok(price: str | None, currency: str = "ILS", **kw) -> FetchResult:
    return FetchResult(
        status=FetchStatus.OK,
        final_url=kw.pop("final_url", URL),
        title="Shoe",
        price=Decimal(price) if price is not None else None,
        currency=currency,
        http_status=200,
        **kw,
    )


def fail(status: FetchStatus = FetchStatus.UNREACHABLE, msg: str = "boom",
         http: Optional[int] = None) -> FetchResult:
    return FetchResult(status=status, final_url=URL, http_status=http, error_message=msg)


def promo(pct: str, code: Optional[str] = "VIP20", condition: Optional[str] = None) -> PromoInfo:
    return PromoInfo(code=code, discount_pct=Decimal(pct), condition=condition)


class FakeSMTP:
    """Records payloads instead of sending; can be told to fail."""

    def __init__(self) -> None:
        self.sent: list = []
        self.fail = False

    def __call__(self, payload, cfg) -> None:
        if self.fail:
            raise OSError("SMTP down")
        self.sent.append(payload)


def install_env(monkeypatch, threshold: int = 3) -> None:
    for k, v in {
        "PW_SMTP_HOST": "smtp.invalid", "PW_SMTP_PORT": "587",
        "PW_SMTP_USER": "u@example.com", "PW_SMTP_PASSWORD": "pw",
        "PW_EMAIL_FROM": "u@example.com", "PW_EMAIL_TO": "u@example.com",
        "PW_FAILURE_THRESHOLD": str(threshold),
        "PW_DELAY_MIN_SEC": "0", "PW_DELAY_MAX_SEC": "0",
    }.items():
        monkeypatch.setenv(k, v)


def write_watchlist(watches: Iterable[Watch]) -> None:
    from price_watcher import watchlist
    watchlist.save_all(list(watches))


def script_fetch(monkeypatch, results: dict[str, list[FetchResult]] | Callable) -> None:
    """Make fetcher.fetch return scripted results per URL (popped in order)."""
    from price_watcher import fetcher

    if callable(results):
        monkeypatch.setattr(fetcher, "fetch", results)
        return

    def _fake(url, selector=None, **_kw):
        queue = results[url]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(fetcher, "fetch", _fake)


def last_run() -> dict:
    from price_watcher.scheduler import last_run_path
    return json.loads(last_run_path().read_text(encoding="utf-8"))


def state_for(watch_id: str = "shop-1") -> dict:
    from price_watcher.state import load_state
    return load_state()["watches"].get(watch_id, {})
