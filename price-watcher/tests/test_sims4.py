"""Sims 4 watcher: embedded-JSON parsing, paging, state merge, links (#19)."""
from __future__ import annotations

import json
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from price_watcher import sims4
from price_watcher.http_headers import USER_AGENT
from price_watcher.sims4 import (
    Sims4Scan,
    pack_url,
    parse_store_page,
    persist,
    scan_and_report,
    scan_ea_detailed,
)

import sims4_fixture_gen as gen


STORE = sims4._EA_STORE_URL


def _read(fixtures_dir, name):
    return (fixtures_dir / name).read_text(encoding="utf-8")


def _by_slug(packs):
    return {p.slug: p for p in packs}


def test_fixture_files_match_generator(fixtures_dir):
    assert _read(fixtures_dir, "sims4_all_packs_p1.html") == gen.page1()
    assert _read(fixtures_dir, "sims4_all_packs_p2.html") == gen.page2()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_parses_escaped_embedded_json(fixtures_dir):
    packs, paging = parse_store_page(_read(fixtures_dir, "sims4_all_packs_p1.html"))
    got = _by_slug(packs)
    assert list(got) == [
        "the-sims-4-seasons",
        "the-sims-4-home-chef-hustles",
        "the-sims-4-romantic-garden-stuff",
        "the-sims-4-star-wars-journey-to-batuu-game-pack",
    ]
    seasons = got["the-sims-4-seasons"]
    assert (seasons.price, seasons.original_price, seasons.discount_percent) == (
        Decimal("19.99"), Decimal("39.99"), 50,
    )
    assert seasons.currency == "USD"
    assert seasons.pack_type == "EXPANSION_PACK"
    assert paging.has_next and paging.cursor == "cursor-50" and paging.total == 6


def test_real_title_is_used_not_rebuilt_from_slug(fixtures_dir):
    packs, _ = parse_store_page(_read(fixtures_dir, "sims4_all_packs_p1.html"))
    assert _by_slug(packs)["the-sims-4-home-chef-hustles"].name == "The Sims™ 4 Home Chef Hustle Stuff Pack"


def test_non_subscriber_price_even_when_subscriber_block_comes_first(fixtures_dir):
    packs, _ = parse_store_page(_read(fixtures_dir, "sims4_all_packs_p1.html"))
    chef = _by_slug(packs)["the-sims-4-home-chef-hustles"]
    # subscriberPricingInfo (EA Play 10%) is listed first in the fixture
    assert chef.discount_percent == 0
    assert chef.price == Decimal("9.99")
    assert not chef.is_on_sale


def test_pack_without_purchase_option_does_not_steal_next_price(fixtures_dir):
    packs, _ = parse_store_page(_read(fixtures_dir, "sims4_all_packs_p1.html"))
    got = _by_slug(packs)
    assert "the-sims-4-coming-soon" not in got
    batuu = got["the-sims-4-star-wars-journey-to-batuu-game-pack"]
    assert (batuu.price, batuu.discount_percent) == (Decimal("13.99"), 30)


def test_original_amount_null_and_non_en_us_id(fixtures_dir):
    packs, _ = parse_store_page(_read(fixtures_dir, "sims4_all_packs_p1.html"))
    garden = _by_slug(packs)["the-sims-4-romantic-garden-stuff"]  # slug from an en-gb_ id
    assert garden.original_price == garden.price == Decimal("9.99")
    assert garden.discount_percent == 0


def test_plain_json_script_and_missing_discount_field():
    data = {"props": {"items": [{
        "id": "fr-fr_the-sims-4-snowy-escape_il_pc",
        "title": "The Sims™ 4 Snowy Escape Expansion Pack",
        "lowestPricePurchaseOption": {"nonSubscriberPricingInfo": {
            "price": {"amount": "29.99", "originalAmount": "39.99", "currencyCode": "eur"}}},
    }]}}
    html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'
    packs, _ = parse_store_page(html)
    assert len(packs) == 1
    p = packs[0]
    assert p.slug == "the-sims-4-snowy-escape"
    assert (p.price, p.original_price, p.discount_percent, p.currency) == (
        Decimal("29.99"), Decimal("39.99"), 25, "EUR",
    )


def test_garbage_page_yields_nothing():
    assert parse_store_page("<html><script>var x = {a: 1}; f('slug')</script></html>")[0] == []


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "slug, pack_type, title, store_url, expected",
    [
        ("the-sims-4-seasons", "EXPANSION_PACK", "The Sims™ 4 Seasons Expansion Pack", None,
         "https://www.ea.com/games/the-sims/the-sims-4/packs/expansion-packs/the-sims-4-seasons"),
        ("the-sims-4-home-chef-hustles", "MICRO_CONTENT", "The Sims™ 4 Home Chef Hustle Stuff Pack", None,
         "https://www.ea.com/games/the-sims/the-sims-4/packs/stuff-packs/the-sims-4-home-chef-hustles"),
        ("the-sims-4-cozy-kitsch-kit", "MICRO_CONTENT", "The Sims™ 4 Cozy Kitsch Kit", None,
         "https://www.ea.com/games/the-sims/the-sims-4/packs/kits/the-sims-4-cozy-kitsch-kit"),
        ("the-sims-4-x", "MICRO_CONTENT", "The Sims™ 4 Something Game Pack", None,
         "https://www.ea.com/games/the-sims/the-sims-4/packs/game-packs/the-sims-4-x"),
        # type unknown: the all-packs store page, which is known to load
        ("the-sims-4-mystery", "MICRO_CONTENT", "The Sims™ 4 Mystery", None, sims4._EA_STORE_URL),
        # a URL carried by the JSON wins
        ("s", "EXPANSION_PACK", "t", "https://www.ea.com/x/s", "https://www.ea.com/x/s"),
    ],
)
def test_pack_url(slug, pack_type, title, store_url, expected):
    assert pack_url(slug, pack_type, title, store_url) == expected


def test_no_link_uses_the_dead_store_slug_pattern(fixtures_dir):
    packs, _ = parse_store_page(_read(fixtures_dir, "sims4_all_packs_p1.html"))
    for p in packs:
        assert not p.url.startswith("https://www.ea.com/games/the-sims/the-sims-4/store/" + p.slug)
        assert "/store/addons/" not in p.url


def test_link_from_pack_json_is_used_and_images_are_ignored(fixtures_dir):
    packs, _ = parse_store_page(_read(fixtures_dir, "sims4_all_packs_p1.html"))
    got = _by_slug(packs)
    assert got["the-sims-4-star-wars-journey-to-batuu-game-pack"].store_url == (
        "https://www.ea.com/games/the-sims/the-sims-4/packs/game-packs/"
        "the-sims-4-star-wars-journey-to-batuu-game-pack"
    )
    assert got["the-sims-4-seasons"].store_url is None  # image URL is not a pack link


# ---------------------------------------------------------------------------
# "Show more" paging
# ---------------------------------------------------------------------------

def _fake_site(fixtures_dir, calls):
    p1 = _read(fixtures_dir, "sims4_all_packs_p1.html")
    p2 = _read(fixtures_dir, "sims4_all_packs_p2.html")

    def fetch(url):
        calls.append(url)
        after = parse_qs(urlsplit(url).query).get(sims4._CURSOR_PARAM)
        return p2 if after == ["cursor-50"] else p1

    return fetch


def test_follows_paging_cursor(fixtures_dir):
    calls = []
    scan = scan_ea_detailed(fetch=_fake_site(fixtures_dir, calls))
    assert len(calls) == 2 and calls[0] == STORE
    assert parse_qs(urlsplit(calls[1]).query) == {sims4._CURSOR_PARAM: ["cursor-50"]}
    assert scan.pages == 2
    assert len(scan.packs) == 6 and scan.complete
    assert "the-sims-4-cozy-kitsch-kit" in _by_slug(scan.packs)


def test_paging_ignored_by_server_stops_and_flags_incomplete(fixtures_dir):
    calls = []
    p1 = _read(fixtures_dir, "sims4_all_packs_p1.html")
    scan = scan_ea_detailed(fetch=lambda url: calls.append(url) or p1)
    assert len(calls) == 2  # second request repeated page 1 -> no new packs -> stop
    assert len(scan.packs) == 4
    assert not scan.complete and scan.total == 6
    report = scan_and_report(scan)
    assert report.status == "ok" and not report.catalog_complete and report.total_reported == 6


def test_explicit_next_page_url_is_followed():
    items1 = [gen.pack("en-us_a_x", "a", "A Kit", "MICRO_CONTENT", gen.price(1, 2, 50))]
    items2 = [gen.pack("en-us_b_x", "b", "B Kit", "MICRO_CONTENT", gen.price(1, 1, 0))]

    def page(items, nxt):
        data = {"items": items, "nextPageUrl": nxt} if nxt else {"items": items}
        return f'<script type="application/json">{json.dumps(data)}</script>'

    pages = {STORE: page(items1, "/games/the-sims/the-sims-4/store/categories/all-packs?page=2")}
    pages[STORE + "?page=2"] = page(items2, None)
    scan = scan_ea_detailed(fetch=pages.get)
    assert [p.slug for p in scan.packs] == ["a", "b"] and scan.complete


def test_fetch_failure_and_parse_failure_statuses():
    assert scan_and_report(scan_ea_detailed(fetch=lambda u: None)).status == "fetch_failed"
    assert scan_and_report(scan_ea_detailed(fetch=lambda u: "<html></html>")).status == "parse_failed"


# ---------------------------------------------------------------------------
# State merge
# ---------------------------------------------------------------------------

def _pack(slug, pct):
    return sims4.Sims4Pack(
        slug=slug, name=slug, price=Decimal("10"), original_price=Decimal("20") if pct else Decimal("10"),
        discount_percent=pct, pack_type="EXPANSION_PACK",
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


def test_persist_stores_type_and_last_seen():
    persist(scan_and_report(Sims4Scan(packs=[_pack("a", 50)], pages=1, fetched=True)))
    entry = sims4._load_state()["packs"]["a"]
    assert entry["pack_type"] == "EXPANSION_PACK"
    assert entry["last_seen_at"]


def test_old_state_file_still_loads():
    sims4._save_state({"schema_version": 1, "packs": {
        "a": {"slug": "a", "name": "A", "price": "10", "original_price": "20",
              "discount_percent": 50, "currency": "USD"}}, "last_scanned_at": None})
    report = scan_and_report(Sims4Scan(packs=[_pack("a", 50)], pages=1, fetched=True))
    assert report.newly_on_sale == [] and report.discount_changed == []


def test_email_section_links_point_to_pack_pages(fixtures_dir):
    from price_watcher import email_sender
    from price_watcher.digest import DailyReport

    calls = []
    report = scan_and_report(scan_ea_detailed(fetch=_fake_site(fixtures_dir, calls)))
    payload = email_sender.build_daily_payload(DailyReport(sims4=report))
    assert payload is not None
    assert "/the-sims-4/packs/expansion-packs/the-sims-4-seasons" in payload.html_body
    assert "/the-sims-4/store/the-sims-4-seasons" not in payload.html_body


def test_shared_user_agent():
    assert sims4._HEADERS["User-Agent"] == USER_AGENT
