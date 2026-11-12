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
    """Internal (English) direction key — see _DIRECTION_HE for display."""
    if pct is None:
        return "changed"
    return "dropped" if pct < 0 else "rose"


_DIRECTION_HE = {"dropped": "ירד", "rose": "עלה", "changed": "השתנה"}

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


# ---------------------------------------------------------------------------
# Subject builders
# ---------------------------------------------------------------------------

def _subject_single(event: ChangeEvent) -> str:
    label = _truncate(_watch_label(event))
    sym = _currency_symbol(event.fetch.currency)

    if event.type == ChangeType.PRICE_CHANGED:
        old = f"{event.old_price:,.0f}" if event.old_price is not None else "?"
        new = f"{event.new_price:,.0f}" if event.new_price is not None else "?"
        pct = f" ({event.pct_change:+.1f}%)" if event.pct_change is not None else ""
        return f"המחיר {_direction_he(event.pct_change)}: {label} – מ-{old} ל-{new} {sym}{pct}"

    if event.type == ChangeType.STOCK_CHANGED:
        return f"עדכון מלאי: {label} – {_stock_he(event.fetch.in_stock)}"

    if event.type == ChangeType.PROMO_CHANGED:
        return f"המבצע השתנה: {label}"

    if event.type == ChangeType.THRESHOLD_FAILURE:
        return f"המעקב הפסיק לעבוד: {label}"

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
# Body builders (plain text)
# ---------------------------------------------------------------------------

def _text_event(event: ChangeEvent) -> str:
    label = _watch_label(event)
    sym = _currency_symbol(event.fetch.currency)
    url = event.watch.url
    ts = event.timestamp.strftime("%Y-%m-%d %H:%M")
    lines: list[str] = []

    if event.type == ChangeType.PRICE_CHANGED:
        old = _fmt_price(event.old_price, sym)
        new = _fmt_price(event.new_price, sym)
        diff_str = ""
        if event.diff is not None and event.pct_change is not None:
            diff_str = f"  ({event.diff:+,.2f} {sym}, {event.pct_change:+.1f}%)"
        lines += [
            f"המחיר {_direction_he(event.pct_change)}: {label}",
            f"  מחיר קודם: {old}",
            f"  מחיר חדש: {new}{diff_str}",
            f"  קישור: {url}",
            f"  נבדק: {ts}",
        ]

    elif event.type == ChangeType.STOCK_CHANGED:
        price_str = _fmt_price(event.new_price or event.fetch.price, sym)
        lines += [
            f"עדכון מלאי: {_stock_he(event.fetch.in_stock)}",
            f"  מוצר: {label}",
            f"  מחיר נוכחי: {price_str}",
            f"  קישור: {url}",
        ]

    elif event.type == ChangeType.PROMO_CHANGED:
        promo = event.fetch.promo
        displayed = _fmt_price(event.fetch.price, sym)
        estimated = _fmt_price(event.fetch.estimated_final_price, sym)
        lines += [f"המבצע השתנה: {label}"]
        if promo:
            if promo.code:
                lines.append(f"  קוד: {promo.code}  ({promo.discount_pct}% הנחה)")
            if promo.condition:
                lines.append(f"  תנאים: {promo.condition}")
        lines += [
            f"  מחיר מוצג: {displayed}",
            f"  מחיר סופי משוער: {estimated}",
            f"  קישור: {url}",
        ]

    elif event.type == ChangeType.THRESHOLD_FAILURE:
        reason = event.reason or "שגיאה לא ידועה"
        lines += [
            f"המעקב הפסיק לעבוד: {label}",
            f"  סיבה: {reason}",
            "  ניסיונות חוזרים יימשכו ברקע עד שהמעקב יתאושש.",
            f"  קישור: {url}",
        ]

    elif event.type == ChangeType.RECOVERED:
        price_str = _fmt_price(event.new_price or event.fetch.price, sym)
        lines += [
            f"המעקב חזר לעבוד: {label}",
            f"  מחיר נוכחי: {price_str}",
            f"  קישור: {url}",
        ]

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

    if event.type == ChangeType.PRICE_CHANGED:
        direction = _direction_word(event.pct_change)
        color = "#c00" if direction == "rose" else "#080"
        old = _fmt_price(event.old_price, sym)
        new = _fmt_price(event.new_price, sym)
        diff_str = ""
        if event.diff is not None and event.pct_change is not None:
            diff_str = f"{event.diff:+,.2f} {sym}, {event.pct_change:+.1f}%"
        heading = (
            f'המחיר <span style="color:{color}">{_DIRECTION_HE[direction]}</span>: {label}'
        )
        rows += [
            row("מחיר קודם", _ltr(old)),
            row("מחיר חדש", f'<strong style="color:{color}">{_ltr(new)}</strong>'),
            row("שינוי", _ltr(diff_str)),
            row("נבדק", _ltr(ts)),
            row("קישור", link),
        ]

    elif event.type == ChangeType.STOCK_CHANGED:
        color = "#080" if event.fetch.in_stock else "#c00"
        price_str = _fmt_price(event.new_price or event.fetch.price, sym)
        heading = f'<span style="color:{color}">{_stock_he(event.fetch.in_stock)}</span>: {label}'
        rows += [
            row("מחיר נוכחי", _ltr(price_str)),
            row("קישור", link),
        ]

    elif event.type == ChangeType.PROMO_CHANGED:
        promo = event.fetch.promo
        displayed = _fmt_price(event.fetch.price, sym)
        estimated = _fmt_price(event.fetch.estimated_final_price, sym)
        heading = f"המבצע השתנה: {label}"
        if promo and promo.code:
            rows.append(row(
                "קוד",
                f'<strong dir="ltr">{escape(promo.code)}</strong> '
                f'({escape(str(promo.discount_pct))}% הנחה)',
            ))
        if promo and promo.condition:
            rows.append(row("תנאים", escape(promo.condition)))
        rows += [
            row("מחיר מוצג", _ltr(displayed)),
            row("מחיר סופי משוער", f'<strong>{_ltr(estimated)}</strong> <em>(הערכה)</em>'),
            row("קישור", link),
        ]

    elif event.type == ChangeType.THRESHOLD_FAILURE:
        reason = escape(event.reason or "שגיאה לא ידועה")
        heading = f'המעקב הפסיק לעבוד: {label}'
        rows += [
            row("סיבה", reason),
            row("הערה", "ניסיונות חוזרים יימשכו ברקע עד שהמעקב יתאושש."),
            row("קישור", link),
        ]

    elif event.type == ChangeType.RECOVERED:
        price_str = _fmt_price(event.new_price or event.fetch.price, sym)
        heading = f'המעקב חזר לעבוד: {label}'
        rows += [
            row("מחיר נוכחי", _ltr(price_str)),
            row("קישור", link),
        ]
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


def _send_via_smtp(
    payload: EmailPayload,
    cfg: "config.EmailConfig",  # type: ignore[name-defined]
    to: list[str] | tuple[str, ...] | None = None,
) -> None:
    """Low-level SMTP send. Raises on any failure.

    `to` overrides the default recipient (PW_EMAIL_TO) for this message.
    """
    msg = EmailMessage()
    msg["Subject"] = payload.subject
    msg["From"] = cfg.email_from
    msg["To"] = ", ".join(to) if to else cfg.email_to
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


def _group_meat_by_category(sm):
    """Returns: dict[category_name -> list[MeatPromo]], ordered by _CATEGORIES."""
    from price_watcher.supermarkets import _categorize, _CATEGORIES
    groups: dict[str, list] = {}
    for p in sm.promos:
        cat = _categorize(p)
        groups.setdefault(cat, []).append(p)
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
    (("עוף", "כנפיים", "ירכיים", "שוקיים", "פולקע", "פרגיות", "קורקבן"), "🍗"),
    (("כבש", "טלה"), "🐑"),
    (("עגל", "בייבי"), "🐄"),
    (("לשון",), "👅"),
    (("מוח",), "🧠"),
)

def _region_order() -> tuple[str, ...]:
    from price_watcher.supermarkets import _REGION_ORDER
    return _REGION_ORDER


def _group_meat_by_region(sm) -> dict[str, list]:
    """region -> its deals, already sorted best-first by the scanner."""
    groups: dict[str, list] = {region: [] for region in _region_order()}
    for p in sm.promos:
        groups.setdefault(getattr(p, "region", "") or "אחר", []).append(p)
    return {region: items for region, items in groups.items() if items}


def _where_label(p) -> str:
    """Cities (and store count) a deal was seen in, e.g. 'נתניה, רעננה · 5 סניפים'."""
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


def _price_cell_html(p) -> str:
    """Render the before/after price cell: strike-through old, bold new."""
    if p.regular_price and p.discounted_price:
        return (
            f'<span style="color:{_COLOR_MUTED};text-decoration:line-through">{_shekel(p.regular_price)}</span>'
            f'<br><b style="color:{_COLOR_TEXT}">{_shekel(p.discounted_price)}</b>'
        )
    if p.discounted_price:
        return f'<b>{_shekel(p.discounted_price)}</b>'
    if p.regular_price:
        return f'<span style="color:{_COLOR_MUTED}">{_shekel(p.regular_price)}</span>'
    return "—"


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


def _supermarkets_section_text(sm) -> list[str]:
    from price_watcher.supermarkets import _categorize
    lines = ["מבצעי בשר בסופרמרקטים", "-" * 50, ""]
    if sm is None:
        lines.append("  (לא נסרק)")
        return lines
    if sm.status != "ok":
        lines.append(f"  הסריקה נכשלה: {_status_he(sm.status)}  {sm.error or ''}")
        return lines
    chains = ", ".join(sm.chains_scanned) if sm.chains_scanned else "אין"
    lines.append(f"  רשתות שנסרקו: {chains}")
    lines.append(f"  סניפים שנסרקו: {len(getattr(sm, 'stores_scanned', None) or [])}")
    if not sm.promos:
        lines.append("  לא נמצאו היום מבצעי בשר.")
        return lines
    stores_by_region = getattr(sm, "stores_by_region", None) or {}
    promos_by_region = getattr(sm, "promos_by_region", None) or {}
    for region, items in _group_meat_by_region(sm).items():
        lines.append("")
        lines.append(f"  ▼ {region}")
        lines.append(
            f"    {stores_by_region.get(region, 0)} סניפים · "
            f"{len(items)} המבצעים הטובים ביותר מתוך {promos_by_region.get(region, len(items))}"
        )
        for i, p in enumerate(items, 1):
            pct = f"{int(p.savings_pct)}%" if p.savings_pct is not None else "—"
            was = f"₪{p.regular_price}" if p.regular_price else "—"
            now = f"₪{p.discounted_price}" if p.discounted_price else "—"
            lines.append(f"    {i}. הנחה {pct} · {p.chain} · {p.item_name[:50]}")
            lines.append(f"       מחיר רגיל {was} · מחיר מבצע {now} · {_categorize(p)}")
            if p.promotion_description:
                lines.append(f"       {p.promotion_description[:90]}")
            where = _where_label(p)
            if where:
                lines.append(f"       {where}")
    return lines


def _supermarkets_section_html(sm) -> str:
    """Meat section: one card per region, deals ranked best to worst. RTL."""
    from price_watcher.supermarkets import _categorize
    if sm is None:
        return '<div style="color:#888;font-style:italic;text-align:right">(לא נסרק)</div>'
    if sm.status != "ok":
        return _failure_box_html("סריקת הסופרמרקטים נכשלה", sm.status, sm.error)

    # Summary card
    chains_scanned = sm.chains_scanned or []
    stores_scanned = list(getattr(sm, "stores_scanned", None) or [])
    stores_by_region = getattr(sm, "stores_by_region", None) or {}
    promos_by_region = getattr(sm, "promos_by_region", None) or {}
    grouped = _group_meat_by_region(sm)

    html = []
    html.append(
        f'<div style="padding:14px 16px;background:{_COLOR_BG_SOFT};'
        f'border:1px solid {_COLOR_BORDER};border-radius:10px;margin-bottom:18px;'
        f'font-family:{_FONT_STACK};color:{_COLOR_TEXT};text-align:right">'
        f'<div style="font-size:13px;color:{_COLOR_MUTED}">הסריקה של היום</div>'
        f'<div style="font-size:15px;margin-top:4px">'
        f'<b>{len(chains_scanned)}</b> רשתות · '
        f'<b>{len(stores_scanned)}</b> סניפים · '
        f'<b>{sum(promos_by_region.values()) or len(sm.promos)}</b> מבצעי בשר</div>'
        f'<div style="font-size:12px;color:{_COLOR_MUTED};margin-top:6px">'
        f'רשתות: ' + " · ".join(escape(c) for c in chains_scanned)
        + '</div>'
        f'<div style="font-size:12px;color:{_COLOR_MUTED};margin-top:2px">'
        f'בכל אזור מוצגים המבצעים הטובים ביותר, מההנחה הגבוהה לנמוכה.'
        f'</div></div>'
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

    # One card per region, best deal first.
    for region, items in grouped.items():
        rows_html = []
        for i, p in enumerate(items):
            is_best = i == 0
            row_bg = "#ecfdf5" if is_best else ("#ffffff" if i % 2 == 0 else _COLOR_BG_SOFT)
            pct_txt = f"{int(p.savings_pct)}%" if p.savings_pct is not None else "—"
            best_badge = (
                f'<div><span style="display:inline-block;background:{_COLOR_ACCENT};'
                f'color:#fff;font-size:10px;font-weight:700;padding:2px 6px;'
                f'border-radius:4px;margin-top:3px">הכי משתלם</span></div>'
                if is_best else ""
            )
            category = _categorize(p)
            where = _where_label(p)
            rows_html.append(
                f'<tr style="background:{row_bg}">'
                # Rank
                f'<td style="padding:8px 6px 8px 0;font-size:12px;color:{_COLOR_MUTED};'
                f'text-align:center;{cell}">{i + 1}</td>'
                # Chain column
                f'<td style="padding:8px 10px;font-size:12px;color:{_COLOR_LINK};'
                f'font-weight:600;{cell};text-align:right">{escape(p.chain)}'
                + (f'<div style="font-size:11px;font-weight:400;color:{_COLOR_MUTED};'
                   f'margin-top:2px">{escape(where)}</div>' if where else '')
                + best_badge
                + '</td>'
                # Item name
                f'<td style="padding:8px 10px;font-size:13px;color:{_COLOR_TEXT};'
                f'{cell};text-align:right">'
                f'{escape(p.item_name[:60])}'
                + (f'<div style="font-size:11px;color:{_COLOR_MUTED};margin-top:2px">'
                   f'{escape(p.promotion_description[:70])}</div>'
                   if p.promotion_description else '')
                + f'<div style="font-size:11px;color:{_COLOR_MUTED};margin-top:2px">'
                  f'{_category_icon(category)} {escape(category)}</div>'
                + '</td>'
                # Price (was / now)
                f'<td style="padding:8px 10px;font-size:12px;text-align:left;'
                f'white-space:nowrap;{cell}">{_price_cell_html(p)}</td>'
                # Discount %
                f'<td style="padding:8px 10px;font-size:14px;font-weight:700;'
                f'color:{_COLOR_DANGER};text-align:left;white-space:nowrap;{cell}">'
                f'<span dir="ltr">-{pct_txt}</span></td>'
                f'</tr>'
            )

        total_in_region = promos_by_region.get(region, len(items))
        shown_txt = (
            f'{len(items)} המבצעים הטובים ביותר מתוך {total_in_region}'
            if total_in_region > len(items) else f'{len(items)} מבצעים'
        )
        html.append(
            f'<div style="margin:0 0 20px;background:#ffffff;border:1px solid {_COLOR_BORDER};'
            f'border-radius:10px;overflow:hidden;font-family:{_FONT_STACK}">'
            # Header strip
            f'<div style="padding:10px 14px;background:{_COLOR_BG_SOFT};'
            f'border-bottom:1px solid {_COLOR_BORDER};text-align:right">'
            f'<div style="font-size:15px;font-weight:700;color:{_COLOR_TEXT}">'
            f'📍 {escape(region)}</div>'
            f'<div style="font-size:11px;color:{_COLOR_MUTED};margin-top:2px">'
            f'{stores_by_region.get(region, 0)} סניפים נסרקו · {shown_txt}'
            f'</div></div>'
            # Table header + body
            '<table dir="rtl" style="border-collapse:collapse;width:100%">'
            '<thead><tr>'
            f'<th style="{th_style};text-align:center">#</th>'
            f'<th style="{th_style};text-align:right">רשת</th>'
            f'<th style="{th_style};text-align:right">מוצר</th>'
            f'<th style="{th_style};text-align:left">מחיר</th>'
            f'<th style="{th_style};text-align:left">הנחה</th>'
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


# ---------------------------------------------------------------------------
# Vacation deals (section of the daily report + standalone deals-only email)
# ---------------------------------------------------------------------------

_VACATION_TITLE = "דילים לחופשות"


def _deal_price(d) -> str:
    sym = _currency_symbol(d.currency)
    return f"{sym}{d.price}"


def _deal_dates(d) -> str:
    if not d.date_from:
        return ""
    return f"{d.date_from:%d/%m}" + (f" – {d.date_to:%d/%m}" if d.date_to else "")


def _deal_where(d) -> str:
    return d.destination + (f", {d.country}" if d.country else "")


def _deal_details(d) -> str:
    return " · ".join(x for x in (d.hotel, d.note) if x)


def _deal_total(d) -> str:
    """Whole-package price for the party the site priced, e.g. '$1,216'."""
    return f"{_currency_symbol(d.currency)}{d.total:,}"


def _party_he(d) -> str:
    """Who the whole-package price covers: 'לזוג', 'ל-2 מבוגרים + 2 ילדים'…"""
    if not d.party_size:
        return ""
    if d.adults == 2 and not d.children:
        return "לזוג"
    parts = []
    if d.adults:
        parts.append("למבוגר אחד" if d.adults == 1 else f"ל-{d.adults} מבוגרים")
    if d.children:
        kids = "ילד אחד" if d.children == 1 else f"{d.children} ילדים"
        parts.append(kids if parts else f"ל-{kids}")
    return " + ".join(parts)


def _vacation_groups(vac) -> list[tuple[str, str, list]]:
    return [
        ("🏖️", "חבילות נופש (טיסה + מלון)", list(vac.packages)),
        ("✈️", "טיסות", list(vac.flights)),
    ]


# Category marks on the deal tables: (label, badge colour, row tint, meaning).
_BADGE_CHEAPEST = ("הכי זול לאדם", "#059669", "#ecfdf5", "המחיר הנמוך ביותר לאדם")
_BADGE_VALUE = ("הכי משתלם", "#2563eb", "#eff6ff", "המחיר הנמוך ביותר לאדם ללילה")
_BADGE_PACKAGE = ("החבילה הכי זולה", "#7c3aed", "#f5f3ff", "מחיר החבילה הכולל הנמוך ביותר")
_BADGE_LONGEST = ("הכי ארוך", "#d97706", "#fffbeb", "הכי הרבה לילות")
_BADGE_SHORTEST = ("הכי קצר", "#0891b2", "#ecfeff", "הכי מעט לילות")


def _vacation_badge_set(is_packages: bool) -> list[tuple[str, str, str, str]]:
    """Categories that apply to a table (per-night value and package total need a hotel stay)."""
    if is_packages:
        return [_BADGE_CHEAPEST, _BADGE_VALUE, _BADGE_PACKAGE, _BADGE_LONGEST, _BADGE_SHORTEST]
    return [_BADGE_CHEAPEST, _BADGE_LONGEST, _BADGE_SHORTEST]


def _per_night(d) -> Decimal | None:
    """Price per person per night, or None when the length of stay is unknown."""
    nights = d.nights_or_computed
    return d.price / nights if nights else None


def _deal_badges(deals: list, is_packages: bool) -> dict[int, list[tuple[str, str, str, str]]]:
    """Row index -> category marks, judged among the rows shown in the table.

    Ties go to the row that appears first. Prices are only compared within the
    table's main currency.
    """
    marks: dict[int, list[tuple[str, str, str, str]]] = {}
    if not deals:
        return marks
    currency = deals[0].currency

    def _money(value_of):
        return lambda d: value_of(d) if d.currency == currency else None

    def _mark(badge, value_of, highest: bool = False) -> None:
        scored = [(value_of(d), i) for i, d in enumerate(deals)]
        scored = [(v, i) for v, i in scored if v is not None]
        if not scored:
            return
        winner = max(scored, key=lambda s: (s[0], -s[1])) if highest else min(scored)
        marks.setdefault(winner[1], []).append(badge)

    _mark(_BADGE_CHEAPEST, _money(lambda d: d.price))
    if is_packages:
        _mark(_BADGE_VALUE, _money(_per_night))
        _mark(_BADGE_PACKAGE, _money(lambda d: d.total))
    lengths = {d.nights_or_computed for d in deals if d.nights_or_computed}
    if len(lengths) > 1:  # no "longest"/"shortest" when every row is the same length
        _mark(_BADGE_LONGEST, lambda d: d.nights_or_computed or None, highest=True)
        _mark(_BADGE_SHORTEST, lambda d: d.nights_or_computed or None)
    return marks


def _vacation_blurb(vac) -> str:
    """Short 'from $X' summary used in subjects."""
    if vac is None or vac.status != "ok":
        return ""
    parts = []
    if vac.packages:
        parts.append(f"חבילות מ-{_deal_price(vac.packages[0])}")
    if vac.flights:
        parts.append(f"טיסות מ-{_deal_price(vac.flights[0])}")
    return " · ".join(parts)


def _vacations_section_text(vac) -> list[str]:
    lines = [_VACATION_TITLE, "-" * 50, ""]
    if vac is None:
        lines.append("  (לא נסרק)")
        return lines
    if vac.status != "ok":
        lines.append(f"  הסריקה נכשלה: {_status_he(vac.status)}  {vac.error or ''}")
        return lines
    ok = [s.name for s in vac.sources if s.ok and s.count]
    failed = [s.name for s in vac.sources if not s.ok]
    lines.append(f"  אתרים שנסרקו: {', '.join(ok) or 'אין'}")
    if failed:
        lines.append(f"  לא זמינים היום: {', '.join(failed)}")
    if getattr(vac, "days_ahead", 0):
        lines.append(f"  רק יציאות ב-{vac.days_ahead} הימים הקרובים.")
    lines.append(
        f"  נמצאו {vac.total_found} דילים. מחיר לאדם = לכל החופשה (לא ללילה); "
        f"מחיר החבילה = הסכום לכל הנוסעים שהאתר תמחר."
    )
    for _icon, title, deals in _vacation_groups(vac):
        lines.append("")
        lines.append(f"  ▼ {title}")
        if not deals:
            lines.append("    לא נמצאו היום.")
            continue
        badges = _deal_badges(deals, is_packages=title.startswith("חבילות"))
        for i, d in enumerate(deals, 1):
            nights = d.nights_or_computed
            when = _deal_dates(d) + (f" ({nights} לילות)" if nights else "")
            tags = " · ".join(b[0] for b in badges.get(i - 1, []))
            lines.append(
                f"    {i}. {_deal_price(d)} לאדם · {_deal_where(d)} · {when} · {d.source}"
                + (f"  [{tags}]" if tags else "")
            )
            if d.total is not None:
                lines.append(f"       מחיר החבילה: {_deal_total(d)} {_party_he(d)}".rstrip())
            if d.includes:
                lines.append(f"       כולל: {' · '.join(d.includes)[:200]}")
            else:
                details = _deal_details(d)
                if details:
                    lines.append(f"       {details[:110]}")
            lines.append(f"       {d.url}")
    return lines


def _vacations_section_html(vac) -> str:
    """Vacation deals: one card for packages, one for flights, cheapest first. RTL."""
    if vac is None:
        return '<div style="color:#888;font-style:italic;text-align:right">(לא נסרק)</div>'
    if vac.status != "ok":
        return _failure_box_html("סריקת הדילים לחופשות נכשלה", vac.status, vac.error)

    ok = [s.name for s in vac.sources if s.ok and s.count]
    failed = [s.name for s in vac.sources if not s.ok]
    html = [
        f'<div style="padding:14px 16px;background:{_COLOR_BG_SOFT};'
        f'border:1px solid {_COLOR_BORDER};border-radius:10px;margin-bottom:18px;'
        f'font-family:{_FONT_STACK};color:{_COLOR_TEXT};text-align:right">'
        f'<div style="font-size:13px;color:{_COLOR_MUTED}">הסריקה של היום</div>'
        f'<div style="font-size:15px;margin-top:4px">'
        f'<b>{len(ok)}</b> אתרים · <b>{vac.total_found}</b> דילים'
        + (f' · יציאה ב-<b>{vac.days_ahead}</b> הימים הקרובים'
           if getattr(vac, "days_ahead", 0) else '')
        + '</div>'
        f'<div style="font-size:12px;color:{_COLOR_MUTED};margin-top:6px">'
        f'אתרים: ' + " · ".join(escape(n) for n in ok) + '</div>'
        + (f'<div style="font-size:12px;color:#991b1b;margin-top:2px">'
           f'לא זמינים היום: ' + " · ".join(escape(n) for n in failed) + '</div>'
           if failed else '')
        + f'<div style="font-size:12px;color:{_COLOR_MUTED};margin-top:2px">'
        f'מוצגים הדילים הזולים ביותר, מהזול ליקר. "מחיר לאדם" הוא לכל החופשה (לא ללילה), '
        f'כפי שמופיע באתר. "מחיר החבילה" הוא הסכום לכל הנוסעים שהאתר תמחר. '
        f'"מה כלול" מציג רק את מה שהאתר מפרט.'
        f'</div></div>'
    ]

    th_style = (
        f'padding:6px 10px;font-size:11px;font-weight:600;color:{_COLOR_MUTED};'
        f'letter-spacing:0.5px'
    )
    cell = f'border-top:1px solid {_COLOR_BORDER};vertical-align:top'

    for icon, title, deals in _vacation_groups(vac):
        is_packages = deals is not None and title.startswith("חבילות")
        if not deals:
            body = (
                f'<div style="padding:12px 14px;color:#888;font-style:italic;'
                f'text-align:right">לא נמצאו היום.</div>'
            )
        else:
            rows = []
            badges = _deal_badges(deals, is_packages)
            for i, d in enumerate(deals):
                row_badges = badges.get(i, [])
                row_bg = (
                    row_badges[0][2] if row_badges
                    else ("#ffffff" if i % 2 == 0 else _COLOR_BG_SOFT)
                )
                nights = d.nights_or_computed
                details = _deal_details(d)
                best_badge = "".join(
                    f'<div><span style="display:inline-block;background:{color};'
                    f'color:#fff;font-size:10px;font-weight:700;padding:2px 6px;'
                    f'border-radius:4px;margin-top:3px">{escape(label)}</span></div>'
                    for label, color, _tint, _meaning in row_badges
                )
                per_night = _per_night(d) if is_packages else None
                pad = "padding:8px 6px"
                source_link = (
                    f'<a href="{escape(d.url)}" style="color:{_COLOR_LINK};font-weight:600;'
                    f'text-decoration:none">{escape(d.source)}</a>'
                )
                cells = [
                    f'<td style="padding:8px 4px 8px 0;font-size:12px;color:{_COLOR_MUTED};'
                    f'text-align:center;{cell}">{i + 1}</td>',
                    # Destination. Packages: site link underneath. Flights: details underneath.
                    f'<td style="{pad};font-size:13px;color:{_COLOR_TEXT};'
                    f'{cell};text-align:right"><b>{escape(_deal_where(d))}</b>'
                    + (f'<div style="font-size:12px;margin-top:2px">{source_link}</div>'
                       if is_packages else
                       (f'<div style="font-size:11px;color:{_COLOR_MUTED};margin-top:2px">'
                        f'{escape(details[:90])}</div>' if details else ''))
                    + best_badge + '</td>',
                    # Dates
                    f'<td style="{pad};font-size:12px;color:{_COLOR_TEXT};'
                    f'white-space:nowrap;{cell};text-align:right">{_ltr(_deal_dates(d) or "—")}'
                    + (f'<div style="font-size:11px;color:{_COLOR_MUTED};margin-top:2px">'
                       f'{nights} לילות</div>' if nights else '')
                    + '</td>',
                ]
                if is_packages:
                    # What the package includes
                    cells.append(
                        f'<td style="{pad};font-size:11px;line-height:1.5;'
                        f'color:{_COLOR_TEXT};{cell};text-align:right">'
                        + ("".join(f'<div>• {escape(x[:80])}</div>' for x in d.includes) or "—")
                        + '</td>'
                    )
                else:
                    cells.append(
                        f'<td style="{pad};font-size:12px;{cell};text-align:right">'
                        f'{source_link}</td>'
                    )
                # Price per person
                cells.append(
                    f'<td style="{pad};font-size:14px;font-weight:700;'
                    f'color:{_COLOR_TEXT};text-align:left;white-space:nowrap;{cell}">'
                    f'{_ltr(_deal_price(d))}'
                    + (f'<div style="font-size:11px;font-weight:400;color:{_COLOR_MUTED};'
                       f'margin-top:2px">{_ltr(f"{_currency_symbol(d.currency)}{per_night:,.0f}")}'
                       f' ללילה</div>' if per_night is not None else '')
                    + '</td>'
                )
                if is_packages:
                    # Whole-package price for the party the site priced
                    cells.append(
                        f'<td style="{pad};font-size:14px;font-weight:700;'
                        f'color:{_COLOR_ACCENT};text-align:left;{cell}">'
                        + (f'{_ltr(_deal_total(d))}'
                           f'<div style="font-size:11px;font-weight:400;color:{_COLOR_MUTED};'
                           f'margin-top:2px">{escape(_party_he(d))}</div>'
                           if d.total is not None else "—")
                        + '</td>'
                    )
                rows.append(f'<tr style="background:{row_bg}">' + "".join(cells) + '</tr>')
            th = th_style.replace("padding:6px 10px", "padding:6px")
            headers = [
                f'<th style="{th};text-align:center">#</th>',
                f'<th style="{th};text-align:right">יעד</th>',
                f'<th style="{th};text-align:right">תאריכים</th>',
                f'<th style="{th};text-align:right">{"מה כלול" if is_packages else "אתר"}</th>',
                f'<th style="{th};text-align:left">מחיר לאדם</th>',
            ]
            if is_packages:
                headers.append(f'<th style="{th};text-align:left">מחיר החבילה</th>')
            body = (
                '<table dir="rtl" style="border-collapse:collapse;width:100%">'
                '<thead><tr>' + "".join(headers) + '</tr></thead>'
                '<tbody>' + "".join(rows) + '</tbody></table>'
            )
        html.append(
            f'<div style="margin:0 0 20px;background:#ffffff;border:1px solid {_COLOR_BORDER};'
            f'border-radius:10px;overflow:hidden;font-family:{_FONT_STACK}">'
            f'<div style="padding:10px 14px;background:{_COLOR_BG_SOFT};'
            f'border-bottom:1px solid {_COLOR_BORDER};text-align:right">'
            f'<div style="font-size:15px;font-weight:700;color:{_COLOR_TEXT}">'
            f'{icon} {escape(title)}</div>'
            f'<div style="font-size:11px;color:{_COLOR_MUTED};margin-top:2px">'
            f'{len(deals)} הדילים הזולים ביותר</div>'
            # Legend for the coloured category marks
            + (f'<div style="font-size:11px;color:{_COLOR_MUTED};margin-top:6px;line-height:2">'
               + " &nbsp; ".join(
                   f'<span style="display:inline-block;white-space:nowrap;'
                   f'background:{color};color:#fff;font-size:10px;font-weight:700;'
                   f'padding:1px 6px;border-radius:4px">{escape(label)}</span> '
                   f'{escape(meaning)}'
                   for label, color, _tint, meaning in _vacation_badge_set(is_packages)
               )
               + '<div style="line-height:1.4">הסימונים נבחרים מתוך השורות שבטבלה.</div></div>'
               if deals else '')
            + '</div>'
            + body + '</div>'
        )
    return "".join(html)


def build_deals_payload(vac) -> EmailPayload | None:
    """Standalone deals-only email (for recipients who get nothing else)."""
    if vac is None or vac.status != "ok" or not vac.deals:
        return None
    blurb = _vacation_blurb(vac)
    subject = f"{_VACATION_TITLE}: {blurb}" if blurb else _VACATION_TITLE

    text_lines = _vacations_section_text(vac) + ["", f"— {_SIGNATURE}"]

    _F = _FONT_STACK
    html_body = "".join([
        '<!DOCTYPE html><html dir="rtl" lang="he"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1"></head>',
        f'<body dir="rtl" style="margin:0;padding:0;background:#f3f4f6;font-family:{_F};color:{_COLOR_TEXT}">',
        '<table role="presentation" dir="rtl" style="width:100%;background:#f3f4f6;border-collapse:collapse">',
        '<tr><td style="padding:16px 4px">',
        '<table role="presentation" dir="rtl" style="max-width:720px;margin:0 auto;width:100%;'
        f'background:#ffffff;border-radius:14px;border:1px solid {_COLOR_BORDER};'
        'border-collapse:separate;overflow:hidden">',
        '<tr><td style="padding:20px 12px 8px;text-align:right">',
        f'<div style="font-size:22px;font-weight:700;color:{_COLOR_TEXT};margin-bottom:4px">'
        f'{_VACATION_TITLE}</div>',
        f'<div style="font-size:13px;color:{_COLOR_MUTED}">חבילות נופש וטיסות מישראל · עדכון יומי</div>',
        '</td></tr>',
        '<tr><td style="padding:12px 8px;text-align:right">',
        _vacations_section_html(vac),
        '</td></tr>',
        f'<tr><td style="padding:16px 24px 24px;border-top:1px solid {_COLOR_BORDER};'
        f'background:{_COLOR_BG_SOFT};color:{_COLOR_MUTED};font-size:11px;'
        f'text-align:center">— {_SIGNATURE} · {_VACATION_TITLE}</td></tr>',
        '</table></td></tr></table></body></html>',
    ])
    return EmailPayload(subject=subject, text_body="\n".join(text_lines), html_body=html_body)


def send_deals_email(vac, recipients: list[str] | tuple[str, ...]) -> bool:
    """Send the deals-only email to `recipients`. True if sent or nothing to send."""
    recipients = [r for r in recipients if r]
    payload = build_deals_payload(vac)
    if payload is None:
        log.info("No vacation deals to send")
        return True
    if not recipients:
        log.info("No deals-only recipients configured")
        return True
    cfg = _config_module.load().email
    try:
        _send_via_smtp(payload, cfg, to=recipients)
        log.info("Deals email sent to %d recipient(s)", len(recipients))
        return True
    except (smtplib.SMTPException, OSError) as exc:
        log.error("Deals email send failed: %s", type(exc).__name__)
        return False


def build_daily_payload(report) -> EmailPayload | None:
    """Combine sales digest + creatine + Sims 4 + supermarket meat + vacation deals."""
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
    vac = getattr(report, "vacations", None)
    vacation_blurb = _vacation_blurb(vac)
    if vacation_blurb:
        headline.append(vacation_blurb)
    subject ="דוח יומי: " + (" · ".join(headline) if headline else "אין חדש היום")

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
    text_lines.extend(_vacations_section_text(vac))
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
        f'<div style="font-size:13px;color:{_COLOR_MUTED}">מבצעים באתרים · קריאטין · הסימס 4 · בשר בסופרמרקטים · חופשות</div>',
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

    html_parts.append(_section_heading_html(_VACATION_TITLE))
    html_parts.append(_vacations_section_html(vac))

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
