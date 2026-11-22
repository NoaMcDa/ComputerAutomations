"""Issue #6 (email content), #48 (failure reason), #11 (promo wording)."""
from __future__ import annotations

import smtplib
from datetime import datetime
from decimal import Decimal

import pytest

from core_helpers import FakeSMTP, cfg, fail, install_env, ok, promo, watch
from price_watcher import email_sender as es
from price_watcher import state
from price_watcher.contracts import ChangeEvent, ChangeType, FetchStatus


def price_event(old="499", new="449", name="Shoe", **kw):
    old, new = Decimal(old), Decimal(new)
    return ChangeEvent(
        type=ChangeType.PRICE_CHANGED, watch=watch(name=name), fetch=ok(str(new), **kw),
        old_price=old, new_price=new, diff=new - old,
        pct_change=float((new - old) / old * 100),
        timestamp=datetime(2026, 11, 12, 9, 0),
    )


# ---- #6 content ------------------------------------------------------------

def test_subject_readable_in_notification():
    p = es.build_payload([price_event()])
    assert p.subject == "המחיר ירד: Shoe – מ-499 ל-449 ₪ (-10.0%)"


def test_subject_keeps_agorot():
    p = es.build_payload([price_event("19.90", "19.50")])
    assert "מ-19.90 ל-19.50 ₪" in p.subject


def test_body_has_all_fields_text_and_html():
    # Merge note: prices in the body always carry two decimals (499.00 ₪).
    p = es.build_payload([price_event()])
    for needle in ("Shoe", "499.00 ₪", "449.00 ₪", "-50.00 ₪", "-10.0%", "ירד",
                   "https://shop.example/products/shoe", "2026-11-12 09:00"):
        assert needle in p.text_body, needle
        assert needle in p.html_body, needle
    assert p.html_body.startswith("<!DOCTYPE html>")


def test_rise_is_marked_as_rise():
    p = es.build_payload([price_event("100", "120")])
    assert p.subject.startswith("המחיר עלה")


def test_fmt_price():
    # Merge note: body prices keep two decimals (the subject stays compact).
    assert es._fmt_price(Decimal("499"), "₪") == "499.00 ₪"
    assert es._fmt_price(Decimal("1299.9"), "₪") == "1,299.90 ₪"
    assert es._fmt_price(Decimal("19.90"), "$") == "19.90 $"
    assert es._fmt_price(None, "₪") == "?"


def test_combined_email_for_several_changes():
    a = price_event(name="A")
    b = price_event("100", "80", name="B")
    no = ChangeEvent(type=ChangeType.NO_CHANGE, watch=watch(name="C"), fetch=ok("5"))
    p = es.build_payload([a, b, no])
    assert p.subject.startswith("מעקב מחירים: 2 שינויים")
    assert "-20.0%" in p.subject
    assert p.text_body.count("קישור:") == 2


def test_nothing_notifiable_means_no_payload():
    assert es.build_payload([ChangeEvent(type=ChangeType.NO_CHANGE, watch=watch(), fetch=ok("1"))]) is None


def test_message_is_plain_text_with_html_alternative(monkeypatch):
    install_env(monkeypatch)
    sent = []

    class FakeSMTPConn:
        def __init__(self, host, port, **kw):  # merge: the sender passes a timeout
            sent.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ehlo(self):
            pass

        def starttls(self):
            sent.append("starttls")

        def login(self, u, p):
            sent.append(("login", u))

        def send_message(self, msg):
            sent.append(msg)

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTPConn)
    assert es.send_change_notification([price_event()]) is True
    msg = sent[-1]
    assert "starttls" in sent
    assert msg.get_content_type() == "multipart/alternative"
    kinds = [part.get_content_type() for part in msg.iter_parts()]
    assert kinds == ["text/plain", "text/html"]


def test_send_failure_returns_false(monkeypatch):
    install_env(monkeypatch)
    smtp = FakeSMTP()
    smtp.fail = True
    monkeypatch.setattr(es, "_send_via_smtp", smtp)
    assert es.send_change_notification([price_event()]) is False


def test_missing_smtp_settings_returns_false_instead_of_raising(monkeypatch):
    for k in ("PW_SMTP_USER", "PW_SMTP_PASSWORD", "PW_EMAIL_FROM", "PW_EMAIL_TO"):
        monkeypatch.delenv(k, raising=False)
    assert es.send_change_notification([price_event()]) is False


def test_test_email_sends_sample(monkeypatch):
    install_env(monkeypatch)
    smtp = FakeSMTP()
    monkeypatch.setattr(es, "_send_via_smtp", smtp)
    es.send_test_email()
    assert len(smtp.sent) == 1 and "בדיקה" in smtp.sent[0].subject


# ---- #48 failure reason --------------------------------------------------

def _threshold_event(fetch_result):
    w = watch()
    c = cfg(threshold=1)
    state.apply_many([state.evaluate(w, ok("100"), c)])
    ev = state.evaluate(w, fetch_result, c)
    assert ev.type == ChangeType.THRESHOLD_FAILURE
    return ev


def test_threshold_alert_says_why_shopify_variant():
    ev = _threshold_event(fail(FetchStatus.NOT_FOUND, "Shopify variant 1 not found in product <x>"))
    p = es.build_payload([ev])
    for body in (p.text_body, p.html_body):
        assert "הווריאציה" in body                         # merge: says the variant is gone
        assert "remove" in body and "add" in body          # re-add suggestion
    assert "Shopify variant 1 not found in product <x>" in p.text_body
    assert "Shopify variant 1 not found in product &lt;x&gt;" in p.html_body   # escaped
    assert "<x>" not in p.html_body
    assert "הדף לא נמצא" in p.subject


def test_threshold_alert_blocked_includes_http_status():
    ev = _threshold_event(fail(FetchStatus.BLOCKED, "Blocked (HTTP 403)", 403))
    p = es.build_payload([ev])
    # merge: the detail line reads "<status> (HTTP 403) – <advice> (<message>)"
    assert "האתר חסם את הבקשה (HTTP 403)" in p.text_body
    assert "Blocked (HTTP 403)" in p.text_body
    assert "HTTP 403" in p.html_body


def test_threshold_alert_price_zero():
    ev = _threshold_event(ok("0"))
    p = es.build_payload([ev])
    # merge: a price of 0 is reported as such ("shown as 0")
    assert "המחיר באתר הוצג כ-0" in p.text_body and "המחיר הוצג כ-0" in p.subject


def test_describe_failure_404():
    info = es.describe_failure(fail(FetchStatus.NOT_FOUND, "404 Not Found", 404))
    assert info["status"] == "not_found" and info["http_status"] == 404
    assert "HTTP 404" in info["detail"] and info["hint"]


# ---- #11 promotion wording -------------------------------------------------

def _promo_run(first, second, w=None):
    w = w or watch()
    state.apply_many([state.evaluate(w, first, cfg())])
    return state.evaluate(w, second, cfg())


def test_estimate_wording_with_code():
    ev = _promo_run(
        ok("189"),
        ok("189", promo=promo("45", "X45", "on orders over ₪200"),
           estimated_final_price=Decimal("103.95")),
    )
    assert ev.type == ChangeType.PROMO_CHANGED
    p = es.build_payload([ev])
    assert "מבצע חדש" in p.subject and "X45" in p.subject
    # merge: the estimate is its own line, marked as an estimate
    assert "מחיר סופי משוער: 103.95 ₪ (הערכה)" in p.text_body and "X45" in p.text_body
    assert "on orders over ₪200" in p.text_body
    assert "המחיר המוצג לא השתנה" in p.text_body


def test_promotion_ended_is_reported_as_such_not_as_price_rise():
    ev = _promo_run(
        ok("189", promo=promo("20"), estimated_final_price=Decimal("151.20")),
        ok("189"),
    )
    p = es.build_payload([ev])
    assert p.subject.startswith("המבצע הסתיים")
    assert "עלה" not in p.subject
    assert "המבצע הסתיים (היה: קוד: VIP20" in p.text_body   # merge: ours' wording


def test_price_change_with_promo_states_what_changed():
    ev = _promo_run(
        ok("189", promo=promo("20"), estimated_final_price=Decimal("151.20")),
        ok("179", promo=promo("20"), estimated_final_price=Decimal("143.20")),
    )
    assert ev.type == ChangeType.PRICE_CHANGED
    p = es.build_payload([ev])
    assert "המחיר המוצג (המבצע לא השתנה)" in p.text_body
    assert "151.20 ₪ → 143.20 ₪" in p.text_body


def test_unreadable_banner_says_displayed_price_only():
    ev = _promo_run(ok("189"), ok("179", promo=promo("0", "SPRING"),
                                  estimated_final_price=Decimal("179")))
    p = es.build_payload([ev])
    assert "מוצג המחיר המוצג בלבד" in p.text_body


def test_excluded_item_note():
    w = watch(excluded_from_promo=True)
    ev = _promo_run(ok("189"), ok("150", promo=promo("20")), w=w)
    p = es.build_payload([ev])
    assert "מוחרג ממבצעי האתר" in p.text_body
    assert "VIP20" not in p.text_body


@pytest.mark.parametrize("etype", [ChangeType.STOCK_CHANGED, ChangeType.RECOVERED])
def test_other_event_types_render(etype):
    ev = ChangeEvent(type=etype, watch=watch(), fetch=ok("100", in_stock=True),
                     new_price=Decimal("100"))
    p = es.build_payload([ev])
    assert p is not None and "Shoe" in p.subject
