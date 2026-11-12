"""Issues #50 (top 5 cheapest per category, Bareket), #40 (supermarkets cleanups)."""
from __future__ import annotations

import io
import json
import warnings
import xml.etree.ElementTree as ET
import zipfile
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from price_watcher import supermarkets as sm


@pytest.fixture
def feeds(fixtures_dir) -> dict[str, bytes]:
    d = fixtures_dir / "supermarkets"
    return {
        "stores": (d / "Stores-chain.xml").read_bytes(),
        "price": (d / "PriceFull-store.xml").read_bytes(),
        "promo": (d / "PromoFull-store.xml").read_bytes(),
    }


@pytest.fixture(autouse=True)
def _default_area(monkeypatch):
    for var in ("PW_MEAT_REGIONS", "PW_STORES_PER_REGION", "PW_MEAT_TOP_PER_CATEGORY",
                "PW_BAREKET_SOURCE"):
        monkeypatch.delenv(var, raising=False)


EMPTY_PROMOS = b'<?xml version="1.0" encoding="utf-8"?><Root><Promotions Count="0"></Promotions></Root>'


def _store() -> sm.StoreInfo:
    return sm.StoreInfo(31, "הרצליה מרכז", "הרצליה", "השרון")


# ---- parsing (#40: no Element truthiness) ----

def test_scan_store_parses_fixture_without_deprecation_warning(feeds):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        promos = sm._scan_store("רשת א", _store(), feeds["price"], feeds["promo"])
    assert promos is not None
    by_name = {p.item_name: p for p in promos}
    # club-only, coupon, cheese and fish promos are dropped
    assert "חזה עוף מועדון" not in by_name
    assert "חזה עוף קופון" not in by_name
    assert not any("סטרצ" in n for n in by_name)
    assert "פילה סלמון" not in by_name
    assert by_name["חזה הודו טרי"].end_date == "2026-11-18"
    assert by_name["חזה הודו טרי"].end_date_display == "18/11/2026"
    assert by_name["חזה הודו טרי"].weighted is True
    assert by_name["חזה הודו טרי"].price_per_kg == Decimal("29.90")


def test_item_index_has_no_truthiness_dependency():
    # An <Items> element with children is truthy; an empty one is falsy. The old
    # find("Items") or find(".//Items") picked the nested one only by accident.
    root = ET.fromstring(
        "<Root><Items/><Sub><Items><Item><ItemCode>1</ItemCode><ItemName>חזה עוף</ItemName>"
        "<ItemPrice>10</ItemPrice></Item></Items></Sub></Root>"
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        index = sm._build_item_index(root)
    assert index["1"].name == "חזה עוף"


def test_per_kg_normalisation(feeds):
    promos = {p.item_name: p for p in sm._scan_store("רשת א", _store(), feeds["price"], feeds["promo"])}
    bag = promos["חזה עוף שקית"]                 # 1 ק"ג pack
    assert bag.pack_kg == Decimal("1") and bag.price_per_kg == Decimal("32.90")
    sliced = promos["חזה עוף פרוס"]              # 500 גרמים
    assert sliced.pack_kg == Decimal("0.5") and sliced.price_per_kg == Decimal("35.80")
    tray = promos["חזה עוף מגש 700 גרם"]          # weight only in the name; "2 ב-50"
    assert tray.promo_price == Decimal("25.00")
    assert tray.price_per_kg == Decimal("35.71")
    assert tray.savings_pct is not None and tray.savings_pct > 0


def test_zip_and_gzip_feeds_are_decompressed(feeds):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("PriceFull.xml", feeds["price"])
    import gzip
    promos = sm._scan_store("רשת א", _store(), buf.getvalue(), gzip.compress(feeds["promo"]))
    assert promos


# ---- orchestration ----

def _fixture_chain(name: str, feeds: dict[str, bytes], promo: bytes | None = None):
    def run() -> sm._ChainResult:
        stores = sm._parse_stores(feeds["stores"])
        return sm._scan_chain(name, stores, lambda s: (feeds["price"], promo or feeds["promo"]))
    return name, run


def _no_store_chain(name: str):
    return name, lambda: sm._scan_chain(name, [], lambda s: None)


def _broken_chain(name: str):
    def run():
        raise httpx.ConnectError("boom")
    return name, run


@pytest.fixture
def report(monkeypatch, feeds) -> sm.SupermarketReport:
    monkeypatch.setattr(sm, "_chain_tasks", lambda: [
        _fixture_chain("רשת א", feeds),
        _fixture_chain("רשת ריקה", feeds, promo=EMPTY_PROMOS),
        _no_store_chain("רשת רחוקה"),
        _broken_chain("רשת תקולה"),
    ])
    return sm.scan_and_report()


def test_chain_with_zero_meat_promos_counts_as_scanned(report):
    assert report.status == "ok"
    assert report.chains_scanned == ["רשת א", "רשת ריקה"]
    assert report.chains_without_promos == ["רשת ריקה"]
    assert report.chains_no_stores == ["רשת רחוקה"]
    assert "רשת תקולה" in report.chains_failed


def test_only_herzliya_and_ramat_hasharon(report):
    assert report.cities == ["הרצליה", "רמת השרון"]
    assert set(report.stores_by_city) == {"הרצליה", "רמת השרון"}
    assert report.promos
    for p in report.promos:
        assert set(p.cities) <= {"הרצליה", "רמת השרון"}
    # 4 fixture stores in those cities x 2 chains
    assert len(report.stores_scanned) == 8


def test_categories_ranked_cheapest_first_and_catch_alls_last(report):
    groups = sm.group_by_category(report.promos)
    seen_unranked = False
    for cat, ranked, rows in groups:
        if not ranked:
            seen_unranked = True
            continue
        assert not seen_unranked, "ranked category after a catch-all"
        assert len(rows) <= 5
        keys = [sm._price_rank_key(p) for p in rows]
        assert keys == sorted(keys)
    cats = [c for c, _r, _rows in groups]
    for expected in ("גרון הודו", "גרון הודו · קפוא", "חזה הודו", "כנפיים עוף",
                     "שייטל", "לשון בקר", "לשון בקר · קפוא", "צלעות טלה", "צלעות בקר"):
        assert expected in cats
    assert "הודו" not in cats and "אחר" not in cats
    assert cats[-1] == "עוף – אחר"


def test_rows_merge_stores_of_one_chain(report):
    breast = [p for p in report.promos if p.item_name == "חזה הודו טרי"]
    assert len(breast) == 1
    assert breast[0].store_count == 4
    assert breast[0].chain == "רשת א"
    assert breast[0].store_names[0] == "הרצליה מרכז"


def test_ranked_category_ratio_is_sane(report):
    for cat, ranked, rows in sm.group_by_category(report.promos):
        prices = [p.price_per_kg for p in rows if p.price_per_kg is not None]
        if ranked and len(prices) > 1:
            assert max(prices) <= 2 * min(prices), (cat, prices)


def _mk(chain: str, name: str, price: str, *, weighted=True, pack_kg=None) -> sm.MeatPromo:
    return sm.MeatPromo(
        chain=chain, store_id="001", item_code=name, item_name=name,
        regular_price=Decimal(price) + 10, discounted_price=Decimal(price), discount_rate=None,
        promotion_description="", store_name="סניף", city="הרצליה",
        weighted=weighted, pack_kg=pack_kg,
    )


def test_top_five_cheapest_per_kg():
    promos = [
        _mk("א", "חזה עוף 1", "39.90"),
        _mk("ב", "חזה עוף 2", "29.90"),
        _mk("ג", "חזה עוף 3", "34.90"),
        _mk("ד", "חזה עוף 4", "31.90"),
        _mk("ה", "חזה עוף 5", "27.90"),
        _mk("ו", "חזה עוף 6", "36.90"),
        # ₪19.90 per 500 g pack = ₪39.80/kg: cheap per pack, not per kg
        _mk("ז", "חזה עוף 7", "19.90", weighted=False, pack_kg=Decimal("0.5")),
        # unknown pack weight goes after every per-kg row
        _mk("ח", "חזה עוף 8", "9.90", weighted=False),
    ]
    rows, counts = sm._rank_promos(promos, 5)
    assert counts == {"חזה עוף": 8}
    assert [p.chain for p in rows] == ["ה", "ב", "ד", "ג", "ו"]


def test_top_per_category_env(monkeypatch):
    monkeypatch.setenv("PW_MEAT_TOP_PER_CATEGORY", "2")
    assert sm._top_per_category() == 2
    monkeypatch.setenv("PW_MEAT_TOP_PER_CATEGORY", "junk")
    assert sm._top_per_category() == 5


# ---- persist (#40: atomic) ----

def test_persist_is_atomic(report, monkeypatch):
    calls = []
    real_replace = sm.os.replace

    def spy(src, dst):
        calls.append((Path(src), Path(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(sm.os, "replace", spy)
    sm.persist(report)
    path = sm._state_file()
    assert calls == [(path.with_name(path.name + ".tmp"), path)]
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["cities"] == ["הרצליה", "רמת השרון"]
    assert data["promos"] and data["promos"][0]["category"]
    assert not path.with_name(path.name + ".tmp").exists()


# ---- Bareket (#50) ----

def test_bareket_source_config(monkeypatch):
    assert sm._bareket_source() == ("bina", "https://superbareket.binaprojects.com")
    names = [n for n, _fn in sm._chain_tasks()]
    assert "ברקת" in names and len(names) == 12
    monkeypatch.setenv("PW_BAREKET_SOURCE", "publishedprices:Bareket")
    assert sm._bareket_source() == ("publishedprices", "Bareket")
    monkeypatch.setenv("PW_BAREKET_SOURCE", "off")
    assert sm._bareket_source() is None
    assert "ברקת" not in [n for n, _fn in sm._chain_tasks()]


def test_bina_portal_scan(feeds):
    base = "https://bareket.example"
    stores_xml = feeds["stores"]
    zipped_price = io.BytesIO()
    with zipfile.ZipFile(zipped_price, "w") as zf:
        zf.writestr("PriceFull.xml", feeds["price"])
    files = {
        "Stores7290875100001-202611120200.xml": stores_xml,
        "PriceFull7290875100001-001-031-202611110200.xml": b"<Root/>",  # older, ignored
        "PriceFull7290875100001-001-031-202611120200.xml": zipped_price.getvalue(),
        "PromoFull7290875100001-001-031-202611120200.xml": feeds["promo"],
        # store 033 (Ramat HaSharon) publishes nothing today -> skipped, not fatal
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/MainIO_Hok.aspx":
            return httpx.Response(200, json=[{"FileNm": n} for n in files])
        if request.url.path == "/Download.aspx":
            name = request.url.params["FileNm"]
            return httpx.Response(200, json=[{"SPath": f"{base}/files/{name}"}])
        if request.url.path.startswith("/files/"):
            return httpx.Response(200, content=files[request.url.path.split("/")[-1]])
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        res = sm.scan_bina_chain(base, "ברקת", client=client)
    assert res.status == "ok"
    assert [s.store_id for s in res.stores] == [31]
    assert any(p.item_name == "חזה הודו טרי" and p.chain == "ברקת" for p in res.promos)


def test_bina_portal_unreachable_is_reported_as_failed():
    transport = httpx.MockTransport(lambda r: httpx.Response(500))
    with httpx.Client(transport=transport) as client:
        res = sm.scan_bina_chain("https://bareket.example", "ברקת", client=client)
    assert res.status == "failed" and res.error


def test_latest_by_store_formats():
    names = [
        "PriceFull7290027600007-001-001-20261112-0300.gz",
        "PriceFull7290027600007-001-001-20261111-0300.gz",
        "https://x/PriceFull7290027600007-002-202611120300.gz?sv=1",
    ]
    assert sm._latest_by_store(names) == {1: names[0], 2: names[2]}
