from datetime import date, time
from email import message_from_bytes
from email.policy import default as default_policy

import pytest

import main
import notifier
import orderer
import searcher
from conftest import FakeResponse, FakeTransport, load_fixture

TODAY = date(2026, 11, 12)
SMTP = notifier.SmtpSettings("smtp.test", 587, "u@test", "pw", "u@test", ("me@test",))


def slot(d=date(2026, 11, 13), t=time(19, 30), area="Inside", platform="ontopo",
         link="https://ontopo.com/he/il/page/afrit-test?date=20261113&time=1930&size=2"):
    return searcher.Slot(d, t, area, platform, link)


def report_with(*slots):
    r = searcher.ScanReport("Afrit", 2)
    for s in slots:
        r.results.append(searcher.DayResult(s.platform, s.date, searcher.OK, [s]))
    return r


class Outbox:
    def __init__(self):
        self.messages = []

    def __call__(self, msg, smtp):
        self.messages.append(msg)


# --- notifier -------------------------------------------------------------

def test_no_slots_sends_nothing(config):
    box = Outbox()
    empty = searcher.ScanReport("Afrit", 2, [searcher.DayResult("ontopo", TODAY, searcher.NO_AVAILABILITY)])
    out = notifier.notify(empty, config, TODAY, smtp=SMTP, sender=box)
    assert out == notifier.NotifyOutcome(sent=False, reason="no_slots")
    assert box.messages == []


def test_suppress_empty_false_sends_empty_notice(make_config):
    cfg = make_config(email_notifications={"frequency": "daily", "suppress_empty": False})
    box = Outbox()
    out = notifier.notify(searcher.ScanReport("Afrit", 2), cfg, TODAY, smtp=SMTP, sender=box)
    assert out.sent and "no open slots" in box.messages[0]["Subject"]


def test_email_content_plain_and_html(config):
    box = Outbox()
    r = report_with(slot(), slot(t=time(20, 45), area="Bar & Counter"),
                    slot(d=date(2026, 11, 14), t=time(21, 0), area="patio", platform="tabit",
                         link="https://tabitisrael.co.il/x?orgId=org-test&date=2026-11-14"))
    out = notifier.notify(r, config, TODAY, smtp=SMTP, sender=box, notes=["note <1>"])
    assert out.sent and len(box.messages) == 1
    msg = message_from_bytes(box.messages[0].as_bytes(), policy=default_policy)
    assert msg["Subject"] == "[Restaurant Orderer] Afrit: 3 open slots on 2 days"
    assert msg["To"] == "me@test"
    plain = msg.get_body(("plain",)).get_content()
    htm = msg.get_body(("html",)).get_content()
    for needle in ("Afrit", "Fri 13.11.2026", "Sat 14.11.2026", "19:30", "20:45", "21:00", "Inside", "patio",
                   "https://ontopo.com/he/il/page/afrit-test?date=20261113&time=1930&size=2", "note <1>"):
        assert needle in plain
    assert "Bar &amp; Counter" in htm and "note &lt;1&gt;" in htm
    assert 'href="https://ontopo.com/he/il/page/afrit-test?date=20261113&amp;time=1930&amp;size=2"' in htm
    assert plain.index("Fri 13.11.2026") < plain.index("Sat 14.11.2026")


def test_dry_run_and_daily_guard(config):
    box = Outbox()
    r = report_with(slot())
    assert notifier.notify(r, config, TODAY, smtp=SMTP, sender=box, dry_run=True).reason == "dry_run"
    assert box.messages == []
    assert notifier.notify(r, config, TODAY, smtp=SMTP, sender=box).sent
    assert notifier.notify(r, config, TODAY, smtp=SMTP, sender=box).reason == "already_sent_today"
    assert notifier.notify(r, config, TODAY, smtp=SMTP, sender=box, force=True).sent
    assert notifier.notify(r, config, date(2026, 11, 13), smtp=SMTP, sender=box).sent
    assert len(box.messages) == 3


def test_missing_smtp_settings_do_not_crash(config):
    out = notifier.notify(report_with(slot()), config, TODAY,
                          smtp=notifier.SmtpSettings.from_env({}), sender=Outbox())
    assert out.reason == "smtp_not_configured" and not out.sent


def test_smtp_failure_is_reported(config):
    import smtplib

    def fail(msg, smtp):
        raise smtplib.SMTPAuthenticationError(535, b"bad creds")

    out = notifier.notify(report_with(slot()), config, TODAY, smtp=SMTP, sender=fail)
    assert not out.sent and out.reason.startswith("smtp_error")


def test_smtp_settings_from_env():
    s = notifier.SmtpSettings.from_env({"RO_SMTP_USER": "a@x", "RO_SMTP_PASSWORD": "p",
                                        "RO_EMAIL_TO": "b@x, c@x", "RO_SMTP_PORT": "465"})
    assert s.sender == "a@x" and s.recipients == ("b@x", "c@x") and s.port == 465 and s.missing() == []


# --- orderer --------------------------------------------------------------

class FakeAdapter(searcher.PlatformAdapter):
    name = "ontopo"

    def __init__(self, otp=False, card=False, fail=False):
        super().__init__({}, None, 2)
        self.otp, self.card, self.fail = otp, card, fail
        self.booked = []

    def is_configured(self):
        return True

    def booking_requirements(self):
        return searcher.BookingRequirements(self.otp, self.card)

    def book(self, slot, contact):
        if self.fail:
            raise RuntimeError("server said no")
        self.booked.append((slot, dict(contact)))
        return {"id": "CONF-1"}


CONTACT = {"name": "N", "phone": "050", "email": "e@x"}


def test_notify_only_never_books(config):
    a = FakeAdapter()
    out = orderer.process(config, report_with(slot()), [a], TODAY, contact=CONTACT)
    assert out.action == orderer.NOTIFY and a.booked == []


@pytest.mark.parametrize("otp,card,needle", [(True, False, "SMS one-time code"),
                                             (False, True, "credit-card guarantee"),
                                             (True, True, "and")])
def test_auto_order_falls_back_when_otp_or_card(make_config, otp, card, needle):
    cfg = make_config(mode="auto_order")
    a = FakeAdapter(otp=otp, card=card)
    out = orderer.process(cfg, report_with(slot()), [a], TODAY, contact=CONTACT)
    assert out.action == orderer.FALLBACK and a.booked == [] and needle in out.notes[0]


def test_real_adapters_declare_otp_so_auto_order_falls_back(make_config, make_http):
    cfg = make_config(mode="auto_order")
    transport = FakeTransport()  # any request would raise
    http = make_http(transport)
    adapters = searcher.build_adapters(cfg, http)
    for a in adapters:
        assert not a.booking_requirements().human_free
        out = orderer.process(cfg, report_with(slot(platform=a.name)), adapters, TODAY, contact=CONTACT)
        assert out.action == orderer.FALLBACK
    assert transport.calls == []


def test_auto_order_missing_contact_falls_back(make_config):
    out = orderer.process(make_config(mode="auto_order"), report_with(slot()), [FakeAdapter()], TODAY,
                          contact={"name": "N"})
    assert out.action == orderer.FALLBACK and "RO_CONTACT_PHONE" in out.notes[0]


def test_auto_order_dry_run_does_not_book(make_config):
    a = FakeAdapter()
    out = orderer.process(make_config(mode="auto_order"), report_with(slot()), [a], TODAY, contact=CONTACT, dry_run=True)
    assert out.action == orderer.WOULD_BOOK and a.booked == []


def test_auto_order_books_best_slot_once(make_config):
    cfg = make_config(mode="auto_order")
    a = FakeAdapter()
    later = slot(d=date(2026, 11, 14))
    early_late_time = slot(t=time(21, 0))
    best = slot(t=time(19, 30))
    out = orderer.process(cfg, report_with(later, early_late_time, best), [a], TODAY, contact=CONTACT)
    assert out.action == orderer.BOOKED and out.slot == best and len(a.booked) == 1
    again = orderer.process(cfg, report_with(best), [a], TODAY, contact=CONTACT)
    assert again.action == orderer.NOTIFY and len(a.booked) == 1 and "already" in again.notes[0]


def test_auto_order_booking_error_falls_back(make_config):
    out = orderer.process(make_config(mode="auto_order"), report_with(slot()), [FakeAdapter(fail=True)], TODAY,
                          contact=CONTACT)
    assert out.action == orderer.FALLBACK and "server said no" in out.notes[0]


def test_base_adapter_book_not_implemented_falls_back(make_config):
    class NoBook(FakeAdapter):
        book = searcher.PlatformAdapter.book

    out = orderer.process(make_config(mode="auto_order"), report_with(slot()), [NoBook()], TODAY, contact=CONTACT)
    assert out.action == orderer.FALLBACK


# --- end to end (main.run) --------------------------------------------------

def _write_cfg(tmp_path, raw_config, **over):
    import json

    raw = dict(raw_config, state_dir=str(tmp_path / "st"), **over)
    p = tmp_path / "config.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    return p


def test_main_run_emails_only_when_slots(tmp_path, raw_config, make_http, monkeypatch):
    path = _write_cfg(tmp_path, raw_config, date_range={"start": "2026-11-13", "end": "2026-11-14"})
    cfg = main.load_config(path)

    def run_with(fixture):
        t = FakeTransport({
            ("POST", "loginAnonymously"): FakeResponse(200, {"jwt_token": "J"}),
            ("POST", "searchAvailability"): FakeResponse(200, load_fixture(fixture)),
        })
        box = Outbox()
        adapters = [searcher.OntopoAdapter(cfg.platforms["ontopo"], make_http(t), 2)]
        rc = main.run(["--config", str(path), "--env", str(tmp_path / "none.env"), "--today", "2026-11-12"],
                      adapters=adapters, smtp=SMTP, sender=box)
        return rc, box

    rc, box = run_with("ontopo_full.json")
    assert rc == 0 and box.messages == []
    rc, box = run_with("ontopo_availability.json")
    assert rc == 0 and len(box.messages) == 1 and "Afrit" in box.messages[0]["Subject"]


def test_main_run_unconfigured_venues_exit_clean_without_email(tmp_path, raw_config):
    raw_config["platforms"]["ontopo"]["venue_slug"] = ""
    raw_config["platforms"]["tabit"]["organization_id"] = ""
    path = _write_cfg(tmp_path, raw_config)
    box = Outbox()
    rc = main.run(["--config", str(path), "--env", str(tmp_path / "x"), "--today", "2026-11-12"], smtp=SMTP, sender=box)
    assert rc == 0 and box.messages == []


def test_main_run_bad_config_returns_2(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{not json", encoding="utf-8")
    assert main.run(["--config", str(p), "--env", str(tmp_path / "x")]) == 2


def test_main_run_after_range_is_noop(tmp_path, raw_config):
    path = _write_cfg(tmp_path, raw_config)
    assert main.run(["--config", str(path), "--env", str(tmp_path / "x"), "--today", "2027-01-01"]) == 0
