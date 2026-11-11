"""creatine._parse_price robustness and powder filtering."""

from __future__ import annotations

from decimal import Decimal

import pytest

from price_watcher import creatine

D = Decimal


@pytest.mark.parametrize("text,expected", [
    ("₪94.64 (₪9.49/100g)", (D("94.64"), "ILS", D("9.49"), "100g", False)),
    ("₪61.87 (₪0.26/count)", (D("61.87"), "ILS", D("0.26"), "count", False)),
    ("₪78.64 (₪22.21/100g) 25% off ₪104.85", (D("78.64"), "ILS", D("22.21"), "100g", True)),
    # sale layout with the struck-through original first: the lower price is the one paid
    ("₪104.85 ₪78.64 (₪22.21/100g)", (D("78.64"), "ILS", D("22.21"), "100g", True)),
    # leading RTL mark / label prefix
    ("‏₪94.64 (₪9.49/100g)", (D("94.64"), "ILS", D("9.49"), "100g", False)),
    ("Price: ₪94.64 (₪9.49/100g)", (D("94.64"), "ILS", D("9.49"), "100g", False)),
    # other currency and spacing
    ("$24.99 ($2.50/100 g)", (D("24.99"), "USD", D("2.50"), "100g", False)),
    ("₪94.64 (₪9.49/100 גרם)", (D("94.64"), "ILS", D("9.49"), "100g", False)),
    ("₪1,094.64 (₪9.49/100g)", (D("1094.64"), "ILS", D("9.49"), "100g", False)),
    # per-kg is normalised to per-100g
    ("₪95.00 (₪95.00/kg)", (D("95.00"), "ILS", D("9.50"), "100g", False)),
    # not comparable units are kept but not 100g
    ("$10.00 ($0.50/oz)", (D("10.00"), "USD", D("0.50"), "oz", False)),
    ("₪94.64", (D("94.64"), "ILS", None, "unknown", False)),
])
def test_parse_price(text, expected):
    assert creatine._parse_price(text) == expected


@pytest.mark.parametrize("text", ["", "out of stock", "no price here (see page)"])
def test_parse_price_rejects_non_prices(text):
    assert creatine._parse_price(text) is None


def _cell(name, price_text, href="/pr/x"):
    return (f'<div class="product-cell"><a href="{href}"><span class="product-title">{name}</span></a>'
            f'<span class="product-price">{price_text}</span></div>')


def test_non_powder_products_do_not_win_cheapest():
    html = "<html><body>" + "".join([
        _cell("Creatine Gummies", "₪40.00 (₪2.00/100g)"),
        _cell("Creatine Capsules 120 caps", "₪30.00 (₪1.00/100g)"),
        _cell("Creatine Monohydrate Powder", "₪94.64 (₪9.49/100g)"),
    ]) + "</body></html>"
    products, cells = creatine._parse_cells(html)
    assert cells == 3 and len(products) == 3
    assert creatine.pick_cheapest(products).name == "Creatine Monohydrate Powder"


def test_cheapest_only_compares_dominant_currency():
    html = "<body>" + "".join([
        _cell("Creatine A Powder", "₪94.64 (₪9.49/100g)"),
        _cell("Creatine B Powder", "₪99.00 (₪10.00/100g)"),
        _cell("Creatine C Powder", "$5.00 ($1.00/100g)"),
    ]) + "</body>"
    products, _ = creatine._parse_cells(html)
    assert creatine.pick_cheapest(products).name == "Creatine A Powder"
