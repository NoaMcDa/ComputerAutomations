"""Email notifications for price / stock / promo changes and the daily report.

Public API
----------
send_change_notification(events)  -> bool
send_test_email()                 -> None   (raises on failure)
build_payload(events)             -> EmailPayload | None
describe_failure(fetch)           -> dict   (status / detail / hint, Hebrew)
build_daily_payload(report)       -> EmailPayload | None
send_daily_email(report)          -> bool
"""

from __future__ import annotations

import smtplib
from decimal import Decimal
from email.message import EmailMessage
from html import escape
from urllib.parse import urlparse

from price_watcher import config as _config_module
from price_watcher.contracts import (
    NOTIFIABLE_TYPES,
    ChangeEvent,
    ChangeType,
    EmailPayload,
    FetchResult,
    FetchStatus,
    PromoInfo,
)
from price_watcher.logging_setup import get as _get_logger

log = _get_logger(__name__)

# Change types that warrant an email (kept under the old private name).
_NOTIFIABLE = NOTIFIABLE_TYPES

# Well-known currency symbols.
_CURRENCY_SYMBOLS: dict[str, str] = {
    "ILS": "₪",
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _currency_symbol(code: str | None) -> str:
    if code is None:
        return "₪"
    return _CURRENCY_SYMBOLS.get(code.upper(), code.upper())


def _watch_label(event: ChangeEvent) -> str:
    """Return `watch.name` if set, else the URL hostname."""
    if event.watch.name:
        return event.watch.name
    return urlparse(event.watch.url).hostname or event.watch.url


def _truncate(s: str, max_len: int = 35) -> str:
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _num(price: Decimal | None) -> str:
    """Format a price: thousands separators, agorot only when non-zero.

    499 -> "499", 1299.9 -> "1,299.90", 19.90 -> "19.90".
    """
    if price is None:
        return "?"
    text = f"{price:,.2f}"
    return text[:-3] if text.endswith(".00") else text


def _fmt_price(price: Decimal | None, symbol: str) -> str:
    if price is None:
        return "?"
    return f"{_num(price)} {symbol}"


def _direction_word(pct: float | None, diff: Decimal | None = None) -> str:
    """Internal (English) direction key — see _DIRECTION_HE for display."""
    if diff is not None:
        if diff < 0:
            return "dropped"
        if diff > 0:
            return "rose"
    if pct is None:
        return "changed"
    return "dropped" if pct < 0 else "rose"


_DIRECTION_HE = {
    "dropped": "ירד", "rose": "עלה", "changed": "השתנה",
    # creatine: a different product took the lead at the identical price
    "same_price": "נשאר זהה (מוצר אחר)",
}

_STATUS_HE = {
    "ok": "תקין",
    "unreachable": "האתר לא זמין",
    "blocked": "האתר חסם את הבקשה",
    "not_found": "הדף לא נמצא (ייתכן שהמוצר הוסר)",
    "price_not_found": "המחיר לא נמצא בדף",
    "fetch_failed": "האחזור נכשל",
    "no_promo": "לא זוהה מבצע כלל-אתרי",
    "no_powder": "לא נמצאה אבקת קריאטין",
    "no_products": "לא נמצאו מוצרים בעמוד",
    "parse_failed": "פענוח העמוד נכשל",
    "unavailable": "לא זמין",
}

_SIGNATURE = "מעקב מחירים"


def _direction_he(pct: float | None, diff: Decimal | None = None) -> str:
    return _DIRECTION_HE[_direction_word(pct, diff)]


def _status_he(status: str | None) -> str:
    return _STATUS_HE.get(status or "unavailable", status or "")


def _stock_he(in_stock: bool | None) -> str:
    return "חזר למלאי" if in_stock else "אזל מהמלאי"


def _ltr(value: object) -> str:
    """Wrap numbers / prices / URLs so they keep their order inside RTL text."""
    return f'<span dir="ltr">{escape(str(value))}</span>'


# ---------------------------------------------------------------------------
# Failure description (threshold alert + last-run.json)
# ---------------------------------------------------------------------------

def describe_failure(fetch: FetchResult) -> dict:
    """Explain why a fetch failed, in Hebrew, with what the user can do.

    Returns {"status", "status_he", "http_status", "error_message",
    "detail", "hint"}; ``detail`` is one line combining the status, the
    HTTP code and the error message.
    """
    status = fetch.status.value if isinstance(fetch.status, FetchStatus) else str(fetch.status)
    msg = (fetch.error_message or "").strip()
    parts = [_status_he(status)]
    if fetch.http_status:
        parts.append(f"HTTP {fetch.http_status}")
    detail = " · ".join(parts)
    if msg:
        detail += f" — {msg}"

    low = msg.lower()
    if fetch.status == FetchStatus.NOT_FOUND and "variant" in low:
        hint = (
            "המידה / הצבע שבקישור כבר לא קיימים במוצר. יש להסיר את המעקב "
            "ולהוסיף אותו מחדש עם קישור לווריאנט קיים "
            "(price-watcher remove ואז price-watcher add)."
        )
    elif fetch.status == FetchStatus.NOT_FOUND:
        hint = (
            "ייתכן שהמוצר הוסר מהאתר או שהקישור השתנה. בדקו את הקישור בדפדפן; "
            "אם המוצר עבר לכתובת חדשה, הסירו את המעקב והוסיפו את הכתובת החדשה."
        )
    elif fetch.status == FetchStatus.BLOCKED:
        hint = (
            "האתר חוסם בקשות אוטומטיות (הגנה מבוטים). לרוב זה חולף מעצמו; "
            "אם זה נמשך ימים, נסו להוסיף את המעקב מחדש עם ‎--selector‎."
        )
    elif fetch.status == FetchStatus.PRICE_NOT_FOUND:
        hint = (
            "הדף נטען אבל המחיר לא זוהה (או שזוהה מחיר 0). אם עיצוב הדף השתנה, "
            "הוסיפו את המעקב מחדש עם ‎--selector‎ שמצביע על המחיר."
        )
    elif fetch.status == FetchStatus.UNREACHABLE:
        hint = "האתר לא ענה. בדקו את החיבור לאינטרנט ושהאתר עולה בדפדפן."
    else:
        hint = None
    return {
        "status": status,
        "status_he": _status_he(status),
        "http_status": fetch.http_status,
        "error_message": fetch.error_message,
        "detail": detail,
        "hint": hint,
    }


# ---------------------------------------------------------------------------
# Promotion (MyProtein banner) wording
# ---------------------------------------------------------------------------

def _promo_computable(promo: PromoInfo | None) -> bool:
    return promo is not None and promo.discount_pct > 0


def _promo_desc(promo: PromoInfo) -> str:
    pct = f"{_num(promo.discount_pct)}%"
    if promo.code:
        return f"הקוד {promo.code} ({pct})"
    return f"הנחה אוטומטית ({pct})"


def _promo_rows(event: ChangeEvent, sym: str) -> list[tuple[str, str]]:
    """(label, plain-text value) rows about the promotion / estimated price.

    The estimated final price is always marked as an estimate. A banner
    that could not be read never turns into a price rise: it is reported as
    "displayed price only".
    """
    fetch = event.fetch
    promo = fetch.promo
    rows: list[tuple[str, str]] = []
    if event.watch.excluded_from_promo:
        rows.append(("מבצעים", "הפריט מסומן כמוחרג ממבצעי האתר — מוצג המחיר המוצג בלבד"))
        return rows
    if not event.watch.track_promo:
        return rows

    old = event.old_promo if _promo_computable(event.old_promo) else None
    if promo is None and old is not None:
        rows.append(("מבצע", f"המבצע הסתיים (היה: {_promo_desc(old)})"))
        rows.append(("מחיר סופי משוער", f"{_fmt_price(fetch.price, sym)} (ללא מבצע)"))
        return rows
    if promo is None:
        return rows
    if not _promo_computable(promo):
        rows.append((
            "מבצע",
            "באנר המבצע לא נקרא במלואו או שהמבצע מדורג ('עד X%') — "
            "לא ניתן להעריך מחיר סופי; מוצג המחיר המוצג בלבד",
        ))
        if promo.source_text:
            rows.append(("טקסט הבאנר", promo.source_text[:200]))
        return rows

    est = fetch.estimated_final_price
    rows.append((
        "מחיר סופי משוער",
        f"מוצג {_fmt_price(fetch.price, sym)}, בערך {_fmt_price(est, sym)} "
        f"עם {_promo_desc(promo)} (הערכה)",
    ))
    if promo.condition:
        rows.append(("תנאי המבצע", promo.condition[:200]))
    if old is not None and (old.code != promo.code or old.discount_pct != promo.discount_pct):
        rows.append(("מבצע קודם", _promo_desc(old)))
    if (
        event.old_estimated_price is not None
        and est is not None
        and event.old_estimated_price != est
    ):
        rows.append((
            "מחיר משוער קודם",
            f"{_fmt_price(event.old_estimated_price, sym)} → {_fmt_price(est, sym)}",
        ))
    return rows


def _what_changed(event: ChangeEvent) -> str | None:
    """Say whether the displayed price, the promotion or both changed."""
    promo_changed = _promo_changed(event)
    if event.type == ChangeType.PROMO_CHANGED:
        return "המבצע (המחיר המוצג לא השתנה)"
    if event.type == ChangeType.PRICE_CHANGED and promo_changed:
        return "המחיר המוצג וגם המבצע"
    if event.type == ChangeType.PRICE_CHANGED and _promo_computable(event.fetch.promo):
        return "המחיר המוצג (המבצע לא השתנה)"
    return None


def _promo_changed(event: ChangeEvent) -> bool:
    if not event.watch.track_promo or event.watch.excluded_from_promo:
        return False
    old = event.old_promo if _promo_computable(event.old_promo) else None
    new = event.fetch.promo if _promo_computable(event.fetch.promo) else None
    if old is None and new is None:
        return False
    if old is None or new is None:
        return True
    return old.code != new.code or old.discount_pct != new.discount_pct


def _promo_heading(event: ChangeEvent) -> str:
    old = event.old_promo if _promo_computable(event.old_promo) else None
    if event.fetch.promo is None and old is not None:
        return "המבצע הסתיים"
    if old is None:
        return "מבצע חדש"
    return "המבצע השתנה"


# ---------------------------------------------------------------------------
# Subject builders
# ---------------------------------------------------------------------------

def _subject_single(event: ChangeEvent) -> str:
    label = _truncate(_watch_label(event))
    sym = _currency_symbol(event.fetch.currency)

    if event.type == ChangeType.PRICE_CHANGED:
        old = _num(event.old_price)
        new = _num(event.new_price)
        pct = f" ({event.pct_change:+.1f}%)" if event.pct_change is not None else ""
        return (
            f"המחיר {_direction_he(event.pct_change, event.diff)}: {label} – "
            f"מ-{old} ל-{new} {sym}{pct}"
        )

    if event.type == ChangeType.STOCK_CHANGED:
        return f"עדכון מלאי: {label} – {_stock_he(event.fetch.in_stock)}"

    if event.type == ChangeType.PROMO_CHANGED:
        promo = event.fetch.promo
        heading = _promo_heading(event)
        if _promo_computable(promo) and event.fetch.estimated_final_price is not None:
            return (
                f"{heading}: {label} – בערך {_num(event.fetch.estimated_final_price)} {sym} "
                f"עם {_promo_desc(promo)}"
            )
        return f"{heading}: {label}"

    if event.type == ChangeType.THRESHOLD_FAILURE:
        return f"המעקב הפסיק לעבוד: {label} – {_status_he(event.fetch.status.value)}"

    if event.type == ChangeType.RECOVERED:
        return f"המעקב חזר לעבוד: {label}"

    return f"עדכון ממעקב המחירים: {label}"


def _subject_multi(events: list[ChangeEvent]) -> str:
    n = len(events)
    # Find the event with the largest absolute % change.
    price_events = [e for e in events if e.type == ChangeType.PRICE_CHANGED and e.pct_change is not None]
    if price_events:
        biggest = max(price_events, key=lambda e: abs(e.pct_change))  # type: ignore[arg-type]
        label = _truncate(_watch_label(biggest), 25)
        pct = f"{biggest.pct_change:+.1f}%"
        return f"מעקב מחירים: {n} שינויים (הגדול ביותר: {pct} – {label})"
    return f"מעקב מחירים: {n} שינויים"


# ---------------------------------------------------------------------------
# Event content (shared by the text and HTML bodies)
# ---------------------------------------------------------------------------

def _event_content(event: ChangeEvent) -> tuple[str, str, list[tuple[str, str]]]:
    """Return (heading, heading colour, [(label, plain value), ...]).

    Prices go in their own rows so the HTML body can keep them LTR.
    """
    label = _watch_label(event)
    sym = _currency_symbol(event.fetch.currency)
    ts = event.timestamp.strftime("%Y-%m-%d %H:%M")
    rows: list[tuple[str, str]] = []
    color = ""

    if event.type == ChangeType.PRICE_CHANGED:
        direction = _direction_word(event.pct_change, event.diff)
        color = "#c00" if direction == "rose" else "#080"
        heading = f"המחיר {_DIRECTION_HE[direction]}: {label}"
        diff_str = ""
        if event.diff is not None:
            diff_str = f"{event.diff:+,.2f} {sym}"
            if event.pct_change is not None:
                diff_str += f", {event.pct_change:+.1f}%"
        rows += [
            ("מחיר קודם", _fmt_price(event.old_price, sym)),
            ("מחיר חדש", _fmt_price(event.new_price, sym)),
        ]
        if diff_str:
            rows.append(("שינוי", diff_str))
        if event.reason and "currency" in event.reason:
            rows.append(("הערה", f"המטבע השתנה ({event.reason})"))
        what = _what_changed(event)
        if what:
            rows.append(("מה השתנה", what))
        rows += _promo_rows(event, sym)

    elif event.type == ChangeType.STOCK_CHANGED:
        color = "#080" if event.fetch.in_stock else "#c00"
        heading = f"{_stock_he(event.fetch.in_stock)}: {label}"
        rows.append(("מחיר נוכחי", _fmt_price(event.new_price or event.fetch.price, sym)))
        rows += _promo_rows(event, sym)

    elif event.type == ChangeType.PROMO_CHANGED:
        heading = f"{_promo_heading(event)}: {label}"
        rows.append(("מחיר מוצג", _fmt_price(event.fetch.price, sym)))
        rows.append(("מה השתנה", _what_changed(event) or "המבצע"))
        rows += _promo_rows(event, sym)

    elif event.type == ChangeType.THRESHOLD_FAILURE:
        color = "#c00"
        heading = f"המעקב הפסיק לעבוד: {label}"
        info = describe_failure(event.fetch)
        rows.append(("סיבה", event.reason or "שגיאה לא ידועה"))
        rows.append(("פירוט", info["detail"]))
        if info["hint"]:
            rows.append(("מה לעשות", info["hint"]))
        rows.append(("הערה", "ניסיונות חוזרים יימשכו ברקע עד שהמעקב יתאושש; לא תישלח התראה נוספת עד אז."))

    elif event.type == ChangeType.RECOVERED:
        color = "#080"
        heading = f"המעקב חזר לעבוד: {label}"
        rows.append(("מחיר נוכחי", _fmt_price(event.new_price or event.fetch.price, sym)))
        if (
            event.old_price is not None
            and event.new_price is not None
            and event.old_price != event.new_price
        ):
            rows.append(("מחיר לפני התקלה", _fmt_price(event.old_price, sym)))
        rows += _promo_rows(event, sym)

    else:
        heading = f"עדכון: {label}"

    rows.append(("נבדק", ts))
    return heading, color, rows


_LTR_ROWS = {
    "מחיר קודם", "מחיר חדש", "שינוי", "מחיר נוכחי", "מחיר מוצג",
    "מחיר לפני התקלה", "נבדק", "מחיר משוער קודם",
}


# ---------------------------------------------------------------------------
# Body builders (plain text)
# ---------------------------------------------------------------------------

def _text_event(event: ChangeEvent) -> str:
    heading, _color, rows = _event_content(event)
    lines = [heading]
    lines += [f"  {key}: {val}" for key, val in rows]
    lines.append(f"  קישור: {event.watch.url}")
    return "\n".join(lines)


def _build_text_body(events: list[ChangeEvent]) -> str:
    sections = [_text_event(e) for e in events]
    return "\n\n---\n\n".join(sections) + f"\n\n-- {_SIGNATURE}"


# ---------------------------------------------------------------------------
# Body builders (HTML)
# ---------------------------------------------------------------------------

def _html_event(event: ChangeEvent) -> str:
    heading_text, color, rows = _event_content(event)
    url = escape(event.watch.url)
    link = f'<a href="{url}" dir="ltr">{url}</a>'

    def row(key: str, val_html: str) -> str:
        return (
            f'<tr><td style="padding:4px 8px;color:#555;white-space:nowrap;text-align:right">'
            f'<strong>{escape(key)}</strong></td>'
            f'<td style="padding:4px 8px;text-align:right">{val_html}</td></tr>'
        )

    html_rows: list[str] = []
    for key, val in rows:
        val_html = _ltr(val) if key in _LTR_ROWS else escape(val)
        if key == "מחיר חדש" and color:
            val_html = f'<strong style="color:{color}">{val_html}</strong>'
        html_rows.append(row(key, val_html))
    html_rows.append(row("קישור", link))

    heading = escape(heading_text)
    if color:
        heading = f'<span style="color:{color}">{heading}</span>'

    return (
        '<table dir="rtl" style="border-collapse:collapse;font-family:sans-serif;font-size:14px;'
        'width:100%;max-width:560px">'
        f'<tr><th colspan="2" style="text-align:right;padding:8px;background:#f4f4f4;'
        f'border-bottom:2px solid #ddd">{heading}</th></tr>'
        + "".join(html_rows)
        + "</table>"
    )


def _build_html_body(events: list[ChangeEvent]) -> str:
    tables = '\n<hr style="margin:20px 0;border:none;border-top:1px solid #ddd">\n'.join(
        _html_event(e) for e in events
    )
    return (
        '<!DOCTYPE html><html dir="rtl" lang="he"><head><meta charset="utf-8">'
        f"<title>{_SIGNATURE}</title></head>"
        '<body dir="rtl" style="margin:0;padding:16px;background:#fff;color:#222">'
        + tables
        + f'<p style="margin-top:24px;font-size:12px;color:#999">— {_SIGNATURE}</p>'
        "</body></html>"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_payload(events: list[ChangeEvent]) -> EmailPayload | None:
    """Build subject + text + HTML from a list of ChangeEvents.

    Returns None if there are no notifiable events after filtering.
    """
    notifiable = [e for e in events if e.type in _NOTIFIABLE]
    if not notifiable:
        return None

    if len(notifiable) == 1:
        subject = _subject_single(notifiable[0])
    else:
        subject = _subject_multi(notifiable)

    text_body = _build_text_body(notifiable)
    html_body = _build_html_body(notifiable)
    return EmailPayload(subject=subject, text_body=text_body, html_body=html_body)


def _send_via_smtp(payload: EmailPayload, cfg: _config_module.EmailConfig) -> None:
    """Low-level SMTP send. Raises on any failure."""
    msg = EmailMessage()
    msg["Subject"] = payload.subject
    msg["From"] = cfg.email_from
    msg["To"] = cfg.email_to
    msg.set_content(payload.text_body)
    msg.add_alternative(payload.html_body, subtype="html")

    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(cfg.smtp_user, cfg.smtp_password)
        smtp.send_message(msg)


def send_change_notification(events: list[ChangeEvent]) -> bool:
    """Send an email for all notifiable events.

    Returns True on success (or when there is nothing to send).
    Returns False on SMTP failure — the scheduler must NOT call mark_notified.
    Never raises.
    """
    payload = build_payload(events)
    if payload is None:
        log.debug("send_change_notification: no notifiable events, skipping email")
        return True

    try:
        cfg = _config_module.load().email
    except Exception as exc:  # missing / invalid SMTP settings in .env
        log.error("Cannot send notification email: %s", exc)
        return False
    try:
        log.info("Sending notification email: %s", payload.subject)
        _send_via_smtp(payload, cfg)
        log.info("Email sent successfully to %s", cfg.email_to)
        return True
    except Exception:
        log.exception(
            "Failed to send notification email (host=%s port=%s user=%s)",
            cfg.smtp_host,
            cfg.smtp_port,
            cfg.smtp_user,
        )
        return False


def send_test_email() -> None:
    """Send a test email to verify SMTP configuration.

    Raises on failure so the CLI can display the full error.
    """
    cfg = _config_module.load().email
    payload = EmailPayload(
        subject="מעקב מחירים: הודעת בדיקה",
        text_body=(
            "אם ההודעה הזו הגיעה אליך, הגדרות ה-SMTP תקינות.\n"
            "התראות על שינויי מחיר יגיעו באותה דרך."
        ),
        html_body=(
            '<!DOCTYPE html><html dir="rtl" lang="he"><head><meta charset="utf-8"></head>'
            '<body dir="rtl" style="font-family:sans-serif;padding:16px;text-align:right">'
            "<p>אם ההודעה הזו הגיעה אליך, הגדרות ה-SMTP תקינות.</p>"
            "<p>התראות על שינויי מחיר יגיעו באותה דרך.</p>"
            '<p style="color:#999;font-size:12px">— Price Watcher</p>'
            "</body></html>"
        ),
    )
    log.info("Sending test email to %s", cfg.email_to)
    _send_via_smtp(payload, cfg)
    log.info("Test email sent successfully")


# ---------------------------------------------------------------------------
# Daily report: site-wide promotions section
# ---------------------------------------------------------------------------

def _site_promos_section_text(site_digests) -> list[str]:
    """Plain-text lines for the "site-wide promotions" part of the daily email."""
    from price_watcher.digest import NO_PROMO_HE

    lines = ["מבצעים כלל-אתריים", "-" * 50, ""]
    if not site_digests:
        lines.append("  (אין אתרים ברשימת המעקב)")
        lines.append("")
    for d in site_digests:
        lines.append(f"▼ {d.display_name}  ({d.host})")
        lines.append(f"  {d.homepage_url}")
        if d.failed:
            lines.append(f"  [{_status_he(d.status)}] {d.error or ''}".rstrip())
            lines.append("")
            continue
        if not d.promos:
            lines.append(f"  ({NO_PROMO_HE})")
            lines.append("")
            continue
        for i, p in enumerate(d.promos, 1):
            code_part = f"קוד: {p.code}" if p.code else "ללא קוד"
            lines.append(f"  {i}. {p.discount_pct}% הנחה  —  {code_part}")
            if p.condition:
                lines.append(f"     {p.condition[:200]}")
        lines.append("")
    return lines


def _site_promos_section_html(site_digests) -> str:
    """HTML for the "site-wide promotions" part of the daily email."""
    from price_watcher.digest import NO_PROMO_HE

    parts: list[str] = []
    if not site_digests:
        parts.append('<div style="color:#888;font-style:italic">אין אתרים ברשימת המעקב.</div>')
    for d in site_digests:
        parts.append(
            f'<div style="margin:0 0 20px;padding:14px 16px;border:1px solid #e5e7eb;'
            f'border-radius:8px;background:#fafafa">'
            f'<div style="font-size:16px;font-weight:600">{escape(d.display_name)}'
            f' <a href="{escape(d.homepage_url)}" dir="ltr" style="font-weight:normal;color:#2563eb;'
            f'text-decoration:none;font-size:13px">({escape(d.host)})</a></div>'
        )
        if d.failed:
            parts.append(
                f'<div style="margin-top:8px;color:#c33">'
                f'<b>{escape(_status_he(d.status))}:</b> {escape(d.error or "—")}</div>'
            )
        elif not d.promos:
            parts.append(
                '<div style="margin-top:8px;color:#888;font-style:italic">'
                f'{NO_PROMO_HE}</div>'
            )
        else:
            for p in d.promos:
                code_html = (
                    f'<code dir="ltr" style="background:#111;color:#fff;padding:2px 8px;'
                    f'border-radius:4px;font-size:14px;font-weight:600">{escape(p.code)}</code>'
                    if p.code
                    else '<span style="color:#888;font-size:13px">(ללא קוד)</span>'
                )
                parts.append(
                    f'<div style="margin:10px 0 0;padding-top:10px;border-top:1px solid #eee">'
                    f'<div style="font-size:15px">'
                    f'<b style="color:#dc2626">{escape(str(p.discount_pct))}% הנחה</b>'
                    f'  &nbsp; {code_html}</div>'
                )
                if p.condition:
                    parts.append(
                        f'<div style="margin-top:4px;color:#555;font-size:13px">'
                        f'{escape(p.condition[:300])}</div>'
                    )
                parts.append('</div>')
        parts.append('</div>')
    return "".join(parts)


def _group_meat_by_category(sm) -> list[tuple[str, bool, list]]:
    """(category, ranked, rows) in the scanner's order: ranked cuts, then catch-alls."""
    from price_watcher.supermarkets import group_by_category
    return group_by_category(list(sm.promos or []))


# ---- Design tokens for email HTML ----
_COLOR_TEXT = "#111827"
_COLOR_MUTED = "#6b7280"
_COLOR_BORDER = "#e5e7eb"
_COLOR_BG_SOFT = "#f9fafb"
_COLOR_ACCENT = "#059669"       # success green (best deal)
_COLOR_DANGER = "#dc2626"       # discount red
_COLOR_LINK = "#2563eb"
_FONT_STACK = ("-apple-system,BlinkMacSystemFont,'Segoe UI',"
               "Roboto,Oxygen,Ubuntu,Helvetica,Arial,sans-serif")

# (keywords in the Hebrew category name, icon) — first match wins.
_CATEGORY_ICONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("הודו",), "🦃"),
    (("אווז", "ברווז"), "🦆"),
    (("עוף", "פרגיות", "פולקע"), "🍗"),
    (("טלה",), "🐑"),
    (("עגל",), "🐄"),
)


def _category_icon(cat_name: str) -> str:
    if cat_name == "אחר":
        return "•"
    for keywords, icon in _CATEGORY_ICONS:
        if any(kw in cat_name for kw in keywords):
            return icon
    return "🥩"


def _meat_store_label(p) -> str:
    """Store the deal is in, e.g. 'סוקולוב (הרצליה) ועוד 2 סניפים'."""
    names = list(getattr(p, "store_names", None) or [])
    first = names[0] if names else (getattr(p, "store_name", "") or "")
    city = getattr(p, "city", "") or ""
    if first and city and city not in first:
        label = f"{first} ({city})"
    else:
        label = first or city
    extra = int(getattr(p, "store_count", 1) or 1) - 1
    if extra == 1:
        label += " ועוד סניף אחד"
    elif extra > 1:
        label += f" ועוד {extra} סניפים"
    return label


def _meat_price_parts(p) -> tuple[str, str]:
    """(main price, secondary line): per kg when known, else per unit."""
    per_kg = getattr(p, "price_per_kg", None)
    promo = getattr(p, "promo_price", None)
    if per_kg is not None:
        main = f'₪{per_kg} לק"ג'
        if getattr(p, "weighted", False):
            regular = getattr(p, "regular_per_kg", None)
            second = f'במקום ₪{regular} לק"ג' if regular else ""
        else:
            second = f"₪{promo} לאריזה" + (f" (במקום ₪{p.regular_price})" if p.regular_price else "")
        return main, second
    if promo is not None:
        return f"₪{promo} ליחידה", (f"במקום ₪{p.regular_price}" if p.regular_price else "")
    return "—", ""


def _meat_tags(p) -> list[str]:
    """Fresh / frozen and the promo end date, e.g. ['קפוא', 'בתוקף עד 15/11/2026']."""
    from price_watcher.supermarkets import FRESHNESS_HE
    tags = []
    fresh = FRESHNESS_HE.get(getattr(p, "freshness", ""), "")
    if fresh:
        tags.append(fresh)
    until = getattr(p, "end_date_display", "")
    if until:
        tags.append(f"בתוקף עד {until}")
    return tags


def _shekel(value: object) -> str:
    return f'<span dir="ltr">₪{value}</span>'


def _usd(value: object) -> str:
    return f'<span dir="ltr">${value}</span>'


def _section_heading_html(title: str, first: bool = False) -> str:
    margin = "0 0 12px" if first else "28px 0 12px"
    return (
        f'<h3 style="margin:{margin};font-size:16px;color:{_COLOR_TEXT};text-align:right;'
        f'padding:6px 0 4px;border-bottom:2px solid {_COLOR_TEXT}">{title}</h3>'
    )


def _failure_box_html(title: str, status: str | None, error: str | None) -> str:
    return (
        f'<div style="margin:0 0 24px;padding:14px 16px;border:1px solid #fecaca;'
        f'border-radius:10px;background:#fef2f2;color:#991b1b;text-align:right;'
        f'font-family:{_FONT_STACK}">'
        f'<b>{escape(title)}:</b> {escape(_status_he(status))}'
        + (f' — {escape(error)}' if error else '')
        + '</div>'
    )


def _meat_scope_lines(sm) -> list[str]:
    cities = ", ".join(getattr(sm, "cities", None) or []) or "—"
    lines = [f"  אזור: {cities}"]
    scanned = list(sm.chains_scanned or [])
    lines.append(f"  רשתות שנסרקו ({len(scanned)}): {', '.join(scanned) if scanned else 'אין'}")
    without = list(getattr(sm, "chains_without_promos", None) or [])
    if without:
        lines.append(f"  נסרקו, ללא מבצעי בשר היום: {', '.join(without)}")
    no_stores = list(getattr(sm, "chains_no_stores", None) or [])
    if no_stores:
        lines.append(f"  אין סניפים באזור: {', '.join(no_stores)}")
    failed = dict(getattr(sm, "chains_failed", None) or {})
    if failed:
        lines.append("  הסריקה נכשלה: " + ", ".join(f"{c} ({why})" for c, why in failed.items()))
    lines.append(f"  סניפים שנסרקו: {len(getattr(sm, 'stores_scanned', None) or [])}")
    return lines


def _supermarkets_section_text(sm) -> list[str]:
    lines = ["מבצעי בשר בסופרמרקטים", "-" * 50, ""]
    if sm is None:
        lines.append("  (לא נסרק)")
        return lines
    if sm.status != "ok":
        lines.append(f"  הסריקה נכשלה: {_status_he(sm.status)}  {sm.error or ''}")
        return lines
    lines.extend(_meat_scope_lines(sm))
    if not sm.promos:
        lines.append("  לא נמצאו היום מבצעי בשר.")
        return lines
    top_n = getattr(sm, "top_per_category", 5)
    counts = getattr(sm, "promos_by_category", None) or {}
    lines.append(f'  בכל קטגוריה: {top_n} הזולים ביותר לפי מחיר מבצע לק"ג.')
    unranked_header_done = False
    for cat, ranked, items in _group_meat_by_category(sm):
        total = counts.get(cat, len(items))
        lines.append("")
        if not ranked and not unranked_header_done:
            lines.append("  ── קטגוריות כלליות (מוצרים שונים, לא מדורג) ──")
            unranked_header_done = True
        more = f" ({len(items)} מתוך {total})" if total > len(items) else ""
        lines.append(f"  ▼ {cat}{more}")
        for i, p in enumerate(items, 1):
            main, second = _meat_price_parts(p)
            bullet = f"{i}." if ranked else "•"
            lines.append(f"    {bullet} {main} · {p.item_name[:50]}")
            lines.append(f"       {p.chain} – {_meat_store_label(p)}")
            details = [d for d in (second, *_meat_tags(p)) if d]
            if details:
                lines.append(f"       {' · '.join(details)}")
            if p.promotion_description:
                lines.append(f"       {p.promotion_description[:90]}")
    return lines


def _supermarkets_section_html(sm) -> str:
    """Meat section: one card per category (one cut of one animal), cheapest first. RTL."""
    if sm is None:
        return '<div style="color:#888;font-style:italic;text-align:right">(לא נסרק)</div>'
    if sm.status != "ok":
        return _failure_box_html("סריקת הסופרמרקטים נכשלה", sm.status, sm.error)

    chains_scanned = list(sm.chains_scanned or [])
    stores_scanned = list(getattr(sm, "stores_scanned", None) or [])
    cities = list(getattr(sm, "cities", None) or [])
    counts = getattr(sm, "promos_by_category", None) or {}
    top_n = getattr(sm, "top_per_category", 5)
    without = list(getattr(sm, "chains_without_promos", None) or [])
    no_stores = list(getattr(sm, "chains_no_stores", None) or [])
    failed = dict(getattr(sm, "chains_failed", None) or {})
    small = f'font-size:12px;color:{_COLOR_MUTED};margin-top:4px'

    html = []
    html.append(
        f'<div style="padding:14px 16px;background:{_COLOR_BG_SOFT};'
        f'border:1px solid {_COLOR_BORDER};border-radius:10px;margin-bottom:18px;'
        f'font-family:{_FONT_STACK};color:{_COLOR_TEXT};text-align:right">'
        f'<div style="font-size:13px;color:{_COLOR_MUTED}">הסריקה של היום · '
        f'{escape(", ".join(cities)) or "—"}</div>'
        f'<div style="font-size:15px;margin-top:4px">'
        f'<b>{len(chains_scanned)}</b> רשתות · '
        f'<b>{len(stores_scanned)}</b> סניפים · '
        f'<b>{sum(counts.values()) or len(sm.promos)}</b> מבצעי בשר</div>'
        f'<div style="{small}">רשתות שנסרקו: '
        + (" · ".join(escape(c) for c in chains_scanned) or "אין") + '</div>'
        + (f'<div style="{small}">נסרקו, ללא מבצעי בשר היום: '
           + " · ".join(escape(c) for c in without) + '</div>' if without else '')
        + (f'<div style="{small}">אין סניפים באזור: '
           + " · ".join(escape(c) for c in no_stores) + '</div>' if no_stores else '')
        + (f'<div style="font-size:12px;color:{_COLOR_DANGER};margin-top:4px">הסריקה נכשלה: '
           + " · ".join(f"{escape(c)} ({escape(why)})" for c, why in failed.items())
           + '</div>' if failed else '')
        + f'<div style="{small}">בכל קטגוריה (נתח אחד של בעל חיים אחד) מוצגים '
        f'{top_n} המבצעים הזולים ביותר, לפי מחיר המבצע לק"ג.</div></div>'
    )

    if not sm.promos:
        html.append(
            '<div style="color:#888;font-style:italic;text-align:right;'
            f'font-family:{_FONT_STACK}">לא נמצאו היום מבצעי בשר.</div>'
        )
        return "".join(html)

    th_style = (
        f'padding:6px 10px;font-size:11px;font-weight:600;color:{_COLOR_MUTED};'
        f'letter-spacing:0.5px'
    )
    cell = f'border-top:1px solid {_COLOR_BORDER};vertical-align:top'
    unranked_header_done = False

    for cat, ranked, items in _group_meat_by_category(sm):
        if not ranked and not unranked_header_done:
            html.append(
                f'<div style="margin:24px 0 10px;font-family:{_FONT_STACK};text-align:right;'
                f'font-size:13px;color:{_COLOR_MUTED}"><b>קטגוריות כלליות</b> — '
                f'מוצרים שונים זה מזה, לכן הרשימה לא מדורגת לפי מחיר.</div>'
            )
            unranked_header_done = True
        rows_html = []
        for i, p in enumerate(items):
            is_best = ranked and i == 0
            row_bg = "#ecfdf5" if is_best else ("#ffffff" if i % 2 == 0 else _COLOR_BG_SOFT)
            main, second = _meat_price_parts(p)
            best_badge = (
                f'<div><span style="display:inline-block;background:{_COLOR_ACCENT};'
                f'color:#fff;font-size:10px;font-weight:700;padding:2px 6px;'
                f'border-radius:4px;margin-top:3px">הכי זול</span></div>'
                if is_best else ""
            )
            tag_html = "".join(
                f'<span style="display:inline-block;border:1px solid {_COLOR_BORDER};'
                f'border-radius:4px;padding:0 5px;margin-left:4px;font-size:11px;'
                f'color:{_COLOR_MUTED}">{escape(t)}</span>'
                for t in _meat_tags(p)
            )
            rows_html.append(
                f'<tr style="background:{row_bg}">'
                f'<td style="padding:8px 6px 8px 0;font-size:12px;color:{_COLOR_MUTED};'
                f'text-align:center;{cell}">{i + 1 if ranked else "•"}</td>'
                f'<td style="padding:8px 10px;font-size:12px;color:{_COLOR_LINK};'
                f'font-weight:600;{cell};text-align:right">{escape(p.chain)}'
                f'<div style="font-size:11px;font-weight:400;color:{_COLOR_MUTED};'
                f'margin-top:2px">{escape(_meat_store_label(p))}</div>'
                + best_badge
                + '</td>'
                f'<td style="padding:8px 10px;font-size:13px;color:{_COLOR_TEXT};'
                f'{cell};text-align:right">{escape(p.item_name[:60])}'
                + (f'<div style="margin-top:3px">{tag_html}</div>' if tag_html else '')
                + (f'<div style="font-size:11px;color:{_COLOR_MUTED};margin-top:2px">'
                   f'{escape(p.promotion_description[:70])}</div>'
                   if p.promotion_description else '')
                + '</td>'
                f'<td style="padding:8px 10px;font-size:13px;text-align:left;'
                f'white-space:nowrap;{cell}"><b style="color:{_COLOR_TEXT}" dir="rtl">'
                f'{escape(main)}</b>'
                + (f'<div style="font-size:11px;color:{_COLOR_MUTED};margin-top:2px" dir="rtl">'
                   f'{escape(second)}</div>' if second else '')
                + '</td></tr>'
            )

        total = counts.get(cat, len(items))
        if ranked:
            shown_txt = (f'{len(items)} הזולים ביותר מתוך {total}'
                         if total > len(items) else f'{len(items)} מבצעים')
        else:
            shown_txt = (f'{len(items)} מתוך {total} · לא מדורג'
                         if total > len(items) else f'{len(items)} מבצעים · לא מדורג')
        html.append(
            f'<div style="margin:0 0 20px;background:#ffffff;border:1px solid {_COLOR_BORDER};'
            f'border-radius:10px;overflow:hidden;font-family:{_FONT_STACK}">'
            f'<div style="padding:10px 14px;background:{_COLOR_BG_SOFT};'
            f'border-bottom:1px solid {_COLOR_BORDER};text-align:right">'
            f'<div style="font-size:15px;font-weight:700;color:{_COLOR_TEXT}">'
            f'{_category_icon(cat)} {escape(cat)}</div>'
            f'<div style="font-size:11px;color:{_COLOR_MUTED};margin-top:2px">{shown_txt}</div>'
            f'</div>'
            '<table dir="rtl" style="border-collapse:collapse;width:100%">'
            '<thead><tr>'
            f'<th style="{th_style};text-align:center">#</th>'
            f'<th style="{th_style};text-align:right">רשת וסניף</th>'
            f'<th style="{th_style};text-align:right">מוצר</th>'
            f'<th style="{th_style};text-align:left">מחיר מבצע</th>'
            '</tr></thead>'
            '<tbody>' + "".join(rows_html) + '</tbody>'
            '</table>'
            '</div>'
        )
    return "".join(html)


def _sims4_section_text(sims4) -> list[str]:
    lines = ["הסימס 4 (EA)", "-" * 50, ""]
    if sims4 is None:
        lines.append("  (לא נסרק)")
        return lines
    if sims4.status != "ok":
        lines.append(f"  הסריקה נכשלה: {_status_he(sims4.status)}  {sims4.error or ''}")
        return lines
    lines.append(f"  נסרקו {len(sims4.packs)} חבילות מהחנות של EA.")
    if sims4.newly_on_sale:
        lines.append("")
        lines.append(f"  חדש במבצע ({len(sims4.newly_on_sale)}):")
        for p in sims4.newly_on_sale:
            lines.append(f"    • {p.name} — {p.discount_percent}% הנחה, ${p.price} (במקום ${p.original_price})")
    if sims4.discount_ended:
        lines.append("")
        lines.append(f"  המבצע הסתיים ({len(sims4.discount_ended)}):")
        for p in sims4.discount_ended:
            lines.append(f"    • {p.name} — חזר ל-${p.price}")
    if sims4.discount_changed:
        lines.append("")
        lines.append("  אחוז ההנחה השתנה:")
        for p, old in sims4.discount_changed:
            lines.append(f"    • {p.name} — מ-{old}% ל-{p.discount_percent}%, ${p.price}")
    if sims4.on_sale_now:
        lines.append("")
        lines.append(f"  כל המבצעים הנוכחיים ({len(sims4.on_sale_now)}):")
        for p in sorted(sims4.on_sale_now, key=lambda x: -x.discount_percent):
            lines.append(f"    • {p.name} — {p.discount_percent}% הנחה, ${p.price} (במקום ${p.original_price})")
    elif not sims4.newly_on_sale and not sims4.discount_ended:
        lines.append("")
        lines.append("  אין כרגע חבילות במבצע.")
    return lines


def _sims4_section_html(sims4) -> str:
    if sims4 is None:
        return '<div style="color:#888;font-style:italic;text-align:right">(לא נסרק)</div>'
    if sims4.status != "ok":
        return _failure_box_html("סריקת הסימס 4 נכשלה", sims4.status, sims4.error)

    def _pack_link(p) -> str:
        return (
            f'<a href="{escape(p.url)}" dir="ltr" '
            f'style="color:#2563eb;text-decoration:none">{escape(p.name)}</a>'
        )

    html = ['<div style="margin:0 0 20px;text-align:right">']
    if sims4.newly_on_sale:
        html.append(
            '<div style="margin:0 0 12px;padding:12px 16px;background:#ecfdf5;'
            'border:1px solid #10b981;border-radius:8px">'
            f'<div style="font-weight:600;color:#065f46">🆕 חדש במבצע ({len(sims4.newly_on_sale)})</div>'
        )
        for p in sims4.newly_on_sale:
            html.append(
                f'<div style="margin-top:6px">{_pack_link(p)}  '
                f'<b style="color:#065f46">{p.discount_percent}% הנחה</b>  '
                f'<span style="color:#666">{_usd(p.price)} (במקום {_usd(p.original_price)})</span></div>'
            )
        html.append('</div>')
    if sims4.discount_ended:
        html.append(
            '<div style="margin:0 0 12px;padding:12px 16px;background:#fef2f2;'
            'border:1px solid #fca5a5;border-radius:8px">'
            f'<div style="font-weight:600;color:#991b1b">⚠ המבצע הסתיים ({len(sims4.discount_ended)})</div>'
        )
        for p in sims4.discount_ended:
            html.append(
                f'<div style="margin-top:6px">{_pack_link(p)}  '
                f'<span style="color:#991b1b">חזר ל-{_usd(p.price)}</span></div>'
            )
        html.append('</div>')
    if sims4.discount_changed:
        html.append(
            '<div style="margin:0 0 12px;padding:12px 16px;background:#eff6ff;'
            'border:1px solid #93c5fd;border-radius:8px">'
            '<div style="font-weight:600;color:#1e40af">🔁 אחוז ההנחה השתנה</div>'
        )
        for p, old in sims4.discount_changed:
            html.append(
                f'<div style="margin-top:6px">{_pack_link(p)}  '
                f'<span>מ-{old}% ל-<b>{p.discount_percent}%</b></span>  '
                f'<span style="color:#666">{_usd(p.price)}</span></div>'
            )
        html.append('</div>')
    if sims4.on_sale_now:
        rows = "".join(
            f'<tr><td style="padding:4px 10px;text-align:right">{_pack_link(p)}</td>'
            f'<td style="padding:4px 10px;text-align:right"><b>{p.discount_percent}% הנחה</b></td>'
            f'<td style="padding:4px 10px;text-align:left">{_usd(p.price)} '
            f'<s style="color:#888">{_usd(p.original_price)}</s></td></tr>'
            for p in sorted(sims4.on_sale_now, key=lambda x: -x.discount_percent)
        )
        html.append(
            f'<div style="margin:0;padding:12px 16px;background:#fafafa;'
            f'border:1px solid #e5e7eb;border-radius:8px">'
            f'<div style="font-weight:600;margin-bottom:6px">כל המבצעים הנוכחיים ({len(sims4.on_sale_now)})</div>'
            f'<table dir="rtl" style="border-collapse:collapse;font-size:13px;width:100%">{rows}</table>'
            f'</div>'
        )
    elif not sims4.newly_on_sale and not sims4.discount_ended:
        html.append(
            f'<div style="color:#888;font-style:italic">נסרקו {len(sims4.packs)} חבילות — '
            f'אין כרגע חבילות במבצע.</div>'
        )
    html.append('</div>')
    return "".join(html)


def _creatine_section_text(creatine) -> list[str]:
    """Plain-text lines for the creatine section of the daily report."""
    lines = ["קריאטין (iHerb)", "-" * 50, ""]
    if creatine is None or creatine.status != "ok":
        lines.append(f"  הסריקה נכשלה: {_status_he(creatine.status if creatine else None)}")
        if creatine and creatine.error:
            lines.append(f"  {creatine.error}")
        return lines
    cur = creatine.cheapest
    sym = _currency_symbol(cur.currency)
    lines.append(f"  הזול ביותר ל-100 גרם: {sym}{cur.price_per_100g}")
    lines.append(f"    {cur.name}")
    lines.append(f"    {sym}{cur.price}" + ("  (במבצע)" if cur.on_sale else ""))
    lines.append(f"    {cur.url}")
    lines.append("")
    if creatine.previous_cheapest is None:
        lines.append("  (תצפית ראשונה)")
    elif creatine.changed:
        prev = creatine.previous_cheapest
        word = _DIRECTION_HE.get(creatine.direction or "changed", "השתנה")
        lines.append(
            f"  שינוי: המחיר {word} מ-{sym}{prev.price_per_100g} ל-{sym}{cur.price_per_100g} ל-100 גרם"
        )
    else:
        lines.append("  ללא שינוי מאז הסריקה הקודמת.")
    lines.append("")
    lines.append("  5 הזולים ביותר ל-100 גרם:")
    for i, p in enumerate(creatine.products[:5], 1):
        lines.append(f"    {i}. {sym}{p.price_per_100g} ל-100 גרם — {p.name[:70]}")
    return lines


def _creatine_section_html(creatine) -> str:
    """HTML section for the creatine part of the daily report."""
    if creatine is None or creatine.status != "ok":
        return _failure_box_html(
            "סריקת הקריאטין נכשלה",
            creatine.status if creatine else None,
            creatine.error if creatine else None,
        )
    cur = creatine.cheapest
    sym = _currency_symbol(cur.currency)

    def _per100(value: object) -> str:
        return f'<span dir="ltr">{escape(sym)}{value}</span> ל-100 גרם'

    header_bg = "#ecfdf5"
    header_border = "#10b981"
    change_html = ""
    if creatine.previous_cheapest is None:
        change_html = '<div style="margin-top:6px;color:#065f46">(תצפית ראשונה)</div>'
    elif creatine.changed:
        prev = creatine.previous_cheapest
        arrow_color = "#065f46" if creatine.direction == "dropped" else "#991b1b"
        word = _DIRECTION_HE.get(creatine.direction or "changed", "השתנה")
        change_html = (
            f'<div style="margin-top:6px;color:{arrow_color}"><b>'
            f'המחיר {word}: מ-<span dir="ltr">{escape(sym)}{prev.price_per_100g}</span> '
            f'ל-{_per100(cur.price_per_100g)}</b></div>'
        )
        if creatine.direction == "rose":
            header_bg = "#fef2f2"
            header_border = "#fca5a5"
    else:
        change_html = '<div style="margin-top:6px;color:#6b7280">ללא שינוי מאז הסריקה הקודמת.</div>'

    rows_html = "".join(
        f'<tr><td style="padding:4px 10px;color:#666;text-align:right">{i}.</td>'
        f'<td style="padding:4px 10px;text-align:right;white-space:nowrap"><b>{_per100(p.price_per_100g)}</b></td>'
        f'<td style="padding:4px 10px;text-align:right"><a href="{escape(p.url)}" dir="auto" '
        f'style="color:#2563eb;text-decoration:none">{escape(p.name[:70])}</a></td></tr>'
        for i, p in enumerate(creatine.products[:5], 1)
    )

    return (
        f'<div style="margin:0 0 24px;padding:14px 16px;border:1px solid {header_border};'
        f'border-radius:8px;background:{header_bg};text-align:right">'
        f'<div style="font-size:16px;font-weight:600">קריאטין (iHerb) · הזול ביותר: '
        f'<span style="color:#065f46">{_per100(cur.price_per_100g)}</span></div>'
        f'<div style="margin-top:6px"><span dir="auto">{escape(cur.name)}</span>'
        + (' <span style="background:#dc2626;color:#fff;padding:1px 6px;border-radius:3px;font-size:11px">במבצע</span>' if cur.on_sale else '')
        + f'</div>'
        f'<a href="{escape(cur.url)}" style="color:#2563eb;font-size:13px">לפתיחה ב-iHerb</a>'
        f'{change_html}'
        f'<div style="margin-top:14px;font-size:13px;color:#555"><b>5 הזולים ביותר ל-100 גרם:</b>'
        f'<table dir="rtl" style="border-collapse:collapse;margin-top:4px;width:100%">{rows_html}</table></div>'
        f'</div>'
    )


def build_daily_payload(report) -> EmailPayload | None:
    """Combine sales digest + creatine + Sims 4 + supermarket meat into one email."""
    # Headline: biggest promo across sites, any creatine change, meat promo count.
    site_digests = report.site_digests
    all_promos = [(d, p) for d in site_digests for p in d.promos]
    best_promo_site, best_promo = (None, None)
    if all_promos:
        best_promo_site, best_promo = max(all_promos, key=lambda dp: dp[1].discount_pct)

    creatine_blurb = ""
    if report.creatine is not None and report.creatine.status == "ok" and report.creatine.changed:
        cur = report.creatine.cheapest
        word = _DIRECTION_HE.get(report.creatine.direction or "changed", "השתנה")
        creatine_blurb = (
            f"הקריאטין {word} ל-{_currency_symbol(cur.currency)}{cur.price_per_100g} ל-100 גרם"
        )

    sm = getattr(report, "supermarkets", None)
    meat_blurb = ""
    if sm is not None and sm.status == "ok" and sm.promos:
        best = max((int(p.savings_pct) for p in sm.promos if p.savings_pct is not None), default=0)
        meat_blurb = (
            f"מבצעי בשר ב-{len(sm.chains_scanned)} רשתות"
            + (f", עד {best}% הנחה" if best else "")
        )

    headline: list[str] = []
    if best_promo:
        headline.append(
            f"עד {best_promo.discount_pct}% הנחה ב-{best_promo_site.display_name}"
            + (f" ({best_promo.code})" if best_promo.code else "")
        )
    if creatine_blurb:
        headline.append(creatine_blurb)
    if meat_blurb:
        headline.append(meat_blurb)
    subject = "דוח יומי: " + (" · ".join(headline) if headline else "אין חדש היום")

    # ---- Plain text ----
    text_lines = ["דוח יומי", "=" * 50, ""]
    text_lines.extend(_site_promos_section_text(site_digests))
    text_lines.extend(_creatine_section_text(report.creatine))
    text_lines.append("")
    text_lines.extend(_sims4_section_text(getattr(report, "sims4", None)))
    text_lines.append("")
    text_lines.extend(_supermarkets_section_text(sm))
    text_lines.append("")
    from price_watcher import marketplace as _marketplace  # #54 hook
    text_lines.extend(_marketplace.section_text(getattr(report, "marketplace", None)))
    from price_watcher import travel_deals as _travel  # #52, #53 hook
    text_lines.extend(_travel.section_text(getattr(report, "travel", None)))
    text_lines.append(f"— {_SIGNATURE}")
    text_body = "\n".join(text_lines)

    # ---- HTML ----
    _F = _FONT_STACK
    html_parts = [
        '<!DOCTYPE html><html dir="rtl" lang="he"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '</head>',
        f'<body dir="rtl" style="margin:0;padding:0;background:#f3f4f6;font-family:{_F};color:{_COLOR_TEXT}">',
        f'<table role="presentation" dir="rtl" style="width:100%;background:#f3f4f6;border-collapse:collapse">',
        f'<tr><td style="padding:24px 12px">',
        f'<table role="presentation" dir="rtl" style="max-width:720px;margin:0 auto;width:100%;'
        f'background:#ffffff;border-radius:14px;border:1px solid {_COLOR_BORDER};'
        f'border-collapse:separate;overflow:hidden">',
        f'<tr><td style="padding:24px 24px 8px;text-align:right">',
        f'<div style="font-size:22px;font-weight:700;color:{_COLOR_TEXT};'
        f'margin-bottom:4px">דוח יומי</div>',
        f'<div style="font-size:13px;color:{_COLOR_MUTED}">מבצעים באתרים · קריאטין · הסימס 4 · בשר בסופרמרקטים</div>',
        f'</td></tr>',
        f'<tr><td style="padding:16px 24px;text-align:right">',
        _section_heading_html("מבצעים כלל-אתריים", first=True),
    ]
    html_parts.append(_site_promos_section_html(site_digests))

    html_parts.append(_section_heading_html("קריאטין"))
    html_parts.append(_creatine_section_html(report.creatine))

    html_parts.append(_section_heading_html("הסימס 4"))
    html_parts.append(_sims4_section_html(getattr(report, "sims4", None)))

    html_parts.append(_section_heading_html("מבצעי בשר בסופרמרקטים"))
    html_parts.append(_supermarkets_section_html(sm))
    html_parts.append(_marketplace.section_html(getattr(report, "marketplace", None)))  # #54 hook
    html_parts.append(_travel.section_html(getattr(report, "travel", None)))  # #52, #53 hook

    html_parts.append(
        f'</td></tr>'
        f'<tr><td style="padding:16px 24px 24px;border-top:1px solid {_COLOR_BORDER};'
        f'background:{_COLOR_BG_SOFT};color:{_COLOR_MUTED};font-size:11px;'
        f'text-align:center">— {_SIGNATURE} · דוח יומי</td></tr>'
        f'</table></td></tr></table></body></html>'
    )
    html_body = "".join(html_parts)

    return EmailPayload(subject=subject, text_body=text_body, html_body=html_body)


def send_daily_email(report) -> bool:
    """Send the combined daily report email."""
    payload = build_daily_payload(report)
    if payload is None:
        log.info("No daily report to send")
        return True
    try:
        cfg = _config_module.load().email
    except Exception as exc:  # missing / invalid SMTP settings in .env
        log.error("Cannot send the daily email: %s", exc)
        return False
    try:
        _send_via_smtp(payload, cfg)
        log.info("Daily email sent to %s", cfg.email_to)
        return True
    except (smtplib.SMTPException, OSError) as exc:
        log.error("Daily email send failed: %s: %s", type(exc).__name__, exc)
        return False
