"""Flight watches (#10) — pure logic with a fake AirlineSource. No network."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from price_watcher import flights as fl

D = Decimal
TODAY = date(2026, 11, 12)
NOW = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)
PINNED = date(2026, 12, 20)


def opt(num, dep, price, sold_out=False, arr="12:00"):
    return fl.FlightOption(num, dep, arr, None if price is None else D(str(price)), "ILS", sold_out)


class FakeSource(fl.AirlineSource):
    code = "elal"
    name = "Fake"

    def __init__(self, days=None, default=None, calendar=None, status_for=None):
        self.days = days or {}
        self.default = default  # price for every other date (None -> no flights)
        self.calendar = calendar
        self.status_for = status_for or {}
        self.calls: list[date] = []
        self.calendar_calls = 0

    def search_day(self, origin, destination, day, passengers, return_date=None):
        self.calls.append(day)
        if day in self.status_for:
            return fl.DayResult(day, self.status_for[day], error="fake problem")
        if day in self.days:
            return fl.DayResult(day, fl.OK, list(self.days[day]))
        if self.default is None:
            return fl.DayResult(day, fl.OK, [])
        return fl.DayResult(day, fl.OK, [opt("LY 9", "06:00", self.default)])

    def fare_calendar(self, origin, destination, start, end, passengers, return_date=None):
        self.calendar_calls += 1
        return self.calendar


def watch(**kw) -> fl.FlightWatch:
    base = dict(id="w1", airline="elal", origin="TLV", destination="LHR", pinned_date=PINNED,
                identity=fl.FlightIdentity("LY315", "10:15"))
    base.update(kw)
    return fl.FlightWatch(**base)


# --------------------------------------------------------------------------- window

def test_window_is_centred_31_days():
    ds = fl.window_dates(PINNED, TODAY)
    assert ds[0] == PINNED - timedelta(days=15) and ds[-1] == PINNED + timedelta(days=15)
    assert len(ds) == 31


def test_window_skips_past_dates_and_ends_after_pinned_date():
    pinned = TODAY + timedelta(days=3)
    ds = fl.window_dates(pinned, TODAY)
    assert ds[0] == TODAY and ds[-1] == pinned + timedelta(days=15)
    assert fl.window_dates(TODAY, TODAY)[0] == TODAY
    assert fl.window_dates(TODAY - timedelta(days=1), TODAY) == []
    assert fl.is_expired(watch(pinned_date=TODAY - timedelta(days=1)), TODAY)
    assert not fl.is_expired(watch(pinned_date=TODAY), TODAY)


# --------------------------------------------------------------------------- matching

@pytest.mark.parametrize("raw,norm", [
    ("LY 001", "LY1"), ("ly1", "LY1"), ("LY-0315", "LY315"), ("IZ 321", "IZ321"), ("6H 12", "6H12"),
    ("315", "315"), (None, None), ("", None),
])
def test_normalize_flight_number(raw, norm):
    assert fl.normalize_flight_number(raw) == norm


def test_match_by_number_first():
    options = [opt("LY 317", "10:15", 900), opt("LY 0315", "14:40", 1200)]
    m = fl.match_flight(fl.FlightIdentity("LY315", "10:15"), options)
    assert m.method == "flight_number" and m.option.price == D(1200)


def test_match_falls_back_to_departure_time_within_tolerance():
    options = [opt(None, "10:40", 800), opt(None, "18:00", 500)]
    m = fl.match_flight(fl.FlightIdentity(None, "10:15"), options, tolerance_minutes=60)
    assert m.method == "departure_time" and m.option.departure_time == "10:40"
    # picks the closest one
    m = fl.match_flight(fl.FlightIdentity(None, "10:15"), [opt(None, "09:30", 1), opt(None, "10:00", 2)])
    assert m.option.departure_time == "10:00"
    # across midnight
    m = fl.match_flight(fl.FlightIdentity(None, "23:50"), [opt(None, "00:20", 1)])
    assert m.method == "departure_time"


def test_no_match_is_not_listed():
    m = fl.match_flight(fl.FlightIdentity("LY315", "10:15"), [opt("LY 317", "15:00", 900)])
    assert m.option is None and m.method == fl.NOT_LISTED


# --------------------------------------------------------------------------- day states

def test_day_status_states():
    assert fl.DayResult(PINNED, fl.OK, []).day_status == fl.NO_FLIGHTS
    assert fl.DayResult(PINNED, fl.OK, [opt("LY1", "10:00", None, sold_out=True)]).day_status == fl.SOLD_OUT
    assert fl.DayResult(PINNED, fl.OK, [opt("LY1", "10:00", 5), opt("LY2", "11:00", 3)]).cheapest == D(3)
    assert fl.DayResult(PINNED, fl.BLOCKED).day_status == fl.BLOCKED


def test_snapshot_pinned_states():
    w = watch()
    assert fl.snapshot_pinned(w, fl.DayResult(PINNED, fl.OK, [])).status == fl.NO_FLIGHTS
    assert fl.snapshot_pinned(w, fl.DayResult(PINNED, fl.OK, [opt("LY315", "10:15", None, True)])).status == fl.SOLD_OUT
    assert fl.snapshot_pinned(w, fl.DayResult(PINNED, fl.OK, [opt("LY9", "20:00", 1)])).status == fl.NOT_LISTED
    assert fl.snapshot_pinned(w, fl.DayResult(PINNED, fl.BLOCKED, error="x")).status == fl.BLOCKED
    s = fl.snapshot_pinned(w, fl.DayResult(PINNED, fl.OK, [opt("LY315", "10:15", 1000)]))
    assert (s.status, s.price, s.method) == (fl.OK, D(1000), "flight_number")


# --------------------------------------------------------------------------- notification rules

def _pinned_day(price, num="LY315", dep="10:15"):
    return fl.DayResult(PINNED, fl.OK, [opt(num, dep, price), opt("LY317", "19:00", 5000)])


def _window(prices: dict[int, object]) -> dict[date, fl.DayResult]:
    out = {}
    for offset, price in prices.items():
        d = PINNED + timedelta(days=offset)
        out[d] = fl.DayResult(d, fl.OK, [] if price is None else [opt("LY9", "06:00", price)])
    return out


def test_first_run_is_baseline_only():
    ev = fl.evaluate(watch(), {}, _pinned_day(1000), _window({-3: 500, 2: 900}), TODAY, NOW)
    assert not ev.notify and ev.reasons == []
    assert ev.new_state["pinned"]["price"] == "1000"
    assert ev.new_state["window_cheapest"] == {"date": (PINNED - timedelta(days=3)).isoformat(), "price": "500"}
    assert ev.new_state["dates"][(PINNED + timedelta(days=2)).isoformat()]["price"] == "900"


def _baseline():
    return fl.evaluate(watch(), {}, _pinned_day(1000), _window({-3: 1100, 2: 1200}), TODAY, NOW).new_state


def test_pinned_price_change_over_threshold_notifies():
    st = _baseline()
    small = fl.evaluate(watch(), st, _pinned_day(1020), None, TODAY, NOW)   # ₪20 < ₪30 default
    assert not small.notify
    big = fl.evaluate(watch(), st, _pinned_day(940), None, TODAY, NOW)
    assert big.notify and any("ירד" in r and "₪1,000" in r and "₪940" in r for r in big.reasons)
    pct = fl.evaluate(watch(min_change_pct=D(10)), st, _pinned_day(940), None, TODAY, NOW)
    assert not pct.notify  # ₪60 < 10% of ₪1000


def test_pinned_not_listed_is_reported_not_as_price_change():
    st = _baseline()
    ev = fl.evaluate(watch(), st, fl.DayResult(PINNED, fl.OK, [opt("LY317", "19:00", 5000)]), None, TODAY, NOW)
    assert ev.notify and ev.reasons[0] == "הטיסה כבר לא מופיעה"
    assert not any("מחיר הטיסה" in r for r in ev.reasons)  # not a price change
    assert ev.new_state["pinned"]["status"] == fl.NOT_LISTED
    # and coming back is reported once, without a price-change line
    back = fl.evaluate(watch(), ev.new_state, _pinned_day(1000), None, TODAY, NOW)
    assert back.notify and "זמינה שוב" in back.reasons[0]
    assert not any("מחיר הטיסה" in r for r in back.reasons)


def test_sold_out_is_a_state():
    st = _baseline()
    ev = fl.evaluate(watch(), st, fl.DayResult(PINNED, fl.OK, [opt("LY315", "10:15", None, True)]),
                     None, TODAY, NOW)
    assert ev.pinned.status == fl.SOLD_OUT and ev.reasons[0] == "אזלו המקומות"
    assert not any("מחיר הטיסה" in r for r in ev.reasons)


def test_window_cheapest_date_change_notifies():
    st = _baseline()  # cheapest: pinned day 1000
    same = fl.evaluate(watch(), st, _pinned_day(1000), _window({-3: 1100, 2: 1200}), TODAY, NOW)
    assert not same.notify
    moved = fl.evaluate(watch(), st, _pinned_day(1000), _window({-3: 1100, 5: 990}), TODAY, NOW)
    assert moved.notify
    assert moved.cheapest == (PINNED + timedelta(days=5), D(990))
    assert any("התאריך הזול ביותר" in r for r in moved.reasons)
    assert [d for d, _ in moved.next_cheapest][:2] == [PINNED, PINNED - timedelta(days=3)]


def test_date_cheaper_than_pinned_notifies_once():
    st = _baseline()
    ev = fl.evaluate(watch(), st, _pinned_day(1000), _window({-3: 900, 2: 1200}), TODAY, NOW)
    assert ev.notify and ev.cheaper_than_pinned == [(PINNED - timedelta(days=3), D(900))]
    assert any("זולים יותר מהטיסה שלך" in r for r in ev.reasons)
    again = fl.evaluate(watch(), ev.new_state, _pinned_day(1000), _window({-3: 900, 2: 1200}), TODAY, NOW)
    assert not again.notify  # same cheaper date: no repeat email
    # within threshold of the pinned price does not count as "cheaper"
    near = fl.evaluate(watch(), st, _pinned_day(1000), _window({-3: 1080, 4: 985}), TODAY, NOW)
    assert near.cheaper_than_pinned == []


def test_window_keeps_last_known_price_on_fetch_problem_and_drops_past_dates():
    st = _baseline()
    w = {PINNED - timedelta(days=3): fl.DayResult(PINNED - timedelta(days=3), fl.BLOCKED)}
    ev = fl.evaluate(watch(), st, _pinned_day(1000), w, TODAY, NOW)
    assert ev.new_state["dates"][(PINNED - timedelta(days=3)).isoformat()]["price"] == "1100"
    later_today = PINNED - timedelta(days=1)
    ev2 = fl.evaluate(watch(), st, _pinned_day(1000), None, later_today, NOW)
    assert (PINNED - timedelta(days=3)).isoformat() not in ev2.new_state["dates"]


def test_no_flights_date_is_stored_as_state():
    st = _baseline()
    ev = fl.evaluate(watch(), st, _pinned_day(1000), _window({1: None}), TODAY, NOW)
    rec = ev.new_state["dates"][(PINNED + timedelta(days=1)).isoformat()]
    assert rec == {"status": fl.NO_FLIGHTS, "price": None, "checked_at": NOW.isoformat()}


def test_repeated_fetch_problems_alert_once():
    st = _baseline()
    for i in range(1, 5):
        ev = fl.evaluate(watch(), st, fl.DayResult(PINNED, fl.BLOCKED, error="captcha"), None, TODAY, NOW)
        assert ev.notify == (i == fl.FAILURE_ALERT_AFTER)
        assert ev.new_state["pinned"]["price"] == "1000"  # last good value kept
        st = ev.new_state


def test_expired_watch():
    ev = fl.evaluate(watch(pinned_date=TODAY - timedelta(days=1)), {}, None, None, TODAY, NOW)
    assert ev.expired and not ev.notify


# --------------------------------------------------------------------------- check_watch cadence

def test_check_watch_uses_calendar_when_available():
    cal = fl.CalendarResult(fl.OK, {PINNED + timedelta(days=1): D(700), PINNED + timedelta(days=2): None})
    src = FakeSource(days={PINNED: [opt("LY315", "10:15", 1000)]}, calendar=cal)
    ev = fl.check_watch(watch(), src, {}, TODAY, NOW)
    assert src.calls == [PINNED] and src.calendar_calls == 1
    assert ev.cheapest == (PINNED + timedelta(days=1), D(700))
    assert ev.new_state["dates"][(PINNED + timedelta(days=2)).isoformat()]["status"] == fl.NO_FLIGHTS


def test_check_watch_day_by_day_and_throttled():
    src = FakeSource(days={PINNED: [opt("LY315", "10:15", 1000)]}, default=1500)
    ev = fl.check_watch(watch(), src, {}, TODAY, NOW, window_interval_hours=6)
    assert len(src.calls) == 31 and len(ev.new_state["dates"]) == 31
    src.calls.clear()
    fl.check_watch(watch(), src, ev.new_state, TODAY, NOW + timedelta(hours=1), window_interval_hours=6)
    assert src.calls == [PINNED]  # only the pinned flight between window checks
    src.calls.clear()
    fl.check_watch(watch(), src, ev.new_state, TODAY, NOW + timedelta(hours=7), window_interval_hours=6)
    assert len(src.calls) == 31


def test_check_watch_stops_after_block_and_survives_exceptions():
    class Boom(FakeSource):
        def search_day(self, *a, **k):
            raise RuntimeError("selector timeout")
    ev = fl.check_watch(watch(), Boom(), {}, TODAY, NOW)
    assert ev.pinned.status == fl.ERROR and not ev.notify

    src = FakeSource(status_for={PINNED: fl.BLOCKED})
    fl.check_watch(watch(), src, {}, TODAY, NOW)
    assert src.calls == [PINNED]  # window not attempted when the pinned search was blocked


# --------------------------------------------------------------------------- add-flight + run

def test_add_flight_pick_from_list_and_persist():
    src = FakeSource(days={PINNED: [opt("LY 0317", "19:00", 900), opt("LY 315", "10:15", 1000)]})
    shown = []

    def choose(options):
        shown.extend(options)
        return 0  # earliest departure is listed first
    w = fl.add_flight(src, "tlv", "lhr", PINNED, choose, passengers=2,
                      return_date=PINNED + timedelta(days=7), today=TODAY)
    assert [o.departure_time for o in shown] == ["10:15", "19:00"]
    assert w.identity == fl.FlightIdentity("LY315", "10:15")
    assert (w.origin, w.destination, w.passengers, w.return_date) == ("TLV", "LHR", 2, PINNED + timedelta(days=7))
    assert [x.id for x in fl.load_watches()] == [w.id]
    assert fl.load_watches()[0] == w
    assert fl.remove_watch(w.id) and fl.load_watches() == []


def test_add_flight_errors():
    with pytest.raises(fl.FlightSearchError):
        fl.add_flight(FakeSource(), "TLV", "LHR", PINNED, lambda o: 0, today=TODAY)  # no flights
    with pytest.raises(fl.FlightSearchError):
        fl.add_flight(FakeSource(status_for={PINNED: fl.BLOCKED}), "TLV", "LHR", PINNED, lambda o: 0, today=TODAY)
    with pytest.raises(fl.FlightSearchError):
        fl.add_flight(FakeSource(), "TLV", "LHR", TODAY - timedelta(days=1), lambda o: 0, today=TODAY)
    src = FakeSource(days={PINNED: [opt("LY1", "10:00", 1)]})
    with pytest.raises(fl.FlightSearchError):
        fl.add_flight(src, "TLV", "LHR", PINNED, lambda o: 5, today=TODAY)
    assert fl.load_watches() == []


def test_run_flight_checks_one_email_per_watch_and_state_only_after_send():
    fl.save_watches([watch(), watch(id="w2", pinned_date=TODAY - timedelta(days=1))])
    src = FakeSource(days={PINNED: [opt("LY315", "10:15", 1000)]}, default=1500)
    sent = []
    rc = fl.run_flight_checks(source_for=lambda c: src, send=lambda p: sent.append(p) or True,
                              today=TODAY, now=NOW, echo=lambda s: None)
    assert rc == 0 and sent == []  # baseline
    assert [w.active for w in fl.load_watches()] == [True, False]  # expired watch stopped

    src.days[PINNED] = [opt("LY315", "10:15", 800)]
    src.days[PINNED + timedelta(days=4)] = [opt("LY9", "06:00", 600)]
    later = NOW + timedelta(hours=7)
    rc = fl.run_flight_checks(source_for=lambda c: src, send=lambda p: False, today=TODAY, now=later,
                              echo=lambda s: None)
    assert rc == 1
    assert fl.load_state()["watches"]["w1"]["pinned"]["price"] == "1000"  # not advanced: retried next run

    rc = fl.run_flight_checks(source_for=lambda c: src, send=lambda p: sent.append(p) or True,
                              today=TODAY, now=later, echo=lambda s: None)
    assert rc == 0 and len(sent) == 1  # several reasons, one combined email
    body = sent[0].text_body
    assert "₪1,000 → ₪800" in body and "₪600" in body
    assert fl.load_state()["watches"]["w1"]["pinned"]["price"] == "800"


def test_dry_run_writes_nothing():
    fl.save_watches([watch()])
    src = FakeSource(days={PINNED: [opt("LY315", "10:15", 1000)]})
    fl.run_flight_checks(dry_run=True, source_for=lambda c: src, send=lambda p: pytest.fail("sent"),
                         today=TODAY, now=NOW, echo=lambda s: None)
    assert not fl.state_file().exists()


def test_watch_roundtrip_and_malformed_file():
    w = watch(return_date=PINNED + timedelta(days=3), passengers=3)
    assert fl.FlightWatch.from_dict(json.loads(json.dumps(w.to_dict()))) == w
    fl.watches_file().write_text("{broken", encoding="utf-8")
    assert fl.load_watches() == []


# --------------------------------------------------------------------------- adapters (offline)

def test_adapters_registered_and_unknown_airline():
    assert isinstance(fl.get_source("ELAL"), fl.ElAlSource)
    assert isinstance(fl.get_source("arkia"), fl.ArkiaSource)
    with pytest.raises(ValueError):
        fl.get_source("wizz")


def test_adapter_interpret_blocked_unsupported_and_parsed():
    src = fl.ElAlSource()
    assert src.interpret(PINNED, "<html>Please complete the CAPTCHA</html>", [], 200).status == fl.BLOCKED
    assert src.interpret(PINNED, "<html></html>", [], 403).status == fl.BLOCKED
    unk = src.interpret(PINNED, "<html><div id=app></div></html>", [{"foo": 1}], 200)
    assert unk.status == fl.UNSUPPORTED and "unverified" in unk.error
    none = fl.ArkiaSource().interpret(PINNED, "<p>לא נמצאו טיסות</p>", [], 200)
    assert none.day_status == fl.NO_FLIGHTS
    payload = {"data": {"flights": [
        {"carrierCode": "LY", "flightNumber": "315", "departureDateTime": "2026-12-20T10:15:00",
         "arrivalDateTime": "2026-12-20T13:40:00", "fares": [], "totalPrice": {"amount": 1234.5, "currency": "ILS"}},
        {"flightNumber": "LY317", "departureTime": "19:00", "price": "₪2,000", "soldOut": True},
        {"carrierCode": "LY", "flightNumber": "315", "departureDateTime": "2026-12-21T10:15:00", "totalPrice": 1},
    ]}}
    res = src.interpret(PINNED, "<html></html>", [payload], 200)
    assert res.status == fl.OK
    by_num = {o.flight_number: o for o in res.options}
    assert set(by_num) == {"LY315", "LY317"}  # next-day flight filtered out
    assert by_num["LY315"].price == D("1234.5") and by_num["LY315"].arrival_time == "13:40"
    assert by_num["LY317"].sold_out


def test_email_payload_contents():
    st = _baseline()
    ev = fl.evaluate(watch(), st, _pinned_day(900), _window({-3: 800}), TODAY, NOW)
    p = fl.build_email(ev)
    assert "LY315" in p.subject and "TLV→LHR" in p.subject
    assert "₪900" in p.text_body and "₪800" in p.text_body and "כיוון אחד" in p.text_body
    assert 'dir="rtl"' in p.html_body


def test_adapter_without_playwright_is_unsupported(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    res = fl.ArkiaSource().search_day("TLV", "ETM", PINNED, 1)
    assert res.status == fl.UNSUPPORTED and "Playwright" in res.error


def test_add_flight_cli_pick_step(monkeypatch):
    from typer.testing import CliRunner

    from price_watcher.cli import app

    day = date.today() + timedelta(days=30)  # the CLI validates against the real clock
    src = FakeSource(days={day: [opt("LY 315", "10:15", 1000), opt("LY 317", "19:00", 900)]})
    monkeypatch.setattr(fl, "get_source", lambda code: src)
    result = CliRunner().invoke(app, ["add-flight", "--airline", "elal", "--from", "TLV", "--to", "LHR",
                                      "--date", day.isoformat()], input="2\n")
    assert result.exit_code == 0, result.output
    assert "19:00" in result.output and "Watching" in result.output
    assert fl.load_watches()[0].identity == fl.FlightIdentity("LY317", "19:00")
    listed = CliRunner().invoke(app, ["list-flights"])
    assert listed.exit_code == 0 and fl.load_watches()[0].id in listed.output

    bad = CliRunner().invoke(app, ["add-flight", "--airline", "elal", "--from", "TLV", "--to", "LHR",
                                   "--date", (day + timedelta(days=1)).isoformat()])
    assert bad.exit_code == 1 and "Could not add" in bad.output
