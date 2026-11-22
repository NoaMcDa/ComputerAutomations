"""Offline tests for price parsing and each extraction step (saved HTML fixtures).

Covers issues #42 (several prices in one element), #43 (pinned JSON-LD
variant missing), #40 (merged `_extract`) and the fixture tests asked for in #4.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from price_watcher import fetcher
from price_watcher.contracts import FetchStatus
from price_watcher.fetcher import _parse_price

FIX = Path(__file__).resolve().parent / "fixtures" / "fetcher"


def _html(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def _extract(name: str, url: str = "https://shop.example/item", selector: str | None = None):
    """Run the extraction ladder on a fixture, without any network (no Shopify step)."""
    return fetcher._extract(
        _html(name), url, url, selector, 200, None, include_shopify=False
    )


# ---------------------------------------------------------------------------
# _parse_price: number formats and currencies
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text, price, currency",
    [
        ("1,299.90", "1299.90", None),
        ("1.299,90", "1299.90", None),
        ("1.299.000", "1299000", None),
        ("1,299,000", "1299000", None),
        ("1,299", "1299", None),
        ("129,9", "129.9", None),
        ("99", "99", None),
        ("0.999", "0.999", None),
        ("₪129.90", "129.90", "ILS"),
        ("₪ 1,299.90", "1299.90", "ILS"),
        ('99.90 ש"ח', "99.90", "ILS"),
        ("99.90 ש״ח", "99.90", "ILS"),
        ("NIS 49", "49", "ILS"),
        ("ILS 1,299.90", "1299.90", "ILS"),
        ("$1,299.90", "1299.90", "USD"),
        ("€1.299,90", "1299.90", "EUR"),
        ("1 299,90 €", "1299.90", "EUR"),
        ("£12.50", "12.50", "GBP"),
        ("¥1,000", "1000", "JPY"),
        ("₪99.90 ₪99.90", "99.90", "ILS"),  # same price twice is unambiguous
        ("Pack of 2 – ₪49.90", "49.90", "ILS"),  # only one number is a price
        ("Details 99", "99", None),  # "ils" inside a word is not a currency
    ],
)
def test_parse_price_formats(text, price, currency):
    assert _parse_price(text) == (Decimal(price), currency)


@pytest.mark.parametrize(
    "text",
    [
        "₪129.90 ₪99.90",  # issue #42
        "₪129.90₪99.90",  # same, as get_text(strip=True) glues it
        "Was ₪129.90 Now ₪99.90",  # issue #42
        "129.90 99.90",
        "129.9099.90",  # two prices already glued: not 129909990
        "Call for price",
        "",
    ],
)
def test_parse_price_refuses_ambiguous_or_missing(text):
    with pytest.raises(ValueError):
        _parse_price(text)


def test_parse_price_never_concatenates_tokens():
    # The #42 regression: whatever happens, never a 9-digit number.
    for text in ("₪129.90 ₪99.90", "Was ₪129.90 Now ₪99.90"):
        try:
            value, _ = _parse_price(text)
        except ValueError:
            continue
        assert value < 1000


# ---------------------------------------------------------------------------
# Extraction steps from saved fixtures
# ---------------------------------------------------------------------------

def test_jsonld_product_hebrew():
    r = _extract("jsonld_product.html")
    assert r.status == FetchStatus.OK
    assert r.price == Decimal("79.90")
    assert r.currency == "ILS"
    assert r.in_stock is True
    assert r.title == "סווטשירט אוף שולדר אספרסו"


def test_meta_og_eu_format_and_out_of_stock():
    r = _extract("meta_og.html")
    assert r.status == FetchStatus.OK
    assert r.price == Decimal("1299.90")
    assert r.currency == "EUR"
    assert r.in_stock is False
    assert r.title == "Tempo Triangle Vest"


def test_microdata_prefers_price_not_struck_through():
    r = _extract("microdata.html")
    assert r.status == FetchStatus.OK
    assert r.price == Decimal("999.90")
    assert r.regular_price == Decimal("1299.90")
    assert r.currency == "USD"
    assert r.in_stock is True


def test_selector_del_ins_reads_sale_price():
    # issue #42: <del>₪129.90</del> <ins>₪99.90</ins>
    r = _extract("selector_sale.html", selector=".price")
    assert r.status == FetchStatus.OK
    assert r.price == Decimal("99.90")
    assert r.regular_price == Decimal("129.90")
    assert r.currency == "ILS"


def test_selector_line_through_style_is_regular_price():
    r = _extract("selector_sale.html", selector=".price-struck")
    assert r.price == Decimal("99.90")
    assert r.regular_price == Decimal("129.90")
    assert r.currency == "ILS"


def test_selector_plain_text_with_two_prices_is_refused():
    r = _extract("selector_sale.html", selector=".price-text")
    assert r.status == FetchStatus.PRICE_NOT_FOUND
    assert r.price is None


def test_selector_single_price_and_missing():
    assert _extract("selector_sale.html", selector=".price-single").price == Decimal("1299.90")
    assert _extract("selector_sale.html", selector=".price-none").status == FetchStatus.PRICE_NOT_FOUND
    assert _extract("selector_sale.html", selector=".nope").status == FetchStatus.PRICE_NOT_FOUND
    # Invalid selector syntax never raises out of the fetcher
    assert _extract("selector_sale.html", selector="div[[").status == FetchStatus.PRICE_NOT_FOUND


def test_no_price_anywhere_is_price_not_found():
    r = _extract("challenge.html")
    assert r.status == FetchStatus.PRICE_NOT_FOUND


# ---------------------------------------------------------------------------
# JSON-LD variants (issue #43)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("param", ["variation", "variant", "sku"])
def test_jsonld_pinned_variant_is_read(param):
    r = _extract("jsonld_productgroup.html", url=f"https://shop.example/p/whey?{param}=222")
    assert r.status == FetchStatus.OK
    assert r.price == Decimal("349.90")
    assert r.title == "Whey 2.5kg"
    assert r.in_stock is False


@pytest.mark.parametrize("param", ["variation", "variant", "sku"])
def test_jsonld_pinned_variant_missing_is_not_found(param):
    r = _extract("jsonld_productgroup.html", url=f"https://shop.example/p/whey?{param}=999")
    assert r.status == FetchStatus.NOT_FOUND
    assert r.price is None
    assert "999" in r.error_message
    assert "JSON-LD" in r.error_message


def test_jsonld_no_variant_in_url_uses_lowest_price():
    r = _extract("jsonld_productgroup.html", url="https://shop.example/p/whey")
    assert r.status == FetchStatus.OK
    assert r.price == Decimal("49.90")


def test_jsonld_offer_url_ids_match_and_miss():
    ok = _extract("jsonld_offers_url_ids.html", url="https://shop.example/p/tee?variant=501")
    assert (ok.status, ok.price, ok.in_stock) == (FetchStatus.OK, Decimal("59.90"), True)
    gone = _extract("jsonld_offers_url_ids.html", url="https://shop.example/p/tee?variant=777")
    assert gone.status == FetchStatus.NOT_FOUND


def test_jsonld_page_without_ids_keeps_lowest_price_fallback():
    r = _extract("jsonld_no_ids.html", url="https://shop.example/p/vest?variation=42")
    assert r.status == FetchStatus.OK
    assert r.price == Decimal("99.00")


def test_jsonld_missing_variant_does_not_fall_through_to_meta():
    # A group page that also has og:price must not let step 3 substitute a price.
    html = _html("jsonld_productgroup.html").replace(
        "<title>",
        '<meta property="og:price:amount" content="49.90"><title>',
    )
    r = fetcher._extract(
        html, "https://s.example/p?variation=999", "https://s.example/p?variation=999",
        None, 200, None, include_shopify=False,
    )
    assert r.status == FetchStatus.NOT_FOUND


# ---------------------------------------------------------------------------
# Issue #40: _extract_from_html merged into _extract
# ---------------------------------------------------------------------------

def test_extract_from_html_is_not_a_second_ladder(monkeypatch):
    # Merge note: the PR #57 tests still call _extract_from_html, so it stays
    # as a thin alias; what #40 asked for is that there is only ONE ladder.
    calls = []

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return "result"

    monkeypatch.setattr(fetcher, "_extract", spy)
    assert fetcher._extract_from_html("<html></html>", "u", "u", None, 200, None) == "result"
    assert calls == [{"include_shopify": False}]


# ---------------------------------------------------------------------------
# Merge: edge cases ported from PR #56 into the PR #57 parser
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "₪100 – 200",          # currency before the first number: a range
    "100 ₪ – 200 ₪",
    "1–2 ₪",
])
def test_dash_between_priced_numbers_is_still_a_range(text):
    with pytest.raises(ValueError):
        _parse_price(text)


def test_count_dash_price_reads_the_price():
    assert _parse_price("2 יח' – ₪49.90") == (Decimal("49.90"), "ILS")


def test_jsonld_offer_url_ids_are_matched_per_offer():
    # Two offers of ONE Product, each with its own ?variant= url: the pinned
    # one is read even though its sibling is cheaper.
    r = _extract("jsonld_offers_url_ids.html", url="https://shop.example/p/tee?variant=501")
    assert r.price == Decimal("59.90")
