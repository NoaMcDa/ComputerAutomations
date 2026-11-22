"""Vanilla extract / paste section of the daily email."""
from decimal import Decimal

from price_watcher import vanilla as v
from price_watcher.creatine import CreatineProduct

BASE = "https://il.iherb.com/pr/"


def _p(slug, price, per, unit):
    return CreatineProduct(name=f"{slug}, 29 מ\"ל (אונקיית נוזל 1)", url=f"{BASE}{slug}",
                           price=Decimal(price), currency="ILS", price_per_100g=None,
                           unit=unit, on_sale=False, seller="iHerb", per_unit=Decimal(per))


def test_classify_keeps_extract_and_paste_only():
    assert v.classify(BASE + "mccormick-pure-vanilla-extract-1-fl-oz-29-ml/99195") == ("extract", "99195")
    assert v.classify(BASE + "mccormick-vanilla-bean-paste-1-98-oz-56-g/121840") == ("paste", "121840")
    assert v.classify(BASE + "frontier-co-op-organic-pure-vanilla-flavoring-alcohol-free-2-fl-oz-59-ml/56763")[0] == "extract"
    for slug in (
        "davids-kids-adults-whitening-antiplaque-toothpaste-natural-orange-vanilla-4-oz-113-g/149311",
        "kal-sure-stevia-extract-vanilla-1-8-fl-oz-53-2-ml/45244",
        "now-foods-essential-oils-vanilla-in-jojoba-oil-1-fl-oz-30-ml/15224",
        "taylor-colledge-gourmet-lavender-extract-paste-1-4-oz-40-g/118782",
        "wilderness-poets-pure-ground-organic-vanilla-0-5-oz-14-g/121521",
        "levels-grass-fed-whey-protein-powder-vanilla-cinnamon-2-lb-907-g/141514",
    ):
        assert v.classify(BASE + slug) is None, slug


def test_to_items_sorts_per_100_and_dedups():
    items = v.to_items([
        _p("a-pure-vanilla-extract-2-fl-oz/2", "40", "70", "100ml"),
        _p("b-pure-vanilla-extract-1-fl-oz/1", "25", "88", "100ml"),
        _p("b-pure-vanilla-extract-1-fl-oz/1", "25", "88", "100ml"),
        _p("c-vanilla-bean-paste-56-g/3", "44", "79", "100g"),
    ])
    assert [i.product_id for i in items] == ["2", "1", "3"]
    assert items[0].name.endswith('29 מ"ל')  # ounces dropped


def test_price_changes_and_headline():
    items = v.to_items([
        _p("a-pure-vanilla-extract/1", "20", "70", "100ml"),
        _p("b-pure-vanilla-extract/2", "30", "80", "100ml"),
        _p("c-pure-vanilla-extract/3", "30", "90", "100ml"),
    ])
    assert v.apply_previous(items, {}) is True
    assert v.apply_previous(items, {"prices": {"1": "25", "2": "28"}}) is False
    by = {i.product_id: i.direction for i in items}
    assert by == {"1": "dropped", "2": "rose", "3": "new"}
    report = v.VanillaReport(status="ok", items=items)
    assert "₪20" in v.headline(report)
    html = v.section_html(report)
    assert "ירד מ-₪25" in html and "עלה מ-₪28" in html


def test_failed_scan_is_shown():
    r = v.VanillaReport(status="blocked", error="iHerb חסם")
    assert v.headline(r) == ""
    assert "נכשלה" in v.section_html(r)
    assert "נכשלה" in "\n".join(v.section_text(r))
