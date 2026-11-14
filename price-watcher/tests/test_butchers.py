"""Online butchers (butchers.py) with mocked sites, in the shapes they served
on 2026-11-14."""

from __future__ import annotations

import json
from decimal import Decimal
from urllib.parse import unquote

import httpx
import pytest

from price_watcher import butchers as B
from price_watcher import supermarkets as sm


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _woo(id_, name, regular, price=None, on_sale=False, cats=("קצביה",), link=""):
    price = regular if price is None else price
    return {
        "id": id_, "name": name, "on_sale": on_sale, "is_in_stock": True,
        "permalink": link or f"https://aharonbros.co.il/product/{id_}/",
        "categories": [{"name": c} for c in cats],
        "prices": {"price": str(int(price * 100)), "regular_price": str(int(regular * 100)),
                   "currency_minor_unit": 2},
    }


def test_aharon_sale_and_regular_items_priced_per_kg():
    products = [
        _woo(1, "כתף טלה", 145, 135, on_sale=True),
        _woo(2, "שריר עגלה מס' 8", 86),
        _woo(3, "יין בן נון רוזה 2020", 88, 78, on_sale=True, cats=("אלכוהול ליד הבשר",)),
        _woo(4, "לשון עגלה", 60),  # page doesn't say per kg -> unpriced
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wp-json/wc/store/v1/products":
            page = int(request.url.params["page"])
            return httpx.Response(200, json=products if page == 1 else [])
        if request.url.path in ("/product/1/", "/product/2/"):
            return httpx.Response(200, text='<p>₪ 135.00 / ק"ג מחיר לק"ג</p>')
        return httpx.Response(200, text="<p>₪ 60.00</p>")

    promos = {p.item_name: p for p in B.scan_aharon(_client(handler))}
    assert set(promos) == {"כתף טלה", "שריר עגלה מס' 8", "לשון עגלה"}  # no wine
    lamb = promos["כתף טלה"]
    assert lamb.is_promo and lamb.regular_price == 145 and lamb.discounted_price == 135
    assert lamb.price_per_kg == Decimal("135.00") and lamb.stores == [B.DELIVERY]
    assert not promos["שריר עגלה מס' 8"].is_promo and promos["שריר עגלה מס' 8"].price_per_kg == 86
    assert promos["לשון עגלה"].price_per_kg is None


def _meatman_page(products) -> str:
    return (f"<script>\n const TRANSLATIONS = {{}};\n var products = {json.dumps(products, ensure_ascii=False)};\n"
            " var products_map = {};\n</script>")


def test_meatman_units_and_multi_kg_deals():
    products = [
        {"id": 1, "name": "בשר טחון טרי", "price": 69.9, "units": "לק''ג", "is_active": 1, "hidden": 0,
         "sale_text": "2 ק״ג ב130₪!", "price_before_discount": 0},
        {"id": 2, "name": "כנפיים עוף קפוא", "price": 25, "units": "קילו", "is_active": 1, "hidden": 0,
         "sale_text": "מבצע! 4 ק''ג ב50 ש''ח", "price_before_discount": 0},
        {"id": 3, "name": "אנטריקוט עגלה טרי", "price": 235, "units": "קילו", "is_active": 1, "hidden": 0,
         "sale_text": "", "price_before_discount": 0},
        {"id": 4, "name": "המבורגר אמריקאי", "price": 99, "units": "יחידה", "is_active": 1, "hidden": 0,
         "sale_text": "", "price_before_discount": 0},  # processed: not a raw cut
        {"id": 5, "name": "שייטל", "price": 145, "units": "קילו", "is_active": 0, "hidden": 0,
         "sale_text": "", "price_before_discount": 0},  # inactive
    ]

    def handler(request):
        return httpx.Response(200, text=_meatman_page(products))

    promos = {p.item_name: p for p in B.scan_meatman(_client(handler))}
    assert set(promos) == {"בשר טחון טרי", "כנפיים עוף קפוא", "אנטריקוט עגלה טרי"}
    ground = promos["בשר טחון טרי"]
    assert ground.is_promo and ground.discounted_price == Decimal("65.00") and ground.regular_price == Decimal("69.9")
    assert ground.promotion_description == "2 ק״ג ב130₪!"
    assert promos["כנפיים עוף קפוא"].price_per_kg == Decimal("12.50")
    assert not promos["אנטריקוט עגלה טרי"].is_promo and promos["אנטריקוט עגלה טרי"].price_per_kg == 235


def test_meatman_page_without_products_fails():
    with pytest.raises(RuntimeError):
        B.scan_meatman(_client(lambda r: httpx.Response(200, text="<html></html>")))


def _mp_item(ident, name, price, unit):
    return (f'<li itemident="{ident}" class="boxItem ee_list_item"><div class="item-name">'
            f'<a ee_list_itemname="{name}">{name}</a></div><div class="product-box-prices">'
            f'<p class="sale-price "><span class="name">רגיל </span><span class="value">'
            f'<span class="priceholder" id="total{ident}" price="{price}">{price}</span><em>₪</em></span></p>'
            f'<p class="price_note"><span class="name">מחיר ל&nbsp;1.00&nbsp;{unit}</span></p></div></li>')


def test_meatpoint_category_pages():
    pages = {
        "בקר": _mp_item(10, "אנטריקוט עגלה", "219.00", 'ק"ג') + _mp_item(11, "צלעות בקר טרי", "119.00", "מגש"),
        "טלה": _mp_item(10, "אנטריקוט עגלה", "219.00", 'ק"ג') + _mp_item(12, "צלעות טלה חשופות", "319.00", 'ק"ג'),
    }

    def handler(request):
        cat = unquote(request.url.path.rsplit("/", 1)[-1])
        body = pages.get(cat, "")
        return httpx.Response(200, content=body.encode("cp1255"))

    promos = {p.item_name: p for p in B.scan_meatpoint(_client(handler))}
    assert promos["אנטריקוט עגלה"].price_per_kg == 219  # seen twice, kept once
    assert promos["צלעות טלה חשופות"].price_per_kg == 319
    assert promos["צלעות בקר טרי"].price_per_kg is None  # priced per tray
    assert all(not p.is_promo for p in promos.values())


def test_kg_in_name():
    assert B._kg_in_name("כבד עוף 500 גרם") == Decimal("0.5")
    assert B._kg_in_name('שוקיים 2 ק"ג') == Decimal("2")
    assert B._kg_in_name("אנטריקוט") is None


def test_butchers_join_the_scan_and_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("PW_BUTCHERS", "1")
    names = [n for n, _ in sm._chain_tasks()]
    assert {"האחים אהרון", "מיטמן", "מיט פוינט"} <= set(names)
    monkeypatch.setenv("PW_BUTCHERS", "0")
    assert "מיטמן" not in [n for n, _ in sm._chain_tasks()]


def test_butcher_deal_competes_in_the_category_table(monkeypatch):
    monkeypatch.setenv("PW_BUTCHERS", "1")
    from price_watcher.supermarkets import MeatPromo

    def supermarket():
        p = MeatPromo(chain="שופרסל", store_id="1", item_code="1", item_name="כנפיים עוף טרי",
                      regular_price=Decimal("29.90"), discounted_price=Decimal("24.90"),
                      discount_rate=None, promotion_description="כנפיים במבצע",
                      is_weighted=True, unit_kg=Decimal("1"), stores=["סוקולוב הרצליה"])
        return [p], []

    butcher = B._promo("מיטמן", "כנפי עוף", Decimal("22.9"), Decimal("19.50"), Decimal("1"), "9")
    monkeypatch.setattr(sm, "_chain_tasks", lambda: [("שופרסל", supermarket), ("מיטמן", lambda: ([butcher], []))])
    report = sm.scan_and_report()
    wings = [p for p in report.promos if p.category == "כנפיים עוף"]
    assert [p.chain for p in wings] == ["מיטמן", "שופרסל"]  # ₪19.50/kg beats ₪24.90/kg
    assert "מיטמן" in report.chains_scanned
