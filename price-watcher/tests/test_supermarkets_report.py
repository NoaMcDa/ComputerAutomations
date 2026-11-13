"""Feed-file pairing, chain failure reporting (#15) and the email section (#34 §6)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from price_watcher import email_sender
from price_watcher import supermarkets as sm

_NOW = datetime(2026, 11, 11, 12, 0)


def test_latest_by_store_ignores_placeholder_and_legacy_names():
    # Yochananof listing: a 2024 placeholder must not be picked.
    promos = [
        "PromoFull0000000000000-00-1-202411010501.gz",
        "PromoFull7290803800003-000-001-20261111-004033.gz",
        "PromoFull7290803800003-000-001-20261110-004033.gz",
    ]
    assert sm._latest_by_store(promos, now=_NOW) == {
        1: "PromoFull7290803800003-000-001-20261111-004033.gz"
    }


def test_latest_by_store_uses_one_chain_id_and_recent_files():
    # Keshet listing: a file of another chain id / old format and stale files.
    prices = [
        "PriceFull7290055700007-0820-202403281445.gz",
        "PriceFull7290055700007-001-014-20261111-001216.gz",  # foreign chain id
        "PriceFull7290785400000-001-014-20261111-001216.gz",
        "PriceFull7290785400000-001-014-20261110-001216.gz",
        "PriceFull7290785400000-001-002-20240523-001216.gz",  # stale store 002
    ]
    promos = ["PromoFull7290785400000-001-014-20261111-001216.gz"]
    chain = sm._dominant_chain_id(promos, prices)
    assert chain == "7290785400000"
    assert sm._latest_by_store(prices, chain, now=_NOW) == {
        14: "PriceFull7290785400000-001-014-20261111-001216.gz"
    }


def test_latest_by_store_accepts_names_without_subchain():
    # Keshet publishes <chain>-<store>-<YYYYMMDDHHMM> alongside the usual form.
    names = [
        "PromoFull7290785400000-105-202611110010.gz",
        "PromoFull7290785400000-105-202611100010.gz",
        "PromoFull7290785400000-001-014-20261111-001216.gz",
    ]
    assert sm._latest_by_store(names, now=_NOW) == {
        105: "PromoFull7290785400000-105-202611110010.gz",
        14: "PromoFull7290785400000-001-014-20261111-001216.gz",
    }


def test_latest_by_store_handles_shufersal_urls_and_short_stamps():
    urls = [
        "https://example.blob/pricefull/PriceFull7290027600007-001-001-20261111-032159.gz?sv=1&sig=x",
        "https://example.blob/pricefull/PriceFull7290027600007-001-001-202611101200.gz?sv=1",
    ]
    assert sm._latest_by_store(urls, now=_NOW) == {1: urls[0]}


def _store(sid: int = 1) -> sm.StoreInfo:
    return sm.StoreInfo(sid, f"סניף {sid}", "תל אביב", "תל אביב")


def test_scan_chain_reports_why_it_is_empty():
    with pytest.raises(sm.ChainScanError) as exc:
        sm._scan_chain("רשת", [], lambda s: None)
    assert not exc.value.failed

    with pytest.raises(sm.ChainScanError) as exc:
        sm._scan_chain("רשת", [_store()], lambda s: None)
    assert exc.value.failed and "עדכניים" in exc.value.reason

    with pytest.raises(sm.ChainScanError) as exc:
        sm._scan_chain("רשת", [_store()], lambda s: (None, None))
    assert exc.value.failed and "נכשלה" in exc.value.reason


def _meat(chain: str, pct_now: str = "70.00") -> sm.MeatPromo:
    return sm.MeatPromo(
        chain=chain, store_id="001", item_code="1", item_name="כתף בקר טרי",
        regular_price=Decimal("89.90"), discounted_price=Decimal(pct_now), discount_rate=None,
        promotion_description="כתף בקר 2 ב-140", store_name="סניף", city="תל אביב",
        regions=["תל אביב"], cities=["תל אביב"], min_qty=Decimal("2"),
        bundle_price=Decimal("140"), unit_kg=Decimal("1"),
    )


def test_scan_and_report_lists_failed_and_empty_chains(monkeypatch):
    def fake_shufersal():
        return [_meat(sm._SHUFERSAL_NAME)], [_store()]

    def fake_pp(user, name):
        if user == "yohananof":
            raise sm.ChainScanError("אין קבצי מחירים ומבצעים עדכניים באתר")
        if user == "Keshet":
            raise RuntimeError("boom")
        if user == "SalachD":
            raise sm.ChainScanError("אין סניפים באזורים שנבחרו", failed=False)
        return [], [_store()]

    def fake_bina(sub, name):
        raise sm.ChainScanError("אין סניפים בערים שנבחרו", failed=False)

    monkeypatch.setattr(sm, "scan_shufersal", fake_shufersal)
    monkeypatch.setattr(sm, "scan_carrefour", lambda: ([], [_store()]))
    monkeypatch.setattr(sm, "scan_publishedprices_chain", fake_pp)
    monkeypatch.setattr(sm, "scan_bina_chain", fake_bina)
    report = sm.scan_and_report()

    assert report.status == "ok"
    assert report.chains_scanned == [sm._SHUFERSAL_NAME]
    assert report.chains_failed == {
        "יוחננוף": "אין קבצי מחירים ומבצעים עדכניים באתר",
        "קשת טעמים": "שגיאה: RuntimeError",
    }
    assert report.chains_empty["סאלח דבאח"] == "אין סניפים באזורים שנבחרו"
    assert report.chains_empty["רמי לוי"] == "לא נמצאו מוצרי בשר"
    assert report.chains_empty["קרפור"] == "לא נמצאו מוצרי בשר"
    # Bareket is scanned too, and a chain without a store in the cities is listed, not dropped
    assert report.chains_empty["סופר ברקת"] == "אין סניפים בערים שנבחרו"
    assert "חצי חינם" not in report.chains_failed  # removed: lists no files on that host

    text = "\n".join(email_sender._supermarkets_section_text(report))
    html = email_sender._supermarkets_section_html(report)
    for out in (text, html):
        assert "רשתות שנכשלו" in out and "יוחננוף" in out and "קשת טעמים" in out
        assert "רשתות ללא מבצעים" in out and "רמי לוי" in out
    # per-unit price plus the deal
    assert "2 ב-₪140" in text and "₪70.00" in text
    assert "מבצע 2 ב-<span dir=\"ltr\">₪140</span>" in html


def test_all_chains_failed_still_lists_them(monkeypatch):
    def boom(*_a):
        raise sm.ChainScanError("ההתחברות לאתר המחירים נכשלה")

    monkeypatch.setattr(sm, "scan_shufersal", boom)
    monkeypatch.setattr(sm, "scan_carrefour", boom)
    monkeypatch.setattr(sm, "scan_publishedprices_chain", boom)
    monkeypatch.setattr(sm, "scan_bina_chain", boom)
    report = sm.scan_and_report()
    assert report.status == "fetch_failed"
    assert len(report.chains_failed) == 2 + len(sm._PUBLISHEDPRICES_CHAINS) + len(sm._BINA_CHAINS)
    assert "ההתחברות לאתר המחירים נכשלה" in email_sender._supermarkets_section_html(report)
    assert "רשתות שנכשלו" in "\n".join(email_sender._supermarkets_section_text(report))


def test_unknown_percentage_is_not_rendered_as_minus_dash():
    p = _meat("רשת")
    p.regular_price = None
    p.discounted_price = None
    p.discount_rate = None
    assert p.savings_pct is None
    report = sm.SupermarketReport(status="ok", chains_scanned=["רשת"], promos=[p])
    html = email_sender._supermarkets_section_html(report)
    assert "-—" not in html
    assert "-—" not in "\n".join(email_sender._supermarkets_section_text(report))

    p2 = _meat("רשת")
    report = sm.SupermarketReport(status="ok", chains_scanned=["רשת"], promos=[p2])
    assert '>-22%</b>' in email_sender._supermarkets_section_html(report)


def test_persist_writes_new_fields(tmp_path):
    report = sm.SupermarketReport(status="ok", promos=[_meat("רשת")])
    sm.persist(report)
    text = sm._state_file().read_text(encoding="utf-8")
    assert '"min_qty": "2"' in text and '"bundle_price": "140"' in text
    assert '"unit_kg": "1"' in text and '"price_per_kg": "70.00"' in text
