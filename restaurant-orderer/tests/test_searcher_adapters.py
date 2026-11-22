from datetime import date, time

import pytest

import searcher
from conftest import FakeResponse, FakeTransport, load_fixture

FRI = date(2026, 11, 13)


def ontopo(config, http):
    return searcher.OntopoAdapter(config.platforms["ontopo"], http, config.party_size)


def tabit(config, http):
    return searcher.TabitAdapter(config.platforms["tabit"], http, config.party_size)


# --- Ontopo ---------------------------------------------------------------

def test_ontopo_parse_fixture(config, make_http):
    a = ontopo(config, make_http(FakeTransport()))
    slots = a.parse(load_fixture("ontopo_availability.json"), FRI)
    got = {(s.time, s.area) for s in slots}
    assert got == {(time(18, 0), "Inside"), (time(19, 30), "Inside"), (time(20, 45), "Bar")}
    assert all(s.platform == "ontopo" and s.date == FRI for s in slots)
    s = next(s for s in slots if s.time == time(19, 30))
    assert s.deep_link == "https://ontopo.com/he/il/page/afrit-test?date=20261113&time=1930&size=2"


def test_ontopo_check_day_flow_filters_window_and_sends_headers(config, make_http):
    t = FakeTransport({
        ("POST", "/api/loginAnonymously"): FakeResponse(200, {"jwt_token": "JWT"}),
        ("POST", "/api/availability/searchAvailability"): FakeResponse(200, load_fixture("ontopo_availability.json")),
    })
    a = ontopo(config, make_http(t))
    r = a.check_day(FRI, config.window_for(FRI))
    assert r.status == searcher.OK
    # 18:00 is outside the 19:00-22:30 window; duplicates across query times are allowed here and
    # removed by ScanReport aggregation.
    assert {(s.time, s.area) for s in r.slots} == {(time(19, 30), "Inside"), (time(20, 45), "Bar")}
    search_calls = [c for c in t.calls if "searchAvailability" in c["url"]]
    assert len(search_calls) == len(config.time_window.query_times)
    first = search_calls[0]
    assert first["headers"]["token"] == "JWT"
    assert "Mozilla/5.0" in first["headers"]["User-Agent"]
    assert first["json"] == {"slug": "afrit-test", "locale": "he",
                             "criteria": {"size": "2", "date": "20261113", "time": "1930"}}


def test_ontopo_full_is_no_availability(config, make_http):
    t = FakeTransport({
        ("POST", "loginAnonymously"): FakeResponse(200, {"jwt_token": "JWT"}),
        ("POST", "searchAvailability"): FakeResponse(200, load_fixture("ontopo_full.json")),
    })
    r = ontopo(config, make_http(t)).check_day(FRI, config.time_window)
    assert r.status == searcher.NO_AVAILABILITY and r.slots == []


def test_ontopo_unknown_shape(config, make_http):
    t = FakeTransport({
        ("POST", "loginAnonymously"): FakeResponse(200, {}),
        ("POST", "searchAvailability"): FakeResponse(200, {"something": "else"}),
    })
    r = ontopo(config, make_http(t)).check_day(FRI, config.time_window)
    assert r.status == searcher.UNKNOWN_RESPONSE


def test_ontopo_captcha_page_is_blocked_and_stops(config, make_http):
    t = FakeTransport({
        ("POST", "loginAnonymously"): FakeResponse(200, {"jwt_token": "JWT"}),
        ("POST", "searchAvailability"): FakeResponse(200, None, text="<html>Attention Required! | Cloudflare captcha</html>"),
    })
    a = ontopo(config, make_http(t))
    r = a.check_day(FRI, config.time_window)
    assert r.status == searcher.BLOCKED and a.blocked
    assert len([c for c in t.calls if "searchAvailability" in c["url"]]) == 1


# --- Tabit ----------------------------------------------------------------

def test_tabit_parse_alternatives_converts_utc_to_local(config, make_http):
    a = tabit(config, make_http(FakeTransport()))
    slots = a.parse(load_fixture("tabit_alternatives.json"), FRI)
    # 17:15Z -> 19:15, 19:00Z -> 21:00, 21:45Z -> 23:45 (Israel winter time, UTC+2); other day dropped
    assert [(s.time, s.area) for s in sorted(slots)] == [
        (time(19, 15), "inside"), (time(21, 0), "bar"), (time(23, 45), "inside")]
    assert slots[0].deep_link.startswith("https://tabitisrael.co.il/online-reservations/create-reservation?")
    assert "orgId=org-test" in slots[0].deep_link and "date=2026-11-13" in slots[0].deep_link


def test_tabit_parse_area_keyed_alternatives(config, make_http):
    a = tabit(config, make_http(FakeTransport()))
    slots = a.parse({"alternative_results": {"patio": [{"time": "2026-11-13T18:30:00Z"}]}}, FRI)
    assert [(s.time, s.area) for s in slots] == [(time(20, 30), "patio")]


def test_tabit_check_day_exact_hold_is_released(config, make_http):
    t = FakeTransport({
        ("POST", "/rsv/booking/temp-reservations"): FakeResponse(201, load_fixture("tabit_reservation.json")),
        ("DELETE", "/rsv/booking/temp-reservations/tmp-hold-123"): FakeResponse(200, {"ok": True}),
    })
    r = tabit(config, make_http(t)).check_day(FRI, config.time_window)
    assert r.status == searcher.OK
    assert (time(19, 30), "outside") in {(s.time, s.area) for s in r.slots}
    posts = [c for c in t.calls if c["method"] == "POST"]
    assert posts[0]["json"]["reserved_from"] == "2026-11-13T17:30:00.000Z"
    assert posts[0]["json"]["seats_count"] == 2 and posts[0]["json"]["organization"] == "org-test"
    assert any(c["method"] == "DELETE" for c in t.calls)


def test_tabit_409_with_alternatives_is_parsed(config, make_http):
    t = FakeTransport({("POST", "temp-reservations"): FakeResponse(409, load_fixture("tabit_alternatives.json"))})
    r = tabit(config, make_http(t)).check_day(FRI, config.time_window)
    assert r.status == searcher.OK
    assert {s.time for s in r.slots} == {time(19, 15), time(21, 0)}  # 23:45 outside window


def test_tabit_409_without_alternatives_is_error(config, make_http):
    t = FakeTransport({("POST", "temp-reservations"): FakeResponse(409, {"message": "nope"})})
    r = tabit(config, make_http(t)).check_day(FRI, config.time_window)
    assert r.status == searcher.ERROR


# --- HTTP client ----------------------------------------------------------

def test_http_retries_5xx_then_succeeds(config, make_http):
    t = FakeTransport({("GET", "x"): [FakeResponse(503, None, "down"), FakeResponse(200, {"ok": 1})]})
    http = make_http(t, max_retries=2)
    res = http.request("GET", "https://example.invalid/x")
    assert res.kind == "ok" and len(t.calls) == 2


def test_http_429_exhausts_retries_to_blocked(make_http):
    t = FakeTransport({("GET", "x"): FakeResponse(429, None, "slow down", headers={"Retry-After": "7"})})
    http = make_http(t, max_retries=2)
    res = http.request("GET", "https://example.invalid/x")
    assert res.kind == "blocked" and len(t.calls) == 3
    assert 7.0 in http.sleeps


def test_http_network_error_never_raises(make_http):
    class Boom:
        def request(self, *a, **k):
            raise ConnectionError("no route")

    res = make_http(Boom(), max_retries=1).request("GET", "https://example.invalid/")
    assert res.kind == "network_error" and "no route" in res.message


def test_http_rate_limit_waits_between_requests():
    clock = [100.0]
    sleeps = []
    t = FakeTransport({("GET", "x"): FakeResponse(200, {})})
    http = searcher.HttpClient(searcher.HttpSettings(min_interval_seconds=3, jitter_seconds=0), transport=t,
                               sleep=sleeps.append, clock=lambda: clock[0])
    http.request("GET", "https://e.invalid/x")
    http.request("GET", "https://e.invalid/x")
    assert sleeps == [3]


# --- scan / aggregation ---------------------------------------------------

def test_scan_aggregates_and_dedupes(make_config, make_http):
    cfg = make_config(date_range={"start": "2026-11-13", "end": "2026-11-14"})
    t = FakeTransport({
        ("POST", "loginAnonymously"): FakeResponse(200, {"jwt_token": "JWT"}),
        ("POST", "searchAvailability"): FakeResponse(200, load_fixture("ontopo_availability.json")),
    })
    adapters = [searcher.OntopoAdapter(cfg.platforms["ontopo"], make_http(t), 2)]
    report = searcher.scan(cfg, adapters, today=date(2026, 11, 12))
    assert [r.date for r in report.results] == [date(2026, 11, 13), date(2026, 11, 14)]
    # 2 query times return the same 2 slots per day -> deduped to 2 per day
    assert len(report.slots) == 4
    assert report.slots == sorted(report.slots)


def test_scan_not_configured_and_blocked_skip(make_config, make_http, raw_config):
    raw_config["platforms"]["tabit"]["organization_id"] = ""
    cfg = make_config(platforms=raw_config["platforms"], date_range={"start": "2026-11-13", "end": "2026-11-21"})
    t = FakeTransport({
        ("POST", "loginAnonymously"): FakeResponse(200, {}),
        ("POST", "searchAvailability"): FakeResponse(403, None, "forbidden"),
    })
    http = make_http(t)
    adapters = searcher.build_adapters(cfg, http)
    assert [a.name for a in adapters] == ["ontopo", "tabit"]
    report = searcher.scan(cfg, adapters, today=date(2026, 11, 12))
    statuses = [(r.platform, r.status) for r in report.results]
    assert statuses[0] == ("ontopo", searcher.BLOCKED)
    assert all(s == searcher.SKIPPED for p, s in statuses[1:4])
    assert ("tabit", searcher.NOT_CONFIGURED) in statuses
    assert report.slots == []


def test_scan_survives_adapter_exception(config):
    class Broken(searcher.PlatformAdapter):
        name = "broken"

        def is_configured(self):
            return True

        def check_day(self, day, window):
            raise RuntimeError("bug")

    report = searcher.scan(config, [Broken({}, None, 2)], today=date(2026, 12, 26))
    assert [r.status for r in report.results] == [searcher.ERROR]


@pytest.mark.parametrize("bad", [None, [], "x", {"areas": "nope"}])
def test_ontopo_parse_rejects_bad_shapes(config, make_http, bad):
    with pytest.raises(searcher.UnknownShape):
        ontopo(config, make_http(FakeTransport())).parse(bad, FRI)
