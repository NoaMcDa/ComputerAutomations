"""Email notifications for price / stock / promo changes.

Public API
----------
send_change_notification(events)  -> bool
send_test_email()                 -> None   (raises on failure)
build_payload(events)             -> EmailPayload | None
"""

from __future__ import annotations

import smtplib
from decimal import Decimal
from email.message import EmailMessage
from html import escape
from urllib.parse import urlparse

from price_watcher import config as _config_module
from price_watcher.contracts import ChangeEvent, ChangeType, EmailPayload, PromoInfo
from price_watcher.logging_setup import get as _get_logger

log = _get_logger(__name__)

# Change types that warrant an email.
_NOTIFIABLE = {
    ChangeType.PRICE_CHANGED,
    ChangeType.STOCK_CHANGED,
    ChangeType.PROMO_CHANGED,
    ChangeType.THRESHOLD_FAILURE,
    ChangeType.RECOVERED,
}

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
    """Symbol for an ISO code; unknown codes are shown as-is, a missing
    currency as nothing (never guessed as ₪)."""
    if not code:
        return ""
    return _CURRENCY_SYMBOLS.get(code.upper(), code.upper())


def _with_sym(amount: str, symbol: str) -> str:
    return f"{amount} {symbol}" if symbol else amount


def _watch_label(event: ChangeEvent) -> str:
    """Return `watch.name` if set, else the URL hostname."""
    if event.watch.name:
        return event.watch.name
    return urlparse(event.watch.url).hostname or event.watch.url


def _truncate(s: str, max_len: int = 35) -> str:
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _fmt_price(price: Decimal | None, symbol: str) -> str:
    if price is None:
        return "?"
    return _with_sym(f"{price:,.2f}", symbol)


def _subject_amounts(*prices: Decimal | None) -> list[str]:
    """Amounts for a subject line: whole numbers when every price is whole,
    otherwise two decimals for all (so 9.90 → 9.50 never reads "10 → 10")."""
    known = [p for p in prices if p is not None]
    decimals = 0 if all(p == p.to_integral_value() for p in known) else 2
    return [f"{p:,.{decimals}f}" if p is not None else "?" for p in prices]


def _direction_word(pct: float | None) -> str:
    """Internal (English) direction key — see _DIRECTION_HE for display."""
    if pct is None:
        return "changed"
    if pct == 0:
        return "unchanged"
    return "dropped" if pct < 0 else "rose"


_DIRECTION_HE = {"dropped": "ירד", "rose": "עלה", "changed": "השתנה", "unchanged": "לא השתנה"}

_STATUS_HE = {
    "unreachable": "האתר לא זמין",
    "blocked": "האתר חסם את הבקשה",
    "fetch_failed": "האחזור נכשל",
    "no_powder": "לא נמצאה אבקת קריאטין",
    "unavailable": "לא זמין",
}

_SIGNATURE = "מעקב מחירים"


def _direction_he(pct: float | None) -> str:
    return _DIRECTION_HE[_direction_word(pct)]


def _status_he(status: str | None) -> str:
    return _STATUS_HE.get(status or "unavailable", status or "")


def _stock_he(in_stock: bool | None) -> str:
    return "חזר למלאי" if in_stock else "אזל מהמלאי"


def _ltr(value: object) -> str:
    """Wrap numbers / prices / URLs so they keep their order inside RTL text."""
    return f'<span dir="ltr">{escape(str(value))}</span>'


def _promo_word(event: ChangeEvent) -> str:
    """Heading for a promo change: ended vs. changed."""
    return "המבצע הסתיים" if event.fetch.promo is None else "המבצע השתנה"


def _promo_details(promo: PromoInfo) -> str:
    """Code + percentage; the percentage is shown even without a code."""
    if promo.code:
        return f"קוד: {promo.code}  ({promo.discount_pct}% הנחה)"
    return f"הנחה: {promo.discount_pct}%"


def _old_symbol(event: ChangeEvent, sym: str) -> str:
    return _currency_symbol(event.old_currency) if event.old_currency else sym


def _price_diff_str(event: ChangeEvent, sym: str) -> str:
    if event.diff is None or event.pct_change is None:
        return ""
    return f"{_with_sym(f'{event.diff:+,.2f}', sym)}, {event.pct_change:+.1f}%"


def _currency_note(event: ChangeEvent) -> str:
    return f"המטבע השתנה מ-{event.old_currency} ל-{event.fetch.currency}"


# ---------------------------------------------------------------------------
# Subject builders
# ---------------------------------------------------------------------------

def _subject_single(event: ChangeEvent) -> str:
    label = _truncate(_watch_label(event))
    sym = _currency_symbol(event.fetch.currency)

    def price_part() -> str:
        old, new = _subject_amounts(event.old_price, event.new_price)
        if event.old_currency:
            return (
                f"מ-{_with_sym(old, _old_symbol(event, sym))} ל-{_with_sym(new, sym)}"
                " (המטבע השתנה)"
            )
        pct = f" ({event.pct_change:+.1f}%)" if event.pct_change is not None else ""
        return f"מ-{old} ל-{_with_sym(new, sym)}{pct}"

    # Secondary changes carried by the same event.
    extras = ""
    if event.stock_changed and event.type != ChangeType.STOCK_CHANGED:
        extras += f" · {_stock_he(event.fetch.in_stock)}"
    if event.promo_changed and event.type != ChangeType.PROMO_CHANGED:
        extras += f" · {_promo_word(event)}"

    if event.type == ChangeType.PRICE_CHANGED:
        return f"המחיר {_direction_he(event.pct_change)}: {label} – {price_part()}{extras}"

    if event.type == ChangeType.STOCK_CHANGED:
        return f"עדכון מלאי: {label} – {_stock_he(event.fetch.in_stock)}{extras}"

    if event.type == ChangeType.PROMO_CHANGED:
        return f"{_promo_word(event)}: {label}"

    if event.type == ChangeType.THRESHOLD_FAILURE:
        return f"המעקב הפסיק לעבוד: {label}"

    if event.type == ChangeType.RECOVERED:
        if event.old_price is not None or event.new_price is not None:
            extras = f" – המחיר {_direction_he(event.pct_change)} {price_part()}" + extras
        return f"המעקב חזר לעבוד: {label}{extras}"

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
# Body builders (plain text)
# ---------------------------------------------------------------------------

def _text_event(event: ChangeEvent) -> str:
    label = _watch_label(event)
    sym = _currency_symbol(event.fetch.currency)
    url = event.watch.url
    ts = event.timestamp.strftime("%Y-%m-%d %H:%M")
    lines: list[str] = []

    def price_lines() -> list[str]:
        old = _fmt_price(event.old_price, _old_symbol(event, sym))
        new = _fmt_price(event.new_price, sym)
        diff_str = _price_diff_str(event, sym)
        out = [
            f"  מחיר קודם: {old}",
            f"  מחיר חדש: {new}" + (f"  ({diff_str})" if diff_str else ""),
        ]
        if event.old_currency:
            out.append(f"  {_currency_note(event)}")
        return out

    def promo_lines() -> list[str]:
        promo = event.fetch.promo
        if promo is None:
            line = "  המבצע הסתיים"
            if event.old_promo is not None:
                line += f" (היה: {_promo_details(event.old_promo)})"
            return [line]
        out = [f"  {_promo_details(promo)}"]
        if promo.condition:
            out.append(f"  תנאים: {promo.condition}")
        return out

    def extra_lines() -> list[str]:
        out: list[str] = []
        if event.stock_changed and event.type != ChangeType.STOCK_CHANGED:
            out.append(f"  מלאי: {_stock_he(event.fetch.in_stock)}")
        if event.promo_changed and event.type != ChangeType.PROMO_CHANGED:
            out += promo_lines()
        return out

    if event.type == ChangeType.PRICE_CHANGED:
        lines += [f"המחיר {_direction_he(event.pct_change)}: {label}"]
        lines += price_lines()
        lines += extra_lines()
        lines += [
            f"  קישור: {url}",
            f"  נבדק: {ts}",
        ]

    elif event.type == ChangeType.STOCK_CHANGED:
        price_str = _fmt_price(event.new_price or event.fetch.price, sym)
        lines += [
            f"עדכון מלאי: {_stock_he(event.fetch.in_stock)}",
            f"  מוצר: {label}",
            f"  מחיר נוכחי: {price_str}",
        ]
        lines += extra_lines()
        lines += [f"  קישור: {url}"]

    elif event.type == ChangeType.PROMO_CHANGED:
        displayed = _fmt_price(event.fetch.price, sym)
        lines += [f"{_promo_word(event)}: {label}"]
        lines += promo_lines()
        lines.append(f"  מחיר מוצג: {displayed}")
        if event.fetch.estimated_final_price is not None:
            estimated = _fmt_price(event.fetch.estimated_final_price, sym)
            lines.append(f"  מחיר סופי משוער: {estimated}")
        lines += [f"  קישור: {url}"]

    elif event.type == ChangeType.THRESHOLD_FAILURE:
        reason = event.reason or "שגיאה לא ידועה"
        lines += [
            f"המעקב הפסיק לעבוד: {label}",
            f"  סיבה: {reason}",
            "  ניסיונות חוזרים יימשכו ברקע עד שהמעקב יתאושש.",
            f"  קישור: {url}",
        ]

    elif event.type == ChangeType.RECOVERED:
        lines += [f"המעקב חזר לעבוד: {label}"]
        if event.old_price is not None or event.new_price is not None:
            lines += [f"  המחיר {_direction_he(event.pct_change)} בזמן התקלה:"]
            lines += price_lines()
        else:
            lines += [f"  מחיר נוכחי: {_fmt_price(event.fetch.price, sym)}"]
        lines += extra_lines()
        lines += [f"  קישור: {url}"]

    return "\n".join(lines)


def _build_text_body(events: list[ChangeEvent]) -> str:
    sections = [_text_event(e) for e in events]
    return "\n\n---\n\n".join(sections) + f"\n\n-- {_SIGNATURE}"


# ---------------------------------------------------------------------------
# Body builders (HTML)
# ---------------------------------------------------------------------------

def _html_event(event: ChangeEvent) -> str:
    label = escape(_watch_label(event))
    sym = _currency_symbol(event.fetch.currency)
    url = escape(event.watch.url)
    ts = event.timestamp.strftime("%Y-%m-%d %H:%M")
    link = f'<a href="{url}" dir="ltr">{url}</a>'

    rows: list[str] = []

    def row(key: str, val: str) -> str:
        return (
            f'<tr><td style="padding:4px 8px;color:#555;white-space:nowrap;text-align:right">'
            f'<strong>{escape(key)}</strong></td>'
            f'<td style="padding:4px 8px;text-align:right">{val}</td></tr>'
        )

    direction = _direction_word(event.pct_change)
    price_color = "#c00" if direction == "rose" else "#080"

    def price_rows() -> list[str]:
        old = _fmt_price(event.old_price, _old_symbol(event, sym))
        new = _fmt_price(event.new_price, sym)
        out = [
            row("מחיר קודם", _ltr(old)),
            row("מחיר חדש", f'<strong style="color:{price_color}">{_ltr(new)}</strong>'),
        ]
        diff_str = _price_diff_str(event, sym)
        if diff_str:
            out.append(row("שינוי", _ltr(diff_str)))
        if event.old_currency:
            out.append(row("מטבע", escape(_currency_note(event))))
        return out

    def promo_rows() -> list[str]:
        promo = event.fetch.promo
        if promo is None:
            val = "המבצע הסתיים"
            old = event.old_promo
            if old is not None:
                val += f" (היה: {escape(_promo_details(old))})"
            return [row("מבצע", val)]
        out: list[str] = []
        if promo.code:
            out.append(row(
                "קוד",
                f'<strong dir="ltr">{escape(promo.code)}</strong> '
                f'({escape(str(promo.discount_pct))}% הנחה)',
            ))
        else:
            out.append(row("הנחה", _ltr(f"{promo.discount_pct}%")))
        if promo.condition:
            out.append(row("תנאים", escape(promo.condition)))
        return out

    def extra_rows() -> list[str]:
        out: list[str] = []
        if event.stock_changed and event.type != ChangeType.STOCK_CHANGED:
            out.append(row("מלאי", escape(_stock_he(event.fetch.in_stock))))
        if event.promo_changed and event.type != ChangeType.PROMO_CHANGED:
            out += promo_rows()
        return out

    if event.type == ChangeType.PRICE_CHANGED:
        heading = (
            f'המחיר <span style="color:{price_color}">{_DIRECTION_HE[direction]}</span>: {label}'
        )
        rows += price_rows()
        rows += extra_rows()
        rows += [
            row("נבדק", _ltr(ts)),
            row("קישור", link),
        ]

    elif event.type == ChangeType.STOCK_CHANGED:
        color = "#080" if event.fetch.in_stock else "#c00"
        price_str = _fmt_price(event.new_price or event.fetch.price, sym)
        heading = f'<span style="color:{color}">{_stock_he(event.fetch.in_stock)}</span>: {label}'
        rows += [row("מחיר נוכחי", _ltr(price_str))]
        rows += extra_rows()
        rows += [row("קישור", link)]

    elif event.type == ChangeType.PROMO_CHANGED:
        displayed = _fmt_price(event.fetch.price, sym)
        heading = f"{_promo_word(event)}: {label}"
        rows += promo_rows()
        rows.append(row("מחיר מוצג", _ltr(displayed)))
        if event.fetch.estimated_final_price is not None:
            estimated = _fmt_price(event.fetch.estimated_final_price, sym)
            rows.append(row(
                "מחיר סופי משוער", f'<strong>{_ltr(estimated)}</strong> <em>(הערכה)</em>'
            ))
        rows += [row("קישור", link)]

    elif event.type == ChangeType.THRESHOLD_FAILURE:
        reason = escape(event.reason or "שגיאה לא ידועה")
        heading = f'המעקב הפסיק לעבוד: {label}'
        rows += [
            row("סיבה", reason),
            row("הערה", "ניסיונות חוזרים יימשכו ברקע עד שהמעקב יתאושש."),
            row("קישור", link),
        ]

    elif event.type == ChangeType.RECOVERED:
        heading = f'המעקב חזר לעבוד: {label}'
        if event.old_price is not None or event.new_price is not None:
            rows += price_rows()
        else:
            rows += [row("מחיר נוכחי", _ltr(_fmt_price(event.fetch.price, sym)))]
        rows += extra_rows()
        rows += [row("קישור", link)]
    else:
        heading = f"עדכון: {label}"

    table = (
        '<table dir="rtl" style="border-collapse:collapse;font-family:sans-serif;font-size:14px;'
        'width:100%;max-width:560px">'
        f'<tr><th colspan="2" style="text-align:right;padding:8px;background:#f4f4f4;'
        f'border-bottom:2px solid #ddd">{heading}</th></tr>'
        + "".join(rows)
        + "</table>"
    )
    return table


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


def _send_via_smtp(payload: EmailPayload, cfg: "config.EmailConfig") -> None:  # type: ignore[name-defined]
    """Low-level SMTP send. Raises on any failure."""
    msg = EmailMessage()
    msg["Subject"] = payload.subject
    msg["From"] = cfg.email_from
    msg["To"] = cfg.email_to
    msg.set_content(payload.text_body)
    msg.add_alternative(payload.html_body, subtype="html")

    if cfg.smtp_port == 465:
        # Implicit TLS (SMTPS)
        with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=30) as smtp:
            smtp.login(cfg.smtp_user, cfg.smtp_password)
            smtp.send_message(msg)
        return

    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as smtp:
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

    cfg = _config_module.load().email
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
# Daily sales digest
# ---------------------------------------------------------------------------

def build_digest_payload(digests) -> EmailPayload | None:
    """Build the detailed daily digest email. Lists every promo per site with
    code, percentage, condition, and raw source excerpt."""
    from price_watcher.digest import SiteDigest  # lazy to avoid circular import
    digests: list[SiteDigest] = list(digests)
    if not digests:
        return None

    all_promos = [(d, p) for d in digests for p in d.promos]
    if all_promos:
        best = max(all_promos, key=lambda dp: dp[1].discount_pct)
        best_site, best_promo = best
        total = len(all_promos)
        subject = (
            f"דוח מבצעים יומי: {total} מבצעים "
            f"(המוביל: {best_promo.discount_pct}% הנחה ב-{best_site.display_name}"
            + (f" עם הקוד {best_promo.code}" if best_promo.code else "")
            + ")"
        )
    else:
        subject = "דוח מבצעים יומי: אין היום מבצעים כלל-אתריים"

    # --- Plain text body ---
    text_lines = ["דוח מבצעים יומי", "=" * 50, ""]
    for d in digests:
        text_lines.append(f"▼ {d.display_name}  ({d.host})")
        text_lines.append(f"   {d.homepage_url}")
        if d.status != "ok":
            text_lines.append(f"   [{_status_he(d.status)}] {d.error or ''}")
            text_lines.append("")
            continue
        if not d.promos:
            text_lines.append("   (לא זוהה מבצע כלל-אתרי)")
            text_lines.append("")
            continue
        for i, p in enumerate(d.promos, 1):
            code_part = f"קוד: {p.code}" if p.code else "ללא קוד"
            text_lines.append(f"   {i}. {p.discount_pct}% הנחה  —  {code_part}")
            if p.condition:
                text_lines.append(f"      פירוט: {p.condition[:200]}")
        text_lines.append("")
    text_lines.append(f"— {_SIGNATURE}")
    text_body = "\n".join(text_lines)

    # --- HTML body ---
    html_parts = [
        '<!DOCTYPE html><html dir="rtl" lang="he"><head><meta charset="utf-8"></head>',
        '<body dir="rtl" style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;'
        'padding:16px;max-width:680px;line-height:1.5;text-align:right">',
        '<h2 style="margin:0 0 20px">דוח מבצעים יומי</h2>',
    ]
    for d in digests:
        html_parts.append(
            f'<div style="margin:0 0 24px;padding:14px 16px;border:1px solid #e5e7eb;'
            f'border-radius:8px;background:#fafafa">'
        )
        html_parts.append(
            f'<div style="font-size:16px;font-weight:600">{escape(d.display_name)}'
            f' <a href="{escape(d.homepage_url)}" style="font-weight:normal;color:#2563eb;'
            f'text-decoration:none;font-size:13px">({escape(d.host)})</a></div>'
        )
        if d.status != "ok":
            html_parts.append(
                f'<div style="margin-top:8px;color:#c33">'
                f'<b>{escape(_status_he(d.status))}:</b> {escape(d.error or "—")}</div>'
            )
        elif not d.promos:
            html_parts.append(
                '<div style="margin-top:8px;color:#888;font-style:italic">'
                'לא זוהה מבצע כלל-אתרי</div>'
            )
        else:
            for p in d.promos:
                code_html = (
                    f'<code style="background:#111;color:#fff;padding:2px 8px;'
                    f'border-radius:4px;font-size:14px;font-weight:600" dir="ltr">{escape(p.code)}</code>'
                    if p.code
                    else '<span style="color:#888;font-size:13px">(ללא קוד)</span>'
                )
                html_parts.append(
                    f'<div style="margin:10px 0 0;padding-top:10px;border-top:1px solid #eee">'
                    f'<div style="font-size:15px">'
                    f'<b style="color:#dc2626">{p.discount_pct}% הנחה</b>  &nbsp; {code_html}</div>'
                )
                if p.condition:
                    html_parts.append(
                        f'<div style="margin-top:4px;color:#555;font-size:13px">'
                        f'{escape(p.condition[:300])}</div>'
                    )
                html_parts.append('</div>')
        html_parts.append('</div>')
    html_parts.append(
        f'<p style="color:#999;font-size:12px;margin-top:24px">— {_SIGNATURE}</p>'
        '</body></html>'
    )
    html_body = "".join(html_parts)

    return EmailPayload(subject=subject, text_body=text_body, html_body=html_body)


def _promo_category(p) -> str:
    category = getattr(p, "category", "")
    if category:
        return category
    from price_watcher.supermarkets import _categorize
    return _categorize(p)


def _group_meat_by_category(sm):
    """category -> its deals (cheapest first as ranked), categories in `_CATEGORIES` order."""
    from price_watcher.supermarkets import _CATEGORIES
    groups: dict[str, list] = {}
    for p in sm.promos:
        groups.setdefault(_promo_category(p), []).append(p)
    ordered = {}
    for cat_name, _ in _CATEGORIES:
        if cat_name in groups:
            ordered[cat_name] = groups.pop(cat_name)
    for cat_name, items in groups.items():
        ordered[cat_name] = items
    return ordered


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
    (("עוף", "פולקע", "פרגיות"), "🍗"),
    (("כבש", "טלה"), "🐑"),
    (("עגל", "בייבי"), "🐄"),
    (("לשון",), "👅"),
    (("מוח",), "🧠"),
)

def _region_order(sm=None) -> tuple[str, ...]:
    """Regions of the scan area, in `_REGION_ORDER` (only regions with an enabled city)."""
    recorded = list(getattr(sm, "regions", None) or [])
    if recorded:
        return tuple(recorded)
    from price_watcher.supermarkets import _active_regions
    return _active_regions()


def _show_regions(sm=None) -> bool:
    """Region chips only help when the scan covers more than one region."""
    return len(_region_order(sm)) > 1


# Soft background / text colour per region, in `_REGION_ORDER` order.
_REGION_CHIP_COLORS: tuple[tuple[str, str], ...] = (
    ("#dbeafe", "#1e40af"),
    ("#fef3c7", "#92400e"),
    ("#ede9fe", "#5b21b6"),
)


# Shorter labels for the narrow region column (full name otherwise).
_REGION_SHORT = {"ראשון לציון והסביבה": "ראשל״צ והסביבה"}


def _region_chip_html(region: str) -> str:
    from price_watcher.supermarkets import _REGION_ORDER as order
    if region in order:
        bg, fg = _REGION_CHIP_COLORS[order.index(region) % len(_REGION_CHIP_COLORS)]
    else:
        bg, fg = "#f3f4f6", "#374151"
    # Kept short on purpose: this is repeated on every row of every table.
    return (
        f'<span style="background:{bg};color:{fg};padding:1px 5px;border-radius:4px;'
        f'line-height:1.9;white-space:nowrap">{escape(_REGION_SHORT.get(region, region))}</span>'
    )


def _promo_regions(p) -> list[str]:
    return list(getattr(p, "regions", None) or [])


def _where_label(p, limit: int = 3) -> str:
    """Branches a deal was seen in, e.g. 'סוקולוב הרצליה · רמת השרון ועוד 2'.

    Falls back to the cities (and store count) for rows without branch names.
    """
    stores = list(getattr(p, "stores", None) or [])
    if stores:
        more = len(stores) - limit
        return " · ".join(stores[:limit]) + (f" ועוד {more}" if more > 0 else "")
    cities = list(getattr(p, "cities", None) or [])
    if not cities:
        return ""
    shown = ", ".join(cities[:3]) + (" ועוד" if len(cities) > 3 else "")
    count = getattr(p, "store_count", 1)
    return shown if count <= 1 else f"{shown} · {count} סניפים"


def _category_icon(cat_name: str) -> str:
    if cat_name == "אחר":
        return "•"
    for keywords, icon in _CATEGORY_ICONS:
        if any(kw in cat_name for kw in keywords):
            return icon
    return "🥩"


def _shekel(value: object) -> str:
    return f'<span dir="ltr">₪{value}</span>'


def _usd(value: object) -> str:
    return f'<span dir="ltr">${value}</span>'


def _price_compact_html(p) -> str:
    """Promo price per kg in bold, then the regular price per kg struck
    through with the discount, then the multi-buy deal / pack size."""
    # Kept lean on purpose: this is repeated on every row of every table.
    # The table header says the prices are per kg.
    per_kg = getattr(p, "price_per_kg", None)
    was_kg = getattr(p, "regular_per_kg", None)
    unit = ""
    if per_kg is None:  # no price per kg: show the unit price as it is
        per_kg = p.discounted_price
        was_kg = p.regular_price
        unit = " לק״ג" if getattr(p, "is_weighted", False) else " ליח׳"
    lines = []
    if per_kg:
        lines.append(f'<b dir="ltr" style="font-size:15px">₪{per_kg}</b>' + (f'<small>{unit}</small>' if unit else ''))
    # "-30%" when known; a bare dash (not "-—") when it is not.
    pct_txt = f"-{int(p.savings_pct)}%" if p.savings_pct is not None else "—"
    pct = f'<b dir="ltr" style="color:{_COLOR_DANGER}">{pct_txt}</b>'
    was = f'<s dir="ltr">₪{was_kg}</s> ' if was_kg else ""
    lines.append(f'<small>{was}{pct}</small>')
    # One short line each, so the (unwrapped) price column stays narrow on phones.
    if getattr(p, "deal_text", ""):
        # "2 ב-₪90" with the amount isolated, or the bidi order scrambles it.
        lines.append(
            f'<small dir="rtl">מבצע {p.min_qty.normalize():f} ב-<span dir="ltr">₪{p.bundle_price}</span></small>'
        )
    if getattr(p, "pack_text", ""):
        lines.append(f'<small>{escape(p.pack_text)}</small>')
    return "<br>".join(lines)


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


def _scope_parts(sm) -> list[tuple[str, int]]:
    """(city, stores scanned there) for every city of the scan area."""
    by_city = getattr(sm, "stores_by_city", None) or {}
    cities = list(getattr(sm, "cities", None) or [])
    if not cities:
        from price_watcher.supermarkets import _enabled_cities
        cities = list(_enabled_cities())
    return [(c, by_city.get(c, 0)) for c in cities]


def _stores_word(n: int) -> str:
    return "סניף אחד" if n == 1 else f"{n} סניפים"


def _ranking_note(sm) -> str:
    """One sentence explaining how the tables are ranked (both text and HTML)."""
    from price_watcher.supermarkets import _top_per_category
    note = (
        f"בכל קטגוריה {_top_per_category()} המבצעים הזולים ביותר לפי מחיר המבצע לק״ג, "
        "מהזול ליקר (במוצרים ארוזים המחיר מחושב לפי משקל המארז)."
    )
    unpriced = sum((getattr(sm, "unpriced_by_category", None) or {}).values())
    if unpriced:
        note += (
            f" {unpriced} מבצעים לא נכללו בדירוג כי לא ניתן לחשב להם מחיר לק״ג"
            " (משקל המארז חסר או סותר בקובץ המחירים)."
        )
    unranked = sum((getattr(sm, "unranked_by_category", None) or {}).values())
    if unranked:
        note += (
            f" {unranked} מבצעים בקבוצות כלליות (כמו ״בשר – אחר״ או ״עוף – אחר״)"
            " לא מוצגים, כי הם מערבבים נתחים שונים."
        )
    return note


def _supermarkets_section_text(sm) -> list[str]:
    lines = ["מבצעי בשר בסופרמרקטים", "-" * 50, ""]
    if sm is None:
        lines.append("  (לא נסרק)")
        return lines
    problems = [
        (title, chains)
        for title, chains in (
            ("רשתות שנכשלו", getattr(sm, "chains_failed", None) or {}),
            ("רשתות ללא מבצעים", getattr(sm, "chains_empty", None) or {}),
        )
        if chains
    ]
    problem_lines = [
        f"  {title}: " + ", ".join(f"{c} ({r})" for c, r in chains.items())
        for title, chains in problems
    ]
    if sm.status != "ok":
        lines.append(f"  הסריקה נכשלה: {_status_he(sm.status)}  {sm.error or ''}")
        lines.extend(problem_lines)
        return lines
    chains = ", ".join(sm.chains_scanned) if sm.chains_scanned else "אין"
    lines.append(f"  רשתות שנסרקו: {chains}")
    lines.extend(problem_lines)
    lines.append(f"  סניפים שנסרקו: {len(getattr(sm, 'stores_scanned', None) or [])}")
    lines.append("  ערים: " + " · ".join(f"{c} ({_stores_word(n)})" for c, n in _scope_parts(sm)))
    show_regions = _show_regions(sm)
    if not sm.promos:
        lines.append("  לא נמצאו היום מבצעי בשר.")
        return lines
    lines.append("  " + _ranking_note(sm))
    promos_by_category = getattr(sm, "promos_by_category", None) or {}
    for category, items in _group_meat_by_category(sm).items():
        total = promos_by_category.get(category, len(items))
        shown = f"{len(items)} מתוך {total}" if total > len(items) else f"{len(items)}"
        lines.append("")
        lines.append(f"  ▼ {category}  ({shown} מבצעים)")
        for i, p in enumerate(items, 1):
            per_kg = getattr(p, "price_per_kg", None)
            was_kg = getattr(p, "regular_per_kg", None)
            if per_kg is not None:
                now = f"₪{per_kg} לק״ג"
                was = f"₪{was_kg} לק״ג" if was_kg is not None else "—"
            else:
                unit = " לק״ג" if getattr(p, "is_weighted", False) else ""
                now = f"₪{p.discounted_price}{unit}" if p.discounted_price else "—"
                was = f"₪{p.regular_price}{unit}" if p.regular_price else "—"
            pct = f"{int(p.savings_pct)}%" if p.savings_pct is not None else "לא ידועה"
            extra = " · ".join(
                x for x in (getattr(p, "deal_text", ""), getattr(p, "pack_text", "")) if x
            )
            lines.append(f"    {i}. {now} · {p.chain} · {p.item_name[:50]}")
            lines.append(
                f"       מחיר רגיל {was} · הנחה {pct}" + (f" · {extra}" if extra else "")
            )
            if p.promotion_description:
                lines.append(f"       {p.promotion_description[:90]}")
            where = _where_label(p, limit=6)
            regions = " · ".join(_promo_regions(p)) if show_regions else ""
            if regions or where:
                lines.append("       " + " — ".join(x for x in (regions, where) if x))
    return lines


def _supermarkets_section_html(sm) -> str:
    """Meat section: one table per category, cheapest promo price per kg first. RTL.

    Each category shows up to PW_TOP_PER_CATEGORY deals. Region chips appear
    only when the scan covers more than one region.
    """
    if sm is None:
        return '<div style="color:#888;font-style:italic;text-align:right">(לא נסרק)</div>'
    problems_html = "".join(
        f'<div style="font-size:12px;color:#991b1b;margin-top:4px;text-align:right">'
        f'{title}: ' + " · ".join(f'{escape(c)} ({escape(r)})' for c, r in chains.items())
        + '</div>'
        for title, chains in (
            ("רשתות שנכשלו", getattr(sm, "chains_failed", None) or {}),
            ("רשתות ללא מבצעים", getattr(sm, "chains_empty", None) or {}),
        )
        if chains
    )
    if sm.status != "ok":
        return (
            _failure_box_html("סריקת הסופרמרקטים נכשלה", sm.status, sm.error)
            + (f'<div style="margin:-16px 0 24px;font-family:{_FONT_STACK}">{problems_html}</div>'
               if problems_html else '')
        )

    chains_scanned = sm.chains_scanned or []
    stores_scanned = list(getattr(sm, "stores_scanned", None) or [])
    stores_by_region = getattr(sm, "stores_by_region", None) or {}
    promos_by_category = getattr(sm, "promos_by_category", None) or {}
    unpriced_by_category = getattr(sm, "unpriced_by_category", None) or {}
    grouped = _group_meat_by_category(sm)
    total_deals = (
        (sum(promos_by_category.values()) or len(sm.promos)) + sum(unpriced_by_category.values())
        + sum((getattr(sm, "unranked_by_category", None) or {}).values())
    )
    show_regions = _show_regions(sm)
    muted = f'color:{_COLOR_MUTED}'

    scope_html = "ערים: " + " · ".join(
        f'{escape(c)} <span style="{muted}">({_stores_word(n)})</span>' for c, n in _scope_parts(sm)
    )
    regions_legend = "".join(
        f'{_region_chip_html(region)}'
        f'<span style="{muted};margin-left:10px"> {_stores_word(stores_by_region.get(region, 0))}</span>'
        for region in _region_order(sm)
    ) if show_regions else ""

    html = []
    # Summary card
    html.append(
        f'<div style="padding:14px 16px;background:{_COLOR_BG_SOFT};'
        f'border:1px solid {_COLOR_BORDER};border-radius:10px;margin-bottom:14px;'
        f'font-family:{_FONT_STACK};color:{_COLOR_TEXT};text-align:right">'
        f'<div style="font-size:13px;{muted}">הסריקה של היום</div>'
        f'<div style="font-size:15px;margin-top:4px">'
        f'<b>{len(chains_scanned)}</b> רשתות · '
        f'<b>{len(stores_scanned)}</b> סניפים · '
        f'<b>{total_deals}</b> מבצעי בשר · '
        f'<b>{len(grouped)}</b> קטגוריות</div>'
        f'<div style="font-size:12px;margin-top:6px">{scope_html}</div>'
        f'<div style="font-size:12px;{muted};margin-top:4px">'
        f'רשתות: ' + " · ".join(escape(c) for c in chains_scanned)
        + '</div>'
        + (f'<div style="margin-top:8px;font-size:11px">{regions_legend}</div>' if regions_legend else '')
        + problems_html
        + (f'<div style="font-size:12px;{muted};margin-top:6px">{escape(_ranking_note(sm))}</div>'
           if sm.promos else '')
        + '</div>'
    )

    if not sm.promos:
        html.append(
            '<div style="color:#888;font-style:italic;text-align:right;'
            f'font-family:{_FONT_STACK}">לא נמצאו היום מבצעי בשר.</div>'
        )
        return "".join(html)

    # Category index as plain text (in-page links are unreliable in mail
    # clients, and one styled chip per category cost ~10 KB with 40 tables).
    html.append(
        f'<div style="margin:0 0 16px;font-size:12px;{muted};text-align:right;line-height:1.7">'
        'קטגוריות: ' + " · ".join(escape(cat) for cat in grouped) + '</div>'
    )

    # One table per category, cheapest first. Kept lean on purpose (Gmail
    # clips mails above ~102 KB): no per-row badges or zebra shading, the
    # muted colour and the cell alignment are the only styles repeated.
    muted_small = f'<small style="{muted}">'
    for category, items in grouped.items():
        rows_html = []
        for i, p in enumerate(items):
            where = _where_label(p)
            chips = " ".join(_region_chip_html(r) for r in _promo_regions(p)) if show_regions else ""
            below = "<br>".join(x for x in (chips, escape(where)) if x)
            desc = (p.promotion_description or "").strip()
            rows_html.append(
                f'<tr valign="top" style="border-top:1px solid {_COLOR_BORDER}'
                + (';background:#ecfdf5">' if i == 0 else '">')
                # Supermarket, then its branch(es) with the city. One column,
                # so the table still fits a phone screen.
                + ('<td width="34%">' if i == 0 else '<td>')
                + f'<b style="color:{_COLOR_LINK}">{escape(p.chain)}</b>'
                + (f'<br>{muted_small}{below}</small>' if below else '')
                + '</td>'
                # Item
                f'<td>{escape(p.item_name[:60])}'
                + (f'<br>{muted_small}{escape(desc[:60])}</small>' if desc and desc != p.item_name else '')
                + '</td>'
                # Price per kg (now / was), discount, deal
                f'<td align="left" nowrap>{_price_compact_html(p)}</td>'
                '</tr>'
            )

        total = promos_by_category.get(category, len(items))
        if total > len(items):
            shown_txt = f'{len(items)} הזולים מתוך {total}'
        elif len(items) == 1:
            shown_txt = 'מבצע אחד'
        else:
            shown_txt = f'{len(items)} מבצעים'
        chains_in_table = len({p.chain for p in items})
        chains_txt = 'רשת אחת' if chains_in_table == 1 else f'{chains_in_table} רשתות'
        unpriced = unpriced_by_category.get(category, 0)
        unpriced_txt = f' · עוד {unpriced} בלי מחיר לק״ג' if unpriced else ''
        html.append(
            f'<table dir="rtl" cellpadding="6" cellspacing="0" width="100%" style="border-collapse:collapse;'
            f'margin:0 0 16px;border:1px solid {_COLOR_BORDER};font-size:13px;color:{_COLOR_TEXT};'
            f'font-family:{_FONT_STACK}">'
            # Header strip: category, counts, and the unit of the price column
            f'<tr><td colspan="3" style="background:{_COLOR_TEXT};color:#fff;padding:8px 10px">'
            f'<b style="font-size:15px">{_category_icon(category)} {escape(category)}</b> '
            f'<small style="color:#d1d5db">{shown_txt} · {chains_txt}{unpriced_txt} · מחיר לק״ג</small>'
            '</td></tr>'
            + "".join(rows_html)
            + '</table>'
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
    scanned = f"  נסרקו {len(sims4.packs)} חבילות מהחנות של EA"
    if getattr(sims4, "page_limited", False):
        scanned += " (העמוד הראשון בלבד; חבילות נוספות אינן נסרקות)"
    lines.append(scanned + ".")
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
    note = " (העמוד הראשון בלבד)" if getattr(sims4, "page_limited", False) else ""
    html.append(
        f'<div style="margin:0 0 8px;color:#6b7280;font-size:12px">'
        f'נסרקו {len(sims4.packs)} חבילות{note}</div>'
    )
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
            '<div style="color:#888;font-style:italic">אין כרגע חבילות במבצע.</div>'
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
        if creatine.direction == "changed" and prev.price_per_100g == cur.price_per_100g:
            lines.append(
                f"  שינוי: המוצר הזול ביותר התחלף (אותו מחיר: {sym}{cur.price_per_100g} ל-100 גרם)"
            )
        elif creatine.direction == "changed":
            lines.append("  שינוי: המוצר הזול ביותר התחלף")
        else:
            lines.append(
                f"  שינוי: המחיר {word} מ-{sym}{prev.price_per_100g} ל-{sym}{cur.price_per_100g} ל-100 גרם"
            )
    else:
        lines.append("  ללא שינוי מאז הסריקה הקודמת.")
    if getattr(creatine, "parsed", 0):
        lines.append(f"  נבדקו {creatine.parsed} מוצרים, מהם {creatine.powders} אבקות.")
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
        arrow_color = {"dropped": "#065f46", "rose": "#991b1b"}.get(creatine.direction or "", "#374151")
        word = _DIRECTION_HE.get(creatine.direction or "changed", "השתנה")
        if creatine.direction == "changed" and prev.price_per_100g == cur.price_per_100g:
            change_html = (
                f'<div style="margin-top:6px;color:{arrow_color}"><b>'
                f'המוצר הזול ביותר התחלף (אותו מחיר: {_per100(cur.price_per_100g)})</b></div>'
            )
        elif creatine.direction == "changed":
            change_html = (
                f'<div style="margin-top:6px;color:{arrow_color}"><b>'
                f'המוצר הזול ביותר התחלף</b></div>'
            )
        else:
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
        if report.creatine.direction == "changed" and report.creatine.previous_cheapest is not None:
            creatine_blurb = "המוצר הזול ביותר בקריאטין התחלף"
        else:
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

    sims4_report = getattr(report, "sims4", None)
    sims_blurb = ""
    if sims4_report is not None and sims4_report.status == "ok":
        if sims4_report.newly_on_sale:
            best_sims = max(p.discount_percent for p in sims4_report.newly_on_sale)
            sims_blurb = f"{len(sims4_report.newly_on_sale)} חבילות סימס 4 חדשות במבצע, עד {best_sims}% הנחה"
        elif sims4_report.on_sale_now:
            sims_blurb = f"{len(sims4_report.on_sale_now)} חבילות סימס 4 במבצע"

    # Sections whose scan failed must show in the subject, not only in the body.
    failed: list[str] = []
    if any(d.status in ("unreachable", "blocked") for d in site_digests):
        failed.append("אתרים")
    if report.creatine is not None and report.creatine.status != "ok":
        failed.append("קריאטין")
    if sims4_report is not None and sims4_report.status != "ok":
        failed.append("סימס 4")
    if sm is not None and sm.status != "ok":
        failed.append("בשר")

    headline: list[str] = []
    if best_promo:
        headline.append(
            f"עד {best_promo.discount_pct}% הנחה ב-{best_promo_site.display_name}"
            + (f" ({best_promo.code})" if best_promo.code else "")
        )
    if creatine_blurb:
        headline.append(creatine_blurb)
    if sims_blurb:
        headline.append(sims_blurb)
    if meat_blurb:
        headline.append(meat_blurb)
    if failed:
        headline.append("סריקה נכשלה: " + ", ".join(failed))
    subject = "דוח יומי: " + (" · ".join(headline) if headline else "אין חדש היום")

    # ---- Plain text ----
    text_lines = ["דוח יומי", "=" * 50, ""]
    text_lines.append("מבצעים כלל-אתריים")
    text_lines.append("-" * 50)
    text_lines.append("")
    if not site_digests:
        text_lines.append("  (אין אתרים ברשימת המעקב)")
        text_lines.append("")
    for d in site_digests:
        text_lines.append(f"▼ {d.display_name}  ({d.host})")
        text_lines.append(f"  {d.homepage_url}")
        if d.status != "ok":
            text_lines.append(f"  [{_status_he(d.status)}] {d.error or ''}")
            text_lines.append("")
            continue
        if not d.promos:
            text_lines.append("  (לא זוהה מבצע כלל-אתרי)")
            text_lines.append("")
            continue
        for i, p in enumerate(d.promos, 1):
            code_part = f"קוד: {p.code}" if p.code else "ללא קוד"
            text_lines.append(f"  {i}. {p.discount_pct}% הנחה  —  {code_part}")
            if p.condition:
                text_lines.append(f"     {p.condition[:200]}")
        text_lines.append("")
    text_lines.extend(_creatine_section_text(report.creatine))
    text_lines.append("")
    text_lines.extend(_sims4_section_text(getattr(report, "sims4", None)))
    text_lines.append("")
    text_lines.extend(_supermarkets_section_text(sm))
    text_lines.append("")
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
    if not site_digests:
        html_parts.append(
            '<div style="color:#888;font-style:italic">אין אתרים ברשימת המעקב.</div>'
        )
    for d in site_digests:
        html_parts.append(
            f'<div style="margin:0 0 20px;padding:14px 16px;border:1px solid #e5e7eb;'
            f'border-radius:8px;background:#fafafa">'
            f'<div style="font-size:16px;font-weight:600">{escape(d.display_name)}'
            f' <a href="{escape(d.homepage_url)}" dir="ltr" style="font-weight:normal;color:#2563eb;'
            f'text-decoration:none;font-size:13px">({escape(d.host)})</a></div>'
        )
        if d.status != "ok":
            html_parts.append(
                f'<div style="margin-top:8px;color:#c33">'
                f'<b>{escape(_status_he(d.status))}:</b> {escape(d.error or "—")}</div>'
            )
        elif not d.promos:
            html_parts.append(
                '<div style="margin-top:8px;color:#888;font-style:italic">'
                'לא זוהה מבצע כלל-אתרי</div>'
            )
        else:
            for p in d.promos:
                code_html = (
                    f'<code dir="ltr" style="background:#111;color:#fff;padding:2px 8px;'
                    f'border-radius:4px;font-size:14px;font-weight:600">{escape(p.code)}</code>'
                    if p.code
                    else '<span style="color:#888;font-size:13px">(ללא קוד)</span>'
                )
                html_parts.append(
                    f'<div style="margin:10px 0 0;padding-top:10px;border-top:1px solid #eee">'
                    f'<div style="font-size:15px">'
                    f'<b style="color:#dc2626">{p.discount_pct}% הנחה</b>  &nbsp; {code_html}</div>'
                )
                if p.condition:
                    html_parts.append(
                        f'<div style="margin-top:4px;color:#555;font-size:13px">'
                        f'{escape(p.condition[:300])}</div>'
                    )
                html_parts.append('</div>')
        html_parts.append('</div>')

    html_parts.append(_section_heading_html("קריאטין"))
    html_parts.append(_creatine_section_html(report.creatine))

    html_parts.append(_section_heading_html("הסימס 4"))
    html_parts.append(_sims4_section_html(getattr(report, "sims4", None)))

    html_parts.append(_section_heading_html("מבצעי בשר בסופרמרקטים"))
    html_parts.append(_supermarkets_section_html(sm))

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
    cfg = _config_module.load().email
    try:
        _send_via_smtp(payload, cfg)
        log.info("Daily email sent to %s", cfg.email_to)
        return True
    except (smtplib.SMTPException, OSError) as exc:
        log.error("Daily email send failed: %s", type(exc).__name__)
        return False


# ---------------------------------------------------------------------------
# Legacy digest-only payload (kept for backward compatibility, unused now)
# ---------------------------------------------------------------------------

def send_digest_email(digests) -> bool:
    """Send the daily digest email. Returns True on success, False on failure."""
    payload = build_digest_payload(digests)
    if payload is None:
        log.info("No digest to send (no sites)")
        return True
    cfg = _config_module.load().email
    try:
        _send_via_smtp(payload, cfg)
        log.info("Digest email sent to %s", cfg.email_to)
        return True
    except (smtplib.SMTPException, OSError) as exc:
        log.error("Digest email send failed: %s", type(exc).__name__)
        return False
