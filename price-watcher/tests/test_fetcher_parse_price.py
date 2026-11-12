"""_parse_price edge cases (Refs #24)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from price_watcher.fetcher import _parse_price


@pytest.mark.parametrize(
    "text, machine, expected",
    [
        # many-decimal machine values: never x1000
        ("89.9000", True, Decimal("89.9")),
        ("89.9000", False, Decimal("89.9")),
        ("129.900", True, Decimal("129.9")),
        ("19.990", True, Decimal("19.99")),
        ("0.999", False, Decimal("0.999")),
        ("0.999", True, Decimal("0.999")),
        # behaviour that must not change
        ("1,299.90", False, Decimal("1299.90")),
        ("1.299,90", False, Decimal("1299.90")),
        ("1,299", False, Decimal("1299")),
        ("1.299", False, Decimal("1299")),  # human text: EU thousands group
        ("1.299.000", False, Decimal("1299000")),
        ("₪ 49.90", False, Decimal("49.90")),
        ("79.9", True, Decimal("79.9")),
        ("159.8", True, Decimal("159.8")),
        ("100", False, Decimal("100")),
    ],
)
def test_number_parsing(text, machine, expected):
    price, _ = _parse_price(text, machine_readable=True) if machine else _parse_price(text)
    assert price == expected


@pytest.mark.parametrize("text", ["1e3", "1E+03", "2.5e2"])
def test_scientific_notation_is_rejected(text):
    with pytest.raises(ValueError):
        _parse_price(text)


@pytest.mark.parametrize(
    "text, currency",
    [
        ("A$10.00", "AUD"),
        ("AU$ 10.00", "AUD"),
        ("CA$ 10.00", "CAD"),
        ("C$10.00", "CAD"),
        ("10.00 CHF", "CHF"),
        ('49.90 ש"ח', "ILS"),
        ("49.90 ש״ח", "ILS"),
        ("$10.00", "USD"),
        ("US$10.00", "USD"),
        ("₪49.90", "ILS"),
        ("€10,00", "EUR"),
    ],
)
def test_currency_detection(text, currency):
    price, detected = _parse_price(text)
    assert detected == currency
    assert price in (Decimal("10.00"), Decimal("49.90"))


# ---------------------------------------------------------------- several numbers (#42)

@pytest.mark.parametrize(
    "text",
    [
        "₪129.90 ₪99.90",            # old + sale price, both with a currency
        "Was ₪129.90 Now ₪99.90",
        "₪129.90₪99.90",             # get_text(strip=True) glues the two nodes
        "129.90 99.90",
    ],
)
def test_several_different_prices_are_refused(text):
    with pytest.raises(ValueError):
        _parse_price(text)


@pytest.mark.parametrize(
    "text",
    ["₪1,299.90 - ₪1,499.90", "1,299.90–1,499.90 ₪", "$10 to $20", "₪100 עד ₪200", "10.00-20.00"],
)
def test_price_ranges_are_refused(text):
    with pytest.raises(ValueError):
        _parse_price(text)


@pytest.mark.parametrize("text", ["129.9099.90", "1.2345.678", "12,34,567"])
def test_badly_grouped_thousands_are_refused(text):
    """Glued prices must not be read as one number with thousands separators."""
    with pytest.raises(ValueError):
        _parse_price(text)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("₪49.90 for 2", Decimal("49.90")),          # only the currency-tagged number counts
        ("49.90 ₪ / 100g", Decimal("49.90")),
        ("Save 20% now ₪79.90", Decimal("79.90")),
        ("₪99.90 ₪99.90", Decimal("99.90")),         # same value twice is not ambiguous
        ("1 299,90 €", Decimal("1299.90")),          # space as thousands separator
        ("1 299,90 €", Decimal("1299.90")),
        ("1'299.90 CHF", Decimal("1299.90")),
        ("10.-", Decimal("10")),
    ],
)
def test_one_price_among_other_text(text, expected):
    price, _ = _parse_price(text)
    assert price == expected
