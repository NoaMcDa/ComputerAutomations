"""Fetch-level tests against a local HTTP server on 127.0.0.1 (no internet).

Covers failure statuses (#4), Shopify variants (#43), the blocked Shopify
rescue with currency (#49), Hebrew URLs (#4) and the Playwright cleanup (#40).
"""
from __future__ import annotations

import socket
import sys
import threading
import types
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

import pytest

from price_watcher import fetcher
from price_watcher.contracts import FetchResult, FetchStatus

FIX = Path(__file__).resolve().parent / "fixtures" / "fetcher"


def _fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class _Server:
    """Routes: unquoted 'path?query' -> (status, body, headers)."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, str, dict]] = {}
        self.hits: list[str] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                key = unquote(self.path)
                server.hits.append(key)
                status, body, headers = server.routes.get(
                    key, server.routes.get(key.split("?")[0], (404, "missing", {}))
                )
                data = body.encode("utf-8")
                self.send_response(status)
                headers = {"Content-Type": "text/html; charset=utf-8", **headers}
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()

    def route(self, path: str, body: str = "", status: int = 200, **headers) -> None:
        self.routes[path] = (status, body, {k.replace("_", "-"): v for k, v in headers.items()})

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(fetcher, "_RETRY_DELAYS", ())  # one attempt, no sleeping
    srv = _Server()
    yield srv
    srv.close()


def _fetch(url: str, selector: str | None = None) -> FetchResult:
    return fetcher.fetch(url, selector, use_browser_fallback=False)


JSON = {"Content_Type": "application/json"}


# ---------------------------------------------------------------------------
# Failure statuses
# ---------------------------------------------------------------------------

def test_unreachable(monkeypatch):
    monkeypatch.setattr(fetcher, "_RETRY_DELAYS", ())
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    r = _fetch(f"http://127.0.0.1:{port}/products/x")
    assert r.status == FetchStatus.UNREACHABLE
    assert r.error_message


def test_server_error_after_retries_is_unreachable(server):
    server.route("/p", "boom", status=503)
    r = _fetch(server.base + "/p")
    assert r.status == FetchStatus.UNREACHABLE
    assert r.error_message == "HTTP 503"


@pytest.mark.parametrize("code", [404, 410])
def test_gone_is_not_found(server, code):
    server.route("/gone", "nope", status=code)
    r = _fetch(server.base + "/gone")
    assert r.status == FetchStatus.NOT_FOUND
    assert r.http_status == code
    assert "moved or removed" in r.error_message


@pytest.mark.parametrize("code", [403, 429])
def test_blocked_status(server, code):
    server.route("/p", "denied", status=code, Retry_After="120")
    r = _fetch(server.base + "/p")
    assert r.status == FetchStatus.BLOCKED
    assert r.http_status == code


def test_challenge_page_is_blocked(server):
    server.route("/p", _fx("challenge.html"))
    assert _fetch(server.base + "/p").status == FetchStatus.BLOCKED


def test_price_not_found(server):
    server.route("/p", "<html><title>x</title><body>no price</body></html>")
    r = _fetch(server.base + "/p")
    assert r.status == FetchStatus.PRICE_NOT_FOUND
    assert r.title == "x"


def test_redirect_records_final_url(server):
    server.route("/old", "", status=301, Location="/new")
    server.route("/new", _fx("jsonld_product.html"))
    r = _fetch(server.base + "/old")
    assert r.status == FetchStatus.OK
    assert r.final_url == server.base + "/new"


# ---------------------------------------------------------------------------
# Non-ASCII (Hebrew) URL
# ---------------------------------------------------------------------------

def test_hebrew_url_is_percent_encoded_for_request(server):
    path = "/סייל/נשים/סווטשירט-אוף-שולדר-אספרסו-7310573"
    server.route(path, _fx("jsonld_product.html"))
    r = _fetch(server.base + path)
    assert r.status == FetchStatus.OK
    assert r.price == Decimal("79.90")
    assert r.currency == "ILS"
    # The request went out percent-encoded (the server saw the decoded path)
    assert server.hits == [path]
    assert "%D7%A1" in r.final_url


# ---------------------------------------------------------------------------
# Shopify product JSON (#4, #43)
# ---------------------------------------------------------------------------

def _shopify(server, page_status: int = 200, page: str | None = None, **page_headers):
    server.route("/products/25-610navy",
                 page if page is not None else _fx("shopify_page.html"),
                 status=page_status, **page_headers)
    server.route("/products/25-610navy.js", _fx("shopify_product.js"), **JSON)


def test_shopify_pinned_variant(server):
    _shopify(server)
    r = _fetch(server.base + "/products/25-610navy?variant=62803155976563")
    assert r.status == FetchStatus.OK
    assert r.price == Decimal("250")
    assert r.regular_price == Decimal("300")
    assert r.currency == "ILS"  # from the HTML meta tags
    assert r.in_stock is True
    assert r.title == "Navy Swimsuit"


def test_shopify_out_of_stock_variant(server):
    _shopify(server)
    r = _fetch(server.base + "/products/25-610navy?variant=111")
    assert (r.status, r.price, r.in_stock) == (FetchStatus.OK, Decimal("300"), False)


def test_shopify_no_variant_reads_default(server):
    _shopify(server)
    r = _fetch(server.base + "/products/25-610navy")
    assert r.price == Decimal("250")  # first available variant


def test_shopify_missing_pinned_variant_is_not_found(server):
    _shopify(server)
    r = _fetch(server.base + "/products/25-610navy?variant=999")
    assert r.status == FetchStatus.NOT_FOUND
    assert r.price is None
    assert r.error_message == "Shopify variant 999 not found in product"


def test_jsonld_missing_variant_end_to_end(server):
    server.route("/p/whey", _fx("jsonld_productgroup.html"))
    assert _fetch(server.base + "/p/whey?variation=222").price == Decimal("349.90")
    r = _fetch(server.base + "/p/whey?variation=999")
    assert r.status == FetchStatus.NOT_FOUND


# ---------------------------------------------------------------------------
# Blocked Shopify page rescued through product JSON, with currency (#49)
# ---------------------------------------------------------------------------

def test_blocked_shopify_rescued_with_cart_currency(server):
    _shopify(server, page_status=429, page="Too Many Requests", Retry_After="120")
    server.route("/cart.js", '{"token": "x", "currency": "ILS", "items": []}', **JSON)
    r = _fetch(server.base + "/products/25-610navy?variant=62803155976563")
    assert r.status == FetchStatus.OK
    assert r.price == Decimal("250")
    assert r.currency == "ILS"


def test_blocked_shopify_currency_from_meta_json(server):
    _shopify(server, page_status=403, page="denied")
    server.route("/meta.json", '{"name": "Shop", "currency": "usd"}', **JSON)
    r = _fetch(server.base + "/products/25-610navy?variant=62803155976563")
    assert r.status == FetchStatus.OK
    assert r.currency == "USD"


def test_blocked_shopify_without_currency_endpoints(server):
    _shopify(server, page_status=403, page="denied")
    r = _fetch(server.base + "/products/25-610navy?variant=62803155976563")
    assert r.status == FetchStatus.OK
    assert r.currency is None


def test_blocked_shopify_missing_variant_is_not_found(server):
    _shopify(server, page_status=403, page="denied")
    r = _fetch(server.base + "/products/25-610navy?variant=999")
    assert r.status == FetchStatus.NOT_FOUND


def test_blocked_non_shopify_stays_blocked(server):
    server.route("/products/x", "denied", status=403)  # no .js endpoint
    assert _fetch(server.base + "/products/x").status == FetchStatus.BLOCKED


def test_state_keeps_stored_currency_when_fetch_has_none():
    """#49 second half: a currency-less OK fetch keeps the stored currency."""
    from price_watcher import state
    from price_watcher.config import CheckConfig
    from price_watcher.contracts import ChangeType, Watch

    watch = Watch(id="w1", url="https://shop.example/products/a?variant=1")
    cfg = CheckConfig(failure_threshold=3, sanity_jump_pct=50.0, delay_min_sec=0, delay_max_sec=0)
    first = FetchResult(status=FetchStatus.OK, final_url=watch.url,
                        price=Decimal("300"), currency="ILS")
    state.apply(state.evaluate(watch, first, cfg))

    later = FetchResult(status=FetchStatus.OK, final_url=watch.url,
                        price=Decimal("250"), currency=None)
    event = state.evaluate(watch, later, cfg)
    assert event.type == ChangeType.PRICE_CHANGED
    assert event.fetch.currency == "ILS"  # emails show the symbol
    state.apply(event)
    assert state.load_state()["watches"]["w1"]["currency"] == "ILS"


# ---------------------------------------------------------------------------
# Playwright fallback closes the browser exactly once (#40)
# ---------------------------------------------------------------------------

def _fake_playwright(monkeypatch, goto_error: bool):
    class FakeError(Exception):
        pass

    calls = {"close": 0}

    class Page:
        url = "https://shop.example/p"

        def goto(self, *a, **k):
            if goto_error:
                raise FakeError("navigation failed")

        def content(self):
            return _fx("jsonld_product.html")

    class Browser:
        def new_page(self):
            return Page()

        def close(self):
            calls["close"] += 1

    class PW:
        chromium = types.SimpleNamespace(launch=lambda **k: Browser())

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    mod = types.ModuleType("playwright.sync_api")
    mod.sync_playwright = lambda: PW()
    mod.Error = FakeError
    monkeypatch.setitem(sys.modules, "playwright.sync_api", mod)
    return calls


@pytest.mark.parametrize("goto_error", [True, False])
def test_playwright_closes_browser_once(monkeypatch, goto_error):
    calls = _fake_playwright(monkeypatch, goto_error)
    prior = FetchResult(status=FetchStatus.PRICE_NOT_FOUND, final_url="https://shop.example/p")
    r = fetcher._playwright_fallback("https://shop.example/p", None, 200, None, prior)
    assert calls["close"] == 1
    if goto_error:
        assert r is prior
    else:
        assert r.status == FetchStatus.OK
        assert r.price == Decimal("79.90")
