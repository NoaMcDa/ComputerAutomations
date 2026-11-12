"""Creatine watcher: iHerb price parsing, de-duplication, filtering, direction (#37, #45)."""
from __future__ import annotations

from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from price_watcher import creatine
from price_watcher.creatine import (
    CreatineProduct,
    IherbScan,
    _Page,
    _parse_cells,
    _parse_price,
    compare,
    is_monohydrate_powder,
    scan_and_report,
    scan_iherb_detailed,
)
from price_watcher.http_headers import USER_AGENT


def _read(fixtures_dir, name: str) -> str:
    return (fixtures_dir / name).read_text(encoding="utf-8")


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
        ("$24.99 ($0.71/oz)", "24.99", "USD", "2.50", "100g", False),
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
    assert cells == 8
    assert products and all(p.currency == "USD" for p in products)


# ---------------------------------------------------------------------------
# #45: de-duplication and product links
# ---------------------------------------------------------------------------

def test_parse_cells_dedupes_on_product_id_and_ignores_brand_badge(fixtures_dir):
    products, cells = _parse_cells(_read(fixtures_dir, "creatine_iherb_search_p1.html"))
    assert cells == 8
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
    report = scan_and_report(IherbScan(products=products, cells=cells, pages=1))
    assert report.status == "ok"
    assert report.products_scanned == 7
    assert report.cells_seen == 8
    # CGN, Micro Ingredients (once), Sports Research
    assert report.powders_found == 3
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
    report = scan_and_report(IherbScan(products=products, cells=cells, pages=1))
    assert report.cheapest.name.startswith("California Gold Nutrition")
    assert report.cheapest.price_per_100g == Decimal("9.49")
    assert all(is_monohydrate_powder(p) for p in report.products)


def test_search_url_paging():
    assert creatine.search_url(1) == "https://il.iherb.com/search?kw=creatine"
    q = parse_qs(urlsplit(creatine.search_url(3)).query)
    assert q == {"kw": ["creatine"], "p": ["3"]}


def test_scan_reads_several_pages_and_dedupes_across_them(fixtures_dir):
    pages = {
        1: _read(fixtures_dir, "creatine_iherb_search_p1.html"),
        2: _read(fixtures_dir, "creatine_iherb_search_p2.html"),
        3: "<html><body><p>No results</p></body></html>",
    }
    served = []

    def fake_pages(max_pages):
        for n in range(1, max_pages + 1):
            served.append(n)
            yield n, _Page(pages.get(n))

    scan = scan_iherb_detailed(max_pages=5, pages=fake_pages)
    assert scan.pages == 3
    assert served == [1, 2, 3]  # stops at the empty page
    assert len(scan.products) == 8  # 7 from p1 + 1 new from p2 (CGN repeat dropped)
    report = scan_and_report(scan)
    assert report.cheapest.url.endswith("/555")
    assert report.cheapest.price_per_100g == Decimal("8.75")  # 87.50/kg


def test_iter_pages_falls_back_to_browser_and_follows_pages(monkeypatch, fixtures_dir):
    challenge = _read(fixtures_dir, "creatine_iherb_challenge.html")
    p1 = _read(fixtures_dir, "creatine_iherb_search_p1.html")
    p2 = _read(fixtures_dir, "creatine_iherb_search_p2.html")
    httpx_calls, browser_calls = [], []

    def fake_httpx(url):
        httpx_calls.append(url)
        return _Page(None, blocked=True, error="HTTP 403")

    class FakeBrowser:
        closed = False

        def __enter__(self):
            return self

        def get(self, url):
            browser_calls.append(url)
            page = parse_qs(urlsplit(url).query).get("p", ["1"])[0]
            return _Page({"1": p1, "2": p2}.get(page, challenge.replace("cf-challenge", "x")))

        def __exit__(self, *a):
            FakeBrowser.closed = True

    monkeypatch.setattr(creatine, "_fetch_html_httpx", fake_httpx)
    monkeypatch.setattr(creatine, "_BrowserSession", FakeBrowser)
    scan = scan_iherb_detailed(max_pages=4)
    assert httpx_calls == [creatine.search_url(1)]  # httpx is not retried once blocked
    assert browser_calls[:2] == [creatine.search_url(1), creatine.search_url(2)]
    assert len(scan.products) == 8
    assert FakeBrowser.closed


# ---------------------------------------------------------------------------
# #37 part 2b: blocked vs parsed 0 of N vs no powders
# ---------------------------------------------------------------------------

def _one_page(html, blocked=False, error=None):
    def pages(max_pages):
        yield 1, _Page(html, blocked=blocked, error=error)
    return pages


def test_status_blocked():
    scan = scan_iherb_detailed(pages=_one_page(None, blocked=True, error="HTTP 403"))
    report = scan_and_report(scan)
    assert report.status == "blocked"
    assert "403" in report.error


def test_status_challenge_page_counts_as_blocked(fixtures_dir):
    scan = scan_iherb_detailed(pages=_one_page(_read(fixtures_dir, "creatine_iherb_challenge.html")))
    assert scan_and_report(scan).status == "blocked"


def test_status_fetch_failed():
    report = scan_and_report(scan_iherb_detailed(pages=_one_page(None, error="ConnectTimeout")))
    assert report.status == "fetch_failed"
    assert "ConnectTimeout" in report.error


def test_status_parsed_zero_of_n_cells():
    html = "".join(
        f'<div class="product-cell"><div class="product-title">Creatine {i}</div>'
        f'<div class="product-price">see price in cart</div></div>'
        for i in range(5)
    )
    report = scan_and_report(scan_iherb_detailed(pages=_one_page(html)))
    assert report.status == "parse_failed"
    assert "0" in report.error and "5" in report.error
    assert report.cells_seen == 5


def test_status_no_products_on_page():
    report = scan_and_report(scan_iherb_detailed(pages=_one_page("<html><body>nothing</body></html>")))
    assert report.status == "no_products"


def test_status_no_powder():
    html = (
        '<div class="product-cell"><a href="/pr/x/1"><div class="product-title">Creatine, 120 Capsules</div></a>'
        '<div class="product-price">₪61.87 (₪0.26/count)</div></div>'
    )
    report = scan_and_report(scan_iherb_detailed(pages=_one_page(html)))
    assert report.status == "no_powder"
    assert report.products_scanned == 1 and report.powders_found == 0


# ---------------------------------------------------------------------------
# #37 part 1: three-way direction
# ---------------------------------------------------------------------------

def test_same_price_different_product_is_not_rose():
    prev = _prod(name="Brand A, Creatine Monohydrate, 500 g", url="https://il.iherb.com/pr/a/1")
    new = _prod(name="Brand B, Creatine Monohydrate, 500 g", url="https://il.iherb.com/pr/b/2")
    assert compare(prev, new) == (True, "same_price")


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
    assert compare(_prod(per100="9.49"), _prod(per100="2.50", currency="USD")) == (True, None)


def test_daily_email_does_not_say_rose_at_identical_price(fixtures_dir):
    from price_watcher import email_sender
    from price_watcher.digest import DailyReport

    prev = _prod(name="Brand A, Creatine Monohydrate, 500 g", url="https://il.iherb.com/pr/a/1")
    creatine._save_state({"schema_version": 1, "cheapest": creatine._product_to_dict(prev)})
    new = _prod(name="Brand B, Creatine Monohydrate, 500 g", url="https://il.iherb.com/pr/b/2")
    report = scan_and_report(IherbScan(products=[new], cells=1, pages=1))
    assert report.changed and report.direction == "same_price"

    payload = email_sender.build_daily_payload(DailyReport(creatine=report))
    assert payload is not None
    text = "\n".join([payload.subject, payload.text_body, payload.html_body])
    assert "עלה" not in text
    assert "נשאר זהה" in text


def test_shared_user_agent():
    assert creatine._HEADERS["User-Agent"] == USER_AGENT
    assert "Chrome/" in USER_AGENT
