"""Shared test setup: every test runs offline against a throwaway data directory."""

from __future__ import annotations

import smtplib

import pytest


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
    monkeypatch.setenv("PW_SMTP_HOST", "smtp.invalid")
    monkeypatch.setenv("PW_SMTP_PORT", "587")
    monkeypatch.setenv("PW_SMTP_USER", "tester")
    monkeypatch.setenv("PW_SMTP_PASSWORD", "not-a-real-password")
    monkeypatch.setenv("PW_EMAIL_FROM", "from@example.invalid")
    monkeypatch.setenv("PW_EMAIL_TO", "to@example.invalid")
    monkeypatch.setattr(smtplib, "SMTP", _NoSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _NoSMTP)
    yield
