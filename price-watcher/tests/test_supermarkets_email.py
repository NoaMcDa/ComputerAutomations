"""Meat section of the daily email (#50, #51, #40: end date shown)."""
from __future__ import annotations

import pytest

from price_watcher import email_sender
from price_watcher import supermarkets as sm


@pytest.fixture
def report(monkeypatch, fixtures_dir) -> sm.SupermarketReport:
    monkeypatch.delenv("PW_MEAT_REGIONS", raising=False)
    monkeypatch.delenv("PW_STORES_PER_REGION", raising=False)
    monkeypatch.delenv("PW_MEAT_TOP_PER_CATEGORY", raising=False)
    d = fixtures_dir / "supermarkets"
    stores_raw = (d / "Stores-chain.xml").read_bytes()
    files = ((d / "PriceFull-store.xml").read_bytes(), (d / "PromoFull-store.xml").read_bytes())
    empty = b"<Root><Promotions/></Root>"
    monkeypatch.setattr(sm, "_chain_tasks", lambda: [
        ("ברקת", lambda: sm._scan_chain("ברקת", sm._parse_stores(stores_raw), lambda s: files)),
        ("רשת ריקה", lambda: sm._scan_chain(
            "רשת ריקה", sm._parse_stores(stores_raw), lambda s: (files[0], empty))),
    ])
    return sm.scan_and_report()


def test_text_section(report):
    text = "\n".join(email_sender._supermarkets_section_text(report))
    assert "אזור: הרצליה, רמת השרון" in text
    assert "רשתות שנסרקו (2): ברקת, רשת ריקה" in text
    assert "נסרקו, ללא מבצעי בשר היום: רשת ריקה" in text
    assert "▼ חזה הודו" in text
    assert "▼ גרון הודו · קפוא" in text
    assert '₪29.90 לק"ג' in text
    assert "ברקת – הרצליה מרכז ועוד 3 סניפים" in text
    assert "בתוקף עד 18/11/2026" in text
    assert "קטגוריות כלליות" in text
    assert text.index("▼ חזה הודו") < text.index("▼ עוף – אחר")
    assert "סטרצ" not in text
    assert "הנחה" not in text.split("▼", 1)[1]  # ranked by price, not by discount


def test_html_section(report):
    html = email_sender._supermarkets_section_html(report)
    assert "הרצליה, רמת השרון" in html
    assert "ברקת" in html
    assert "הכי זול" in html
    assert "בתוקף עד 18/11/2026" in html
    assert "קפוא" in html
    assert "לא מדורג" in html
    assert html.index("חזה הודו") < html.index("עוף – אחר")


def test_failed_and_empty_sections():
    failed = sm.SupermarketReport(status="fetch_failed", error="x")
    assert "x" in email_sender._supermarkets_section_html(failed)
    empty = sm.SupermarketReport(status="ok", cities=["הרצליה"], chains_no_stores=["ברקת"])
    text = "\n".join(email_sender._supermarkets_section_text(empty))
    assert "אין סניפים באזור: ברקת" in text
    assert "לא נמצאו היום מבצעי בשר" in text
    assert "לא נמצאו היום מבצעי בשר" in email_sender._supermarkets_section_html(empty)
