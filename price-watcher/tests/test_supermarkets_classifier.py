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
        ("כרעיים עוף טרי ארוז", "", "כרעיים עוף"),
        ("כרעים עוף טרי ארוז שלי ל", "", "כרעיים עוף"),
        ("קורקבן עוף טרי", "", "קורקבן עוף"),
        # #18: chicken fillet is not beef fillet
        ("פילה עוף", "פילה עוף 3 קג ב 100", "חזה / פילה עוף"),
        ("פילה בקר טרי מוכשר", "", "פילה בקר"),
        # #18: פרגית is boneless thigh, kept in its own group (not "Cornish hen")
        ("פרגית עוף טרי קהילות", "", "פרגיות"),
        # #18: goose liver is not chicken liver
        ("פלמי טופ כבד אווז", "", "כבד אווז"),
        ("כבד עוף טרי ארוז שלי", "", "כבד עוף"),
        # species taken from the description when the name has none
        ("כבד במגש לצלייה בלבד", "כבד עוף טרי ב14.90 מעל75 מו3", "כבד עוף"),
        # #18: "טיבון וויל" is a brand, not T-bone
        ("בשר הודו טחון טיבון", "הודו טחון קפוא טיבון וויל ב9.90", "טחון הודו – קפוא"),
        ("טיבון טלה טרי", "", "כבש – אחר"),
        # #18 / #51: necks, by animal
        ("גרון עוף", "", "גרון / צוואר עוף"),
        ("צוואר בקר טרי", "", "גרון / צוואר בקר"),
        ("צוואר טרי עם עצם", "", "גרון / צוואר בקר"),
        ("צוואר כבש טרי בדצ", "", "צוואר כבש"),
        ("גרון הודו קפוא", "", "גרון / צוואר הודו – קפוא"),
        # #18: word boundaries — לב / לצליה is not צלי
        ("לב עוף לצליה בלבד", "לבבות עוף טרי ב34.90", "לבבות עוף"),
        # #18: the name wins over the description
        ("כתף עגל טרי עטרה", "בריסקט/כתף/גולש דל שומן עטרה 2 ב 100 מגד", "כתף בקר"),
        ("כתף טרי", "גולש/כתף/שריר/צלעות טרי 2 ב160", "כתף בקר"),
        ("שריר טרי", "גולש/כתף/שריר/צלעות טרי 2 ב160", "שריר / זרוע"),
        ("צלעות טרי ארגנטינה", "אסאדו ללא עצם/צלעות ארגנטינה 2 ב150", "צלעות בקר"),
        ("אוסובוקו עע", "אסאדו/אוסבוקו טרי 2 ב150", "אוסובוקו"),
        # #18 / #51: cut numbers name the cut
        ("בשר בקר מס 8 מוכשר ק", "", "שריר / זרוע"),
        ("בשר מס 8 חלק קפוא", "", "שריר / זרוע – קפוא"),
        ("בשר מוכשר מס' 8 קפוא", "", "שריר / זרוע – קפוא"),
        ("בשר בקר טחון טרי", "", "טחון בקר"),
        ("טחון צלעות טרי עטרה", "", "טחון בקר"),
        ("טחון בקר עם  כבש", "", "טחון בקר"),
        # description is used only when the name gives nothing
        ("נתח מובחר", "אנטריקוט טרי ב99", "אנטריקוט"),
        # #51: turkey split by cut (live scan 2026-11-11)
        ("כנפיים הודו  טרי", "", "כנפיים הודו"),
        ("שוקיים הודו טרי עטרה", "", "שוקיים הודו"),
        ("חזה הודו טרי", "", "חזה הודו"),
        ("נתחי חזה הודו טרי תפ", "", "חזה הודו"),
        ("שישליק הודו  טרי", "", "שישליק / נתחי הודו"),
        ("הודו אדום טרי", "", "הודו אדום"),
        ("טחון הודו אדום  טרי", "", "טחון הודו אדום"),
        ("הודו טחון עוף ירושלי", "", "טחון הודו"),
        # #51: goose / duck / lamb by cut
        ("כנפיים אווז ארוז קפו", "", "כנפיים אווז – קפוא"),
        ("שוק אווז קפוא", "", "שוק אווז – קפוא"),
        ("חזה אווז קפוא בוואקו", "", "חזה אווז – קפוא"),
        ("צלעות טלה פרוס ארוז מוצפי", "", "צלעות כבש"),
        ("רבע כבש טרי מחפוד", "", "כבש – אחר"),
        # #51: poultry cuts without a species word are chicken, not "אחר"
        ("כנפיים טרי מחפוד", "", "כנפיים עוף"),
        ("כרעיים טרי ארוז", "", "כרעיים עוף"),
        ("שוקיים מחפוד טרי", "", "שוקיים עוף"),
        ("שוקיים מקוצר", "", "שוקיים עוף"),
        ("קוקטיל עוף טרי ארוז שופר", "", "כרעיים עוף"),
        ("קוביות עוף להקפצה", "", "חזה / פילה עוף"),
        ("רצועות  חזה להקפצה", "", "חזה / פילה עוף"),
        ("חצי עוף  מקוצב טרי", "", "עוף שלם / חצי עוף"),
        ("כנפית עוף טרי", "", "עוף – אחר"),
        # #51: cuts that used to land in "אחר" / "בשר – אחר"
        ("שייטל טרי", "", "שייטל"),
        ("דנבר בקר טרי", "", "דנבר"),
        ("רוסטביף טרי", "", "רוסטביף"),
        ("בשר ראש קפוא חלק", "", "בשר ראש – קפוא"),
        ("עצמות מוח פרוס - חב\"ד - ויקטוריה פרגוואי", "", "עצמות בקר / מח עצם"),
        # Bones and marrow; a bare "מח" is an abbreviation, not marrow or brain
        ("עצמות בקר טרי", "", "עצמות בקר / מח עצם"),
        ("עצמות מח ח. טרי", "", "עצמות בקר / מח עצם"),
        ("עצמות (מוח ) בקר  קפ", "", "עצמות בקר / מח עצם – קפוא"),
        ("מח עצם בקר קפוא", "", "עצמות בקר / מח עצם – קפוא"),
        ("זנב בקר מח. טרי", "", "בקר – כללי"),
        ("מוח בקר קפוא", "", "מוח – קפוא"),
        ("פילה מדומה בקר טרי", "", "פילה מדומה"),
        ("מינוט סטייק אנטריקוט", "", "מינוט סטייק"),
        ("אנטריקוט עם עצם טרי", "", "אנטריקוט עם עצם"),
        ("סטייק אנטריקוט עם עצ", "", "אנטריקוט עם עצם"),
        ("ריבס אנטריקוט טרי", "", "אנטריקוט עם עצם"),
        ("אנטריקוט טרי ארגנטינ", "", "אנטריקוט"),
        ("צלעות בקר קפוא מוכשר", "", "צלעות בקר – קפוא"),
        # #51: fresh and frozen are separate tables (frozen tongue 39.90, fresh 139)
        ("לשון בקר קפוא חלק", "", "לשון בקר – קפוא"),
        ("לשון בקר איטלקי", "", "לשון בקר"),
        ("לשון בקר מוכשר", "לשון בקר קפואה ב39.90", "לשון בקר – קפוא"),
        ("כתף עגל טרי עטרה", "כתף קפוא", "כתף בקר"),  # the name says fresh
        ("שריר הזרוע 8 בקר קפו", "", "שריר / זרוע – קפוא"),
        ("צלעות מס' 2 צרכני קפ", "", "צלעות בקר – קפוא"),
    ],
)
def test_categorize(name, desc, expected):
    assert sm._categorize(_promo(name, desc)) == expected


def test_stracciatella_is_cheese_not_lamb():
    assert not sm._is_meat_product("סטרצ'טלה 24% 150 גר")
    assert sm._species("סטרצ'טלה 24% 150 גר") is None
    assert sm._species("צלעות טלה") == "lamb"


def test_catch_all_groups_are_not_ranked():
    for label in ("אחר", "בשר – אחר", "עוף – אחר", "הודו – אחר", "בשר – אחר – קפוא"):
        assert sm._is_catch_all(label), label
    for label in ("כנפיים עוף", "חזה הודו", "לשון בקר – קפוא"):
        assert not sm._is_catch_all(label), label


def test_categories_table_has_unique_labels():
    labels = [label for label, _kws in sm._CATEGORIES]
    assert len(labels) == len(set(labels))
    assert "כרעיים עוף" in labels and "כרעיים עוף – קפוא" in labels
    # every frozen twin follows its fresh category
    for i, label in enumerate(labels):
        if label.endswith(" – קפוא"):
            assert labels[i - 1] == label.removesuffix(" – קפוא")


def test_bones_and_marrow_are_meat():
    for name in ("עצמות בקר טרי", "עצמות מח ח. טרי", "עצמות מוח פרוס אורגנ",
                 "עצמות מוח פרוס - מוכ", "מח עצם בקר קפוא"):
        assert sm._is_meat_product(name), name
    # Soup, broth and pet bones are not
    for name in ("מרק עצמות 500 גרם", "ציר עצמות בקר", "עצמות לכלב"):
        assert not sm._is_meat_product(name), name
