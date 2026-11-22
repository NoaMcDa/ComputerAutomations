"""creatine.scan_and_report: direction, failure reasons, fetch fallbacks (all mocked)."""

from __future__ import annotations

import builtins
import sys
import types
from decimal import Decimal
from pathlib import Path

import httpx

from price_watcher import creatine

FIXTURES = Path(__file__).parent / "fixtures" / "daily"


def _prod(name, per100, cur="ILS"):
    return creatine.CreatineProduct(
        name=name, url="https://il.iherb.com/pr/x", price=Decimal("90"), currency=cur,
        price_per_100g=Decimal(per100), unit="100g", on_sale=False, seller="iHerb")


def _state(prod):
    creatine._save_state({"schema_version": 1, "cheapest": creatine._product_to_dict(prod),
                          "last_scanned_at": None})


def _scan_with(monkeypatch, products):
    monkeypatch.setattr(creatine, "scan_iherb", lambda: products)
    return creatine.scan_and_report()


def test_same_price_different_product_is_not_reported_as_rose(monkeypatch):
    _state(_prod("Brand A", "9.49"))
    r = _scan_with(monkeypatch, [_prod("Brand B", "9.49")])
    assert r.changed and r.direction == "changed"


def test_direction_dropped_and_rose(monkeypatch):
    _state(_prod("Brand A", "9.49"))
    assert _scan_with(monkeypatch, [_prod("Brand A", "8.00")]).direction == "dropped"
    assert _scan_with(monkeypatch, [_prod("Brand A", "11.00")]).direction == "rose"
    r = _scan_with(monkeypatch, [_prod("Brand A", "9.49")])
    assert not r.changed and r.direction is None


def test_old_state_file_still_loads(monkeypatch):
    creatine._save_state({"schema_version": 1, "cheapest": {
        "name": "Old", "url": "", "price": "90", "currency": "ILS", "price_per_100g": "9.49",
        "unit": "100g", "on_sale": False, "seller": "iHerb"}, "last_scanned_at": None})
    r = _scan_with(monkeypatch, [_prod("Old", "9.49")])
    assert r.status == "ok" and not r.changed


# ---- fetch failures -------------------------------------------------------

class _Resp:
    def __init__(self, status, text):
        self.status_code, self.text = status, text


def _patch_httpx(monkeypatch, resp=None, exc=None):
    class Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url):
            if exc:
                raise exc
            return resp
    monkeypatch.setattr(creatine.httpx, "Client", Client)


def _no_playwright(monkeypatch):
    real = builtins.__import__

    def fake(name, *a, **k):
        if name.startswith("playwright"):
            raise ImportError(name)
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake)


def test_cloudflare_403_and_no_playwright_is_reported_as_blocked(monkeypatch):
    html = (FIXTURES / "iherb_cloudflare_403.html").read_text(encoding="utf-8")
    _patch_httpx(monkeypatch, _Resp(403, html))
    _no_playwright(monkeypatch)
    r = creatine.scan_and_report()
    assert r.status == "blocked"
    assert "403" in r.error and "Cloudflare" in r.error and "Playwright" in r.error


def test_browser_not_installed_is_named(monkeypatch):
    _patch_httpx(monkeypatch, _Resp(403, "x"))

    class _CM:
        def __enter__(self):
            raise RuntimeError("BrowserType.launch: Executable doesn't exist at C:\\x")

        def __exit__(self, *a):
            return False

    fake = types.ModuleType("playwright.sync_api")
    fake.sync_playwright = lambda: _CM()
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake)
    r = creatine.scan_and_report()
    assert r.status == "blocked" and "Chromium" in r.error and "playwright install" in r.error


def test_network_error_is_fetch_failed_not_blocked(monkeypatch):
    _patch_httpx(monkeypatch, exc=httpx.ConnectTimeout("t"))
    _no_playwright(monkeypatch)
    r = creatine.scan_and_report()
    assert r.status == "fetch_failed" and "ConnectTimeout" in r.error


def test_zero_parsed_cells_is_not_reported_as_blocking(monkeypatch):
    html = ('<body><div class="product-cell"><span class="product-title">X</span>'
            '<span class="product-price">price on request 123</span></div></body>')
    _patch_httpx(monkeypatch, _Resp(200, html))
    r = creatine.scan_and_report()
    assert r.status == "fetch_failed" and r.cells_seen == 1
    assert "לא ניתן היה לפענח" in r.error and "חסם" not in r.error


def test_httpx_403_falls_back_to_browser_html(monkeypatch):
    _patch_httpx(monkeypatch, _Resp(403, "x"))
    cell = ('<div class="product-cell"><span class="product-title">Creatine Monohydrate</span>'
            '<span class="product-price">₪94.64 (₪9.49/100g)</span></div>')
    monkeypatch.setattr(creatine, "_fetch_html_browser", lambda: f"<body>{cell}</body>")
    r = creatine.scan_and_report()
    assert r.status == "ok" and str(r.cheapest.price_per_100g) == "9.49"


def test_only_non_powder_products_gives_no_powder(monkeypatch):
    r = _scan_with(monkeypatch, [_prod("Creatine Gummies", "2.00")])
    assert r.status == "no_powder" and r.error
