"""Multi-buy / unit pricing and non-promotion filtering (#17)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from price_watcher import supermarkets as sm

_FIX = Path(__file__).parent / "fixtures" / "supermarkets"


def _scan() -> dict[str, sm.MeatPromo]:
    store = sm.StoreInfo(1, "סניף בדיקה", "תל אביב", "תל אביב")
    promos = sm._scan_store(
        "רשת", store,
        (_FIX / "PriceFull_small.xml").read_bytes(),
        (_FIX / "PromoFull_small.xml").read_bytes(),
    )
    return {p.item_code: p for p in promos}


def test_only_consistent_meat_promos_are_kept():
    # 1004 unit mismatch, 1005 class-action credit, 1006 "תו זהב", 1007 pet food,
    # 1008 fish, 1009 no saving, 1010 noodles, 1011 club-only, 1013 feed numbers
    # contradict the advertised "2 קג ב 60".
    assert set(_scan()) == {"1001", "1002", "1003", "1012"}


def test_now_is_per_unit_and_never_above_was():
    for p in _scan().values():
        assert p.discounted_price < p.regular_price
        assert p.savings_amount > 0
        assert 0 < p.savings_pct < 90


def test_weighted_multibuy_is_per_kg():
    p = _scan()["1001"]  # "2 ב-20" on a per-kg item: MinQty 1.5, DiscountedPrice 15
    assert p.discounted_price == Decimal("10.00")
    assert p.savings_pct == Decimal("28")
    assert p.is_weighted
    assert p.deal_text == ""  # the description carries the deal for weighed items

    p = _scan()["1002"]  # "2 ב-100", MinQty 1.85, DiscountedPrice 92.50
    assert p.discounted_price == Decimal("50.00")
    assert p.savings_pct == Decimal("44")


def test_packaged_multibuy_keeps_deal_text():
    p = _scan()["1003"]  # "3 ק"ג ב-40": DiscountedPrice 39.99 for MinQty 3
    assert p.discounted_price == Decimal("13.33")
    assert p.savings_pct == Decimal("46")
    assert p.min_qty == Decimal("3")
    assert p.bundle_price == Decimal("39.99")
    assert p.deal_text == "3 ב-₪39.99"


def test_per_unit_price_helper():
    f = sm._per_unit_promo_price
    D = Decimal
    # bundle total / MinQty, agreeing with DiscountRate
    assert f(D("39.90"), None, D("100"), None, None, D("2")) == D("50.00")
    # synthetic case from the issue: no DiscountRate, bundle 100 for 1 unit of 39.90
    # -> 100 > 39.90; the caller drops it (never a negative saving)
    assert f(D("39.90"), None, D("100"), None, None, D("1")) == D("100.00")
    # per-kg regular vs per-100g promo with a DiscountRate that matches neither -> dropped
    assert f(D("490"), D("18.57"), D("39.90"), None, None, D("1")) is None
    # ... but resolvable when DiscountedPricePerMida says what the unit is
    assert f(D("490"), D("18.57"), D("39.90"), D("399"), D("490"), D("1")) == D("399.00")
    # DiscountedPrice and PerMida disagree, no rate to arbitrate -> dropped
    assert f(D("50"), None, D("40"), D("2"), D("10"), D("1")) is None
    # rate only
    assert f(D("50"), D("20"), None, None, None, None) == D("40.00")
    # no regular price -> cannot be checked
    assert f(None, D("20"), D("10"), None, None, None) is None


def test_description_deal_parsing():
    D = Decimal
    assert sm._desc_unit_price("בריסקט/כתף/גולש דל שומן עטרה 2 ב 100 מגד") == D("50")
    assert sm._desc_unit_price("ב2- ב 20 שח  כנפיים עוף טרי") == D("10")
    assert sm._desc_unit_price("רשת כרעיים/ירכיים עוף טרי 2קג ב42שח") == D("21")
    assert sm._desc_unit_price("נודלס 70 גרם רמי לוי 4ב10") == D("2.5")
    for desc in ("כנפיים עוף טרי ב7.90 מעל 75 מו3", "מגוון מוצרי דים סאם 170-400 גרם ב25.90",
                 "השני ב50% הנחה", "ב. 10 גרון הודו קפוא", "1.5 קג-ב14.9"):
        assert sm._desc_unit_price(desc) is None, desc


def test_savings_from_per_unit_values():
    p = sm.MeatPromo(
        chain="", store_id="", item_code="", item_name="", regular_price=Decimal("39.90"),
        discounted_price=Decimal("29.90"), discount_rate=Decimal("99"), promotion_description="",
    )
    assert p.savings_pct == Decimal("25")  # from prices, not the raw DiscountRate
    assert p.savings_amount == Decimal("10.00")


def test_not_a_promotion_descriptions():
    for desc in (
        "הנחה בגין תביעה יצוגית קפואים",
        "הנחה בגין תביעה ייצוגית",
        "פיצוי ללקוחות",
        "תו זהב 5% הנחה מותג שופרסל",
        "5% הנחה בתשלום בכרטיס אשראי",
    ):
        assert sm._NOT_A_PROMO_PAT.search(sm._norm(desc)), desc
    assert not sm._NOT_A_PROMO_PAT.search("ב2- ב 20 שח  כנפיים עוף טרי")


# ---- price per kg (#50) ----

def _item(name, price, uq, q, uom, uomp, weighted=False):
    return sm._FeedItem(
        name=name, price=Decimal(price), weighted=weighted, unit_qty=uq,
        quantity=sm._dec(q), unit_of_measure=uom, unit_price=sm._dec(uomp),
    )


def test_pack_weight_from_real_pricefull_rows():
    D = Decimal
    cases = [
        # (row, grams) — rows as published on 2026-11-11
        (_item("רצועות ריב אנטריקוט 300", "39.90", "גרם", "300.00", "100 גרם", "13.30"), D("300")),  # Shufersal
        (_item("בשר טחון מובחר 500 גרם", "34.90", "גרם", "500.00", "100 גרם", "6.98"), D("500")),
        (_item("סטייק בקר חלק 800 ג", "59.90", "גרם", "800.00", "100 גרם", "7.49"), D("800")),     # Rami Levy
        (_item("בקר טחון עטרה 1.2 ק\"", "44.90", "לא ידוע", "1.20", 'ק"ג', "37.42"), D("1200")),    # Osher Ad
        (_item("בשר בקר טחון קפוא 1 ק\"ג", "45.90", "גרם", "1000.00", "100 גרם", "4.59"), D("1000")),  # Tiv Taam
        (_item("פרגית 80 גרם", "6.90", "", "1", "", "6.9"), D("80")),                                # Bareket
        # no weight anywhere / contradicting sources
        (_item("חצי ראש כבש קפוא עטר", "39.90", "יחידות", "0.00", "יחידה", "0.00"), None),          # Osher Ad
        (_item("כרעיים עוף", "24.90", "יחידות", "1.00", "יחידות", "24.90"), None),
        (_item("סטייק בקר 500 גרם", "59.90", "גרם", "800.00", "100 גרם", "7.49"), None),             # name says 500
        (_item("בשר טחון", "34.90", "גרם", "500.00", "100 גרם", "3.49"), None),                     # per-100 g price says 1 kg
        (_item("שומן", "4.90", "גרם", "10.00", "100 גרם", "49.00"), None),                          # 10 g: implausible
    ]
    for item, grams in cases:
        assert sm._pack_grams(item) == grams, item.name


def test_unit_kg_for_weighed_items():
    D = Decimal
    # Osher Ad writes "יחידה" for weighed meat priced per kg: the flag wins
    assert sm._unit_kg(_item("חזה עוף טרי קהילות", "47.90", "יחידות", "1.00", "יחידה", "47.90", True), True) == D(1)
    assert sm._unit_kg(_item("כנפיים עוף טרי ארוז", "13.90", "קילוגרם", "1.00", "1קילוגרם", "13.90", True), True) == D(1)
    # weighed but priced per 100 g: not comparable, left out
    assert sm._unit_kg(_item("פסטרמה", "8.90", "גרם", "100", "100 גרם", "8.90", True), True) is None
    assert sm._unit_kg(_item("לשון בקר 400 גר", "32.90", "גרם", "400.00", "100 גרם", "8.23"), False) == D("0.4")


def test_scanned_promos_carry_their_price_per_kg():
    promos = _scan()
    assert promos["1001"].price_per_kg == Decimal("10.00")  # weighed
    assert promos["1003"].unit_kg is None                   # packaged "יחידות", weight unknown
    assert promos["1003"].price_per_kg is None
