"""The daily digest must survive one bad homepage or a corrupt watch list (#13)."""

from __future__ import annotations

import httpx
import pytest

from price_watcher import digest, paths


class _FakeClient:
    """Stands in for httpx.Client; behaviour per host is set in `plan`."""

    plan: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url):
        action = self.plan[httpx.URL(url).host]
        if isinstance(action, Exception):
            raise action
        return httpx.Response(200, text=action, request=httpx.Request("GET", url))


@pytest.fixture
def fake_http(monkeypatch):
    _FakeClient.plan = {}
    monkeypatch.setattr(digest.httpx, "Client", _FakeClient)
    return _FakeClient.plan


@pytest.mark.parametrize("exc", [
    httpx.TooManyRedirects("Exceeded maximum allowed redirects."),
    httpx.DecodingError("bad gzip"),
    httpx.InvalidURL("bad host"),
    httpx.ConnectError("refused"),
])
def test_fetch_homepage_reports_unreachable_instead_of_raising(fake_http, exc):
    fake_http["bad.example"] = exc
    html, status, err = digest._fetch_homepage("bad.example")
    assert html is None
    assert status == "unreachable"
    assert type(exc).__name__ in err


def test_one_bad_homepage_does_not_stop_the_others(fake_http, monkeypatch):
    monkeypatch.setattr(digest, "_unique_hosts", lambda: ["a.example", "b.example", "c.example"])
    fake_http["a.example"] = httpx.TooManyRedirects("loop")
    fake_http["b.example"] = "<html><body><p>EXTRA 20% OFF CODE: GOOD20</p></body></html>"
    fake_http["c.example"] = "<html><body>boom</body></html>"

    real_extract = digest.extract_promos

    def flaky_extract(html):
        if "boom" in html:
            raise ValueError("parser blew up")
        return real_extract(html)

    monkeypatch.setattr(digest, "extract_promos", flaky_extract)

    by_host = {d.host: d for d in digest.collect_digest()}
    assert set(by_host) == {"a.example", "b.example", "c.example"}
    assert by_host["a.example"].status == "unreachable"
    assert by_host["b.example"].status == "ok"
    assert [p.code for p in by_host["b.example"].promos] == ["GOOD20"]
    assert by_host["c.example"].status == "unreachable"
    assert "ValueError" in by_host["c.example"].error


def test_corrupt_watchlist_does_not_cancel_daily_report(monkeypatch):
    path = paths.watchlist_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    from price_watcher import creatine, sims4, supermarkets

    monkeypatch.setattr(creatine, "scan_and_report", lambda: "creatine-report")
    monkeypatch.setattr(sims4, "scan_and_report", lambda: "sims4-report")
    monkeypatch.setattr(supermarkets, "scan_and_report", lambda: "supermarket-report")

    report = digest.collect_daily_report()
    assert report.site_digests == []
    assert report.creatine == "creatine-report"
    assert report.sims4 == "sims4-report"
    assert report.supermarkets == "supermarket-report"
