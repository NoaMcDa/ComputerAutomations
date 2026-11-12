"""Sims 4 watcher: fetch/parse statuses, state merge, links (#19).

From origin/main, adapted to the merged sims4 module (the PR #57 one, checked
against the live EA store): packs are read from the real page shape
(``lowestPriceGameProduct.nonSubscriberPricingInfo.lowestPricePurchaseOption``,
see tests/fixtures/daily/ea_packs_sample.html), links are
``/the-sims-4/buy/addon/<slug>`` (confirmed live), and only the first store
page is read (the store has no plain-HTTP paging). origin/main's tests for its
synthetic page shape, its ``/packs/<type>/<slug>`` links and its cursor paging
were dropped together with the synthetic fixtures.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from price_watcher import sims4
from price_watcher.http_headers import USER_AGENT
from price_watcher.sims4 import (
    Sims4Scan,
    parse_packs,
    persist,
    scan_and_report,
    scan_ea_detailed,
)

SAMPLE = Path(__file__).parent / "fixtures" / "daily" / "ea_packs_sample.html"


def _sample() -> str:
    return SAMPLE.read_text(encoding="utf-8")


def test_garbage_page_yields_nothing():
    assert parse_packs("<html><script>var x = {a: 1}; f('slug')</script></html>") == []


def test_fetch_failure_and_parse_failure_statuses():
    assert scan_and_report(scan_ea_detailed(fetch=lambda u: None)).status == "fetch_failed"
    assert scan_and_report(scan_ea_detailed(fetch=lambda u: "<html></html>")).status == "parse_failed"


def test_parse_failure_reported_from_a_real_fetch(monkeypatch):
    """The page loaded but held no packs: parse_failed, not fetch_failed."""
    monkeypatch.setattr(sims4, "_fetch", lambda url: "<html></html>")
    report = scan_and_report()
    assert report.status == "parse_failed" and report.error
    monkeypatch.setattr(sims4, "_fetch", lambda url: None)
    assert scan_and_report().status == "fetch_failed"


def test_no_link_uses_the_dead_store_slug_pattern():
    packs = parse_packs(_sample())
    assert packs
    for p in packs:
        assert not p.url.startswith("https://www.ea.com/games/the-sims/the-sims-4/store/" + p.slug)
        assert "/store/addons/" not in p.url


# ---------------------------------------------------------------------------
# State merge
# ---------------------------------------------------------------------------

def _pack(slug, pct):
    return sims4.Sims4Pack(
        slug=slug, name=slug, price=Decimal("10"), original_price=Decimal("20") if pct else Decimal("10"),
        discount_percent=pct,
    )


def test_pack_that_drops_out_and_returns_is_not_reannounced():
    day1 = scan_and_report(Sims4Scan(packs=[_pack("a", 50), _pack("b", 30)], pages=1, fetched=True))
    assert {p.slug for p in day1.newly_on_sale} == {"a", "b"}
    persist(day1)

    day2 = scan_and_report(Sims4Scan(packs=[_pack("a", 50)], pages=1, fetched=True))  # "b" missing
    assert day2.newly_on_sale == [] and day2.discount_ended == []
    persist(day2)
    assert set(sims4._load_state()["packs"]) == {"a", "b"}  # merged, not replaced

    day3 = scan_and_report(Sims4Scan(packs=[_pack("a", 50), _pack("b", 30)], pages=1, fetched=True))
    assert day3.newly_on_sale == []


def test_returning_pack_with_ended_discount_is_reported_as_ended():
    persist(scan_and_report(Sims4Scan(packs=[_pack("b", 30)], pages=1, fetched=True)))
    persist(scan_and_report(Sims4Scan(packs=[_pack("a", 0)], pages=1, fetched=True)))
    day3 = scan_and_report(Sims4Scan(packs=[_pack("b", 0)], pages=1, fetched=True))
    assert [p.slug for p in day3.discount_ended] == ["b"]


def test_persist_stores_last_seen():
    persist(scan_and_report(Sims4Scan(packs=[_pack("a", 50)], pages=1, fetched=True)))
    entry = sims4._load_state()["packs"]["a"]
    assert entry["last_seen_at"]


def test_old_state_file_still_loads():
    sims4._save_state({"schema_version": 1, "packs": {
        "a": {"slug": "a", "name": "A", "price": "10", "original_price": "20",
              "discount_percent": 50, "currency": "USD"}}, "last_scanned_at": None})
    report = scan_and_report(Sims4Scan(packs=[_pack("a", 50)], pages=1, fetched=True))
    assert report.newly_on_sale == [] and report.discount_changed == []


def test_email_section_links_point_to_pack_pages():
    from price_watcher import email_sender
    from price_watcher.digest import DailyReport

    report = scan_and_report(scan_ea_detailed(fetch=lambda url: _sample()))
    assert report.status == "ok"
    persist(report)
    # put one pack on sale so the section lists it with its link
    seasons = next(p for p in report.packs if p.slug == "the-sims-4-seasons")
    seasons.discount_percent, seasons.original_price = 50, seasons.price * 2
    report.on_sale_now = [seasons]
    payload = email_sender.build_daily_payload(DailyReport(sims4=report))
    assert payload is not None
    assert "/the-sims-4/buy/addon/the-sims-4-seasons" in payload.html_body
    assert "/the-sims-4/store/the-sims-4-seasons" not in payload.html_body


def test_shared_user_agent():
    assert sims4._HEADERS["User-Agent"] == USER_AGENT
