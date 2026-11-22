"""Shared test setup: every test runs offline against a throwaway data directory.

Every test runs with the per-user data/log folders redirected to a temporary
directory, so nothing touches the real state file, and SMTP is never reached.
"""

from __future__ import annotations

import smtplib
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class _NoSMTP:
    def __init__(self, *args, **kwargs):
        raise AssertionError("tests must not open a real SMTP connection; patch _send_via_smtp")


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path, monkeypatch):
    """Point the app's data/log dirs at tmp_path and provide dummy SMTP settings."""
    for var in ("APPDATA", "LOCALAPPDATA", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        target = tmp_path / var.lower()
        target.mkdir()
        monkeypatch.setenv(var, str(target))
    # macOS resolves its data folder under ~/Library.
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PW_SMTP_HOST", "smtp.invalid")
    monkeypatch.setenv("PW_SMTP_PORT", "587")
    monkeypatch.setenv("PW_SMTP_USER", "tester")
    monkeypatch.setenv("PW_SMTP_PASSWORD", "not-a-real-password")
    monkeypatch.setenv("PW_EMAIL_FROM", "from@example.invalid")
    monkeypatch.setenv("PW_EMAIL_TO", "to@example.invalid")
    # Most vacation fixtures don't state a party; the couples-only rule has
    # its own tests (test_vacations_couples.py), which turn it back on.
    monkeypatch.setenv("PW_DEALS_COUPLES_ONLY", "0")
    monkeypatch.setattr(smtplib, "SMTP", _NoSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _NoSMTP)
    yield


@pytest.fixture
def _isolated_app_dirs(_isolated_environment, tmp_path):
    """The temp folder the app-data / log paths were redirected to.

    Every test is isolated by the autouse fixture above; this one only hands a
    test the folder (its parent is the test's tmp_path).
    """
    data = tmp_path / "appdata"
    return data


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES
