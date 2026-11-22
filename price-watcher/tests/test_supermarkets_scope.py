"""Scan area (#50: only Herzliya + Ramat HaSharon, switchable) and matching a
store to its city by name / address when the Stores file has City=0 (#46).

Store rows are real rows from the Stores files of 2026-11-11."""

from __future__ import annotations

import pytest

from price_watcher import supermarkets as sm

SHARON, TLV, RISHON = sm._REGION_ORDER


def _store(name, address="", city="0", sid="001"):
    return {"storeid": sid, "storename": name, "address": address, "city": city}


# ---- which cities are scanned ----

def test_default_scope_is_herzliya_and_ramat_hasharon(monkeypatch):
    monkeypatch.delenv("PW_MEAT_CITIES", raising=False)
    assert sm._enabled_cities() == ("הרצליה", "רמת השרון")
    assert sm._active_regions() == (SHARON,)
    # the other cities are still listed, just switched off
    assert {"נתניה", "תל אביב", "ראשון לציון", "חולון"} <= set(sm._CITY_ORDER)


@pytest.mark.parametrize("value, expected", [
    ("הרצליה, תל אביב", ("הרצליה", "תל אביב")),
    ('ת"א;רמה"ש', ("רמת השרון", "תל אביב")),              # any listed spelling, table order
    ("ראשון לציון והסביבה", ("ראשון לציון", "נס ציונה", "באר יעקב", "בית דגן", "חולון", "בת ים")),
    ("הכל", sm._CITY_ORDER),
    ("all", sm._CITY_ORDER),
])
def test_pw_meat_cities_overrides_the_on_off_column(monkeypatch, value, expected):
    monkeypatch.setenv("PW_MEAT_CITIES", value)
    assert sm._enabled_cities() == expected


def test_unknown_city_names_fall_back_to_the_table(monkeypatch):
    monkeypatch.setenv("PW_MEAT_CITIES", "חיפה")
    assert sm._enabled_cities() == ("הרצליה", "רמת השרון")
    monkeypatch.setenv("PW_MEAT_CITIES", "חיפה,נתניה")
    assert sm._enabled_cities() == ("נתניה",)


_STORES_XML = """<?xml version="1.0" encoding="utf-8"?>
<Root><SubChains><SubChain><Stores>
  <Store><StoreID>061</StoreID><StoreName>רמת השרון</StoreName><Address>סוקלוב 67</Address><City>2650</City></Store>
  <Store><StoreID>063</StoreID><StoreName>רחובות</StoreName><Address>יעקב 3</Address><City>8400</City></Store>
  <Store><StoreID>715</StoreID><StoreName>סוקולוב הרצליה</StoreName><Address>סוקולוב 56</Address><City>6400</City></Store>
  <Store><StoreID>006</StoreID><StoreName>תל אביב</StoreName><Address>שלבים 26</Address><City>0</City></Store>
  <Store><StoreID>026</StoreID><StoreName>רמת השרון</StoreName><Address>unknown</Address><City>0</City></Store>
  <Store><StoreID>090</StoreID><StoreName>נתניה מרכז</StoreName><Address>רחוב הרצליה 4</Address><City>0</City></Store>
  <Store><StoreID>091</StoreID><StoreName>קרית אליעזר</StoreName><Address>הרצליה 11</Address><City>0</City></Store>
</Stores></SubChain></SubChains></Root>""".encode("utf-8")


def test_parse_stores_keeps_only_the_enabled_cities(monkeypatch):
    monkeypatch.delenv("PW_MEAT_CITIES", raising=False)
    kept = {(s.store_id, s.city) for s in sm._parse_stores(_STORES_XML)}
    assert kept == {(61, "רמת השרון"), (715, "הרצליה"), (26, "רמת השרון")}

    monkeypatch.setenv("PW_MEAT_CITIES", "תל אביב")
    assert [(s.store_id, s.region) for s in sm._parse_stores(_STORES_XML)] == [(6, TLV)]


def test_store_branch_label_names_the_city_once():
    assert sm.StoreInfo(715, "סוקולוב הרצליה", "הרצליה", SHARON).branch == "סוקולוב הרצליה"
    assert sm.StoreInfo(61, "רמת השרון", "רמת השרון", SHARON).branch == "רמת השרון"
    assert sm.StoreInfo(2, "ארנה", "הרצליה", SHARON).branch == "ארנה (הרצליה)"


# ---- #46: matching by name / address ----

@pytest.mark.parametrize("store, expected", [
    # Keshet 005: a Haifa store on a street called "תל אביב"
    (_store("קרית אליעזר", "תל אביב 11"), None),
    # Yochananof 028 / 027: Rishon LeZion without gershayim, a Tel Aviv neighbourhood
    (_store("ראשלצ רמת אליהו", "משורר השואה 15"), (RISHON, "ראשון לציון")),
    (_store("יד אליהו", "יגאל אלון 57"), (TLV, "תל אביב")),
    (_store('ראשל"צ מערב'), (RISHON, "ראשון לציון")),
    (_store("ראשון-לציון"), (RISHON, "ראשון לציון")),
    # the bare word "ראשון" (first) no longer means Rishon LeZion
    (_store("ראשון במבצעים", "הרצל 3"), None),
    # Yochananof 026: name only
    (_store("רמת השרון", "unknown"), (SHARON, "רמת השרון")),
    # streets named after the target cities do not pull a store in
    (_store("קניון הצפון", "רחוב הרצליה 4"), None),
    (_store("סניף מרכז", "דרך רמת השרון"), None),
    (_store("סניף מרכז", "שד' רמת השרון 20"), None),
    (_store("סניף מרכז", "הרצליה 5"), None),
    (_store("סניף מרכז", "רמת השרון 3"), None),
    # ... while a city named at the end of an address still counts
    (_store("טיפ טוב אשר ברש", "רח'אשר ברש הרצליה"), (SHARON, "הרצליה")),
    (_store("סניף", "רח' גולומב 56 הרצליה"), (SHARON, "הרצליה")),
    # the store name wins over the address
    (_store("נתניה מרכז", "רחוב הרצליה 4"), (SHARON, "נתניה")),
    (_store("נתניה מרכז", "הרצליה פינת ויצמן"), (SHARON, "נתניה")),
    # "הרצל" (street) is not "הרצליה"
    (_store("סופר יודה הרצל", "הרצל 50"), None),
])
def test_region_for_store_by_name(store, expected):
    assert sm._region_for_store(store) == expected


def test_city_code_decides_unless_the_name_names_another_listed_city():
    # Fresh Market 324: "סופר יודה רמת השרון" carries Holon's code 6600
    assert sm._region_for_store(_store("סופר יודה רמת השרון", "אוסישקין 43", "6600")) == (SHARON, "רמת השרון")
    assert sm._region_for_store(_store("שלי הרצליה- הבנים", "הבנים 46", "6400")) == (SHARON, "הרצליה")
    # a code outside the table stays outside, whatever the name says
    assert sm._region_for_store(_store("רחובות- הרצל", "הרצל 183", "8400")) is None
    assert sm._region_for_store(_store("ארנה", "קניון ארנה", "6400")) == (SHARON, "הרצליה")
    # a street named after a city in the store name is not a city either
    assert sm._region_for_store(_store("שלי דרך נתניה", "", "6400")) == (SHARON, "הרצליה")
