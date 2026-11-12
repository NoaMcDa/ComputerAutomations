"""Issue #11: MyProtein promotion banner and estimated final price (offline fixtures)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from core_helpers import ok
from price_watcher import sites
from price_watcher.sites import myprotein
from price_watcher.sites.myprotein import extractor

URL = "https://www.myprotein.co.il/p/sports-clothing/mp-women-s-tempo-triangle-vest-black/17669270/"


def run_fixture(fixtures_dir, name, price="189"):
    html = (fixtures_dir / "myprotein" / name).read_text(encoding="utf-8")
    return extractor.extract(html, URL, ok(price))


def test_registered_for_myprotein_only():
    assert sites.for_url(URL) is extractor
    assert sites.for_url("https://seestarz.com/products/x") is None


def test_pct_and_code_with_conditions(fixtures_dir):
    r = run_fixture(fixtures_dir, "pct_and_code.html")
    assert r.promo.code == "SAVE45X"
    assert r.promo.discount_pct == Decimal("45")
    assert "מעל 200" in r.promo.condition and "30.11" in r.promo.condition
    assert r.estimated_final_price == Decimal("103.95")
    assert r.price == Decimal("189")                     # displayed price kept


def test_no_promotion(fixtures_dir):
    r = run_fixture(fixtures_dir, "no_promo.html")
    assert r.promo is None and r.estimated_final_price is None


def test_hundred_percent_is_not_a_discount(fixtures_dir):
    # "100% Whey" / "100% polyester" must not be read as a 0% or 00% promo
    r = run_fixture(fixtures_dir, "no_promo.html")
    assert r.promo is None


def test_tiered_promotion_not_computed(fixtures_dir):
    r = run_fixture(fixtures_dir, "tiered.html")
    assert r.promo is not None and r.promo.discount_pct == 0
    assert r.estimated_final_price == Decimal("189")
    assert "Up to 70%" in r.promo.source_text


def test_unreadable_banner_falls_back_to_displayed_price(fixtures_dir):
    r = run_fixture(fixtures_dir, "unreadable.html")
    assert r.promo is not None and r.promo.code == "SPRING" and r.promo.discount_pct == 0
    assert r.estimated_final_price == Decimal("189")


def test_no_base_price_no_estimate(fixtures_dir):
    html = (fixtures_dir / "myprotein" / "pct_and_code.html").read_text(encoding="utf-8")
    r = extractor.extract(html, URL, ok(None))
    assert r.promo.discount_pct == 45 and r.estimated_final_price is None


@pytest.mark.parametrize("text, code, pct", [
    ("קוד הנחה: VIP20 – 20% הנחה על כל האתר", "VIP20", 20),
    ("20% הנחה על כל האתר בקוד הנחה VIP20", "VIP20", 20),
    ("10% הנחה עם קוד הטבה: WELCOME10", "WELCOME10", 10),
    ("קוד מבצע SALE15 - 15% הנחה", "SALE15", 15),
    ("15% הנחה | קוד הנחה: SALE15", "SALE15", 15),
    ("20% off with discount code VIP20", "VIP20", 20),
])
def test_issue_41_phrasings_on_product_page(text, code, pct):
    r = extractor.extract(f'<div class="promo">{text}</div>', URL, ok("100"))
    assert (r.promo.code, r.promo.discount_pct) == (code, Decimal(pct))
    assert r.estimated_final_price == Decimal(100 - pct)


def test_condition_phrases():
    assert myprotein._extract_condition("30% off selected items, ends 30/11") == \
        "selected items; ends 30/11"
    assert myprotein._extract_condition("Extra 10% on orders over ₪200.") == "on orders over ₪200"
    assert myprotein._extract_condition("20% הנחה על מוצרים נבחרים") == "על מוצרים נבחרים"
    assert myprotein._extract_condition("20% off") is None


def test_unused_helpers_removed():
    assert not hasattr(myprotein, "_RE_CONDITION")


# ---- Merge: PR #56 behaviour ported into the PR #57 extractor ----------------

def test_conditions_from_another_offer_are_not_borrowed():
    body = ('<div class="promo">30% off everything | Extra 10% on orders over ₪200 '
            'with code: TEN10</div>')
    r = extractor.extract(body, URL, ok("100"))
    assert (r.promo.code, r.promo.discount_pct) == (None, Decimal("30"))
    assert "200" not in (r.promo.condition or "")


def test_code_next_to_a_non_discount_percentage_is_not_a_promo():
    r = extractor.extract('<div class="promo">Impact Whey: 80% protein | code: WHEY80</div>',
                          URL, ok("100"))
    assert r.promo is None
