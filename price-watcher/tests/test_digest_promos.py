"""digest.extract_promos: real homepage banners + synthetic false positives (#20, #21)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from price_watcher import digest

FIXTURES = Path(__file__).parent / "fixtures" / "promos"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _pairs(html: str) -> set[tuple[str | None, int]]:
    return {(p.code, int(p.discount_pct)) for p in digest.extract_promos(html)}


def _page(body: str) -> str:
    return f"<html><body>{body}</body></html>"


# --- Real homepages captured 2026-11-11 -------------------------------------

def test_castro_banner_split_over_two_divs():
    # "EXTRA 11% על כל האתר ..." and "בהקלדת קוד קופון: OFF11" sit in sibling divs.
    promos = digest.extract_promos(_load("castro_home.html"))
    assert [(p.code, p.discount_pct) for p in promos] == [("OFF11", Decimal(11))]
    assert "על כל האתר" in promos[0].condition


def test_myprotein_home_offers_paired_with_their_own_percentage():
    assert _pairs(_load("myprotein_home.html")) == {
        (None, 30),       # "30% OFF EVERYTHING", no code
        ("NEWMYP", 10),   # "Extra 10% on first order | Code: NEWMYP"
        ("APP5", 5),      # "EXTRA 5% OFF | USE CODE: APP5"
    }


def test_myprotein_newmyp_condition_mentions_first_order():
    newmyp = next(p for p in digest.extract_promos(_load("myprotein_home.html")) if p.code == "NEWMYP")
    assert "first order" in newmyp.condition


def test_terminalx_banner_inside_script_json():
    html = _load("terminalx_home.html")
    assert "<img" not in html  # the only copy of the banner is the JSON blob
    promos = digest.extract_promos(html)
    assert [(p.code, p.discount_pct) for p in promos] == [("SINGLES20", Decimal(20))]


def test_seestarz_has_no_promo():
    assert digest.extract_promos(_load("seestarz_home.html")) == []


def test_myprotein_product_page_banner():
    assert _pairs(_load("myprotein_product.html")) == {(None, 30), ("APP5", 5)}


# --- Pairing inside multi-offer banners (#20) --------------------------------

@pytest.mark.parametrize("body, expected", [
    (
        '<div class="strip">30% OFF EVERYTHING<br>APP EXCLUSIVE | EXTRA 5% OFF | USE CODE APP5</div>',
        {(None, 30), ("APP5", 5)},
    ),
    (
        '<div class="usp">Extra 10% on first order | Code: NEWMYP</div>'
        '<div class="usp">App Exclusive | Extra 5% - USE CODE: APP5</div>',
        {("NEWMYP", 10), ("APP5", 5)},
    ),
    (
        '<div class="promo">EXTRA 11% על כל האתר | קוד קופון: OFF11</div>',
        {("OFF11", 11)},
    ),
    (
        # several percentages in one segment: the code takes the nearest one
        '<p>Up to 70% off + extra 10% with code WINTER10</p>',
        {("WINTER10", 10)},
    ),
])
def test_codes_pair_within_their_offer(body, expected):
    assert _pairs(_page(body)) == expected


def test_code_does_not_borrow_from_a_segment_that_has_its_own_code():
    body = "<p>20% OFF CODE: AAA20 | USE CODE: BBB</p>"
    assert _pairs(_page(body)) == {("AAA20", 20)}


# --- False positives (#21) ---------------------------------------------------

@pytest.mark.parametrize("html", [
    # CSS percentage + JSON keys/values in scripts
    '<html><head><style>.a{width:50%}</style><script>var cfg={"sale":true,'
    '"code":"currency","off":1};</script></head><body></body></html>',
    # lower-case word after "code"
    _page('<div class="promo">20% off sale! Use the code below at checkout</div>'),
    # neighbouring JSON fields are not combined; lower-case value is not a code
    '<html><body><script>{"discount":"25% off","sku_code":"ABC123",'
    '"coupon code":"abc"}</script></body></html>',
    # "150%" is not 50%
    _page('<div class="promo">Earn 150% off points. Site rewards</div>'),
    # "website" is not "site"
    _page("<p>Get 10% off when you sign up on our website newsletter</p>"),
    # non-discount percentages
    _page("<p>100% plant-based</p><p>Clothing is subject to a 10% tax rate</p>"),
    _page('<div class="banner">Impact Whey: 80% protein per serving | code: WHEY80</div>'),
    # "postcode" is not "code"
    _page('<div class="banner">15% off for students. Enter your postcode AB12 below</div>'),
    # markup attributes are not text
    _page('<div data-x="20% off everything code: ATTR20" class="promo-code"></div>'),
])
def test_false_positives_are_ignored(html):
    assert digest.extract_promos(html) == []


def test_up_to_is_not_reported_as_sitewide():
    assert digest.extract_promos(_page("<p>Up to 70% off everything</p>")) == []


def test_hebrew_sitewide_without_code():
    assert _pairs(_page("<p>30% הנחה על כל האתר</p>")) == {(None, 30)}
