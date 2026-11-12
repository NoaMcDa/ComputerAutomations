"""JSON-LD extraction (Refs #23, #13)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from price_watcher.contracts import FetchStatus
from price_watcher.fetcher import _extract_from_html, _try_jsonld

FIXTURES = Path(__file__).parent / "fixtures" / "fetcher"
MP_URL = "https://www.myprotein.co.il/p/sports-clothing/mp-women-s-tempo-triangle-vest-black/17669270/"


def _page(*blocks: object) -> str:
    scripts = "".join(
        f'<script type="application/ld+json">{json.dumps(b)}</script>' for b in blocks
    )
    return f"<html><head><title>Page</title>{scripts}</head><body></body></html>"


def _jsonld(html: str, url: str = "https://shop.example/item"):
    return _try_jsonld(html, url, url, "Page", 200, None)


def test_myprotein_productgroup_fixture_is_extracted():
    html = (FIXTURES / "myprotein_productgroup.html").read_text(encoding="utf-8")
    result = _jsonld(html, MP_URL)
    assert result is not None
    assert result.status == FetchStatus.OK
    assert result.price == Decimal("159.8")
    assert result.currency == "ILS"
    assert result.in_stock is True
    assert result.title == "MP Women's Tempo Triangle Vest"


def test_myprotein_variation_in_url_picks_that_variant():
    html = (FIXTURES / "myprotein_productgroup.html").read_text(encoding="utf-8")
    result = _jsonld(html, MP_URL + "?variation=17669520")
    assert result is not None
    assert result.price == Decimal("159.8")
    assert result.title == "MP Women's Tempo Triangle Vest Black S"


def test_castro_plain_product_fixture_still_works():
    html = (FIXTURES / "castro_product.html").read_text(encoding="utf-8")
    url = "https://www.castro.com/item"
    result = _extract_from_html(html, url, url, None, 200, None)
    assert result.status == FetchStatus.OK
    assert result.price == Decimal("79.9")
    assert result.currency == "ILS"
    assert result.in_stock is True


def test_variant_matching_url_beats_lowest_price():
    group = {
        "@context": "https://schema.org",
        "@type": "ProductGroup",
        "name": "Shirt",
        "hasVariant": [
            {"@type": "Product", "sku": "A1", "name": "Shirt S",
             "offers": {"@type": "Offer", "price": "50.00", "priceCurrency": "ILS",
                        "availability": "https://schema.org/InStock"}},
            {"@type": "Product", "sku": "B2", "name": "Shirt XL",
             "offers": {"@type": "Offer", "price": "70.00", "priceCurrency": "ILS",
                        "url": "https://shop.example/item?variation=B2",
                        "availability": "https://schema.org/OutOfStock"}},
        ],
    }
    pinned = _jsonld(_page(group), "https://shop.example/item?variation=B2")
    assert pinned.price == Decimal("70.00")
    assert pinned.in_stock is False
    assert pinned.title == "Shirt XL"

    by_sku = _jsonld(_page(group), "https://shop.example/item?sku=B2")
    assert by_sku.price == Decimal("70.00")

    unpinned = _jsonld(_page(group), "https://shop.example/item")
    assert unpinned.price == Decimal("50.00")
    assert unpinned.title == "Shirt"


def test_first_product_without_offers_does_not_stop_search():
    html = _page(
        {"@context": "https://schema.org", "@type": "Product", "name": "Related item"},
        {"@context": "https://schema.org", "@type": "Product", "name": "Real item",
         "offers": {"@type": "Offer", "price": 12.5, "priceCurrency": "USD"}},
    )
    result = _jsonld(html)
    assert result is not None
    assert result.price == Decimal("12.5")
    assert result.title == "Real item"


def test_aggregate_offer_low_price():
    html = _page({
        "@context": "https://schema.org", "@type": "Product", "name": "Thing",
        "offers": {"@type": "AggregateOffer", "lowPrice": "19.90", "highPrice": "29.90",
                   "priceCurrency": "EUR"},
    })
    result = _jsonld(html)
    assert result is not None
    assert result.price == Decimal("19.90")
    assert result.currency == "EUR"


def test_nested_graph_is_walked():
    html = _page({
        "@context": "https://schema.org",
        "@graph": [{"@graph": [{"@type": "Product", "name": "Deep",
                                "offers": [{"@type": "Offer", "price": 5, "priceCurrency": "GBP"}]}]}],
    })
    result = _jsonld(html)
    assert result is not None and result.price == Decimal("5")


@pytest.mark.parametrize(
    "offers",
    [
        ["not-a-dict", {"@type": "Offer", "price": "10.00", "availability": None}],
        {"@type": "Offer", "price": "10.00", "availability": None},
        {"@type": "Offer", "price": "10.00", "availability": ["https://schema.org/InStock"]},
        [None, 42, {"@type": "Offer", "price": "10.00", "priceCurrency": None}],
    ],
)
def test_malformed_offers_do_not_raise(offers):
    html = _page({"@context": "https://schema.org", "@type": "Product", "name": "X", "offers": offers})
    result = _jsonld(html)
    assert result is not None
    assert result.price == Decimal("10.00")
    assert result.in_stock is None


def test_four_decimal_jsonld_price_is_not_multiplied():
    html = _page({"@context": "https://schema.org", "@type": "Product", "name": "X",
                  "offers": {"@type": "Offer", "price": "89.9000", "priceCurrency": "ILS"}})
    assert _jsonld(html).price == Decimal("89.9")


# ---------------------------------------------------------------- missing pinned variant (#43)

def _group(with_sku: bool = True, with_url: bool = False) -> dict:
    def _variant(sku, price, name):
        v = {"@type": "Product", "name": name,
             "offers": {"@type": "Offer", "price": price, "priceCurrency": "ILS",
                        "availability": "https://schema.org/InStock"}}
        if with_sku:
            v["sku"] = sku
        if with_url:
            v["offers"]["url"] = f"https://shop.example/item?variation={sku}"
        return v

    return {
        "@context": "https://schema.org",
        "@type": "ProductGroup",
        "name": "Whey",
        "hasVariant": [
            _variant("111", "199.90", "Whey 1kg"),
            _variant("222", "349.90", "Whey 2.5kg"),
            _variant("333", "49.90", "Whey sample"),
        ],
    }


@pytest.mark.parametrize("query", ["variation=999", "variant=999", "sku=999"])
def test_missing_pinned_variant_is_not_found(query):
    result = _jsonld(_page(_group()), f"https://shop.example/item?{query}")
    assert result.status == FetchStatus.NOT_FOUND
    assert result.price is None
    assert "999" in result.error_message
    assert result.title == "Whey"


def test_missing_pinned_variant_with_offer_urls_only():
    result = _jsonld(_page(_group(with_sku=False, with_url=True)),
                     "https://shop.example/item?variation=999")
    assert result.status == FetchStatus.NOT_FOUND


def test_present_pinned_variant_still_found():
    result = _jsonld(_page(_group()), "https://shop.example/item?variation=222")
    assert result.status == FetchStatus.OK
    assert result.price == Decimal("349.90")


def test_no_ids_on_page_keeps_lowest_price_fallback():
    """Pages whose JSON-LD carries no variant ids cannot be matched: keep the old behaviour."""
    result = _jsonld(_page(_group(with_sku=False)), "https://shop.example/item?variation=999")
    assert result.status == FetchStatus.OK
    assert result.price == Decimal("49.90")


def test_single_product_with_unrelated_sku_keeps_working():
    product = {"@context": "https://schema.org", "@type": "Product", "name": "Kettle", "sku": "KT-1",
               "offers": {"@type": "Offer", "price": "99.00", "priceCurrency": "ILS"}}
    result = _jsonld(_page(product), "https://shop.example/item?variant=red")
    assert result.status == FetchStatus.OK
    assert result.price == Decimal("99.00")


def test_unpinned_url_keeps_lowest_price():
    result = _jsonld(_page(_group()), "https://shop.example/item")
    assert result.status == FetchStatus.OK
    assert result.price == Decimal("49.90")


def test_myprotein_fixture_with_unknown_variation_is_not_found():
    html = (FIXTURES / "myprotein_productgroup.html").read_text(encoding="utf-8")
    result = _jsonld(html, MP_URL + "?variation=99999999")
    assert result.status == FetchStatus.NOT_FOUND
    assert "99999999" in result.error_message


def test_missing_pinned_variant_through_extract_ladder():
    """NOT_FOUND stops the ladder: no meta-tag price is used instead."""
    html = _page(_group()).replace(
        "</head>", '<meta property="product:price:amount" content="49.90"></head>')
    url = "https://shop.example/item?variation=999"
    result = _extract_from_html(html, url, url, None, 200, None)
    assert result.status == FetchStatus.NOT_FOUND
