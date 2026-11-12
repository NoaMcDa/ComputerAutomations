"""Issue #46 (store → city matching) and #50 (scan area: Herzliya + Ramat HaSharon)."""
from __future__ import annotations

import pytest

from price_watcher import supermarkets as sm


def _row(name: str, address: str, city: str = "0") -> dict[str, str]:
    return {"storename": name, "address": address, "city": city}


# ---- #46: the exact store rows from the issue ----

def test_haifa_store_on_tel_aviv_street_is_not_tel_aviv():
    # Keshet Te'amim 005: Kiryat Eliezer, Haifa; the street is called "תל אביב".
    assert sm._region_for_store(_row("קרית אליעזר", "תל אביב 11")) is None


def test_rishon_store_spelled_without_gershayim():
    # Yochananof 028
    assert sm._region_for_store(_row("ראשלצ רמת אליהו", "משורר השואה 15")) == (
        "ראשון לציון והסביבה", "ראשון לציון",
    )


def test_tel_aviv_neighbourhood_store():
    # Yochananof 027
    assert sm._region_for_store(_row("יד אליהו", "יגאל אלון 57")) == ("תל אביב", "תל אביב")


@pytest.mark.parametrize("name", ["ראשל״צ הזהב", 'ראשל"צ הזהב', "ראשון-לציון מערב", "ראשון לציון"])
def test_rishon_spellings(name):
    assert sm._region_for_store(_row(name, "")) == ("ראשון לציון והסביבה", "ראשון לציון")


def test_bare_rishon_word_no_longer_matches():
    assert sm._region_for_store(_row("ראשון", "הרצל 2")) is None


@pytest.mark.parametrize("address", [
    "רחוב נתניה 4", "רח' הרצליה", "דרך רעננה", "שד' ראשון לציון", "שדרות רמת השרון",
    "תל אביב 11", "הרצליה 5",
])
def test_street_named_after_a_city_is_not_that_city(address):
    assert sm._region_for_store(_row("סניף מרכז", address)) is None


def test_address_city_without_house_number_still_matches():
    assert sm._region_for_store(_row("סניף ים", "בן גוריון 3, הרצליה")) == ("השרון", "הרצליה")


def test_name_wins_over_address():
    # Name says Herzliya; the address mentions a Tel Aviv street name.
    assert sm._region_for_store(_row("הרצליה פיתוח", "תל אביב 11")) == ("השרון", "הרצליה")


@pytest.mark.parametrize("name,city", [
    ("רמת אביב", "תל אביב"), ("נווה צדק", "תל אביב"),
    ("נוף ים", "הרצליה"), ("נווה עמל", "הרצליה"),
    ("מורשה", "רמת השרון"), ("נווה מגן", "רמת השרון"),
    ("ברמת השרון", "רמת השרון"),
])
def test_neighbourhoods(name, city):
    hit = sm._region_for_store(_row(name, ""))
    assert hit is not None and hit[1] == city


def test_cbs_code_still_wins():
    assert sm._region_for_store(_row("תל אביב", "תל אביב 1", "6400")) == ("השרון", "הרצליה")
    assert sm._region_for_store(_row("הרצליה", "", "4000")) is None  # Haifa code


# ---- #50: only Herzliya + Ramat HaSharon by default, switchable via PW_MEAT_REGIONS ----

def test_default_scan_area(monkeypatch):
    monkeypatch.delenv("PW_MEAT_REGIONS", raising=False)
    assert sm._enabled_cities() == {"הרצליה", "רמת השרון"}
    assert sm.scan_area_label() == "הרצליה, רמת השרון"


@pytest.mark.parametrize("value,expected", [
    ("herzliya", {"הרצליה"}),
    ("הרצליה, רמת השרון", {"הרצליה", "רמת השרון"}),
    ("tel-aviv,rishon-lezion", {"תל אביב", "ראשון לציון"}),
    ("rishon", {"ראשון לציון", "נס ציונה", "באר יעקב", "בית דגן", "חולון", "בת ים"}),
])
def test_meat_regions_env(monkeypatch, value, expected):
    monkeypatch.setenv("PW_MEAT_REGIONS", value)
    assert sm._enabled_cities() == expected


def test_meat_regions_all_and_sharon(monkeypatch):
    monkeypatch.setenv("PW_MEAT_REGIONS", "all")
    assert len(sm._enabled_cities()) == len(sm._CITY_ORDER)
    monkeypatch.setenv("PW_MEAT_REGIONS", "sharon")
    assert len(sm._enabled_cities()) == 16


def test_meat_regions_unknown_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("PW_MEAT_REGIONS", "atlantis")
    assert sm._enabled_cities() == {"הרצליה", "רמת השרון"}


def test_parse_stores_fixture_default_area(monkeypatch, fixtures_dir):
    monkeypatch.delenv("PW_MEAT_REGIONS", raising=False)
    raw = (fixtures_dir / "supermarkets" / "Stores-chain.xml").read_bytes()
    stores = {s.store_id: s.city for s in sm._parse_stores(raw)}
    assert stores == {31: "הרצליה", 32: "הרצליה", 33: "רמת השרון", 34: "רמת השרון"}


def test_parse_stores_fixture_all_cities(monkeypatch, fixtures_dir):
    monkeypatch.setenv("PW_MEAT_REGIONS", "all")
    raw = (fixtures_dir / "supermarkets" / "Stores-chain.xml").read_bytes()
    stores = {s.store_id: s.city for s in sm._parse_stores(raw)}
    assert 5 not in stores        # Haifa, "תל אביב 11"
    assert 35 not in stores       # Haifa, "רחוב הרצליה 5"
    assert 37 not in stores       # "שד' רמת השרון 3"
    assert 40 not in stores       # bare "ראשון"
    assert stores[28] == "ראשון לציון"
    assert stores[27] == "תל אביב"
    assert stores[36] == "רעננה"
    assert stores[38] == stores[39] == "ראשון לציון"
