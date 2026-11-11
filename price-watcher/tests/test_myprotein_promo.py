"""MyProtein product-page promo extraction (#22)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from price_watcher.contracts import FetchResult, FetchStatus
from price_watcher.sites import myprotein

FIXTURES = Path(__file__).parent / "fixtures" / "promos"
URL = "https://www.myprotein.co.il/p/x"


@pytest.fixture
def base() -> FetchResult:
    return FetchResult(status=FetchStatus.OK, final_url=URL, price=Decimal("200"), currency="ILS")


def _extract(html: str, base: FetchResult) -> FetchResult:
    return myprotein.extractor.extract(html, URL, base)


def _page(body: str) -> str:
    return f"<html><body>{body}</body></html>"


def test_real_product_banner_sitewide_wins_without_code(base):
    # "30% Off EVERYTHING | Applies as Basket <br> | Extra 5% on App using code: APP5":
    # the 30% applies at the basket; APP5 is an app-only extra 5%, not the 30%.
    result = _extract((FIXTURES / "myprotein_product.html").read_text(encoding="utf-8"), base)
    assert result.promo is not None
    assert result.promo.code is None
    assert result.promo.discount_pct == Decimal(30)
    assert result.estimated_final_price == Decimal("140.00")


def test_real_home_banner_never_pairs_app5_with_30(base):
    result = _extract((FIXTURES / "myprotein_home.html").read_text(encoding="utf-8"), base)
    assert (result.promo.code, result.promo.discount_pct) == (None, Decimal(30))


@pytest.mark.parametrize("body", [
    '<div class="product-banner">100% Whey Protein. Premium quality.</div>',
    '<div class="offer-box">Impact Whey: 80% protein per serving</div>',
    '<div class="promo">30% off — discount code applied at checkout</div>',
    '<div class="banner">15% off for students. Enter your postcode 90210 below</div>',
    '<div class="promo">20% הנחה לזמן מוגבל, קודם כל הירשמו NOW</div>',
])
def test_non_discounts_and_bogus_codes_are_ignored(body, base):
    result = _extract(_page(body), base)
    assert result.promo is None
    assert result.estimated_final_price is None


def test_coded_offer_keeps_full_condition(base):
    body = '<div class="promo-strip">45% off everything | use code: SAVE45 at checkout. T&Cs apply</div>'
    result = _extract(_page(body), base)
    assert result.promo.code == "SAVE45"
    assert result.promo.discount_pct == Decimal(45)
    assert "at checkout" in result.promo.condition
    assert result.estimated_final_price == Decimal("110.00")


def test_code_pairs_with_its_own_offer_not_the_first_percentage(base):
    body = '<div class="promo">Up to 70% off + extra 10% with code WINTER10</div>'
    result = _extract(_page(body), base)
    assert (result.promo.code, result.promo.discount_pct) == ("WINTER10", Decimal(10))
    assert result.estimated_final_price == Decimal("180.00")


def test_up_to_only_is_not_computed(base):
    result = _extract(_page('<div class="promo">Up to 70% off sale</div>'), base)
    assert result.promo.discount_pct == Decimal(0)
    assert result.estimated_final_price == base.price


def test_no_banner_returns_base_result(base):
    assert _extract(_page("<p>Hello</p>"), base) is base
