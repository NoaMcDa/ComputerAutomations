"""The meat report is organised by category: one table per category in the
email, the PW_TOP_PER_CATEGORY (default 5) cheapest deals each, ranked by
promo price per kg (#50)."""

from __future__ import annotations

import re
from decimal import Decimal

from price_watcher import email_sender
from price_watcher import supermarkets as sm

SHARON, TLV, RISHON = sm._REGION_ORDER


def _promo(chain, name, now, *, was="100.00", region=SHARON, desc=None, store="001",
           city="הרצליה", branch=None, weighted=True, unit_kg=None):
    """A raw per-store row: regular price `was`, promo price `now` (per unit)."""
    return sm.MeatPromo(
        chain=chain,
        store_id=store,
        item_code="1",
        item_name=name,
        regular_price=Decimal(was),
        discounted_price=Decimal(str(now)),
        discount_rate=None,
        promotion_description=desc if desc is not None else f"{name} {chain} {now}",
        regions=[region],
        city=city,
        cities=[city],
        stores=[branch or f"סניף {store} ({city})"],
        is_weighted=weighted,
        unit_kg=None if unit_kg is None else Decimal(unit_kg),
    )


def _report(promos, **kw):
    report = sm.SupermarketReport(status="ok", chains_scanned=sorted({p.chain for p in promos}), **kw)
    report.promos = list(promos)
    sm._finalize_report(report)
    return report


def _by_category(report):
    return email_sender._group_meat_by_category(report)


def test_category_keeps_five_cheapest_by_default(monkeypatch):
    monkeypatch.delenv("PW_TOP_PER_CATEGORY", raising=False)
    # Bigger discount does not matter: the price does.
    promos = [
        _promo("רמי לוי", "כנפיים עוף טרי", now, was=str(now + 40 - i), store=f"{i:03d}")
        for i, now in enumerate(range(10, 40))
    ]
    report = _report(promos)
    wings = _by_category(report)["כנפיים עוף"]
    assert [p.price_per_kg for p in wings] == [Decimal(n) for n in (10, 11, 12, 13, 14)]
    assert report.promos_by_category["כנפיים עוף"] == 30


def test_top_per_category_is_configurable(monkeypatch):
    monkeypatch.setenv("PW_TOP_PER_CATEGORY", "3")
    promos = [_promo("רמי לוי", "כנפיים עוף טרי", now, store=f"{now:03d}") for now in range(10, 40)]
    assert len(_by_category(_report(promos))["כנפיים עוף"]) == 3


def test_ranking_is_by_price_not_by_discount_percent():
    promos = [
        _promo("טיב טעם", "אנטריקוט בקר טרי", "99.90", was="199.90"),  # 50% off, expensive
        _promo("שופרסל", "אנטריקוט בקר טרי", "79.90", was="89.90"),    # 11% off, cheaper
    ]
    rows = _by_category(_report(promos))["אנטריקוט"]
    assert [p.chain for p in rows] == ["שופרסל", "טיב טעם"]


def test_packaged_items_are_compared_per_kg():
    promos = [
        # 500 g pack at 22.90 = 45.80 per kg: dearer than the weighed 39.90 per kg
        _promo("רמי לוי", "בשר בקר טחון 500 גרם", "22.90", was="28.90", weighted=False, unit_kg="0.5"),
        _promo("שופרסל", "בשר בקר טחון טרי", "39.90", was="49.90"),
    ]
    rows = _by_category(_report(promos))["טחון בקר"]
    assert [p.chain for p in rows] == ["שופרסל", "רמי לוי"]
    assert rows[1].price_per_kg == Decimal("45.80")
    assert rows[1].regular_per_kg == Decimal("57.80")
    assert rows[1].pack_text == "מארז 500 גרם"


def test_deals_without_a_price_per_kg_are_left_out_and_counted():
    promos = [
        _promo("שופרסל", "בשר בקר טחון טרי", "39.90"),
        _promo("אושר עד", "בשר בקר טחון ארוז", "9.90", was="12.90", weighted=False),  # weight unknown
        _promo("אושר עד", "כנפיים עוף ארוז", "5.90", was="7.90", weighted=False),
    ]
    report = _report(promos)
    assert [p.chain for p in report.promos] == ["שופרסל"]
    assert report.unpriced_by_category == {"טחון בקר": 1, "כנפיים עוף": 1}
    assert report.unpriced_count == 2
    assert report.promos_by_category == {"טחון בקר": 1}
    html = email_sender._supermarkets_section_html(report)
    assert "2 מבצעים לא נכללו בדירוג" in html
    assert "עוד 1 בלי מחיר לק״ג" in html
    assert "2 מבצעים לא נכללו בדירוג" in "\n".join(email_sender._supermarkets_section_text(report))


def test_one_chain_may_fill_the_table_when_it_is_cheapest(monkeypatch):
    # No region / chain quotas any more: strictly the five lowest prices.
    monkeypatch.delenv("PW_TOP_PER_CATEGORY", raising=False)
    promos = [_promo("רמי לוי", "כנפיים עוף טרי", 10 + i, store=f"{i:03d}") for i in range(8)]
    promos += [_promo(chain, "כנפיים עוף טרי", 30 + i, region=TLV)
               for i, chain in enumerate(["שופרסל", "יוחננוף"])]
    wings = _by_category(_report(promos))["כנפיים עוף"]
    assert {p.chain for p in wings} == {"רמי לוי"}
    assert len(wings) == 5


def test_same_deal_in_several_stores_is_one_row_listing_the_stores():
    promos = [
        _promo("שופרסל", "כנפיים עוף טרי", "11.90", desc="2 קג ב-23.80", store=store, city=city, branch=branch)
        for store, city, branch in (
            ("374", "הרצליה", "שלי הרצליה- הבנים"),
            ("057", "רמת השרון", "שלי רמת השרון- אוסישקין"),
            ("034", "רמת השרון", "שלי רמת השרון- סוקולוב"),
        )
    ]
    promos.append(_promo("שופרסל", "כנפיים עוף טרי", "12.90", store="051", branch="אקספרס הרצליה"))
    wings = _by_category(_report(promos))["כנפיים עוף"]
    assert len(wings) == 2  # two different deals
    assert wings[0].store_count == 3
    assert wings[0].stores == ["שלי הרצליה- הבנים", "שלי רמת השרון- אוסישקין", "שלי רמת השרון- סוקולוב"]
    html = email_sender._supermarkets_section_html(_report(promos))
    assert "שלי הרצליה- הבנים · שלי רמת השרון- אוסישקין" in html


def test_report_is_grouped_by_category_in_the_fixed_category_order():
    promos = [
        _promo("רמי לוי", "אנטריקוט בקר טרי", 70),
        _promo("רמי לוי", "כנפיים עוף טרי", 10),
        _promo("שופרסל", "אנטריקוט בקר טרי", 90),
        _promo("שופרסל", "כנפיים עוף טרי", 12),
    ]
    report = _report(promos)
    seen = [p.category for p in report.promos]
    # rows of one category are contiguous
    assert [c for i, c in enumerate(seen) if i == 0 or seen[i - 1] != c] == list(dict.fromkeys(seen))
    labels = [label for label, _ in sm._CATEGORIES]
    positions = [labels.index(c) for c in dict.fromkeys(seen)]
    assert positions == sorted(positions)
    assert all(p.category == sm._categorize(p) for p in report.promos)


def test_email_has_one_table_per_category_cheapest_first_with_branch(monkeypatch):
    monkeypatch.delenv("PW_MEAT_CITIES", raising=False)
    promos = [
        _promo("רמי לוי", "אנטריקוט בקר טרי", "89.90", was="129.90", branch="סוקולוב הרצליה"),
        _promo("סופר ברקת", "אנטריקוט בקר טרי", "79.90", was="99.90", city="רמת השרון",
               branch="רמת השרון"),
        _promo("יוחננוף", "כנפיים עוף טרי", "12.90", was="16.90", city="רמת השרון"),
    ]
    report = _report(promos)
    html = email_sender._supermarkets_section_html(report)
    assert html.count("<table") == 2
    tables = re.findall(r"<table.*?</table>", html, flags=re.S)
    entrecote = next(t for t in tables if "אנטריקוט בקר טרי" in t)
    assert "רמי לוי" in entrecote and "סופר ברקת" in entrecote and "יוחננוף" not in entrecote
    assert entrecote.index("סופר ברקת") < entrecote.index("רמי לוי")  # cheapest first
    assert "סוקולוב הרצליה" in entrecote
    # price column leads with the promo price per kg, then the regular one struck through
    cell = entrecote[entrecote.index("₪79.90"):]
    assert cell.index("₪79.90") < cell.index("₪99.90") < cell.index("-20%")
    assert "<s" in cell[:cell.index("₪99.90")]
    # one region only: no region chips
    assert email_sender._region_chip_html(SHARON) not in html
    assert "אנטריקוט" in html and "כנפיים" in html
    assert "המוצרים הזולים ביותר בסופרמרקטים" in html and "לפי מחיר לק״ג" in html

    text = "\n".join(email_sender._supermarkets_section_text(report))
    assert "▼ אנטריקוט" in text and "▼ כנפיים עוף" in text
    assert text.index("▼ כנפיים עוף") < text.rindex("יוחננוף")  # listed under its category
    assert "₪79.90 לק״ג" in text
    assert "ערים: הרצליה" in text and f"{SHARON} — " not in text


def test_region_chips_return_when_several_regions_are_enabled(monkeypatch):
    monkeypatch.setenv("PW_MEAT_CITIES", "הרצליה,ראשון לציון")
    report = _report([
        _promo("יוחננוף", "כנפיים עוף טרי", "11.90", region=RISHON, city="ראשון לציון"),
        _promo("שופרסל", "כנפיים עוף טרי", "12.90"),
    ])
    html = email_sender._supermarkets_section_html(report)
    assert email_sender._REGION_SHORT[RISHON] in html  # shortened in the table
    assert email_sender._region_chip_html(SHARON) in html
    assert RISHON in "\n".join(email_sender._supermarkets_section_text(report))


def test_recorded_scan_area_wins_over_current_settings(monkeypatch):
    monkeypatch.setenv("PW_MEAT_CITIES", "הכל")
    report = _report([_promo("שופרסל", "כנפיים עוף טרי", "12.90")],
                     cities=["הרצליה"], regions=[SHARON], stores_by_city={"הרצליה": 4})
    html = email_sender._supermarkets_section_html(report)
    assert email_sender._region_chip_html(SHARON) not in html
    assert "הרצליה" in html and "4 סניפים" in html


def test_catch_all_groups_are_counted_but_not_shown():
    # #51: "בשר – אחר" / "עוף – אחר" mix unrelated cuts, so they get no table.
    promos = [
        _promo("רמי לוי", "בשר ראש חלק", "89.90", was="99.90"),     # a real cut: ranked
        _promo("רמי לוי", "בקר טרי מובחר", "39.90", was="59.90"),   # בקר – כללי
        _promo("יוחננוף", "כנפית עוף טרי", "12.50", was="16.90"),  # עוף – אחר
    ]
    report = _report(promos)
    assert [p.category for p in report.promos] == ["בשר ראש"]
    assert report.unranked_by_category == {"עוף – אחר": 1, "בקר – כללי": 1}
    html = email_sender._supermarkets_section_html(report)
    assert html.count("<table") == 1 and "כנפית עוף טרי" not in html
    assert "2 מבצעים בקבוצות כלליות" in html
    assert "<b>3</b> מוצרי בשר" in html


def test_fresh_and_frozen_cuts_get_separate_tables():
    promos = [
        _promo("שופרסל", "לשון בקר קפוא חלק", "39.90", was="59.90"),
        _promo("טיב טעם", "לשון בקר איטלקי", "139.00", was="159.00"),
    ]
    groups = _by_category(_report(promos))
    assert [p.chain for p in groups["לשון בקר"]] == ["טיב טעם"]
    assert [p.chain for p in groups["לשון בקר – קפוא"]] == ["שופרסל"]


def test_red_meat_tables_come_before_poultry():
    """Display order: beef, then lamb, then chicken / turkey / goose / duck."""
    labels = [label for label, _ in sm._CATEGORIES]
    species_of = {}
    for label, species, _kws in sm._CATEGORY_RULES:
        species_of.setdefault(label, species)
    order = [species_of[lb] for lb in labels if lb in species_of]
    first = {species: order.index(species) for species in dict.fromkeys(order)}
    last = {species: len(order) - 1 - order[::-1].index(species) for species in first}
    assert last["beef"] < first["lamb"] < first["chicken"]
    assert last["lamb"] < first["chicken"] < first["turkey"] < first["goose"]
    # a frozen table directly follows its fresh twin
    assert labels.index("לשון בקר" + sm._FROZEN_SUFFIX) == labels.index("לשון בקר") + 1
    # and a real report follows the same order
    assert labels.index("אנטריקוט") < labels.index("כנפיים עוף") < labels.index("חזה הודו")


def _regular(chain, name, price, *, branch="סניף", city="הרצליה", store="001", weighted=True, unit_kg=None):
    """An item at its regular price (not on sale)."""
    return sm.MeatPromo(
        chain=chain, store_id=store, item_code=name, item_name=name,
        regular_price=Decimal(price), discounted_price=None, discount_rate=None,
        promotion_description="", regions=[SHARON], city=city, cities=[city],
        stores=[f"{branch} ({city})"], is_weighted=weighted, unit_kg=unit_kg,
    )


def test_cheaper_regular_price_outranks_a_deal():
    promos = [
        _promo("רמי לוי", "אנטריקוט בקר טרי", "129.90", was="159.90"),
        _regular("יוחננוף", "אנטריקוט בקר טרי", "119.90"),
        _regular("שופרסל", "אנטריקוט בקר טרי", "149.90"),
    ]
    report = _report(promos)
    rows = _by_category(report)["אנטריקוט"]
    assert [(p.chain, p.is_promo) for p in rows] == [
        ("יוחננוף", False), ("רמי לוי", True), ("שופרסל", False),
    ]
    assert [str(p.price_per_kg) for p in rows] == ["119.90", "129.90", "149.90"]
    assert report.promos_by_category["אנטריקוט"] == 3

    html = email_sender._supermarkets_section_html(report)
    table = html[html.index("<table"):]
    first_row = table[table.index("יוחננוף"):table.index("רמי לוי")]
    assert "₪119.90" in first_row and "מחיר רגיל" in first_row
    assert "<s dir" not in first_row and "%" not in first_row  # no fake discount on a regular price
    deal_row = table[table.index("רמי לוי"):table.index("שופרסל")]
    assert "-19%" in deal_row and "מחיר רגיל" not in deal_row

    text = "\n".join(email_sender._supermarkets_section_text(report))
    assert "מחיר רגיל (לא במבצע)" in text


def test_tie_between_a_deal_and_a_regular_price_lists_the_deal_first():
    promos = [
        _regular("יוחננוף", "אנטריקוט בקר טרי", "129.90"),
        _promo("רמי לוי", "אנטריקוט בקר טרי", "129.90", was="159.90"),
    ]
    rows = _by_category(_report(promos))["אנטריקוט"]
    assert [p.chain for p in rows] == ["רמי לוי", "יוחננוף"]


def test_category_without_any_deal_gets_no_table():
    promos = [
        _promo("רמי לוי", "אנטריקוט בקר טרי", "129.90", was="159.90"),
        _regular("יוחננוף", "כנפיים עוף טרי", "9.90"),   # cheap, but nothing on sale in this cut
    ]
    report = _report(promos)
    assert list(_by_category(report)) == ["אנטריקוט"]
    assert "כנפיים עוף" not in report.promos_by_category


def test_same_regular_item_in_two_branches_of_a_chain_is_one_row():
    promos = [
        _promo("רמי לוי", "אנטריקוט בקר טרי", "129.90", was="159.90"),
        _regular("שופרסל", "אנטריקוט בקר טרי", "139.90", branch="שלי הרצליה", store="051"),
        _regular("שופרסל", "אנטריקוט בקר טרי", "139.90", branch="דיל רמת השרון", city="רמת השרון", store="057"),
        _regular("שופרסל", "אנטריקוט בקר טרי", "145.00", branch="אקספרס", store="660"),
    ]
    rows = [p for p in _by_category(_report(promos))["אנטריקוט"] if p.chain == "שופרסל"]
    assert [str(p.price_per_kg) for p in rows] == ["139.90", "145.00"]
    assert rows[0].store_count == 2 and len(rows[0].stores) == 2


def test_regular_item_without_a_price_per_kg_is_ignored():
    promos = [
        _promo("רמי לוי", "אנטריקוט בקר טרי", "129.90", was="159.90"),
        _regular("יוחננוף", "אנטריקוט בקר ארוז", "39.90", weighted=False, unit_kg=None),
    ]
    report = _report(promos)
    assert [p.chain for p in _by_category(report)["אנטריקוט"]] == ["רמי לוי"]
    assert report.unpriced_by_category == {}  # only deals are counted as "left out"
