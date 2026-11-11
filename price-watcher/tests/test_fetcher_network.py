"""fetch() with a mocked httpx: robustness, 429/blocked handling, Shopify (Refs #13, #25, #26)."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from price_watcher import fetcher
from price_watcher.contracts import FetchResult, FetchStatus

PLAIN_HTML = "<html><head><title>Plain page</title></head><body>nothing here</body></html>"
PRICED_HTML = (
    '<html><head><title>Item</title>'
    '<meta property="product:price:amount" content="49.90">'
    '<meta property="product:price:currency" content="ILS"></head><body></body></html>'
)


class Router:
    """Replacement for httpx.Client.get: url -> list of responses / exceptions (last repeats)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, client, url, *args, **kwargs):
        url = str(url)
        self.calls.append(url)
        key = url if url in self.routes else url.split("?")[0]
        if key not in self.routes:
            spec = (404, "not found", {})
        else:
            seq = self.routes[key]
            spec = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(spec, BaseException):
            raise spec
        status, body, headers = spec
        request = httpx.Request("GET", url)
        if isinstance(body, (dict, list)):
            return httpx.Response(status, json=body, headers=headers, request=request)
        return httpx.Response(status, text=body, headers=headers, request=request)


@pytest.fixture(autouse=True)
def _no_sleep_no_browser(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(fetcher.time, "sleep", lambda s: sleeps.append(s))
    getattr(fetcher, "_rate_limited_hosts", {}).clear()
    browser_calls: list[str] = []

    def _no_browser(url, selector, http_status, raw_path, prior_result):
        browser_calls.append(url)
        return prior_result

    monkeypatch.setattr(fetcher, "_playwright_fallback", _no_browser)
    yield {"sleeps": sleeps, "browser": browser_calls}
    getattr(fetcher, "_rate_limited_hosts", {}).clear()


def _route(monkeypatch, routes) -> Router:
    router = Router(routes)
    monkeypatch.setattr(httpx.Client, "get", lambda client, url, *a, **kw: router(client, url, *a, **kw))
    return router


# ---------------------------------------------------------------- #13 never raises

@pytest.mark.parametrize(
    "exc",
    [
        httpx.TooManyRedirects("Exceeded maximum allowed redirects."),
        httpx.DecodingError("bad gzip"),
        httpx.InvalidURL("Invalid non-printable ASCII character in URL"),
        httpx.UnsupportedProtocol("Request URL has an unsupported protocol"),
    ],
)
def test_non_transport_httpx_errors_give_unreachable(monkeypatch, exc, _no_sleep_no_browser):
    url = "https://shop.example/item"
    router = _route(monkeypatch, {url: [exc]})
    result = fetcher.fetch(url)
    assert result.status == FetchStatus.UNREACHABLE
    assert result.error_message
    assert len(router.calls) == 1  # not retried: retrying cannot help
    assert _no_sleep_no_browser["sleeps"] == []


def test_transport_errors_are_still_retried(monkeypatch, _no_sleep_no_browser):
    url = "https://shop.example/item"
    router = _route(monkeypatch, {url: [httpx.ConnectError("refused")]})
    result = fetcher.fetch(url)
    assert result.status == FetchStatus.UNREACHABLE
    assert len(router.calls) == 4
    assert _no_sleep_no_browser["sleeps"] == [1, 2, 4]


def test_unexpected_exception_inside_extraction_gives_unreachable(monkeypatch):
    url = "https://shop.example/item"
    _route(monkeypatch, {url: [(200, PRICED_HTML, {})]})

    def boom(*args, **kwargs):
        raise RuntimeError("extractor bug")

    monkeypatch.setattr(fetcher, "_extract", boom)
    result = fetcher.fetch(url)
    assert isinstance(result, FetchResult)
    assert result.status == FetchStatus.UNREACHABLE
    assert "extractor bug" in result.error_message


# ---------------------------------------------------------------- #25 429 / blocked

def test_429_waits_at_most_once_per_url(monkeypatch, _no_sleep_no_browser):
    url = "https://shop.example/item"
    router = _route(monkeypatch, {url: [(429, "slow down", {"Retry-After": "10"})]})
    result = fetcher.fetch(url, use_browser_fallback=False)
    assert result.status == FetchStatus.BLOCKED
    assert _no_sleep_no_browser["sleeps"] == [10.0]
    assert router.calls.count(url) == 2


def test_429_with_long_retry_after_is_not_waited_for(monkeypatch, _no_sleep_no_browser):
    url = "https://shop.example/item"
    router = _route(monkeypatch, {url: [(429, "slow down", {"Retry-After": "60"})]})
    result = fetcher.fetch(url, use_browser_fallback=False)
    assert result.status == FetchStatus.BLOCKED
    assert _no_sleep_no_browser["sleeps"] == []
    assert router.calls.count(url) == 1


def test_rate_limited_host_is_remembered(monkeypatch, _no_sleep_no_browser):
    a, b = "https://shop.example/a", "https://shop.example/b"
    _route(monkeypatch, {a: [(429, "", {"Retry-After": "5"})], b: [(429, "", {"Retry-After": "5"})]})
    fetcher.fetch(a, use_browser_fallback=False)
    fetcher.fetch(b, use_browser_fallback=False)
    assert _no_sleep_no_browser["sleeps"] == [5.0]  # only the first URL waited


def test_429_then_success(monkeypatch, _no_sleep_no_browser):
    url = "https://shop.example/item"
    _route(monkeypatch, {url: [(429, "", {"Retry-After": "2"}), (200, PRICED_HTML, {})]})
    result = fetcher.fetch(url, use_browser_fallback=False)
    assert result.status == FetchStatus.OK
    assert result.price == Decimal("49.90")


@pytest.mark.parametrize(
    "status, body",
    [
        (403, "Forbidden"),
        (200, "<html><head><title>Just a moment...</title></head></html>"),
    ],
)
def test_blocked_page_tries_browser_fallback(monkeypatch, status, body):
    url = "https://shop.example/item"
    _route(monkeypatch, {url: [(status, body, {})]})
    seen = []

    def fake_browser(u, selector, http_status, raw_path, prior_result):
        seen.append(prior_result.status)
        return FetchResult(status=FetchStatus.OK, final_url=u, price=Decimal("12.00"), currency="ILS")

    monkeypatch.setattr(fetcher, "_playwright_fallback", fake_browser)
    result = fetcher.fetch(url)
    assert seen == [FetchStatus.BLOCKED]
    assert result.status == FetchStatus.OK
    assert result.price == Decimal("12.00")


def test_blocked_stays_blocked_when_browser_finds_nothing(monkeypatch, _no_sleep_no_browser):
    url = "https://shop.example/item"
    _route(monkeypatch, {url: [(403, "Forbidden", {})]})
    result = fetcher.fetch(url)
    assert result.status == FetchStatus.BLOCKED
    assert _no_sleep_no_browser["browser"] == [url]


def test_blocked_without_browser_fallback_flag(monkeypatch, _no_sleep_no_browser):
    url = "https://shop.example/item"
    _route(monkeypatch, {url: [(403, "Forbidden", {})]})
    assert fetcher.fetch(url, use_browser_fallback=False).status == FetchStatus.BLOCKED
    assert _no_sleep_no_browser["browser"] == []


# ---------------------------------------------------------------- Shopify (#25, #26)

SHIRT = {
    "title": "Shirt",
    "variants": [
        {"id": 111, "price": 9900, "available": False},
        {"id": 222, "price": 14900, "available": True},
    ],
}


def test_shopify_json_rescues_blocked_html(monkeypatch, _no_sleep_no_browser):
    url = "https://shop.example/products/shirt?variant=222"
    _route(monkeypatch, {
        "https://shop.example/products/shirt": [(403, "Forbidden", {})],
        "https://shop.example/products/shirt.js": [(200, SHIRT, {})],
    })
    result = fetcher.fetch(url)
    assert result.status == FetchStatus.OK
    assert result.price == Decimal("149")
    assert result.http_status == 200
    assert _no_sleep_no_browser["browser"] == []


def test_shopify_unpinned_uses_first_variant_with_its_own_stock(monkeypatch):
    _route(monkeypatch, {
        "https://shop.example/products/shirt": [(200, PLAIN_HTML, {})],
        "https://shop.example/products/shirt.js": [(200, SHIRT, {})],
    })
    result = fetcher.fetch("https://shop.example/products/shirt")
    assert result.status == FetchStatus.OK
    assert result.price == Decimal("99")
    assert result.in_stock is False


def test_shopify_pinned_variant(monkeypatch):
    _route(monkeypatch, {
        "https://shop.example/products/shirt": [(200, PLAIN_HTML, {})],
        "https://shop.example/products/shirt.js": [(200, SHIRT, {})],
    })
    result = fetcher.fetch("https://shop.example/products/shirt?variant=111")
    assert result.price == Decimal("99")
    assert result.in_stock is False


def test_shopify_missing_pinned_variant_is_an_error(monkeypatch, _no_sleep_no_browser):
    _route(monkeypatch, {
        "https://shop.example/products/shirt": [(200, PRICED_HTML, {})],
        "https://shop.example/products/shirt.js": [(200, SHIRT, {})],
    })
    result = fetcher.fetch("https://shop.example/products/shirt?variant=999")
    assert result.status == FetchStatus.NOT_FOUND
    assert result.price is None
    assert "999" in result.error_message
    assert _no_sleep_no_browser["browser"] == []


@pytest.mark.parametrize(
    "url, js_url",
    [
        ("https://shop.example/products/shirt", "https://shop.example/products/shirt.js"),
        ("https://shop.example/products/shirt/", "https://shop.example/products/shirt.js"),
        ("https://shop.example/collections/sale/products/shirt?variant=111",
         "https://shop.example/products/shirt.js"),
        ("https://shop.example/en-us/products/shirt", "https://shop.example/en-us/products/shirt.js"),
        ("https://shop.example/he/collections/sale/products/shirt",
         "https://shop.example/he/products/shirt.js"),
        ("https://shop.example/p/thing", None),
        ("https://shop.example/products", None),
        ("https://shop.example/blog/news/products/shirt", None),
    ],
)
def test_shopify_js_url(url, js_url):
    assert fetcher._shopify_js_url(url) == js_url


def test_shopify_collection_path_uses_json(monkeypatch):
    router = _route(monkeypatch, {
        "https://shop.example/collections/sale/products/shirt": [(200, PLAIN_HTML, {})],
        "https://shop.example/products/shirt.js": [(200, SHIRT, {})],
    })
    result = fetcher.fetch("https://shop.example/collections/sale/products/shirt?variant=222")
    assert result.status == FetchStatus.OK
    assert result.price == Decimal("149")
    assert "https://shop.example/products/shirt.js" in router.calls
