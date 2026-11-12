"""Issue #8 failure handling at the HTTP level, against a local 127.0.0.1 server:
404 -> not_found, 429 + Retry-After honoured, redirect to the home page -> not_found
once evaluated for a watch. Uses only the public fetcher.fetch()."""
from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core_helpers import cfg, watch
from price_watcher import fetcher, state
from price_watcher.contracts import ChangeType, FetchStatus

PAGE = """<html><head><title>Shoe</title><script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"Shoe",
 "offers":{"@type":"Offer","price":"99.90","priceCurrency":"ILS"}}
</script></head><body>Shoe</body></html>"""
HOME = "<html><head><title>Shop</title></head><body>Welcome</body></html>"


class _Handler(BaseHTTPRequestHandler):
    hits: dict = {}

    def log_message(self, *a):
        pass

    def _send(self, code, body="", headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        data = body.encode()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        n = _Handler.hits[self.path] = _Handler.hits.get(self.path, 0) + 1
        if self.path == "/products/shoe":
            self._send(200, PAGE)
        elif self.path == "/products/gone":
            self._send(404, "nope")
        elif self.path == "/products/busy":
            if n == 1:
                self._send(429, "slow down", {"Retry-After": "3"})
            else:
                self._send(200, PAGE)
        elif self.path == "/products/moved":
            self._send(302, "", {"Location": "/"})
        elif self.path == "/":
            self._send(200, HOME)
        else:
            self._send(404, "")


@pytest.fixture
def server(monkeypatch):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    _Handler.hits = {}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def sleeps(monkeypatch):
    recorded: list[float] = []
    monkeypatch.setattr(time, "sleep", recorded.append)
    return recorded


def test_ok_page(server, sleeps):
    r = fetcher.fetch(server + "/products/shoe", use_browser_fallback=False)
    assert r.status == FetchStatus.OK and str(r.price) in ("99.90", "99.9")


def test_404_is_not_found(server, sleeps):
    r = fetcher.fetch(server + "/products/gone", use_browser_fallback=False)
    assert r.status == FetchStatus.NOT_FOUND and r.http_status == 404


def test_429_retry_after_is_honoured(server, sleeps):
    r = fetcher.fetch(server + "/products/busy", use_browser_fallback=False)
    assert 3.0 in sleeps
    assert r.status == FetchStatus.OK


def test_redirect_to_home_page_counts_as_removed(server, sleeps):
    url = server + "/products/moved"
    r = fetcher.fetch(url, use_browser_fallback=False)
    ev = state.evaluate(watch(url=url), r, cfg())
    assert ev.type == ChangeType.FAILED
    assert ev.fetch.status == FetchStatus.NOT_FOUND
