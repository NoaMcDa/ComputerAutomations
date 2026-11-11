"""The meat report is organised by category: one table per category in the
email, up to PW_TOP_PER_CATEGORY deals each, every region represented."""

from __future__ import annotations

import re
from decimal import Decimal

from price_watcher import email_sender
from price_watcher import supermarkets as sm

SHARON, TLV, RISHON = sm._REGION_ORDER


def _promo(chain, region, name, pct, *, desc=None, store="001", city="עיר"):
    """A raw per-store row with a regular price of 100 and `pct` percent off."""
    return sm.MeatPromo(
        chain=chain,
        store_id=store,
        item_code="1",
        item_name=name,
        regular_price=Decimal("100.00"),
        discounted_price=Decimal(100 - pct),
        discount_rate=None,
        promotion_description=desc if desc is not None else f"{name} {chain} {pct} {region}",
        regions=[region],
        city=city,
        is_weighted=True,
    )


def _report(promos):
    report = sm.SupermarketReport(status="ok", chains_scanned=sorted({p.chain for p in promos}))
    report.promos = list(promos)
    sm._finalize_report(report)
    return report


def _by_category(report):
    return email_sender._group_meat_by_category(report)


def test_category_keeps_ten_best_by_default(monkeypatch):
    monkeypatch.delenv("PW_TOP_PER_CATEGORY", raising=False)
    promos = [_promo("רמי לוי", SHARON, "כנפיים עוף טרי", pct, store=f"{pct:03d}") for pct in range(10, 40)]
    report = _report(promos)
    wings = _by_category(report)["כנפיים"]
    assert len(wings) == 10
    assert [int(p.savings_pct) for p in wings] == sorted((int(p.savings_pct) for p in wings), reverse=True)
    assert int(wings[0].savings_pct) == 39
    assert report.promos_by_category["כנפיים"] == 30


def test_top_per_category_is_configurable(monkeypatch):
    monkeypatch.setenv("PW_TOP_PER_CATEGORY", "4")
    promos = [_promo("רמי לוי", SHARON, "כנפיים עוף טרי", pct, store=f"{pct:03d}") for pct in range(10, 40)]
    assert len(_by_category(_report(promos))["כנפיים"]) == 4


def test_every_region_with_a_deal_gets_a_row_even_if_its_deal_is_weaker(monkeypatch):
    monkeypatch.delenv("PW_TOP_PER_CATEGORY", raising=False)
    # 15 strong deals in the Sharon, one weak deal in Tel Aviv, one in Rishon.
    promos = [_promo("רמי לוי", SHARON, "כנפיים עוף טרי", 30 + i, store=f"{i:03d}") for i in range(15)]
    promos.append(_promo("שופרסל", TLV, "כנפיים עוף טרי", 6))
    promos.append(_promo("יוחננוף", RISHON, "כנפיים עוף טרי", 5))
    wings = _by_category(_report(promos))["כנפיים"]
    assert len(wings) == 10
    covered = {r for p in wings for r in p.regions}
    assert covered == {SHARON, TLV, RISHON}
    # still shown best first
    assert [int(p.savings_pct) for p in wings][-2:] == [6, 5]


def test_region_without_a_deal_in_the_category_is_simply_absent():
    promos = [_promo("רמי לוי", SHARON, "כנפיים עוף טרי", 20), _promo("שופרסל", TLV, "כנפיים עוף טרי", 10)]
    wings = _by_category(_report(promos))["כנפיים"]
    assert {r for p in wings for r in p.regions} == {SHARON, TLV}


def test_chains_are_spread_before_one_chain_fills_the_table(monkeypatch):
    monkeypatch.delenv("PW_TOP_PER_CATEGORY", raising=False)
    # One chain has 20 deals that all beat the other chains' single deals.
    promos = [_promo("רמי לוי", SHARON, "כנפיים עוף טרי", 40 + i, store=f"{i:03d}") for i in range(20)]
    others = ["שופרסל", "יוחננוף", "אושר עד", "טיב טעם", "פרש מרקט"]
    promos += [_promo(chain, SHARON, "כנפיים עוף טרי", 10 + i) for i, chain in enumerate(others)]
    wings = _by_category(_report(promos))["כנפיים"]
    assert len(wings) == 10
    assert {p.chain for p in wings} == {"רמי לוי", *others}


def test_same_deal_in_several_regions_is_one_row_listing_all_of_them():
    promos = [
        _promo("שופרסל", region, "כנפיים עוף טרי", 25, desc="2 קג ב-30", store=store, city=city)
        for region, store, city in ((RISHON, "003", "חולון"), (SHARON, "001", "נתניה"), (TLV, "002", "תל אביב"))
    ]
    wings = _by_category(_report(promos))["כנפיים"]
    assert len(wings) == 1
    assert wings[0].regions == [SHARON, TLV, RISHON]  # region order, not scan order
    assert wings[0].store_count == 3
    assert set(wings[0].cities) == {"נתניה", "תל אביב", "חולון"}


def test_report_is_grouped_by_category_in_the_fixed_category_order():
    promos = [
        _promo("רמי לוי", SHARON, "אנטריקוט בקר טרי", 30),
        _promo("רמי לוי", SHARON, "כנפיים עוף טרי", 50),
        _promo("שופרסל", TLV, "אנטריקוט בקר טרי", 10),
        _promo("שופרסל", TLV, "כנפיים עוף טרי", 20),
    ]
    report = _report(promos)
    seen = [p.category for p in report.promos]
    # rows of one category are contiguous
    assert [c for i, c in enumerate(seen) if i == 0 or seen[i - 1] != c] == list(dict.fromkeys(seen))
    labels = [label for label, _ in sm._CATEGORIES]
    positions = [labels.index(c) for c in dict.fromkeys(seen)]
    assert positions == sorted(positions)
    assert all(p.category == sm._categorize(p) for p in report.promos)


def test_email_has_one_table_per_category_with_region_labels():
    promos = [
        _promo("רמי לוי", SHARON, "אנטריקוט בקר טרי", 30, city="נתניה"),
        _promo("שופרסל", TLV, "אנטריקוט בקר טרי", 10, city="תל אביב"),
        _promo("יוחננוף", RISHON, "כנפיים עוף טרי", 20, city="חולון"),
    ]
    report = _report(promos)
    html = email_sender._supermarkets_section_html(report)
    assert html.count("<table") == 2
    tables = re.findall(r"<table.*?</table>", html, flags=re.S)
    entrecote = next(t for t in tables if "אנטריקוט בקר טרי" in t)
    assert "רמי לוי" in entrecote and "שופרסל" in entrecote and "יוחננוף" not in entrecote
    assert entrecote.index("רמי לוי") < entrecote.index("שופרסל")  # best deal first
    assert SHARON in entrecote and TLV in entrecote
    assert '<span dir="ltr">-30%</span>' in entrecote
    # headings name the categories
    assert "אנטריקוט" in html and "כנפיים" in html

    text = "\n".join(email_sender._supermarkets_section_text(report))
    assert "▼ אנטריקוט" in text and "▼ כנפיים" in text
    assert text.index("▼ כנפיים") < text.rindex("יוחננוף")  # listed under its category
    assert RISHON in text


def test_long_region_name_is_shortened_in_the_table_only():
    report = _report([_promo("יוחננוף", RISHON, "כנפיים עוף טרי", 20)])
    html = email_sender._supermarkets_section_html(report)
    assert email_sender._REGION_SHORT[RISHON] in html
    assert RISHON in "\n".join(email_sender._supermarkets_section_text(report))
