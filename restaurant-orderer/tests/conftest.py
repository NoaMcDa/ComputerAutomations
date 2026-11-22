from __future__ import annotations

import copy
import json
import smtplib
from pathlib import Path

import pytest

import searcher
from settings import HttpSettings, parse_config

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, status_code=200, data=None, text=None, headers=None):
        self.status_code = status_code
        self._data = data
        self.text = text if text is not None else (json.dumps(data) if data is not None else "")
        self.headers = headers or {}

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data


class FakeTransport:
    """Routes requests by (METHOD, url substring) to queued responses or callables."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def request(self, method, url, *, headers, params, json, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "json": json})
        for (m, frag), resp in self.routes.items():
            if m == method and frag in url:
                if callable(resp):
                    return resp(json)
                if isinstance(resp, list):
                    return resp.pop(0) if len(resp) > 1 else resp[0]
                return resp
        raise AssertionError(f"unexpected request {method} {url}")


@pytest.fixture(autouse=True)
def _no_network_no_email(monkeypatch):
    def _deny(*a, **k):
        raise AssertionError("real network access attempted in tests")

    monkeypatch.setattr(searcher.RequestsTransport, "__init__", _deny)
    monkeypatch.setattr(smtplib, "SMTP", _deny)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _deny)


@pytest.fixture
def raw_config():
    raw = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    raw = copy.deepcopy(raw)
    raw["platforms"]["ontopo"]["venue_slug"] = "afrit-test"
    raw["platforms"]["tabit"]["organization_id"] = "org-test"
    return raw


@pytest.fixture
def make_config(raw_config, tmp_path):
    def _make(**overrides):
        raw = copy.deepcopy(raw_config)
        raw.update(overrides)
        raw["state_dir"] = str(tmp_path / "state")
        return parse_config(raw, base_dir=tmp_path)

    return _make


@pytest.fixture
def config(make_config):
    return make_config()


@pytest.fixture
def make_http():
    def _make(transport, **kw):
        settings = HttpSettings(min_interval_seconds=0, jitter_seconds=0, backoff_seconds=0, **kw)
        sleeps = []
        client = searcher.HttpClient(settings, transport=transport, sleep=sleeps.append)
        client.sleeps = sleeps
        return client

    return _make
