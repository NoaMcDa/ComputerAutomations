"""Watch list add / list / remove through the real CLI (issue #3).

`fetch` is replaced by a stub except in the end-to-end #42 test, which uses a
local page on 127.0.0.1.
"""
from __future__ import annotations

import json
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from typer.testing import CliRunner

from price_watcher import fetcher, paths, watchlist
from price_watcher.cli import app
from price_watcher.contracts import FetchResult, FetchStatus

runner = CliRunner()

HEB_READABLE = "https://www.castro.com/סייל/נשים/סווטשירט-7310573"
HEB_ENCODED = (
    "https://www.castro.com/%D7%A1%D7%99%D7%99%D7%9C/%D7%A0%D7%A9%D7%99%D7%9D/"
    "%D7%A1%D7%95%D7%95%D7%98%D7%A9%D7%99%D7%A8%D7%98-7310573"
)


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch):
    monkeypatch.setenv("COLUMNS", "300")


@pytest.fixture
def fake_fetch(monkeypatch):
    calls: list[tuple[str, str | None]] = []
    result = {"value": FetchResult(status=FetchStatus.OK, final_url="x",
                                   price=Decimal("79.90"), currency="ILS")}

    def _fake(url, selector=None, **kw):
        calls.append((url, selector))
        return result["value"]

    monkeypatch.setattr(fetcher, "fetch", _fake)
    return calls, result


def _invoke(*args):
    return runner.invoke(app, list(args))


def _stored():
    return watchlist.load_all()


# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------

def test_normalise_drops_tracking_keeps_variant_params():
    n = watchlist._normalise_url(
        "https://shop.example/p/whey?utm_source=x&variation=222&fbclid=y&variant=5#reviews"
    )
    assert n == "https://shop.example/p/whey?variation=222&variant=5"


def test_normalise_makes_hebrew_path_readable():
    assert watchlist._normalise_url(HEB_ENCODED) == HEB_READABLE
    assert watchlist._normalise_url(HEB_READABLE) == HEB_READABLE
    # encoded ASCII (here an encoded slash) keeps its meaning
    assert watchlist._normalise_url("https://a.example/x%2Fy") == "https://a.example/x%2Fy"


@pytest.mark.parametrize("bad", ["not a url", "shop.example/p", "ftp://shop.example/p", "javascript:alert(1)"])
def test_normalise_rejects_invalid(bad):
    with pytest.raises(ValueError):
        watchlist._normalise_url(bad)


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------

def test_add_normalises_fetches_once_and_reports_price(fake_fetch):
    calls, _ = fake_fetch
    r = _invoke("add", "https://shop.example/p?utm_campaign=z&variant=7", "--name", "Tee",
                "--selector", ".price")
    assert r.exit_code == 0, r.output
    assert "79.90 ILS" in r.output
    assert calls == [("https://shop.example/p?variant=7", ".price")]
    [w] = _stored()
    assert (w.url, w.name, w.selector) == ("https://shop.example/p?variant=7", "Tee", ".price")


def test_add_rejects_duplicate(fake_fetch):
    calls, _ = fake_fetch
    assert _invoke("add", "https://shop.example/p?variant=7").exit_code == 0
    r = _invoke("add", "https://shop.example/p?variant=7&utm_source=mail#top")
    assert r.exit_code == 1
    assert "Already watching" in r.output
    assert len(_stored()) == 1
    assert len(calls) == 1  # no second fetch


def test_add_rejects_hebrew_duplicate_in_other_encoding(fake_fetch):
    assert _invoke("add", HEB_ENCODED).exit_code == 0
    assert _stored()[0].url == HEB_READABLE
    r = _invoke("add", HEB_READABLE)
    assert r.exit_code == 1
    assert len(_stored()) == 1


def test_add_invalid_url(fake_fetch):
    calls, _ = fake_fetch
    r = _invoke("add", "ftp://shop.example/p")
    assert r.exit_code == 1
    assert "Invalid URL" in r.output
    assert calls == [] and _stored() == []


@pytest.mark.parametrize(
    "status, message",
    [
        (FetchStatus.PRICE_NOT_FOUND, None),
        (FetchStatus.BLOCKED, "Blocked (HTTP 403)"),
        (FetchStatus.NOT_FOUND, "Shopify variant 9 not found in product"),
        (FetchStatus.UNREACHABLE, "connection refused"),
    ],
)
def test_add_unreadable_page_is_rejected_with_selector_tip(fake_fetch, status, message):
    _, result = fake_fetch
    result["value"] = FetchResult(status=status, final_url="x", error_message=message)
    r = _invoke("add", "https://shop.example/p")
    assert r.exit_code == 1
    assert "Could not read price" in r.output
    assert (message or status.value) in r.output
    assert "--selector" in r.output
    assert _stored() == []


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def test_list_empty():
    r = _invoke("list")
    assert r.exit_code == 0
    assert "No watches yet" in r.output


def test_list_shows_label_last_price_and_check_time(fake_fetch):
    assert _invoke("add", "https://shop.example/p", "--name", "My Tee").exit_code == 0
    [w] = _stored()
    paths.state_file().write_text(json.dumps({
        "schema_version": 1,
        "watches": {w.id: {"last_price": "79.90", "currency": "ILS",
                           "last_check_time": "2026-11-12T09:00:00+02:00"}},
    }), encoding="utf-8")
    r = _invoke("list")
    assert r.exit_code == 0
    for text in (w.id, "My Tee", "https://shop.example/p", "79.90 ILS", "2026-11-12T09:00:00+02:00"):
        assert text in r.output


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------

def test_remove_by_id_and_by_url(fake_fetch):
    assert _invoke("add", "https://shop.example/a").exit_code == 0
    assert _invoke("add", "https://shop.example/b?variant=1").exit_code == 0
    a, b = _stored()
    paths.state_file().write_text(json.dumps({"watches": {a.id: {"last_price": "1"},
                                                          b.id: {"last_price": "2"}}}),
                                  encoding="utf-8")

    r = _invoke("remove", a.id)
    assert r.exit_code == 0, r.output
    assert [w.id for w in _stored()] == [b.id]

    r = _invoke("remove", "https://shop.example/b?variant=1&utm_source=x")
    assert r.exit_code == 0, r.output
    assert _stored() == []
    assert json.loads(paths.state_file().read_text(encoding="utf-8"))["watches"] == {}


def test_remove_legacy_encoded_url_by_readable_form(fake_fetch):
    # A watch saved before URLs were stored in readable form.
    from price_watcher.contracts import Watch
    watchlist.save_all([Watch(id="legacy-1", url=HEB_ENCODED)])
    r = _invoke("remove", HEB_READABLE)
    assert r.exit_code == 0, r.output
    assert _stored() == []


def test_remove_unknown():
    r = _invoke("remove", "nothing-here")
    assert r.exit_code == 1
    assert "Not found" in r.output


# ---------------------------------------------------------------------------
# #42 end to end: `add --selector` on a sale-price element
# ---------------------------------------------------------------------------

def test_add_selector_on_sale_element_reads_sale_price(monkeypatch):
    monkeypatch.setattr(fetcher, "_RETRY_DELAYS", ())
    page = (b'<html><head><title>Sale</title></head><body>'
            b'<div class="price"><del>&#8362;129.90</del> <ins>&#8362;99.90</ins></div>'
            b'</body></html>')

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/sale.html"
        r = _invoke("add", url, "--selector", ".price")
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert r.exit_code == 0, r.output
    assert "current price: 99.90 ILS" in r.output
    assert "129909990" not in r.output
