"""Travel deals: 4–5 star hotel filter (#52) and continent quotas/windows (#53)."""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from price_watcher import travel_deals as td

TODAY = date(2026, 11, 12)


def raw(dest, country, days, price, hotels="none", **kw):
    d = {"destination": dest, "country": country,
         "depart_date": (TODAY + timedelta(days=days)).isoformat(), "price": price,
         "url": f"https://deals.example/{dest}"}
    if hotels != "none":
        d["hotels"] = hotels
    d.update(kw)
    return d


def deal(dest, country, days, price, hotels="none", **kw):
    return td.deal_from_dict(raw(dest, country, days, price, hotels, **kw))


# ------------------------------------------------------------- #52 stars
@pytest.mark.parametrize("stars, ok", [
    (5, True), (4, True), (4.5, True), ("4.5", True), ("5★", True), ("4 stars", True),
    (Decimal("4.0"), True),
    (3.5, False), (3, False), (2, False), (1, False), (0, False), (5.5, False),
    (None, False), ("", False), ("unrated", False), (True, False), ("nan", False), ([], False),
])
def test_hotel_qualifies(stars, ok):
    assert td.hotel_qualifies(stars) is ok


def test_package_keeps_only_qualifying_hotels():
    d = deal("Rome", "Italy", 10, 1500, hotels=[
        {"name": "Three", "stars": 3}, {"name": "FourHalf", "stars": 4.5},
        {"name": "Unrated"}, {"name": "Five", "stars": 5}, {"name": "ThreeHalf", "stars": "3.5"},
    ])
    out = td.filter_hotels(d)
    assert [h.name for h in out.hotels] == ["FourHalf", "Five"]


@pytest.mark.parametrize("hotels", [
    [{"name": "A", "stars": 3}, {"name": "B", "stars": 3.5}],
    [{"name": "A"}],
    [],
])
def test_package_without_4plus_hotel_is_dropped(hotels):
    assert td.filter_hotels(deal("Rome", "Italy", 10, 1500, hotels=hotels)) is None


def test_single_hotel_object_and_flight_only():
    d = td.deal_from_dict({**raw("Rome", "Italy", 5, 900), "hotel": {"name": "X", "stars": 2}})
    assert d.is_package and td.filter_hotels(d) is None
    flight = deal("Rome", "Italy", 5, 400)
    assert not flight.is_package and td.filter_hotels(flight) is flight


def test_build_report_counts_dropped_and_never_shows_low_star():
    feed = [raw("Rome", "Italy", 5, 900, hotels=[{"name": "Cheap", "stars": 3}]),
            raw("Paris", "France", 5, 1200, hotels=[{"name": "Lux", "stars": 5}])]
    rep = td.build_report(feed, TODAY)
    europe = rep.groups[0]
    assert rep.dropped_low_star == 1
    assert [d.destination for d in europe.deals] == ["Paris"]
    html = td.section_html(rep)
    assert "Cheap" not in html and "Lux" in html


# --------------------------------------------------------- #53 continents
def test_country_resolution():
    assert td.resolve_country("JP") == ("Japan", "Asia")
    assert td.resolve_country("thailand") == ("Thailand", "Asia")
    assert td.resolve_country("יפן") == ("Japan", "Asia")
    assert td.resolve_country("Italy") == ("Italy", "Europe")
    assert td.resolve_country("Narnia") == ("Narnia", None)
    assert td.deal_from_dict(raw("X", "Narnia", 5, 100, continent="africa")).continent == "Africa"


@pytest.mark.parametrize("days, ok", [(-1, False), (0, True), (49, True), (50, True), (51, False)])
def test_europe_50_day_cutoff(days, ok):
    groups = td.group_by_continent([deal("Rome", "Italy", days, 500)], TODAY)
    assert (len(groups[0].deals) == 1) is ok


@pytest.mark.parametrize("days, ok", [(0, True), (51, True), (365, True), (366, False)])
def test_other_continents_365_day_cutoff(days, ok):
    groups = {g.continent: g for g in td.group_by_continent([deal("NYC", "US", days, 500)], TODAY)}
    assert (len(groups["North America"].deals) == 1) is ok


def test_europe_keeps_baseline_count_cheapest_first():
    europe = [deal(f"E{i}", "Spain", i, 1000 - i) for i in range(20)]
    europe.append(deal("Late", "Spain", 60, 1))  # outside the 50-day window
    g = td.group_by_continent(europe, TODAY, europe_limit=12)[0]
    assert g.continent == "Europe" and len(g.deals) == 12
    prices = [d.price for d in g.deals]
    assert prices == sorted(prices) and "Late" not in {d.destination for d in g.deals}


def test_each_other_continent_has_exactly_four():
    deals = []
    for country in ["US", "BR", "EG", "AU", "VN"]:
        deals += [deal(f"{country}{i}", country, 100 + i, 2000 + i) for i in range(7)]
    deals += [deal("Tokyo", "Japan", 200, 9000), deal("Bangkok", "Thailand", 300, 8000)]
    groups = td.group_by_continent(deals, TODAY)
    for g in groups[1:]:
        assert len(g.deals) == 4, g.continent


def test_asia_always_includes_japan_and_thailand():
    # Six cheap Vietnam deals would fill Asia without the quota rule.
    deals = [deal(f"Hanoi{i}", "Vietnam", 30 + i, 1000 + i) for i in range(6)]
    deals += [deal("Tokyo", "Japan", 200, 5000), deal("Osaka", "JP", 210, 4500),
              deal("Bangkok", "TH", 300, 6000)]
    asia = {g.continent: g for g in td.group_by_continent(deals, TODAY)}["Asia"]
    countries = [d.country for d in asia.deals]
    assert len(asia.deals) == 4
    assert countries.count("Japan") >= 1 and countries.count("Thailand") >= 1
    assert "Osaka" in {d.destination for d in asia.deals}  # cheapest Japan deal
    assert asia.missing_required == []
    assert [d.price for d in asia.deals] == sorted(d.price for d in asia.deals)


def test_asia_reports_missing_thailand():
    deals = [deal("Tokyo", "Japan", 20, 5000), deal("Hanoi", "Vietnam", 20, 3000)]
    asia = td.group_by_continent(deals, TODAY)[1]
    assert asia.missing_required == ["Thailand"]
    assert "תאילנד" in "\n".join(td.section_text(td.TravelReport(groups=[asia], today=TODAY)))


def test_japan_outside_year_window_does_not_count():
    deals = [deal("Tokyo", "Japan", 366, 100), deal("Bangkok", "Thailand", 10, 100)]
    asia = td.group_by_continent(deals, TODAY)[1]
    assert asia.missing_required == ["Japan"]


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("PW_TRAVEL_EUROPE_LIMIT", "2")
    monkeypatch.setenv("PW_TRAVEL_EUROPE_DAYS", "10")
    feed = [raw(f"E{i}", "France", i * 3, 100 + i) for i in range(6)]  # days 0..15
    g = td.build_report(feed, TODAY).groups[0]
    assert len(g.deals) == 2 and all(d.depart_date <= TODAY + timedelta(days=10) for d in g.deals)


# -------------------------------------------------------------- feed / io
def test_feature_off_without_feed(monkeypatch, tmp_path):
    monkeypatch.setenv("PW_TRAVEL_DEALS_FILE", str(tmp_path / "missing.json"))
    assert td.scan_and_report(TODAY) is None
    assert td.section_text(None) == [] and td.section_html(None) == ""


def test_feed_file_round_trip(monkeypatch, tmp_path):
    f = tmp_path / "deals.json"
    f.write_text(json.dumps({"deals": [raw("Rome", "Italy", 5, 900), {"bad": 1},
                                       raw("Nowhere", "IT", 5, -3)]}), encoding="utf-8")
    monkeypatch.setenv("PW_TRAVEL_DEALS_FILE", str(f))
    rep = td.scan_and_report(TODAY)
    assert rep.status == "ok" and [d.destination for d in rep.groups[0].deals] == ["Rome"]


def test_corrupt_feed_is_reported_not_raised(monkeypatch, tmp_path):
    f = tmp_path / "deals.json"
    f.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("PW_TRAVEL_DEALS_FILE", str(f))
    rep = td.scan_and_report(TODAY)
    assert rep.status == "error" and "לא ניתן" in td.section_html(rep)


def test_empty_continent_renders_fallback_and_escapes():
    rep = td.build_report([raw("<b>Rome</b>", "Italy", 5, 900)], TODAY)
    html = td.section_html(rep)
    assert "&lt;b&gt;Rome&lt;/b&gt;" in html and "<b>Rome" not in html
    assert html.count("אין דילים מתאימים") == len(td.CONTINENT_ORDER) - 1


def test_daily_email_includes_travel_section():
    from price_watcher import email_sender
    rep = td.build_report([raw("Paris", "France", 5, 1200,
                               hotels=[{"name": "Lux", "stars": 5}])], TODAY)
    report = SimpleNamespace(site_digests=[], creatine=None, sims4=None,
                             supermarkets=None, marketplace=None, travel=rep)
    payload = email_sender.build_daily_payload(report)
    assert payload is not None
    assert "Paris" in payload.text_body and "Lux" in payload.html_body
