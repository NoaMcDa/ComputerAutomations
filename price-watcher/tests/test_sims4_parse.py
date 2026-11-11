"""Sims 4 pack parsing, links and state merging (offline, trimmed EA fixture)."""

from __future__ import annotations

from pathlib import Path

from price_watcher import sims4

FIXTURE = Path(__file__).parent / "fixtures" / "daily" / "ea_packs_sample.html"


def _html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_parse_uses_real_titles_and_non_subscriber_price():
    packs = {p.slug: p for p in sims4.parse_packs(_html())}
    assert set(packs) == {
        "the-sims-4-star-wars-journey-to-batuu-game-pack",
        "the-sims-4-home-chef-hustles",
        "the-sims-4-seasons",
    }
    batuu = packs["the-sims-4-star-wars-journey-to-batuu-game-pack"]
    assert batuu.name == "The Sims 4 Star Wars: Journey to Batuu Game Pack"
    # subscriber (EA Play) price is 17.99 / 10%; the list price must win
    assert str(batuu.price) == "19.99" and batuu.discount_percent == 0
    assert packs["the-sims-4-home-chef-hustles"].name == "The Sims 4 Home Chef Hustle Stuff Pack"


def test_pack_with_duplicate_listing_is_returned_once():
    slugs = [p.slug for p in sims4.parse_packs(_html())]
    assert len(slugs) == len(set(slugs))


def test_pack_link_is_the_working_buy_addon_url():
    pack = sims4.parse_packs(_html())[0]
    assert pack.url == f"https://www.ea.com/games/the-sims/the-sims-4/buy/addon/{pack.slug}"
    assert "/store/" not in pack.url


def _page(*nodes: dict) -> str:
    """Minimal EA-style page: one flight row listing the given pack nodes."""
    import json
    row = "1f:" + json.dumps({"items": list(nodes)})
    return f"<script>self.__next_f.push([1,{json.dumps(row)}])</script>"


def _node(slug: str, amount, original, pct, option_key="lowestPricePurchaseOption") -> dict:
    return {
        "slug": slug,
        "title": f"Title of {slug}",
        "lowestPriceGameProduct": {"nonSubscriberPricingInfo": {option_key: {
            "amount": amount, "originalAmount": original, "discountPercentage": pct}}},
    }


def test_missing_purchase_option_does_not_shift_prices_to_next_pack():
    # old regex paired a pack with the NEXT pack's price when its own was missing
    html = _page(_node("pack-a", 1.0, 1.0, 0, option_key="other"), _node("pack-b", 20.0, 40.0, 50))
    packs = {p.slug: p for p in sims4.parse_packs(html)}
    assert "pack-a" not in packs
    assert str(packs["pack-b"].price) == "20.0" and packs["pack-b"].discount_percent == 50


def test_sale_is_detected_with_real_title():
    packs = sims4.parse_packs(_page(_node("pack-b", 20.0, 40.0, 50)))
    assert packs[0].is_on_sale and packs[0].name == "Title of pack-b"


def test_null_original_amount_and_missing_percentage(monkeypatch):
    node = {
        "slug": "the-sims-4-test-kit",
        "title": "The Sims\u2122 4 Test Kit\n",
        "lowestPriceGameProduct": {"nonSubscriberPricingInfo": {"lowestPricePurchaseOption": {
            "amount": 5.0, "originalAmount": None, "discountPercentage": "$undefined"}}},
    }
    pack = sims4._pack_from_node(node)
    assert pack is not None and pack.price == pack.original_price and pack.discount_percent == 0
    assert pack.name == "The Sims 4 Test Kit"
    node["lowestPriceGameProduct"]["nonSubscriberPricingInfo"]["lowestPricePurchaseOption"].update(
        originalAmount=10.0, discountPercentage=None)
    assert sims4._pack_from_node(node).discount_percent == 50


def test_pack_without_pricing_is_skipped():
    node = {"slug": "x", "title": "X", "lowestPriceGameProduct": {"nonSubscriberPricingInfo": "$5a:ref"}}
    assert sims4._pack_from_node(node) is None


def _pack(slug, price, orig, pct):
    from decimal import Decimal
    return sims4.Sims4Pack(slug=slug, name=slug, price=Decimal(price), original_price=Decimal(orig),
                           discount_percent=pct)


def test_persist_merges_so_dropped_packs_are_not_reannounced(monkeypatch):
    sale = _pack("old-pack", "10", "20", 50)
    sims4.persist(sims4.Sims4Report(status="ok", packs=[sale, _pack("other", "5", "5", 0)]))
    # day 2: old-pack fell off the first page
    monkeypatch.setattr(sims4, "scan_ea", lambda: [_pack("other", "5", "5", 0)])
    r2 = sims4.scan_and_report()
    sims4.persist(r2)
    # day 3: it is back, still on sale -> must NOT be "newly on sale"
    monkeypatch.setattr(sims4, "scan_ea", lambda: [sale, _pack("other", "5", "5", 0)])
    r3 = sims4.scan_and_report()
    assert r3.newly_on_sale == []


def test_scan_ea_parses_http_response(monkeypatch):
    class Resp:
        status_code = 200
        text = _html()

    class Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url): return Resp()

    monkeypatch.setattr(sims4.httpx, "Client", Client)
    report = sims4.scan_and_report()
    assert report.status == "ok" and len(report.packs) == 3 and not report.page_limited


def test_full_page_marks_report_as_page_limited(monkeypatch):
    monkeypatch.setattr(sims4, "scan_ea", lambda: [_pack(f"p{i}", "1", "1", 0) for i in range(50)])
    assert sims4.scan_and_report().page_limited is True
