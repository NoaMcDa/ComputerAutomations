"""Issue #51: one cut of one animal per category; catch-alls are not ranked."""
from __future__ import annotations

from decimal import Decimal

import pytest

from price_watcher import supermarkets as sm


def _promo(name: str, desc: str = "") -> sm.MeatPromo:
    return sm.MeatPromo(
        chain="x", store_id="001", item_code="1", item_name=name,
        regular_price=None, discounted_price=Decimal("10"), discount_rate=None,
        promotion_description=desc,
    )


# Real item names from the 2026-11-11 scan quoted in the issue.
@pytest.mark.parametrize("name,category", [
    # was all "הודו"
    ("גרון הודו קפוא", "גרון הודו"),
    ("כנפיים הודו טרי", "כנפיים הודו"),
    ("גרון הודו טרי", "גרון הודו"),
    ("שוקיים הודו טרי", "שוקיים הודו"),
    ("חזה הודו טרי", "חזה הודו"),
    ("שישליק הודו טרי", "שישליק הודו"),
    ("הודו אדום טרי", "הודו אדום"),
    # species-less poultry cuts were "אחר"
    ("כנפיים טרי", "כנפיים עוף"),
    ("כרעיים טרי ארוז", "כרעיים עוף"),
    ("שוקיים מחפוד טרי", "שוקיים עוף"),
    # cuts that fell into "אחר"
    ("שייטל טרי", "שייטל"),
    ("רוסטביף", "רוסטביף"),
    ("דנבר טרי", "דנבר"),
    # neck / ribs split by animal
    ("גרון עוף", "גרון עוף"),
    ("צוואר בקר", "גרון בקר"),
    ("צוואר טלה", "צוואר טלה"),
    ("צלעות בקר", "צלעות בקר"),
    ("צלעות טלה", "צלעות טלה"),
    ("צלעות עגל", "צלעות עגל"),
    # lamb split by cut
    ("כתף טלה", "כתף טלה"),
    ("חזה כבש", "חזה טלה"),
    ("רבע טלה אחורי", "רבע טלה"),
    # goose / duck split by cut
    ("חזה אווז", "חזה אווז"),
    ("כנפיים אווז", "כנפיים אווז"),
    ("שוקיים ברווז", "שוקיים ברווז"),
    ("חזה ברווז", "חזה ברווז"),
    # chicken
    ("חזה עוף טרי", "חזה עוף"),
    ("פרגיות טריות", "פרגיות"),
    ("כבד עוף", "כבד עוף"),
    ("עוף שלם טרי", "עוף שלם"),
    # beef
    ("אנטריקוט בקר טרי", "אנטריקוט"),
    ("לשון בקר קפואה", "לשון בקר"),
    ("בשר בקר טחון", "בקר טחון"),
    ("בקר נתח מס' 8", "נתח מס' 8"),
    ("טי-בון", "טי בון"),
    # catch-alls
    ("קצוות כנפיים עוף", "עוף – אחר"),
    ("חצי עוף", "עוף – אחר"),
    ("בשר ראש", "בשר ראש"),
    ("סטייק בקר", "בשר – אחר"),
])
def test_category(name, category):
    assert sm._categorize(_promo(name)) == category


def test_no_ranked_category_mixes_animals():
    for cat in sm.CATEGORY_ORDER:
        animals = [a for a in ("עוף", "הודו", "אווז", "ברווז", "טלה", "עגל") if a in cat.split()]
        assert len(animals) <= 1, cat
    assert "הודו" not in sm.CATEGORY_ORDER
    assert "אווז וברווז" not in sm.CATEGORY_ORDER
    assert "כבש / טלה" not in sm.CATEGORY_ORDER


@pytest.mark.parametrize("name", ["סטרצ'טלה 200 גרם", "סטרצ׳טלה", "גבינת סטרציאטלה"])
def test_stracciatella_is_not_meat(name):
    assert not sm._is_meat_product(name)


@pytest.mark.parametrize("name", ["כנפיים טרי", "כרעיים טרי ארוז", "שוקיים מחפוד טרי",
                                  "שייטל טרי", "רוסטביף", "דנבר טרי", "חזה אווז"])
def test_new_cuts_pass_the_meat_filter(name):
    assert sm._is_meat_product(name)


def test_catch_alls_are_not_ranked():
    for cat in sm.CATCH_ALL_CATEGORIES:
        assert not sm.is_ranked_category(cat)
    assert sm.is_ranked_category("חזה הודו")
    assert sm.is_ranked_category("לשון בקר · קפוא")


@pytest.mark.parametrize("name,freshness", [
    ("גרון הודו קפוא", "frozen"), ("לשון בקר קפואה", "frozen"),
    ("לשון בקר טרייה", "fresh"), ("כנפיים טרי", "fresh"), ("רוסטביף", ""),
])
def test_freshness(name, freshness):
    assert sm._freshness(name) == freshness


def test_frozen_gets_its_own_table():
    assert sm._category_label(_promo("לשון בקר קפואה")) == "לשון בקר · קפוא"
    assert sm._category_label(_promo("לשון בקר טרייה")) == "לשון בקר"
    # catch-alls are not split
    assert sm._category_label(_promo("קצוות כנפיים עוף קפוא")) == "עוף – אחר"
