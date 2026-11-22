"""Meat section of the daily email (#50, #51, #40: end date shown).

Adapted in the merge of PR #56 into #57 to the merged email's wording: the
scan area is listed per city with its store count, chains without deals or
without a store in the cities are listed with their reason, prices per kg use
״, the cheapest row is highlighted by its row class, every row shows its
discount, and catch-all groups are counted in the ranking note instead of
being listed.
"""
from __future__ import annotations

import pytest

from price_watcher import email_sender
from price_watcher import supermarkets as sm


@pytest.fixture
def report(monkeypatch, fixtures_dir) -> sm.SupermarketReport:
    monkeypatch.delenv("PW_MEAT_REGIONS", raising=False)
    monkeypatch.delenv("PW_MEAT_CITIES", raising=False)
    monkeypatch.delenv("PW_STORES_PER_REGION", raising=False)
    monkeypatch.delenv("PW_MEAT_TOP_PER_CATEGORY", raising=False)
    monkeypatch.delenv("PW_TOP_PER_CATEGORY", raising=False)
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
    assert "ערים: הרצליה (" in text and "רמת השרון (" in text
    assert "רשתות שנסרקו: ברקת, רשת ריקה" in text
    assert "רשתות ללא מבצעים: רשת ריקה (אין היום מבצעי בשר)" in text
    assert "▼ חזה הודו" in text
    assert "▼ גרון / צוואר הודו – קפוא" in text
    assert "₪29.90 לק״ג · ברקת · חזה הודו טרי" in text
    assert "הרצליה מרכז · " in text and "סניף ים (הרצליה)" in text   # branches, with the city
    assert "בתוקף עד 18/11/2026" in text
    assert "קבוצות כלליות" in text           # catch-alls are counted, not listed
    assert "▼ עוף – אחר" not in text
    assert "סטרצ" not in text


def test_html_section(report):
    html = email_sender._supermarkets_section_html(report)
    assert "הרצליה" in html and "רמת השרון" in html
    assert "ברקת" in html
    assert '<tr class="f">' in html          # the cheapest row of a table
    assert "עד 18/11" in html
    assert "קפוא" in html
    assert "קבוצות כלליות" in html
    assert html.index("לשון בקר") < html.index("חזה הודו")  # red meat before poultry


def test_failed_and_empty_sections():
    failed = sm.SupermarketReport(status="fetch_failed", error="x")
    assert "x" in email_sender._supermarkets_section_html(failed)
    empty = sm.SupermarketReport(status="ok", cities=["הרצליה"],
                                 chains_empty={"ברקת": "אין סניפים בערים שנבחרו"})
    text = "\n".join(email_sender._supermarkets_section_text(empty))
    assert "ברקת (אין סניפים בערים שנבחרו)" in text
    assert "לא נמצאו היום מבצעי בשר" in text
    assert "לא נמצאו היום מבצעי בשר" in email_sender._supermarkets_section_html(empty)
