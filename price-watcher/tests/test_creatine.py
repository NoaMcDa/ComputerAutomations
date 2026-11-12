"""Creatine watcher: iHerb price parsing, de-duplication, filtering, direction (#37, #45).

From origin/main, adapted to the merged creatine module (the PR #57 one, which
was checked against live iHerb): one search page is read (httpx, then the
headless browser), the cell count is the de-duplicated count, a same-price
change of product is direction "changed", per-oz prices stay "oz" (not
comparable), and a page with zero parseable prices is "fetch_failed". The
multi-page scan (`scan_iherb_detailed`, `IherbScan`, `_Page`, `search_url`) was
not merged, so its tests were dropped.
"""
from __future__ import annotations

import builtins
from decimal import Decimal

import httpx
import pytest

from price_watcher import creatine
from price_watcher.creatine import (
    CreatineProduct,
    _parse_cells,
    _parse_price,
    compare,
    is_monohydrate_powder,
)
from price_watcher.http_headers import USER_AGENT


def _read(fixtures_dir, name: str) -> str:
    return (fixtures_dir / name).read_text(encoding="utf-8")


def scan_and_report(products, cells):
    """Run creatine.scan_and_report() on already-parsed products (no network)."""
    def fake_scan():
        creatine._scan_cells_seen = cells
        return products
    orig = creatine.scan_iherb
    creatine.scan_iherb = fake_scan
    try:
        return creatine.scan_and_report()
    finally:
        creatine.scan_iherb = orig


class _Resp:
    def __init__(self, status, text):
        self.status_code, self.text = status, text


def _offline(monkeypatch, resp=None, exc=None):
    """httpx answers with `resp` (or raises `exc`); Playwright is not available."""
    class Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url):
            if exc:
                raise exc
            return resp
    monkeypatch.setattr(creatine.httpx, "Client", Client)
    real = builtins.__import__

    def fake(name, *a, **k):
        if name.startswith("playwright"):
            raise ImportError(name)
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake)


def _prod(name="Brand, Creatine Monohydrate, 500 g", per100="9.49", url="", currency="ILS"):
    return CreatineProduct(
        name=name, url=url, price=Decimal("47.45"), currency=currency,
        price_per_100g=Decimal(per100) if per100 is not None else None,
        unit="100g", on_sale=False, seller="iHerb",
    )


# ---------------------------------------------------------------------------
# #37 part 2: _parse_price
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text, price, currency, per_unit, unit, on_sale",
    [
        ("₪94.64 (₪9.49/100g)", "94.64", "ILS", "9.49", "100g", False),
        ("₪61.87 (₪0.26/count)", "61.87", "ILS", "0.26", "count", False),
        ("₪78.64 (₪22.21/100g) 25% off ₪104.85", "78.64", "ILS", "22.21", "100g", True),
        # old + sale price: keep the sale price and the per-unit price
        ("₪104.85 ₪78.64 (₪22.21/100g)", "78.64", "ILS", "22.21", "100g", True),
        # RTL mark before the price
        ("‏₪94.64 (₪9.49/100g)", "94.64", "ILS", "9.49", "100g", False),
        ("‏₪94.64‏ (‏₪9.49‏/100 גרם)", "94.64", "ILS", "9.49", "100g", False),
        ("Price: ₪94.64 (₪9.49/100g)", "94.64", "ILS", "9.49", "100g", False),
        # currency from cookie / geo
        ("$24.99 ($2.50/100g)", "24.99", "USD", "2.50", "100g", False),
        ("€19,99 (€3,99/100g)", "19.99", "EUR", "3.99", "100g", False),
        ("£18.00 (£1.80/100g)", "18.00", "GBP", "1.80", "100g", False),
        # tolerant units, normalised to per-100g
        ("₪94.64 (₪9.49/100 g)", "94.64", "ILS", "9.49", "100g", False),
        ("₪94.64 (₪94.90/kg)", "94.64", "ILS", "9.49", "100g", False),
        ('₪94.64 (₪94.90/ק"ג)', "94.64", "ILS", "9.49", "100g", False),
        # (per-oz stays "oz", not converted: see test_creatine_parse.py)
        ("₪94.64 (₪0.0949/g)", "94.64", "ILS", "9.49", "100g", False),
        # volume cannot be turned into grams: kept as per-100ml
        ("₪94.64 (₪9.49/100ml)", "94.64", "ILS", "9.49", "100ml", False),
        # number before the symbol (Hebrew locale)
        ("94.64 ₪ (9.49 ₪/100 גרם)", "94.64", "ILS", "9.49", "100g", False),
        ("₪1,234.50 (₪12.35/100g)", "1234.50", "ILS", "12.35", "100g", False),
    ],
)
def test_parse_price(text, price, currency, per_unit, unit, on_sale):
    got = _parse_price(text)
    assert got is not None, text
    p, cur, per, u, sale = got
    assert p == Decimal(price)
    assert cur == currency
    assert per == Decimal(per_unit)
    assert u == unit
    assert sale is on_sale


@pytest.mark.parametrize("text", ["", "   ", "‏", "Out of stock", "25% off"])
def test_parse_price_rejects_non_prices(text):
    assert _parse_price(text) is None


def test_usd_page_is_parsed_not_reported_as_blocked(fixtures_dir):
    html = _read(fixtures_dir, "creatine_iherb_search_p1.html").replace("₪", "$")
    products, cells = _parse_cells(html)
    assert cells == 7  # 8 cells, one repeated product counted once
    assert products and all(p.currency == "USD" for p in products)


# ---------------------------------------------------------------------------
# #45: de-duplication and product links
# ---------------------------------------------------------------------------

def test_parse_cells_dedupes_on_product_id_and_ignores_brand_badge(fixtures_dir):
    products, cells = _parse_cells(_read(fixtures_dir, "creatine_iherb_search_p1.html"))
    assert cells == 7  # de-duplicated count
    assert len(products) == 7  # Micro Ingredients appears twice (sponsored + organic)
    urls = [p.url for p in products]
    assert not any("iherb-brands" in u for u in urls)
    assert all("/pr/" in u and u.startswith("https://il.iherb.com/") for u in urls)
    cgn = products[0]
    assert cgn.url.endswith("/71026")
    assert sum("146523" in u for u in urls) == 1
    # first occurrence kept
    assert next(p for p in products if "146523" in p.url).url.endswith("?sponsored=1")


def test_link_from_title_anchor_or_first_pr_anchor():
    html = """
    <div class="product-cell">
      <a href="/iherb-brands">badge</a>
      <a href="/c/creatine">category</a>
      <div class="product-title">CGN, Creatine Monohydrate, 454 g</div>
      <a href="/pr/cgn-creatine/71026"><img></a>
      <div class="product-price">₪43.08 (₪9.49/100g)</div>
    </div>
    <div class="product-cell">
      <a href="/iherb-brands">badge</a>
      <a class="product-title" href="/pr/other-creatine/42">Other, Creatine Monohydrate, 1 kg</a>
      <div class="product-price">₪99.00 (₪9.90/100g)</div>
    </div>"""
    products, _ = _parse_cells(html)
    assert [p.url for p in products] == [
        "https://il.iherb.com/pr/cgn-creatine/71026",
        "https://il.iherb.com/pr/other-creatine/42",
    ]


def test_counts_use_deduplicated_products(fixtures_dir):
    html = _read(fixtures_dir, "creatine_iherb_search_p1.html")
    products, cells = _parse_cells(html)
    report = scan_and_report(products, cells)
    assert report.status == "ok"
    assert report.parsed == 7
    assert report.cells_seen == 7
    # CGN, Micro Ingredients (once), Sports Research
    assert report.powders == 3
    names = [p.name for p in report.products]
    assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# #37 part 3: monohydrate-powder filter and pagination
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name, expected",
    [
        ("California Gold Nutrition, Creatine Monohydrate, Unflavored, 454 g", True),
        ('Micro Ingredients, אבקת קריאטין מונוהידרט, ללא טעם, 1 ק"ג', True),
        ("Optimum Nutrition, Micronized Creatine Powder, Unflavored, 1.32 lb", True),
        ("Brand X, Creatine Gummies, Sour Apple, 60 Gummies", False),
        ("Brand Y, Creatine Monohydrate, Fruit Punch Flavored, 500 g", False),
        ("Brand Y, Creatine, Fruit Punch Flavour, 500 g", False),
        ("Brand Z, Creatine + Beta-Alanine Blend, 400 g", False),
        ("Brand, Creatine HCl Powder, 120 g", False),
        ("Brand, Pre-Workout with Creatine, 300 g", False),
        ("Brand, קריאטין בטעם לימון, 300 גרם", False),
        ("Brand, Whey Protein, Vanilla, 2 lb", False),
    ],
)
def test_is_monohydrate_powder(name, expected):
    assert is_monohydrate_powder(_prod(name=name)) is expected


def test_capsules_are_never_powders():
    assert not is_monohydrate_powder(_prod(per100=None))


def test_cheapest_skips_gummies_and_flavoured_mixes(fixtures_dir):
    products, cells = _parse_cells(_read(fixtures_dir, "creatine_iherb_search_p1.html"))
    report = scan_and_report(products, cells)
    assert report.cheapest.name.startswith("California Gold Nutrition")
    assert report.cheapest.price_per_100g == Decimal("9.49")
    assert all(is_monohydrate_powder(p) for p in report.products)


# ---------------------------------------------------------------------------
# #37 part 2b: blocked vs parsed 0 of N vs no powders
# ---------------------------------------------------------------------------

def test_status_blocked(monkeypatch):
    _offline(monkeypatch, _Resp(403, "x"))
    report = creatine.scan_and_report()
    assert report.status == "blocked"
    assert "403" in report.error


def test_status_challenge_page_counts_as_blocked(monkeypatch, fixtures_dir):
    _offline(monkeypatch, _Resp(200, _read(fixtures_dir, "creatine_iherb_challenge.html")))
    assert creatine.scan_and_report().status == "blocked"


def test_status_fetch_failed(monkeypatch):
    _offline(monkeypatch, exc=httpx.ConnectTimeout("t"))
    report = creatine.scan_and_report()
    assert report.status == "fetch_failed"
    assert "ConnectTimeout" in report.error


def test_status_parsed_zero_of_n_cells(monkeypatch):
    html = "".join(
        f'<div class="product-cell"><div class="product-title">Creatine {i}</div>'
        f'<div class="product-price">see price in cart</div></div>'
        for i in range(5)
    )
    _offline(monkeypatch, _Resp(200, html))
    report = creatine.scan_and_report()
    assert report.status == "fetch_failed"  # a parsing problem, not a block
    assert "5" in report.error and "חסם" not in report.error
    assert report.cells_seen == 5


def test_status_no_products_on_page(monkeypatch):
    # A 200 page without product cells is most likely a Cloudflare challenge.
    _offline(monkeypatch, _Resp(200, "<html><body>nothing</body></html>"))
    report = creatine.scan_and_report()
    assert report.status == "blocked"
    assert "ללא מוצרים" in report.error


def test_status_no_powder(monkeypatch):
    html = (
        '<div class="product-cell"><a href="/pr/x/1"><div class="product-title">Creatine, 120 Capsules</div></a>'
        '<div class="product-price">₪61.87 (₪0.26/count)</div></div>'
    )
    _offline(monkeypatch, _Resp(200, html))
    report = creatine.scan_and_report()
    assert report.status == "no_powder"
    assert report.parsed == 1 and report.powders == 0


# ---------------------------------------------------------------------------
# #37 part 1: three-way direction
# ---------------------------------------------------------------------------

def test_same_price_different_product_is_not_rose():
    prev = _prod(name="Brand A, Creatine Monohydrate, 500 g", url="https://il.iherb.com/pr/a/1")
    new = _prod(name="Brand B, Creatine Monohydrate, 500 g", url="https://il.iherb.com/pr/b/2")
    assert compare(prev, new) == (True, "changed")


@pytest.mark.parametrize("new_price, direction", [("8.00", "dropped"), ("10.00", "rose")])
def test_direction_dropped_and_rose(new_price, direction):
    assert compare(_prod(per100="9.49"), _prod(per100=new_price)) == (True, direction)


def test_unchanged_and_first_observation():
    assert compare(_prod(), _prod()) == (False, None)
    assert compare(None, _prod()) == (True, None)


def test_same_product_renamed_by_language_is_unchanged():
    url = "https://il.iherb.com/pr/micro-ingredients-creatine/146523"
    prev = _prod(name="Micro Ingredients, Creatine Monohydrate, 1 kg", url=url)
    new = _prod(name='Micro Ingredients, אבקת קריאטין מונוהידרט, 1 ק"ג', url=url + "?sponsored=1")
    assert compare(prev, new) == (False, None)


def test_currency_change_has_no_direction():
    assert compare(_prod(per100="9.49"), _prod(per100="2.50", currency="USD")) == (True, "changed")


def test_daily_email_does_not_say_rose_at_identical_price(fixtures_dir):
    from price_watcher import email_sender
    from price_watcher.digest import DailyReport

    prev = _prod(name="Brand A, Creatine Monohydrate, 500 g", url="https://il.iherb.com/pr/a/1")
    creatine._save_state({"schema_version": 1, "cheapest": creatine._product_to_dict(prev)})
    new = _prod(name="Brand B, Creatine Monohydrate, 500 g", url="https://il.iherb.com/pr/b/2")
    report = scan_and_report([new], 1)
    assert report.changed and report.direction == "changed"

    payload = email_sender.build_daily_payload(DailyReport(creatine=report))
    assert payload is not None
    text = "\n".join([payload.subject, payload.text_body, payload.html_body])
    assert "עלה" not in text
    assert "אותו מחיר" in text


def test_shared_user_agent():
    assert creatine._HEADERS["User-Agent"] == USER_AGENT
    assert creatine._BROWSER_UA == USER_AGENT
    assert "Chrome/" in USER_AGENT
