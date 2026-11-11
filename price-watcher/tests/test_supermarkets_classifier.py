"""Meat filter (#16) and category mapping (#18) regression tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from price_watcher import supermarkets as sm

_CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "supermarkets" / "classifier_cases.json").read_text(encoding="utf-8")
)


def _check(case: dict) -> bool:
    item = SimpleNamespace(
        weighted=case.get("weighted", False), manufacturer=case.get("manufacturer", "")
    )
    return sm._is_meat_product(case["name"], case.get("desc", ""), item)


@pytest.mark.parametrize("case", _CASES["not_meat"], ids=lambda c: c["name"])
def test_not_meat_is_rejected(case):
    assert not _check(case)


@pytest.mark.parametrize("case", _CASES["meat"], ids=lambda c: c["name"])
def test_meat_is_accepted(case):
    assert _check(case)


def test_issue_16_examples_rejected():
    # The live false positives quoted in the issue (name, promotion description).
    for name, desc in [
        ("פרמיו דליקט עוף 56גר", "פרמיו דליקט 2 ב18"),
        ("פרמיו פאוץ עוף לחתלת", "פרמיו פאוץ 3 ב14"),
        ("פנסי פיסט גורמה עוף", ""),
        ('דוגלי בוגר עוף 3 ק"ג', ""),
        ('בונזו בשר 3 ק"ג', ""),
        ("סטיק הודו וכבש פרמיו", ""),
        ("פילה ברמונדי 7-9 בוו", ""),
        ("פילה ברבוניה קפוא 40", ""),
        ("פילה מקרל מומלח עישון150", ""),
        ("תחבושת עבה עם כנפיים", ""),
        ('סכין בשר כתומה 26 ס"', ""),
        ("לאבנה בקר 5% 250 גר", ""),
        ("נתח מפורק סגנון בקר", "מגוון רידפיין תחליפי בשר 280-388גרם 2ב65"),
        ("פטריות סטייק פורטובל", ""),
        ("צלעות תירס 500גרם בת", ""),
        ("גולד סטייק צ'יפס", ""),
        ("מאמא עוף צ'יפס עבה 1", ""),
        ("נודלס בקר 70גר", ""),
        ("מגיק בשר בשקית 85 גר", ""),
        ("גריל עוף שקית 80גר", ""),
        ("דים סאם גיוזה עוף 30", ""),
        ("פלמיני כיסוני בשר 60", ""),
        ("קובה במילוי בשר שלוש", ""),
        ("חציל טעם כבד אחלה טע", ""),
        ("נגיסי בקר ישרלה מחבת", ""),
        ("כוכבי עוף מעודן 700", ""),
        ("קרפצ'יו בקר 210 גרם", ""),
        ("פרושוטו בקר 100 גרם", ""),
        ("לכסניות גרון", ""),
        ("מנקה לשון", ""),
        ("שקית צלי", ""),
        ("אורז בסמטי הודו", ""),
        ("תיק כתף", ""),
        ("בקר השקיה", ""),
    ]:
        assert not sm._is_meat_product(name, desc), name


def test_issue_16_false_negatives_accepted():
    assert sm._is_meat_product("קוביות בקר לגולש")
    assert sm._is_meat_product("קורקבנים עוף טרי")


def test_weighed_flag_only_helps_a_single_meat_word():
    weighed = SimpleNamespace(weighted=True, manufacturer="")
    loose = SimpleNamespace(weighted=False, manufacturer="")
    assert sm._is_meat_product("כתף מרכזי", item=weighed)
    assert not sm._is_meat_product("כתף מרכזי", item=loose)
    # Weighed spices / fish are still not meat.
    assert not sm._is_meat_product("קינמון טחון במשקל", item=weighed)
    assert not sm._is_meat_product("פילה אלתית פרימיום במשקל", item=weighed)


def _promo(name: str, desc: str = "") -> sm.MeatPromo:
    return sm.MeatPromo(
        chain="", store_id="", item_code="", item_name=name, regular_price=None,
        discounted_price=None, discount_rate=None, promotion_description=desc,
    )


@pytest.mark.parametrize(
    "name, desc, expected",
    [
        # #18: leg quarters are not giblets
        ("כרעיים עוף טרי ארוז", "", "כרעיים"),
        ("כרעים עוף טרי ארוז שלי ל", "", "כרעיים"),
        ("קורקבן עוף טרי", "", "קרביים, קורקבן ולבבות"),
        # #18: chicken fillet is not beef fillet
        ("פילה עוף", "פילה עוף 3 קג ב 100", "חזה / פילה עוף"),
        ("פילה בקר טרי מוכשר", "", "פילה בקר"),
        # #18: פרגית is boneless thigh, kept in its own group (not "Cornish hen")
        ("פרגית עוף טרי קהילות", "", "פרגיות"),
        # #18: goose liver is not chicken liver
        ("פלמי טופ כבד אווז", "", "אווז וברווז"),
        ("כבד עוף טרי ארוז שלי", "", "כבד עוף"),
        # species taken from the description when the name has none
        ("כבד במגש לצלייה בלבד", "כבד עוף טרי ב14.90 מעל75 מו3", "כבד עוף"),
        # #18: "טיבון וויל" is a brand, not T-bone
        ("בשר הודו טחון טיבון", "הודו טחון קפוא טיבון וויל ב9.90", "בשר טחון"),
        ("טיבון טלה טרי", "", "כבש / טלה"),
        # #18: גרון / neck
        ("גרון עוף", "", "גרון וצוואר"),
        ("צוואר בקר טרי", "", "גרון וצוואר"),
        # #18: word boundaries — לב / לצליה is not צלי
        ("לב עוף לצליה בלבד", "לבבות עוף טרי ב34.90", "קרביים, קורקבן ולבבות"),
        # #18: the name wins over the description
        ("כתף עגל טרי עטרה", "בריסקט/כתף/גולש דל שומן עטרה 2 ב 100 מגד", "כתף"),
        ("כתף טרי", "גולש/כתף/שריר/צלעות טרי 2 ב160", "כתף"),
        ("שריר טרי", "גולש/כתף/שריר/צלעות טרי 2 ב160", "שריר / חגורה"),
        ("צלעות טרי ארגנטינה", "אסאדו ללא עצם/צלעות ארגנטינה 2 ב150", "צלעות"),
        ("אוסובוקו עע", "אסאדו/אוסבוקו טרי 2 ב150", "אוסובוקו"),
        # #18: generic beef is "beef – general", not ground beef
        ("בשר בקר מס 8 מוכשר ק", "", "בקר – כללי"),
        ("בשר בקר טחון טרי", "", "בשר טחון"),
        ("טחון צלעות טרי עטרה", "", "בשר טחון"),
        # description is used only when the name gives nothing
        ("נתח מובחר", "אנטריקוט טרי ב99", "אנטריקוט"),
    ],
)
def test_categorize(name, desc, expected):
    assert sm._categorize(_promo(name, desc)) == expected


def test_categories_table_has_unique_labels():
    labels = [label for label, _kws in sm._CATEGORIES]
    assert len(labels) == len(set(labels))
    assert "כרעיים" in labels
