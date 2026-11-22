"""Issue #51: one cut of one animal per category; catch-alls are not ranked.

Adapted to the scanner's category names (merge of PR #56 into #57): the
labels are those of `supermarkets._CATEGORY_RULES` ("גרון / צוואר הודו",
"צלעות כבש", ...) and a frozen item goes to the " – קפוא" twin of its
category, so the expected values below are the merged scanner's.
"""
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


_FROZEN = sm._FROZEN_SUFFIX


# Real item names from the 2026-11-11 scan quoted in the issue.
@pytest.mark.parametrize("name,category", [
    # was all "הודו"
    ("גרון הודו קפוא", "גרון / צוואר הודו" + _FROZEN),
    ("כנפיים הודו טרי", "כנפיים הודו"),
    ("גרון הודו טרי", "גרון / צוואר הודו"),
    ("שוקיים הודו טרי", "שוקיים הודו"),
    ("חזה הודו טרי", "חזה הודו"),
    ("שישליק הודו טרי", "שישליק / נתחי הודו"),
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
    ("גרון עוף", "גרון / צוואר עוף"),
    ("צוואר בקר", "גרון / צוואר בקר"),
    ("צוואר טלה", "צוואר כבש"),
    ("צלעות בקר", "צלעות בקר"),
    ("צלעות טלה", "צלעות כבש"),
    ("צלעות עגל", "צלעות בקר"),      # veal is ranked with beef
    # lamb split by cut
    ("כתף טלה", "כתף כבש"),
    ("חזה כבש", "חזה כבש"),
    ("רבע טלה אחורי", "כבש – אחר"),  # a quarter mixes cuts: catch-all
    # goose / duck split by cut
    ("חזה אווז", "חזה אווז"),
    ("כנפיים אווז", "כנפיים אווז"),
    ("שוקיים ברווז", "שוק ברווז"),
    ("חזה ברווז", "חזה ברווז"),
    # chicken
    ("חזה עוף טרי", "חזה / פילה עוף"),
    ("פרגיות טריות", "פרגיות"),
    ("כבד עוף", "כבד עוף"),
    ("עוף שלם טרי", "עוף שלם / חצי עוף"),
    # beef
    ("אנטריקוט בקר טרי", "אנטריקוט"),
    ("לשון בקר קפואה", "לשון בקר" + _FROZEN),
    ("בשר בקר טחון", "טחון בקר"),
    ("בקר נתח מס' 8", "שריר / זרוע"),   # cut number 8 = foreshank
    ("טי-בון", "טי-בון"),
    # catch-alls
    ("קצוות כנפיים עוף", "עוף – אחר"),
    ("בשר ראש", "בשר ראש"),
])
def test_category(name, category):
    assert sm._categorize(_promo(name)) == category


def test_no_ranked_category_mixes_animals():
    labels = [label for label, _kws in sm._CATEGORIES]
    for cat in labels:
        animals = [a for a in ("עוף", "הודו", "אווז", "ברווז", "טלה", "כבש") if a in cat.split()]
        assert len(animals) <= 1, cat
    assert "הודו" not in labels
    assert "אווז וברווז" not in labels
    assert "כבש / טלה" not in labels


@pytest.mark.parametrize("name", ["סטרצ'טלה 200 גרם", "סטרצ׳טלה", "גבינת סטרציאטלה"])
def test_stracciatella_is_not_meat(name):
    assert not sm._is_meat_product(name)


@pytest.mark.parametrize("name", ["כנפיים טרי", "כרעיים טרי ארוז", "שוקיים מחפוד טרי",
                                  "שייטל טרי", "דנבר טרי", "חזה אווז"])
def test_new_cuts_pass_the_meat_filter(name):
    assert sm._is_meat_product(name)


def test_roastbeef_needs_a_fresh_signal():
    # "רוסטביף" alone is also sliced deli roast beef: it needs a weighed item
    # or a freshness word (the scanner's two-gate classifier).
    assert not sm._is_meat_product("רוסטביף")
    weighed = sm._FeedItem(name="רוסטביף", price=Decimal("129.90"), weighted=True)
    assert sm._is_meat_product("רוסטביף", item=weighed)
    assert sm._is_meat_product("רוסטביף טרי")


def test_catch_alls_are_not_ranked():
    for cat in ("עוף – אחר", "בשר – אחר", "אחר", "כבש – אחר"):
        assert sm._is_catch_all(cat)
    assert not sm._is_catch_all("חזה הודו")
    assert not sm._is_catch_all("לשון בקר" + _FROZEN)
    assert sm._is_catch_all("עוף – אחר" + _FROZEN)


@pytest.mark.parametrize("name,frozen", [
    ("גרון הודו קפוא", True), ("לשון בקר קפואה", True),
    ("לשון בקר טרייה", False), ("כנפיים טרי", False), ("רוסטביף", False),
])
def test_freshness(name, frozen):
    assert sm._is_frozen(name) is frozen


def test_frozen_gets_its_own_table():
    assert sm._categorize(_promo("לשון בקר קפואה")) == "לשון בקר" + _FROZEN
    assert sm._categorize(_promo("לשון בקר טרייה")) == "לשון בקר"
    # a frozen catch-all is still a catch-all (not ranked, not shown)
    assert sm._is_catch_all(sm._categorize(_promo("קצוות כנפיים עוף קפוא")))
