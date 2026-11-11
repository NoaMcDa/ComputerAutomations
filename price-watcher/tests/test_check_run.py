"""scheduler._run_check_locked end to end with fetch + SMTP mocked (offline)."""

from __future__ import annotations

import io
from decimal import Decimal

import pytest
from rich.console import Console

from price_watcher import config, email_sender, fetcher, scheduler, state, watchlist
from price_watcher.contracts import FetchResult, FetchStatus, Watch


def ok(url, price) -> FetchResult:
    return FetchResult(status=FetchStatus.OK, final_url=url, price=Decimal(price), currency="ILS", in_stock=True)


@pytest.fixture
def run(monkeypatch):
    """Return run(prices, smtp=...) -> (exit_code, sent_subjects, console_text)."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("PW_FAILURE_THRESHOLD", "2")

    def _run(prices: dict, smtp_ok: bool = True):
        sent: list[str] = []

        def fake_fetch(url, selector=None, **kw):
            val = prices[url]
            if isinstance(val, Exception):
                raise val
            if val is None:
                return FetchResult(status=FetchStatus.UNREACHABLE, final_url=url, error_message="down")
            return ok(url, val)

        def fake_smtp(payload, cfg):
            if not smtp_ok:
                raise OSError("smtp down")
            sent.append(payload.subject)

        out = io.StringIO()
        monkeypatch.setattr(fetcher, "fetch", fake_fetch)
        monkeypatch.setattr(email_sender, "_send_via_smtp", fake_smtp)
        monkeypatch.setattr(scheduler, "_console", Console(file=out, width=200))
        code = scheduler._run_check_locked(config.load(), dry_run=False)
        return code, sent, out.getvalue()

    return _run


A = "https://a.example/p"
B = "https://b.example/p"


def _watches(*urls):
    watchlist.save_all([Watch(id=f"w{i}", url=u) for i, u in enumerate(urls)])


def test_failed_email_is_retried_next_run(run):  # #12
    _watches(A)
    run({A: "100"})
    _, sent, out = run({A: "80"}, smtp_ok=False)
    assert sent == []
    assert "retried" in out
    assert state.load_state()["watches"]["w0"]["last_price"] == "100"
    _, sent, _ = run({A: "80"})
    assert len(sent) == 1 and "100" in sent[0] and "80" in sent[0]
    _, sent, _ = run({A: "80"})
    assert sent == []


def test_failed_threshold_alert_is_retried(run):  # #12 for the failure alert
    _watches(A)
    run({A: "100"})
    run({A: None})
    _, sent, _ = run({A: None}, smtp_ok=False)
    assert sent == []
    _, sent, _ = run({A: None})
    assert len(sent) == 1


def test_one_fetch_raising_does_not_abort_the_run(run):  # #13
    _watches(A, B)
    code, _, out = run({A: RuntimeError("kaboom"), B: "5"})
    assert code == 1
    watches = state.load_state()["watches"]
    assert watches["w1"]["last_price"] == "5"
    assert watches["w0"]["consecutive_failures"] == 1
    assert "kaboom" in out


def test_results_table_labels_threshold_and_recovery(run):  # #30
    _watches(A)
    run({A: "100"})
    run({A: None})
    _, sent, out = run({A: None})
    assert len(sent) == 1
    row = next(line for line in out.splitlines() if "a.example" in line)
    assert "failure alert" in row and "no email" not in row

    _, sent, out = run({A: "60"})
    assert len(sent) == 1
    row = next(line for line in out.splitlines() if "a.example" in line)
    assert "recovered" in row and "100 → 60" in row
    assert "— → —" not in out


def test_last_notified_at_only_on_changed_watch(run):  # #31
    _watches(A, B)
    run({A: "100", B: "50"})
    _, sent, _ = run({A: "80", B: "50"})
    assert len(sent) == 1
    watches = state.load_state()["watches"]
    assert "last_notified_at" in watches["w0"]
    assert "last_notified_at" not in watches["w1"]
