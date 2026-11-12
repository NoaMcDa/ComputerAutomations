"""Facebook Marketplace tracking (#54) — offline tests with synthetic fixtures."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from price_watcher import marketplace as mp

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)


def _html(fixtures_dir, name: str) -> str:
    return (fixtures_dir / "marketplace" / name).read_text(encoding="utf-8")


def _cfg(**kw) -> mp.MarketplaceConfig:
    base = dict(queries=(mp.SearchQuery("bike", Decimal("3000")),))
    base.update(kw)
    return mp.MarketplaceConfig(**base)


def _listing(item_id: str, price, title: str = "Item", location: str | None = "Tel Aviv") -> mp.Listing:
    return mp.Listing(item_id, title, None if price is None else Decimal(str(price)),
                      f"https://www.facebook.com/marketplace/item/{item_id}/", location)


def _ok(query: mp.SearchQuery, *listings: mp.Listing) -> mp.QueryResult:
    return mp.QueryResult(query, mp.STATUS_OK, list(listings))


# --------------------------------------------------------------------------- config

def test_unconfigured_means_feature_off(monkeypatch):
    monkeypatch.delenv("PW_FB_QUERIES", raising=False)
    assert mp.load_config() is None
    assert mp.scan_and_report() is None
    assert mp.section_text(None) == []
    assert mp.section_html(None) == ""


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("PW_FB_QUERIES", "iphone 13|2,000; כיסא משרדי ;; IPHONE 13")
    monkeypatch.setenv("PW_FB_MAX_PRICE", "500")
    monkeypatch.setenv("PW_FB_LOCATION", "telaviv")
    monkeypatch.setenv("PW_FB_MIN_DROP_PCT", "10")
    cfg = mp.load_config()
    assert cfg is not None
    assert [(q.text, q.max_price) for q in cfg.queries] == [
        ("iphone 13", Decimal("2000")), ("כיסא משרדי", Decimal("500"))]
    assert cfg.min_drop_pct == Decimal("10")
    url = mp.search_url(cfg.queries[0], cfg)
    assert url.startswith("https://www.facebook.com/marketplace/telaviv/search/?")
    assert "query=iphone+13" in url and "maxPrice=2000" in url


def test_search_url_without_location():
    cfg = _cfg()
    assert mp.search_url(cfg.queries[0], cfg).startswith("https://www.facebook.com/marketplace/search/?query=bike")


# --------------------------------------------------------------------------- parsing

@pytest.mark.parametrize("raw,expected", [
    ("₪1,200", Decimal("1200")), ("1200.00", Decimal("1200.00")), ("Free", Decimal("0")),
    ("חינם", Decimal("0")), ("1,850 ₪", Decimal("1850")), ("", None), (None, None), ("call me", None),
])
def test_parse_price(raw, expected):
    assert mp.parse_price(raw) == expected


def test_parse_embedded_json(fixtures_dir):
    status, listings, err = mp.classify_page(_html(fixtures_dir, "search_embedded_json.html"),
                                             "https://www.facebook.com/marketplace/search/?query=x", 200)
    assert status == mp.STATUS_OK and err is None
    by_id = {l.item_id: l for l in listings}
    assert set(by_id) == {"1111111111111111", "2222222222222222", "3333333333333333"}  # sold one skipped
    bike = by_id["1111111111111111"]
    assert bike.title == "אופניים חשמליים במצב מצוין"
    assert bike.price == Decimal("2400.00")
    assert bike.location == "Tel Aviv-Yafo"
    assert bike.url == "https://www.facebook.com/marketplace/item/1111111111111111/"
    assert by_id["2222222222222222"].location == "Herzliya, Center District"


def test_parse_dom_fallback(fixtures_dir):
    status, listings, _ = mp.classify_page(_html(fixtures_dir, "search_dom_only.html"), "", 200)
    assert status == mp.STATUS_OK
    by_id = {l.item_id: l for l in listings}
    assert set(by_id) == {"5555555555555555", "6666666666666666"}
    shelf = by_id["5555555555555555"]
    assert (shelf.title, shelf.price, shelf.location) == ("Bookshelf IKEA Billy", Decimal("350"), "Ra'anana, Israel")
    assert by_id["6666666666666666"].price == Decimal("0")


def test_login_wall_and_block_are_statuses(fixtures_dir):
    assert mp.classify_page(_html(fixtures_dir, "login_wall.html"), "", 200)[0] == mp.STATUS_LOGIN_WALL
    assert mp.classify_page("<html></html>", "https://www.facebook.com/login/?next=x", 200)[0] == mp.STATUS_LOGIN_WALL
    assert mp.classify_page(_html(fixtures_dir, "blocked.html"), "", 200)[0] == mp.STATUS_BLOCKED
    assert mp.classify_page("", "https://www.facebook.com/checkpoint/1", 200)[0] == mp.STATUS_BLOCKED
    assert mp.classify_page("", "", 429)[0] == mp.STATUS_BLOCKED


# --------------------------------------------------------------------------- delta logic

def test_noise_and_thresholds():
    cfg = _cfg(min_drop_amount=Decimal("20"), min_drop_pct=Decimal("5"))
    D = Decimal
    assert mp.is_meaningful_drop(D(500), D(450), cfg)
    assert not mp.is_meaningful_drop(D(500), D(490), cfg)        # ₪10 < ₪20
    assert not mp.is_meaningful_drop(D(5000), D(4970), cfg)      # ₪30 but 0.6% < 5%
    assert not mp.is_meaningful_drop(D(500), D(1), cfg)          # placeholder price
    assert not mp.is_meaningful_drop(D(500), D(0), cfg)
    assert not mp.is_meaningful_drop(D(1), D(300), cfg)          # rise from placeholder
    assert not mp.is_meaningful_drop(D(300), D(350), cfg)        # a rise
    assert not mp.is_meaningful_drop(None, D(300), cfg)


def test_first_run_is_baseline_then_drops_and_new_items_are_reported():
    cfg = _cfg()
    q = cfg.queries[0]
    deals, st = mp.compute([_ok(q, _listing("1", 1000), _listing("2", 1))], mp._empty_state(), cfg, NOW)
    assert deals == []                      # baseline: no flood on the first run
    assert st["items"]["1"]["price"] == "1000"
    assert st["items"]["2"]["price"] is None  # placeholder never stored as a price
    assert st["items"]["1"]["first_seen"] == NOW.isoformat()

    later = NOW + timedelta(days=1)
    deals, st2 = mp.compute(
        [_ok(q, _listing("1", 800, location="Haifa"), _listing("2", 1), _listing("3", 250), _listing("4", 9999))],
        st, cfg, later)
    kinds = {(d.kind, d.item_id) for d in deals}
    assert kinds == {("drop", "1"), ("new", "3")}   # 4 is above the query max price
    drop = next(d for d in deals if d.kind == "drop")
    assert (drop.prev_price, drop.price, drop.delta, drop.delta_pct) == (
        Decimal("1000"), Decimal("800"), Decimal("-200"), Decimal("-20"))
    assert deals[0].kind == "drop"                   # drops listed first
    rec = st2["items"]["1"]
    assert rec["price"] == "800" and rec["last_seen"] == later.isoformat()
    assert rec["first_seen"] == NOW.isoformat() and rec["location"] == "Haifa"


def test_placeholder_does_not_overwrite_real_price():
    cfg = _cfg()
    q = cfg.queries[0]
    _, st = mp.compute([_ok(q, _listing("1", 1000))], mp._empty_state(), cfg, NOW)
    deals, st = mp.compute([_ok(q, _listing("1", 1))], st, cfg, NOW + timedelta(days=1))
    assert deals == [] and st["items"]["1"]["price"] == "1000"
    deals, _ = mp.compute([_ok(q, _listing("1", 900))], st, cfg, NOW + timedelta(days=2))
    assert [(d.kind, d.prev_price) for d in deals] == [("drop", Decimal("1000"))]


def test_failed_query_does_not_create_baseline_and_report_new_off():
    cfg = _cfg(report_new=False)
    q = cfg.queries[0]
    _, st = mp.compute([mp.QueryResult(q, mp.STATUS_LOGIN_WALL)], mp._empty_state(), cfg, NOW)
    assert st["queries"] == {}
    _, st = mp.compute([_ok(q, _listing("1", 100))], st, cfg, NOW)
    deals, _ = mp.compute([_ok(q, _listing("1", 100), _listing("2", 50))], st, cfg, NOW)
    assert deals == []


def test_old_items_are_forgotten():
    cfg = _cfg(forget_days=30)
    q = cfg.queries[0]
    _, st = mp.compute([_ok(q, _listing("1", 100))], mp._empty_state(), cfg, NOW)
    _, st = mp.compute([_ok(q, _listing("2", 100))], st, cfg, NOW + timedelta(days=31))
    assert set(st["items"]) == {"2"}


# --------------------------------------------------------------------------- stage + state

def test_scan_report_persist_roundtrip(monkeypatch):
    monkeypatch.setenv("PW_FB_QUERIES", "bike|3000;chair")
    calls = []

    def fake_fetch(q, cfg):
        calls.append(q.text)
        if q.text == "chair":
            return mp.QueryResult(q, mp.STATUS_LOGIN_WALL, error="login wall shown instead of results")
        return _ok(q, _listing("1", 1000, "Road bike"))

    r1 = mp.scan_and_report(fetch=fake_fetch, now=NOW)
    assert calls == ["bike", "chair"]
    assert r1.status == "partial" and r1.deals == []
    assert not mp.state_file().exists()          # nothing written before the email is sent
    mp.persist(r1)
    saved = json.loads(mp.state_file().read_text(encoding="utf-8"))
    assert set(saved["items"]["1"]) >= {"item_id", "title", "price", "url", "location", "first_seen", "last_seen"}
    assert not list(mp.state_file().parent.glob("*.tmp"))  # atomic write leaves no temp file

    def cheaper(q, cfg):
        return _ok(q, _listing("1", 700, "Road bike"))
    r2 = mp.scan_and_report(fetch=cheaper, now=NOW + timedelta(days=1))
    assert r2.status == "ok" and [d.item_id for d in r2.deals] == ["1"]


def test_fetcher_exception_becomes_error_status(monkeypatch):
    monkeypatch.setenv("PW_FB_QUERIES", "bike")

    def boom(q, cfg):
        raise RuntimeError("browser crashed")
    r = mp.scan_and_report(fetch=boom, now=NOW)
    assert r.status == "failed" and r.results[0].status == mp.STATUS_ERROR


def test_corrupt_state_file_is_tolerated():
    mp.state_file().write_text("{not json", encoding="utf-8")
    assert mp.load_state() == mp._empty_state()


# --------------------------------------------------------------------------- rendering

def _report_with(deals, failures=()):
    q = mp.SearchQuery("bike")
    results = [mp.QueryResult(q, mp.STATUS_OK)] + list(failures)
    return mp.MarketplaceReport(status="ok", results=results, deals=deals)


def test_section_renders_deals():
    d = mp.Deal("drop", "1", "Road <bike>", Decimal("120"), "https://www.facebook.com/marketplace/item/1/",
                "Tel Aviv", "bike", prev_price=Decimal("150"))
    n = mp.Deal("new", "2", "Chair", Decimal("80"), "https://www.facebook.com/marketplace/item/2/", None, "bike")
    text = "\n".join(mp.section_text(_report_with([d, n])))
    assert mp.SECTION_TITLE in text
    assert "₪150 → ₪120" in text and "-20%" in text
    assert "מיקום: Tel Aviv" in text and "https://www.facebook.com/marketplace/item/1/" in text
    assert "₪80 (מודעה חדשה)" in text
    assert mp.EMPTY_STATE not in text
    html = mp.section_html(_report_with([d, n]))
    assert "Road &lt;bike&gt;" in html and 'href="https://www.facebook.com/marketplace/item/1/"' in html
    assert "line-through" in html and "Tel Aviv" in html


def test_section_empty_state_and_failures():
    fail = mp.QueryResult(mp.SearchQuery("chair"), mp.STATUS_LOGIN_WALL, error="login wall")
    rep = _report_with([], [fail])
    text = mp.section_text(rep)
    assert mp.EMPTY_STATE in text
    assert any("«chair»" in ln and "התחברות" in ln for ln in text)
    assert mp.EMPTY_STATE in mp.section_html(rep)


def test_daily_email_includes_or_omits_section():
    from price_watcher.digest import DailyReport
    from price_watcher.email_sender import build_daily_payload

    off = build_daily_payload(DailyReport())
    assert mp.SECTION_TITLE not in off.text_body and mp.SECTION_TITLE not in off.html_body

    on = build_daily_payload(DailyReport(marketplace=_report_with([])))
    assert mp.SECTION_TITLE in on.text_body and mp.EMPTY_STATE in on.text_body
    assert mp.SECTION_TITLE in on.html_body and mp.EMPTY_STATE in on.html_body


def test_collect_daily_report_hook(monkeypatch):
    from price_watcher import digest

    monkeypatch.setattr(digest, "collect_digest", lambda: [])
    for mod in ("creatine", "sims4", "supermarkets"):
        m = __import__(f"price_watcher.{mod}", fromlist=["x"])
        monkeypatch.setattr(m, "scan_and_report", lambda: None)
    sentinel = _report_with([])
    monkeypatch.setattr(mp, "scan_and_report", lambda: sentinel)
    assert digest.collect_daily_report().marketplace is sentinel
