"""Shared pytest fixtures for Price Watcher.

Every test runs with the per-user data/log folders redirected to a temporary
directory, so nothing touches the real state file, and SMTP is never reached.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolated_app_dirs(tmp_path, monkeypatch):
    """Point every app-data / log path at a temp folder."""
    data = tmp_path / "appdata"
    data.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setenv("XDG_STATE_HOME", str(data / "state"))
    monkeypatch.setenv("APPDATA", str(data))
    monkeypatch.setenv("LOCALAPPDATA", str(data))
    for var in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    yield data


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES
