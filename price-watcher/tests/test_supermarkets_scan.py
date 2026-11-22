"""Issues #50 (top 5 cheapest per category, Bareket), #40 (supermarkets cleanups).

Adapted in the merge of PR #56 into #57 to the merged scanner's API: chains
return (promos, stores) or raise `ChainScanError`; a chain with no store in
the cities or no deal today is in `chains_empty` with its reason; the report
rows are only the ranked categories (catch-alls are counted in
`unranked_by_category`, not shown); items at their regular price compete
with the deals (`MeatPromo.is_promo`); `_finalize_report` does the ranking.
"""
from __future__ import annotations

import io
import json
import warnings
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import groupby
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
    for var in ("PW_MEAT_REGIONS", "PW_MEAT_CITIES", "PW_STORES_PER_REGION",
                "PW_MEAT_TOP_PER_CATEGORY", "PW_TOP_PER_CATEGORY", "PW_BAREKET_SOURCE"):
        monkeypatch.delenv(var, raising=False)


EMPTY_PROMOS = b'<?xml version="1.0" encoding="utf-8"?><Root><Promotions Count="0"></Promotions></Root>'


def _store() -> sm.StoreInfo:
    return sm.StoreInfo(31, "הרצליה מרכז", "הרצליה", "השרון")


def _deals(promos) -> dict[str, sm.MeatPromo]:
    return {p.item_name: p for p in promos if p.is_promo}


def _groups(promos) -> list[tuple[str, list[sm.MeatPromo]]]:
    return [(cat, list(rows)) for cat, rows in groupby(promos, key=lambda p: p.category)]


# ---- parsing (#40: no Element truthiness) ----

def test_scan_store_parses_fixture_without_deprecation_warning(feeds):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        promos = sm._scan_store("רשת א", _store(), feeds["price"], feeds["promo"])
    assert promos is not None
    by_name = _deals(promos)
    # club-only, coupon, cheese and fish promos are dropped
    assert "חזה עוף מועדון" not in by_name
    assert "חזה עוף קופון" not in by_name
    assert not any("סטרצ" in p.item_name for p in promos)
    assert not any(p.item_name == "פילה סלמון" for p in promos)
    # the standard PromoFull layout: PromotionEndDate, prices on <Promotion>
    assert by_name["חזה הודו טרי"].end_date == "2026-11-18"
    assert by_name["חזה הודו טרי"].end_date_display == "18/11/2026"
    assert by_name["חזה הודו טרי"].is_weighted is True
    assert by_name["חזה הודו טרי"].price_per_kg == Decimal("29.90")


def test_item_index_ignores_tag_case_and_namespaces():
    root = ET.fromstring(
        '<Root xmlns="urn:x"><Products><Product><itemcode>7</itemcode><ItemName>כתף בקר</ItemName>'
        "<ItemPrice>59.90</ItemPrice><bIsWeighted>1</bIsWeighted></Product></Products></Root>"
    )
    item = sm._build_item_index(root)["7"]
    assert item.name == "כתף בקר" and item.price == Decimal("59.90") and item.weighted


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
    promos = _deals(sm._scan_store("רשת א", _store(), feeds["price"], feeds["promo"]))
    # "שקית" (bag) is a non-food exclusion of the classifier, so the 1 ק"ג
    # pack is checked on the PriceFull row itself.
    assert "חזה עוף שקית" not in promos
    bag = sm._FeedItem(name="חזה עוף", price=Decimal("45"), unit_qty='ק"ג', quantity=Decimal("1"))
    assert sm._unit_kg(bag, weighted=False) == Decimal("1")
    sliced = promos["חזה עוף פרוס"]              # 500 גרמים
    assert sliced.unit_kg == Decimal("0.5") and sliced.price_per_kg == Decimal("35.80")
    tray = promos["חזה עוף מגש 700 גרם"]          # weight only in the name; "2 ב-50"
    assert tray.discounted_price == Decimal("25.00")
    assert tray.price_per_kg == Decimal("35.71")
    assert tray.savings_pct is not None and tray.savings_pct > 0


def test_zip_and_gzip_feeds_are_decompressed(feeds):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("PriceFull.xml", feeds["price"])
    import gzip
    promos = sm._scan_store("רשת א", _store(), buf.getvalue(), gzip.compress(feeds["promo"]))
    assert _deals(promos)


def test_gift_item_of_a_promotion_is_not_a_deal(feeds):
    promo = feeds["promo"].replace(
        b"<ItemCode>105</ItemCode><IsGiftItem>0</IsGiftItem>",
        b"<ItemCode>105</ItemCode><IsGiftItem>1</IsGiftItem>",
    )
    promos = sm._scan_store("רשת א", _store(), feeds["price"], promo)
    assert "חזה הודו טרי" not in _deals(promos)


# ---- orchestration ----

def _fixture_chain(name: str, feeds: dict[str, bytes], promo: bytes | None = None):
    def run():
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
    assert report.chains_empty["רשת ריקה"] == "אין היום מבצעי בשר"
    assert report.chains_empty["רשת רחוקה"] == "אין סניפים בערים שנבחרו"
    assert "רשת תקולה" in report.chains_failed


def test_only_herzliya_and_ramat_hasharon(report):
    assert report.cities == ["הרצליה", "רמת השרון"]
    assert set(report.stores_by_city) == {"הרצליה", "רמת השרון"}
    assert report.promos
    for p in report.promos:
        assert set(p.cities) <= {"הרצליה", "רמת השרון"}
    # 4 fixture stores in those cities x 2 chains
    assert len(report.stores_scanned) == 8


def test_categories_ranked_cheapest_first_and_catch_alls_not_shown(report):
    groups = _groups(report.promos)
    for cat, rows in groups:
        assert not sm._is_catch_all(cat)
        assert len(rows) <= 5
        keys = [sm._price_rank_key(p) for p in rows]
        assert keys == sorted(keys)
    cats = [c for c, _rows in groups]
    assert len(cats) == len(set(cats))  # one block per category
    for expected in ("גרון / צוואר הודו", "גרון / צוואר הודו" + sm._FROZEN_SUFFIX, "חזה הודו",
                     "כנפיים עוף", "שייטל", "לשון בקר", "לשון בקר" + sm._FROZEN_SUFFIX,
                     "צלעות כבש", "צלעות בקר"):
        assert expected in cats
    assert "הודו" not in cats and "אחר" not in cats
    assert report.unranked_by_category.get("עוף – אחר")  # wing tips: counted, not shown


def test_rows_merge_stores_of_one_chain(report):
    breast = [p for p in report.promos if p.item_name == "חזה הודו טרי" and p.is_promo]
    assert len(breast) == 1
    assert breast[0].store_count == 4
    assert breast[0].chain == "רשת א"
    assert breast[0].stores[0] == "הרצליה מרכז"


def test_ranked_category_ratio_is_sane(report):
    for cat, rows in _groups(report.promos):
        prices = [p.price_per_kg for p in rows if p.price_per_kg is not None]
        if len(prices) > 1:
            assert max(prices) <= 2 * min(prices), (cat, prices)


def _mk(chain: str, name: str, price: str, *, weighted=True, pack_kg=None) -> sm.MeatPromo:
    return sm.MeatPromo(
        chain=chain, store_id="001", item_code=name, item_name=name,
        regular_price=Decimal(price) + 10, discounted_price=Decimal(price), discount_rate=None,
        promotion_description="", store_name="סניף", city="הרצליה",
        is_weighted=weighted, unit_kg=pack_kg,
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
        # unknown pack weight: left out of the ranking and counted
        _mk("ח", "חזה עוף 8", "9.90", weighted=False),
    ]
    report = sm.SupermarketReport(status="ok", promos=promos)
    sm._finalize_report(report)
    assert report.promos_by_category == {"חזה / פילה עוף": 7}
    assert report.unpriced_by_category == {"חזה / פילה עוף": 1}
    assert [p.chain for p in report.promos] == ["ה", "ב", "ד", "ג", "ו"]


def test_top_per_category_env(monkeypatch):
    monkeypatch.setenv("PW_MEAT_TOP_PER_CATEGORY", "2")   # older name
    assert sm._top_per_category() == 2
    monkeypatch.setenv("PW_MEAT_TOP_PER_CATEGORY", "junk")
    assert sm._top_per_category() == 5
    monkeypatch.setenv("PW_TOP_PER_CATEGORY", "3")         # current name wins
    assert sm._top_per_category() == 3


def test_earliest_end_date_is_kept_when_stores_merge():
    a = _mk("א", "חזה עוף", "29.90")
    a.end_date, a.stores = "2026-11-20", ["סניף 1"]
    b = _mk("א", "חזה עוף", "29.90")
    b.store_id, b.end_date, b.stores = "002", "2026-11-15", ["סניף 2"]
    report = sm.SupermarketReport(status="ok", promos=[a, b])
    sm._finalize_report(report)
    [row] = report.promos
    assert row.store_count == 2 and row.end_date == "2026-11-15"


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
    assert sm._bareket_source() == ("bina", "superbareket")
    names = [n for n, _fn in sm._chain_tasks()]
    assert sm._BAREKET_NAME in names
    assert len(names) == 2 + len(sm._PUBLISHEDPRICES_CHAINS) + len(sm._BINA_CHAINS)  # + Shufersal, Carrefour
    monkeypatch.setenv("PW_BAREKET_SOURCE", "bina:https://superbareket.binaprojects.com")
    assert sm._bareket_source() == ("bina", "https://superbareket.binaprojects.com")
    monkeypatch.setenv("PW_BAREKET_SOURCE", "publishedprices:Bareket")
    assert sm._bareket_source() == ("publishedprices", "Bareket")
    monkeypatch.setenv("PW_BAREKET_SOURCE", "off")
    assert sm._bareket_source() is None
    assert sm._BAREKET_NAME not in [n for n, _fn in sm._chain_tasks()]


def test_bareket_source_is_used_by_the_scan(monkeypatch):
    seen = []
    monkeypatch.setattr(sm, "scan_shufersal", lambda: ([], []))
    monkeypatch.setattr(sm, "scan_carrefour", lambda: ([], []))
    monkeypatch.setattr(sm, "scan_publishedprices_chain", lambda u, n: seen.append(("pp", u, n)) or ([], []))
    monkeypatch.setattr(sm, "scan_bina_chain", lambda s, n: seen.append(("bina", s, n)) or ([], []))
    monkeypatch.setenv("PW_BAREKET_SOURCE", "publishedprices:Bareket")
    sm.scan_and_report()
    assert ("pp", "Bareket", sm._BAREKET_NAME) in seen
    assert not [s for s in seen if s[0] == "bina"]


def _feed_day(days_ago: int = 0) -> str:
    return (sm.datetime.now() - timedelta(days=days_ago)).strftime("%Y%m%d")


def test_bina_portal_scan(feeds):
    base = "https://bareket.example"
    today, yesterday = _feed_day(), _feed_day(1)
    zipped_price = io.BytesIO()
    with zipfile.ZipFile(zipped_price, "w") as zf:
        zf.writestr("PriceFull.xml", feeds["price"])
    files = {
        f"Stores7290875100001-{today}0200.xml": feeds["stores"],
        f"PriceFull7290875100001-001-031-{yesterday}0200.xml": b"<Root/>",  # older, ignored
        f"PriceFull7290875100001-001-031-{today}0200.xml": zipped_price.getvalue(),
        f"PromoFull7290875100001-001-031-{today}0200.xml": feeds["promo"],
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
        promos, stores = sm.scan_bina_chain(base, "ברקת", client=client)
    assert [s.store_id for s in stores] == [31]
    assert any(p.item_name == "חזה הודו טרי" and p.chain == "ברקת" and p.is_promo for p in promos)


def test_bina_download_accepts_the_file_itself_and_relative_links():
    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.params.get("FileNm", "")
        if request.url.path == "/Download.aspx" and name == "direct":
            return httpx.Response(200, content=b"<?xml version='1.0'?><Root/>")
        if request.url.path == "/Download.aspx":
            return httpx.Response(200, json=[{"SPath": "/files/rel.xml"}])
        if request.url.path == "/files/rel.xml":
            return httpx.Response(200, content=b"<Root>rel</Root>")
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        base = "https://bareket.example/"
        assert sm._bina_download(client, base, "direct").startswith(b"<?xml")
        assert sm._bina_download(client, base, "rel") == b"<Root>rel</Root>"


def test_bina_portal_unreachable_is_reported_as_failed():
    transport = httpx.MockTransport(lambda r: httpx.Response(500))
    with httpx.Client(transport=transport) as client:
        with pytest.raises(sm.ChainScanError) as exc:
            sm.scan_bina_chain("https://bareket.example", "ברקת", client=client)
    assert exc.value.failed and exc.value.reason


def test_latest_by_store_formats():
    names = [
        "PriceFull7290027600007-001-001-20261112-0300.gz",
        "PriceFull7290027600007-001-001-20261111-0300.gz",
        "https://x/PriceFull7290027600007-002-202611120300.gz?sv=1",
    ]
    # Files older than a week are dropped, so the test fixes "today".
    now = datetime(2026, 11, 12, 12, 0)
    assert sm._latest_by_store(names, now=now) == {1: names[0], 2: names[2]}
