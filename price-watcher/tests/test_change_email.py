"""Change-notification email content (email_sender.build_payload); nothing is sent."""

from __future__ import annotations

from decimal import Decimal

from price_watcher import email_sender
from price_watcher.contracts import (
    ChangeEvent,
    ChangeType,
    FetchResult,
    FetchStatus,
    PromoInfo,
    Watch,
)

W = Watch(id="w1", url="https://x.example/p")


def fetch(price="100", currency="ILS", in_stock=True, promo=None, **kw) -> FetchResult:
    return FetchResult(
        status=FetchStatus.OK, final_url=W.url, price=Decimal(price) if price is not None else None,
        currency=currency, in_stock=in_stock, promo=promo, **kw,
    )


def price_event(old, new, currency="ILS", **kw) -> ChangeEvent:
    old, new = Decimal(old), Decimal(new)
    return ChangeEvent(
        type=ChangeType.PRICE_CHANGED, watch=W, fetch=fetch(str(new), currency=currency),
        old_price=old, new_price=new, diff=new - old,
        pct_change=float((new - old) / old * 100), **kw,
    )


def payload(ev):
    p = email_sender.build_payload([ev])
    assert p is not None
    return p


# --------------------------------------------------------------------- #34 §1

def test_subject_keeps_decimals_for_fractional_prices():
    p = payload(price_event("9.90", "9.50"))
    assert "9.90" in p.subject and "9.50" in p.subject
    assert "10 ל-10" not in p.subject


def test_subject_whole_prices_stay_compact():
    p = payload(price_event("100", "80"))
    assert "מ-100 ל-80 ₪" in p.subject


# --------------------------------------------------------------------- #34 §2

def test_unknown_currency_is_not_labelled_shekel():
    p = payload(price_event("100", "80", currency=None))
    for part in (p.subject, p.text_body, p.html_body):
        assert "₪" not in part


def test_text_and_html_use_the_same_currency_label():
    p = payload(price_event("1299.90", "1199.90"))
    assert "1,199.90 ₪" in p.text_body and "1,199.90 ₪" in p.html_body
    assert "ILS" not in p.text_body and "ILS" not in p.html_body


def test_fmt_price_formats_two_decimals_with_optional_symbol():
    assert email_sender._fmt_price(Decimal("100"), "₪") == "100.00 ₪"
    assert email_sender._fmt_price(Decimal("9.9"), "") == "9.90"
    assert email_sender._currency_symbol(None) == ""
    assert email_sender._currency_symbol("chf") == "CHF"


# --------------------------------------------------------------------- #34 §3

def test_promo_without_code_shows_percentage():
    ev = ChangeEvent(
        type=ChangeType.PROMO_CHANGED, watch=W,
        fetch=fetch(promo=PromoInfo(code=None, discount_pct=Decimal(25)), estimated_final_price=Decimal(75)),
        promo_changed=True,
    )
    p = payload(ev)
    assert "25%" in p.text_body and "25%" in p.html_body


def test_promo_removed_says_it_ended():
    ev = ChangeEvent(
        type=ChangeType.PROMO_CHANGED, watch=W, fetch=fetch(promo=None),
        promo_changed=True, old_promo=PromoInfo(code="X10", discount_pct=Decimal(10)),
    )
    p = payload(ev)
    assert p.subject.startswith("המבצע הסתיים")
    assert "המבצע הסתיים" in p.text_body and "X10" in p.text_body
    assert "המבצע הסתיים" in p.html_body
    assert "?" not in p.text_body  # no "estimated final: ?" line


# --------------------------------------------------------------------- #27

def test_stock_plus_promo_event_mentions_both_and_no_zero_percent():
    ev = ChangeEvent(
        type=ChangeType.STOCK_CHANGED, watch=W,
        fetch=fetch(in_stock=False, promo=PromoInfo(code="X10", discount_pct=Decimal(10))),
        stock_changed=True, promo_changed=True,
    )
    p = payload(ev)
    assert "0.0%" not in p.subject
    assert "אזל מהמלאי" in p.subject and "המבצע השתנה" in p.subject
    assert "X10" in p.text_body and "X10" in p.html_body


def test_price_event_with_stock_change_mentions_stock():
    ev = price_event("100", "80")
    ev.fetch.in_stock = False
    ev.stock_changed = True
    p = payload(ev)
    assert "אזל מהמלאי" in p.subject and "אזל מהמלאי" in p.text_body and "אזל מהמלאי" in p.html_body


def test_zero_pct_is_not_worded_as_rise():
    assert email_sender._direction_he(0.0) != email_sender._DIRECTION_HE["rose"]


# --------------------------------------------------------------------- #30 / #28

def test_recovered_email_includes_price_change():
    ev = ChangeEvent(
        type=ChangeType.RECOVERED, watch=W, fetch=fetch("60"),
        old_price=Decimal(100), new_price=Decimal(60), diff=Decimal(-40), pct_change=-40.0,
    )
    p = payload(ev)
    assert "חזר לעבוד" in p.subject and "ירד" in p.subject
    assert "100.00 ₪" in p.text_body and "60.00 ₪" in p.text_body
    assert "100.00 ₪" in p.html_body


def test_currency_change_email_shows_both_currencies():
    ev = ChangeEvent(
        type=ChangeType.PRICE_CHANGED, watch=W, fetch=fetch("30", currency="USD"),
        old_price=Decimal(100), new_price=Decimal(30), old_currency="ILS",
    )
    p = payload(ev)
    assert "100 ₪" in p.subject and "30 $" in p.subject
    assert "100.00 ₪" in p.text_body and "30.00 $" in p.text_body
    assert "המטבע השתנה" in p.text_body


def _threshold(fetch_result):
    return ChangeEvent(type=ChangeType.THRESHOLD_FAILURE, watch=W, fetch=fetch_result,
                       reason="האחזור נכשל 3 פעמים ברצף (הסף: 3)")


def test_threshold_alert_detail_per_failure_kind():  # #48
    cases = [
        (FetchResult(status=FetchStatus.BLOCKED, final_url=W.url, http_status=403), "חסם", "HTTP 403"),
        (FetchResult(status=FetchStatus.NOT_FOUND, final_url=W.url, http_status=404), "הדף לא נמצא", "HTTP 404"),
        (FetchResult(status=FetchStatus.NOT_FOUND, final_url=W.url,
                     error_message="Shopify variant 1 not found in product"), "להוסיף אותו מחדש", "variant 1"),
        (FetchResult(status=FetchStatus.OK, final_url=W.url, price=Decimal("0")), "כ-0", "כ-0"),
        (FetchResult(status=FetchStatus.UNREACHABLE, final_url=W.url, error_message="timed <out>"),
         "האתר לא זמין", "timed"),
    ]
    for fr, a, b in cases:
        p = payload(_threshold(fr))
        assert "פירוט:" in p.text_body and a in p.text_body and b in p.text_body
        assert ">פירוט<" in p.html_body and a in p.html_body
    p = payload(_threshold(cases[-1][0]))
    assert "timed &lt;out&gt;" in p.html_body and "<out>" not in p.html_body
