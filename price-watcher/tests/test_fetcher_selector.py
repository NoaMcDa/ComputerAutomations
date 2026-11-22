"""CSS-selector and microdata steps with sale-price markup (Refs #42)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from bs4 import BeautifulSoup

from price_watcher.contracts import FetchStatus
from price_watcher.fetcher import _extract_from_html, _try_microdata, _try_selector

URL = "https://shop.example/item"


def _select(body: str):
    soup = BeautifulSoup(f"<html><body>{body}</body></html>", "html.parser")
    return _try_selector(soup, ".price", URL, "Item", 200, None)


@pytest.mark.parametrize(
    "body",
    [
        '<div class="price"><del>&#8362;129.90</del> <ins>&#8362;99.90</ins></div>',
        '<div class="price"><ins>&#8362;99.90</ins> <del>&#8362;129.90</del></div>',
        '<div class="price"><s>&#8362;129.90</s> &#8362;99.90</div>',
        '<div class="price">&#8362;99.90 <strike>&#8362;129.90</strike></div>',
        '<div class="price"><span>Was </span><del><span>&#8362;129.90</span></del>'
        '<span> Now &#8362;99.90</span></div>',
    ],
)
def test_sale_markup_gives_current_price(body):
    result = _select(body)
    assert result is not None
    assert result.status == FetchStatus.OK
    assert result.price == Decimal("99.90")
    assert result.currency == "ILS"


def test_only_struck_price_is_still_read():
    result = _select('<div class="price"><del>&#8362;129.90</del></div>')
    assert result.price == Decimal("129.90")


def test_split_price_nodes_are_still_joined():
    result = _select('<div class="price"><span>&#8362;</span><span>1,299</span><span>.90</span></div>')
    assert result.price == Decimal("1299.90")


@pytest.mark.parametrize(
    "body",
    [
        '<div class="price">&#8362;129.90 &#8362;99.90</div>',
        '<div class="price">Was &#8362;129.90 Now &#8362;99.90</div>',
        '<div class="price"><span>129.90</span><span>99.90</span></div>',
        '<div class="price">&#8362;1,299.90 - &#8362;1,499.90</div>',
    ],
)
def test_ambiguous_element_is_not_a_price(body):
    """Without markup telling the prices apart, no (wrong) number is returned."""
    assert _select(body) is None


def test_fetch_ladder_with_sale_selector():
    html = (
        "<html><head><title>Sale</title></head><body>"
        '<div class="price"><del>&#8362;129.90</del> <ins>&#8362;99.90</ins></div>'
        "</body></html>"
    )
    result = _extract_from_html(html, URL, URL, ".price", 200, None)
    assert result.status == FetchStatus.OK
    assert result.price == Decimal("99.90")


def test_microdata_with_two_prices_is_not_multiplied():
    html = (
        '<html><body><div itemscope itemtype="https://schema.org/Product">'
        '<span itemprop="name">Item</span>'
        '<div itemprop="offers" itemscope itemtype="https://schema.org/Offer">'
        '<span itemprop="price">&#8362;129.90 &#8362;99.90</span></div></div></body></html>'
    )
    assert _try_microdata(html, URL, URL, "Item", 200, None) is None
