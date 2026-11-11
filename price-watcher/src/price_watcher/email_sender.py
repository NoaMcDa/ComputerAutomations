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
from price_watcher.contracts import ChangeEvent, ChangeType, EmailPayload
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


def _fmt_price(price: Decimal | None, symbol: str) -> str:
    if price is None:
        return "?"
    return f"{price:,.2f} {symbol}".rstrip("0").rstrip(".")


def _direction_word(pct: float | None) -> str:
    if pct is None:
        return "changed"
    return "dropped" if pct < 0 else "rose"


# ---------------------------------------------------------------------------
# Subject builders
# ---------------------------------------------------------------------------

def _subject_single(event: ChangeEvent) -> str:
    label = _truncate(_watch_label(event))
    sym = _currency_symbol(event.fetch.currency)

    if event.type == ChangeType.PRICE_CHANGED:
        direction = "dropped" if (event.pct_change or 0) < 0 else "rose"
        old = f"{event.old_price:,.0f}" if event.old_price is not None else "?"
        new = f"{event.new_price:,.0f}" if event.new_price is not None else "?"
        pct = f"{event.pct_change:+.1f}%" if event.pct_change is not None else ""
        return f"Price {direction}: {label} {old} → {new} {sym} ({pct})"

    if event.type == ChangeType.STOCK_CHANGED:
        status = "back in stock" if event.fetch.in_stock else "out of stock"
        return f"Stock update: {label} is {status}"

    if event.type == ChangeType.PROMO_CHANGED:
        return f"Promo changed: {label}"

    if event.type == ChangeType.THRESHOLD_FAILURE:
        return f"Watch stopped working: {label}"

    if event.type == ChangeType.RECOVERED:
        return f"Watch working again: {label}"

    return f"Price Watcher update: {label}"


def _subject_multi(events: list[ChangeEvent]) -> str:
    n = len(events)
    # Find the event with the largest absolute % change.
    price_events = [e for e in events if e.type == ChangeType.PRICE_CHANGED and e.pct_change is not None]
    if price_events:
        biggest = max(price_events, key=lambda e: abs(e.pct_change))  # type: ignore[arg-type]
        label = _truncate(_watch_label(biggest), 25)
        pct = f"{biggest.pct_change:+.1f}%"
        return f"Price Watcher: {n} changes (biggest: {pct} on {label})"
    return f"Price Watcher: {n} changes"


# ---------------------------------------------------------------------------
# Body builders (plain text)
# ---------------------------------------------------------------------------

def _text_event(event: ChangeEvent) -> str:
    label = _watch_label(event)
    sym = _currency_symbol(event.fetch.currency)
    url = event.watch.url
    ts = event.timestamp.strftime("%Y-%m-%d %H:%M")
    lines: list[str] = []

    if event.type == ChangeType.PRICE_CHANGED:
        direction = _direction_word(event.pct_change)
        old = _fmt_price(event.old_price, sym)
        new = _fmt_price(event.new_price, sym)
        diff_str = ""
        if event.diff is not None and event.pct_change is not None:
            diff_str = f"  ({event.diff:+,.2f} {sym}, {event.pct_change:+.1f}%)"
        lines += [
            f"PRICE {direction.upper()}: {label}",
            f"  {old} → {new}{diff_str}",
            f"  Link: {url}",
            f"  Checked: {ts}",
        ]

    elif event.type == ChangeType.STOCK_CHANGED:
        status = "Back in stock" if event.fetch.in_stock else "Now out of stock"
        price_str = _fmt_price(event.new_price or event.fetch.price, sym)
        lines += [
            f"STOCK UPDATE: {status}",
            f"  Product: {label}",
            f"  Current price: {price_str}",
            f"  Link: {url}",
        ]

    elif event.type == ChangeType.PROMO_CHANGED:
        promo = event.fetch.promo
        displayed = _fmt_price(event.fetch.price, sym)
        estimated = _fmt_price(event.fetch.estimated_final_price, sym)
        lines += [f"PROMOTION CHANGED: {label}"]
        if promo:
            if promo.code:
                lines.append(f"  Code: {promo.code}  ({promo.discount_pct}% off)")
            if promo.condition:
                lines.append(f"  Condition: {promo.condition}")
        lines += [
            f"  Displayed: {displayed}",
            f"  Estimated final: {estimated}",
            f"  Link: {url}",
        ]

    elif event.type == ChangeType.THRESHOLD_FAILURE:
        reason = event.reason or "unknown error"
        lines += [
            f"WATCH STOPPED WORKING: {label}",
            f"  Reason: {reason}",
            "  Retries will continue silently until the watch recovers.",
            f"  Link: {url}",
        ]

    elif event.type == ChangeType.RECOVERED:
        price_str = _fmt_price(event.new_price or event.fetch.price, sym)
        lines += [
            f"WATCH WORKING AGAIN: {label}",
            f"  Current price: {price_str}",
            f"  Link: {url}",
        ]

    return "\n".join(lines)


def _build_text_body(events: list[ChangeEvent]) -> str:
    sections = [_text_event(e) for e in events]
    return "\n\n---\n\n".join(sections) + "\n\n-- Price Watcher"


# ---------------------------------------------------------------------------
# Body builders (HTML)
# ---------------------------------------------------------------------------

def _html_event(event: ChangeEvent) -> str:
    label = escape(_watch_label(event))
    sym = escape(_currency_symbol(event.fetch.currency))
    url = escape(event.watch.url)
    ts = event.timestamp.strftime("%Y-%m-%d %H:%M")

    rows: list[str] = []

    def row(key: str, val: str) -> str:
        return (
            f'<tr><td style="padding:4px 8px;color:#555;white-space:nowrap">'
            f'<strong>{escape(key)}</strong></td>'
            f'<td style="padding:4px 8px">{val}</td></tr>'
        )

    if event.type == ChangeType.PRICE_CHANGED:
        direction = _direction_word(event.pct_change)
        color = "#c00" if direction == "rose" else "#080"
        old = _fmt_price(event.old_price, event.fetch.currency or "ILS")
        new = _fmt_price(event.new_price, event.fetch.currency or "ILS")
        diff_str = ""
        if event.diff is not None and event.pct_change is not None:
            diff_str = f" ({event.diff:+,.2f} {sym}, {event.pct_change:+.1f}%)"
        heading = f'Price <span style="color:{color}">{escape(direction)}</span>: {label}'
        rows += [
            row("Old price", escape(old)),
            row("New price", f'<strong style="color:{color}">{escape(new)}</strong>'),
            row("Change", escape(diff_str.strip(" ()"))),
            row("Checked", escape(ts)),
            row("Link", f'<a href="{url}">{url}</a>'),
        ]

    elif event.type == ChangeType.STOCK_CHANGED:
        status = "Back in stock" if event.fetch.in_stock else "Now out of stock"
        color = "#080" if event.fetch.in_stock else "#c00"
        price_str = _fmt_price(event.new_price or event.fetch.price, event.fetch.currency or "ILS")
        heading = f'<span style="color:{color}">{escape(status)}</span>: {label}'
        rows += [
            row("Current price", escape(price_str)),
            row("Link", f'<a href="{url}">{url}</a>'),
        ]

    elif event.type == ChangeType.PROMO_CHANGED:
        promo = event.fetch.promo
        displayed = _fmt_price(event.fetch.price, event.fetch.currency or "ILS")
        estimated = _fmt_price(event.fetch.estimated_final_price, event.fetch.currency or "ILS")
        heading = f"Promotion changed: {label}"
        if promo and promo.code:
            rows.append(row("Code", f'<strong>{escape(promo.code)}</strong> ({escape(str(promo.discount_pct))}% off)'))
        if promo and promo.condition:
            rows.append(row("Condition", escape(promo.condition)))
        rows += [
            row("Displayed price", escape(displayed)),
            row("Estimated final", f'<strong>{escape(estimated)}</strong> <em>(estimate)</em>'),
            row("Link", f'<a href="{url}">{url}</a>'),
        ]

    elif event.type == ChangeType.THRESHOLD_FAILURE:
        reason = escape(event.reason or "unknown error")
        heading = f'Watch stopped working: {label}'
        rows += [
            row("Reason", reason),
            row("Note", "Retries will continue silently until the watch recovers."),
            row("Link", f'<a href="{url}">{url}</a>'),
        ]

    elif event.type == ChangeType.RECOVERED:
        price_str = _fmt_price(event.new_price or event.fetch.price, event.fetch.currency or "ILS")
        heading = f'Watch working again: {label}'
        rows += [
            row("Current price", escape(price_str)),
            row("Link", f'<a href="{url}">{url}</a>'),
        ]
    else:
        heading = f"Update: {label}"

    table = (
        '<table style="border-collapse:collapse;font-family:sans-serif;font-size:14px;'
        'width:100%;max-width:560px">'
        f'<tr><th colspan="2" style="text-align:left;padding:8px;background:#f4f4f4;'
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
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        "<title>Price Watcher</title></head>"
        '<body style="margin:0;padding:16px;background:#fff;color:#222">'
        + tables
        + '<p style="margin-top:24px;font-size:12px;color:#999">— Price Watcher</p>'
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
        subject="Price Watcher: test email",
        text_body=(
            "If you got this, your SMTP setup is working.\n"
            "Price-change emails will arrive the same way."
        ),
        html_body=(
            '<!DOCTYPE html><html><head><meta charset="utf-8"></head>'
            '<body style="font-family:sans-serif;padding:16px">'
            "<p>If you got this, your SMTP setup is working.</p>"
            "<p>Price-change emails will arrive the same way.</p>"
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
    """Build the daily digest email payload. Returns None if nothing to report."""
    from price_watcher.digest import SiteDigest  # lazy to avoid circular import
    digests: list[SiteDigest] = list(digests)
    if not digests:
        return None

    count_with_promo = sum(1 for d in digests if d.status == "ok" and d.promo and d.promo.discount_pct > 0)
    if count_with_promo > 0:
        subject = f"Daily sales digest: {count_with_promo} site{'s' if count_with_promo != 1 else ''} with active promos"
    else:
        subject = "Daily sales digest: no active promos today"

    text_lines = ["Daily sales digest", "=" * 40, ""]
    html_rows = []
    for d in digests:
        if d.status == "ok" and d.promo and d.promo.discount_pct > 0:
            pct = d.promo.discount_pct
            code_part = f" with code {d.promo.code}" if d.promo.code else ""
            cond_part = f" ({d.promo.condition})" if d.promo.condition else ""
            text_lines.append(f"• {d.display_name}: {pct}% off{code_part}{cond_part}")
            html_rows.append(
                f'<tr><td style="padding:6px 12px"><b>{escape(d.display_name)}</b></td>'
                f'<td style="padding:6px 12px">{pct}% off'
                + (f' with code <code>{escape(d.promo.code)}</code>' if d.promo.code else '')
                + (f'<br><span style="color:#666;font-size:13px">{escape(d.promo.condition or "")}</span>' if d.promo.condition else '')
                + '</td></tr>'
            )
        elif d.status == "ok" and d.promo and d.promo.discount_pct == 0:
            text_lines.append(f"• {d.display_name}: tiered promo ({d.promo.condition or 'up-to'})")
            html_rows.append(
                f'<tr><td style="padding:6px 12px"><b>{escape(d.display_name)}</b></td>'
                f'<td style="padding:6px 12px;color:#666">tiered promo (up-to, not computable)</td></tr>'
            )
        elif d.status == "no_banner":
            text_lines.append(f"• {d.display_name}: no site-wide promo detected")
            html_rows.append(
                f'<tr><td style="padding:6px 12px"><b>{escape(d.display_name)}</b></td>'
                f'<td style="padding:6px 12px;color:#999">no site-wide promo detected</td></tr>'
            )
        else:
            text_lines.append(f"• {d.display_name}: {d.status} ({d.error or '—'})")
            html_rows.append(
                f'<tr><td style="padding:6px 12px"><b>{escape(d.display_name)}</b></td>'
                f'<td style="padding:6px 12px;color:#c33">{escape(d.status)}: {escape(d.error or "—")}</td></tr>'
            )

    text_body = "\n".join(text_lines) + "\n\n— Price Watcher\n"
    html_body = (
        '<!DOCTYPE html><html><head><meta charset="utf-8"></head>'
        '<body style="font-family:sans-serif;padding:16px;max-width:600px">'
        '<h2>Daily sales digest</h2>'
        '<table style="border-collapse:collapse;width:100%">'
        + "".join(html_rows)
        + '</table>'
        '<p style="color:#999;font-size:12px;margin-top:24px">— Price Watcher</p>'
        '</body></html>'
    )
    return EmailPayload(subject=subject, text_body=text_body, html_body=html_body)


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
