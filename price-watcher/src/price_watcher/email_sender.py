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
            f"Daily digest: {total} promo{'s' if total != 1 else ''} "
            f"(top: {best_promo.discount_pct}% off at {best_site.display_name}"
            + (f" with {best_promo.code}" if best_promo.code else "")
            + ")"
        )
    else:
        subject = "Daily sales digest: no site-wide promos today"

    # --- Plain text body ---
    text_lines = ["Daily sales digest", "=" * 50, ""]
    for d in digests:
        text_lines.append(f"▼ {d.display_name}  ({d.host})")
        text_lines.append(f"   {d.homepage_url}")
        if d.status != "ok":
            text_lines.append(f"   [{d.status}] {d.error or ''}")
            text_lines.append("")
            continue
        if not d.promos:
            text_lines.append("   (no site-wide promo detected)")
            text_lines.append("")
            continue
        for i, p in enumerate(d.promos, 1):
            code_part = f"code: {p.code}" if p.code else "no code"
            text_lines.append(f"   {i}. {p.discount_pct}% off  —  {code_part}")
            if p.condition:
                text_lines.append(f"      context: {p.condition[:200]}")
        text_lines.append("")
    text_lines.append("— Price Watcher")
    text_body = "\n".join(text_lines)

    # --- HTML body ---
    html_parts = [
        '<!DOCTYPE html><html><head><meta charset="utf-8"></head>',
        '<body style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;'
        'padding:16px;max-width:680px;line-height:1.5">',
        '<h2 style="margin:0 0 20px">Daily sales digest</h2>',
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
                f'<b>{escape(d.status)}:</b> {escape(d.error or "—")}</div>'
            )
        elif not d.promos:
            html_parts.append(
                '<div style="margin-top:8px;color:#888;font-style:italic">'
                'no site-wide promo detected</div>'
            )
        else:
            for p in d.promos:
                code_html = (
                    f'<code style="background:#111;color:#fff;padding:2px 8px;'
                    f'border-radius:4px;font-size:14px;font-weight:600">{escape(p.code)}</code>'
                    if p.code
                    else '<span style="color:#888;font-size:13px">(no code)</span>'
                )
                html_parts.append(
                    f'<div style="margin:10px 0 0;padding-top:10px;border-top:1px solid #eee">'
                    f'<div style="font-size:15px">'
                    f'<b style="color:#dc2626">{p.discount_pct}% off</b>  &nbsp; {code_html}</div>'
                )
                if p.condition:
                    html_parts.append(
                        f'<div style="margin-top:4px;color:#555;font-size:13px">'
                        f'{escape(p.condition[:300])}</div>'
                    )
                html_parts.append('</div>')
        html_parts.append('</div>')
    html_parts.append(
        '<p style="color:#999;font-size:12px;margin-top:24px">— Price Watcher</p>'
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
               "Roboto,Oxygen,Ubuntu,Helvetica,sans-serif")

_CATEGORY_ICON = {
    "Entrecote": "🥩", "Steak": "🥩", "Picanha": "🥩", "Sirloin": "🥩",
    "T-bone": "🥩", "Chuck": "🥩", "Flank": "🥩", "Asado": "🥩",
    "Brisket": "🥩", "Osso buco": "🥩", "Goulash": "🥩", "Shoulder": "🥩",
    "Shank": "🥩", "Ribs": "🥩", "Fillet": "🥩", "Filet": "🥩",
    "Ground beef": "🥩", "Beef roast": "🥩", "Beef liver": "🥩",
    "Beef tongue": "👅", "Brain": "🧠", "Neck": "🥩",
    "Giblets": "🫀", "Baby beef": "🐄", "Veal": "🐄", "Lamb": "🐑",
    "Chicken breast": "🍗", "Chicken wings": "🍗", "Chicken thighs": "🍗",
    "Chicken drumsticks": "🍗", "Chicken liver": "🍗", "Drumsticks": "🍗",
    "Cornish hen": "🍗", "Turkey": "🦃",
    "Chicken": "🍗", "Beef": "🥩", "Other": "•",
}


def _category_icon(cat_name: str) -> str:
    for key, icon in _CATEGORY_ICON.items():
        if key in cat_name:
            return icon
    return "•"


def _price_cell_html(p) -> str:
    """Render the before/after price cell: strike-through old, bold new."""
    if p.regular_price and p.discounted_price:
        return (
            f'<span style="color:{_COLOR_MUTED};text-decoration:line-through">₪{p.regular_price}</span>'
            f'<br><b style="color:{_COLOR_TEXT}">₪{p.discounted_price}</b>'
        )
    if p.discounted_price:
        return f'<b>₪{p.discounted_price}</b>'
    if p.regular_price:
        return f'<span style="color:{_COLOR_MUTED}">₪{p.regular_price}</span>'
    return "—"


def _supermarkets_section_text(sm) -> list[str]:
    lines = ["Supermarket meat discounts", "-" * 50, ""]
    if sm is None:
        lines.append("  (not scanned)")
        return lines
    if sm.status != "ok":
        lines.append(f"  scan failed: {sm.status}  {sm.error or ''}")
        return lines
    chains = ", ".join(sm.chains_scanned) if sm.chains_scanned else "none"
    lines.append(f"  Chains scanned: {chains}")
    if not sm.promos:
        lines.append("  No meat promos found today.")
        return lines
    grouped = _group_meat_by_category(sm)
    for category, items in grouped.items():
        lines.append("")
        lines.append(f"  ▼ {category}")
        # Sort within category: best discount first per chain
        items_sorted = sorted(items, key=lambda p: -(float(p.savings_pct) if p.savings_pct is not None else 0))
        for p in items_sorted:
            pct = f"{p.savings_pct}%" if p.savings_pct is not None else "—"
            was = f"₪{p.regular_price}" if p.regular_price else "—"
            now = f"₪{p.discounted_price}" if p.discounted_price else "—"
            lines.append(f"    [{p.chain:15s}] -{pct:>6}  {was:>8} → {now:>8}  {p.item_name[:50]}")
            if p.promotion_description:
                lines.append(f"       {p.promotion_description[:90]}")
    return lines


def _supermarkets_section_html(sm) -> str:
    """Redesigned meat section: one card per category, proper table with
    header row, best-deal highlighting, side-by-side chain comparison."""
    if sm is None:
        return '<div style="color:#888;font-style:italic">(not scanned)</div>'
    if sm.status != "ok":
        return (
            f'<div style="padding:14px 16px;border:1px solid #fecaca;'
            f'border-radius:10px;background:#fef2f2;color:#991b1b;'
            f'font-family:{_FONT_STACK}">'
            f'<b>Supermarket scan failed:</b> {escape(sm.status)}'
            + (f' — {escape(sm.error)}' if sm.error else '')
            + '</div>'
        )

    # Summary card
    chains_scanned = sm.chains_scanned or []
    total = len(sm.promos)
    grouped = _group_meat_by_category(sm)
    categories_hit = len(grouped)

    html = []
    html.append(
        f'<div style="padding:14px 16px;background:{_COLOR_BG_SOFT};'
        f'border:1px solid {_COLOR_BORDER};border-radius:10px;margin-bottom:18px;'
        f'font-family:{_FONT_STACK};color:{_COLOR_TEXT}">'
        f'<div style="font-size:13px;color:{_COLOR_MUTED}">Today\'s scan</div>'
        f'<div style="font-size:15px;margin-top:4px">'
        f'<b>{len(chains_scanned)}</b> chains · '
        f'<b>{total}</b> meat promos · '
        f'<b>{categories_hit}</b> categories</div>'
        f'<div style="font-size:12px;color:{_COLOR_MUTED};margin-top:6px">'
        + " · ".join(escape(c) for c in chains_scanned)
        + '</div></div>'
    )

    if not sm.promos:
        html.append(
            '<div style="color:#888;font-style:italic;'
            f'font-family:{_FONT_STACK}">No meat promos found today.</div>'
        )
        return "".join(html)

    # One card per category
    for category, items in grouped.items():
        items_sorted = sorted(
            items,
            key=lambda p: -(float(p.savings_pct) if p.savings_pct is not None else 0),
        )
        icon = _category_icon(category)
        best_pct = max(
            (float(p.savings_pct) if p.savings_pct is not None else 0 for p in items_sorted),
            default=0,
        )

        # Build rows
        rows_html = []
        for i, p in enumerate(items_sorted):
            is_best = (
                p.savings_pct is not None
                and float(p.savings_pct) == best_pct
                and best_pct > 0
                and i == 0
            )
            row_bg = "#ecfdf5" if is_best else ("#ffffff" if i % 2 == 0 else _COLOR_BG_SOFT)
            pct_txt = f"{int(p.savings_pct)}%" if p.savings_pct is not None else "—"
            best_badge = (
                f'<span style="display:inline-block;background:{_COLOR_ACCENT};'
                f'color:#fff;font-size:10px;font-weight:700;padding:2px 6px;'
                f'border-radius:4px;margin-right:6px;letter-spacing:0.5px">BEST</span>'
                if is_best else ""
            )
            rows_html.append(
                f'<tr style="background:{row_bg}">'
                # Chain column
                f'<td style="padding:8px 10px;font-size:12px;color:{_COLOR_LINK};'
                f'font-weight:600;white-space:nowrap;border-top:1px solid {_COLOR_BORDER};'
                f'vertical-align:top">{best_badge}{escape(p.chain)}</td>'
                # Item name
                f'<td style="padding:8px 10px;font-size:13px;color:{_COLOR_TEXT};'
                f'border-top:1px solid {_COLOR_BORDER};vertical-align:top">'
                f'{escape(p.item_name[:60])}'
                + (f'<div style="font-size:11px;color:{_COLOR_MUTED};margin-top:2px">'
                   f'{escape(p.promotion_description[:70])}</div>'
                   if p.promotion_description else '')
                + '</td>'
                # Price (was / now)
                f'<td style="padding:8px 10px;font-size:12px;text-align:right;'
                f'white-space:nowrap;border-top:1px solid {_COLOR_BORDER};'
                f'vertical-align:top">{_price_cell_html(p)}</td>'
                # Discount %
                f'<td style="padding:8px 10px;font-size:14px;font-weight:700;'
                f'color:{_COLOR_DANGER};text-align:right;white-space:nowrap;'
                f'border-top:1px solid {_COLOR_BORDER};vertical-align:top">'
                f'-{pct_txt}</td>'
                f'</tr>'
            )

        # Category card
        html.append(
            f'<div style="margin:0 0 20px;background:#ffffff;border:1px solid {_COLOR_BORDER};'
            f'border-radius:10px;overflow:hidden;font-family:{_FONT_STACK}">'
            # Header strip
            f'<div style="padding:10px 14px;background:{_COLOR_BG_SOFT};'
            f'border-bottom:1px solid {_COLOR_BORDER}">'
            f'<div style="font-size:15px;font-weight:700;color:{_COLOR_TEXT}">'
            f'{icon} {escape(category)}</div>'
            f'<div style="font-size:11px;color:{_COLOR_MUTED};margin-top:2px">'
            f'{len(items)} promo{"s" if len(items) != 1 else ""} across chains'
            f'</div></div>'
            # Table header + body
            '<table style="border-collapse:collapse;width:100%">'
            '<thead><tr>'
            f'<th style="padding:6px 10px;text-align:left;font-size:11px;'
            f'font-weight:600;color:{_COLOR_MUTED};text-transform:uppercase;'
            f'letter-spacing:0.5px">Chain</th>'
            f'<th style="padding:6px 10px;text-align:left;font-size:11px;'
            f'font-weight:600;color:{_COLOR_MUTED};text-transform:uppercase;'
            f'letter-spacing:0.5px">Item</th>'
            f'<th style="padding:6px 10px;text-align:right;font-size:11px;'
            f'font-weight:600;color:{_COLOR_MUTED};text-transform:uppercase;'
            f'letter-spacing:0.5px">Price</th>'
            f'<th style="padding:6px 10px;text-align:right;font-size:11px;'
            f'font-weight:600;color:{_COLOR_MUTED};text-transform:uppercase;'
            f'letter-spacing:0.5px">Off</th>'
            '</tr></thead>'
            '<tbody>' + "".join(rows_html) + '</tbody>'
            '</table>'
            '</div>'
        )
    return "".join(html)


def _sims4_section_text(sims4) -> list[str]:
    lines = ["The Sims 4 (EA)", "-" * 50, ""]
    if sims4 is None:
        lines.append("  (not scanned)")
        return lines
    if sims4.status != "ok":
        lines.append(f"  scan failed: {sims4.status}  {sims4.error or ''}")
        return lines
    lines.append(f"  Scanned {len(sims4.packs)} packs from EA.")
    if sims4.newly_on_sale:
        lines.append("")
        lines.append(f"  NEWLY ON SALE ({len(sims4.newly_on_sale)}):")
        for p in sims4.newly_on_sale:
            lines.append(f"    • {p.discount_percent}% off  ${p.price} (was ${p.original_price})  {p.name}")
    if sims4.discount_ended:
        lines.append("")
        lines.append(f"  DISCOUNT ENDED ({len(sims4.discount_ended)}):")
        for p in sims4.discount_ended:
            lines.append(f"    • back to ${p.price}  {p.name}")
    if sims4.discount_changed:
        lines.append("")
        lines.append(f"  DISCOUNT % CHANGED:")
        for p, old in sims4.discount_changed:
            lines.append(f"    • {old}% → {p.discount_percent}%  ${p.price}  {p.name}")
    if sims4.on_sale_now:
        lines.append("")
        lines.append(f"  All current sales ({len(sims4.on_sale_now)}):")
        for p in sorted(sims4.on_sale_now, key=lambda x: -x.discount_percent):
            lines.append(f"    • {p.discount_percent}% off  ${p.price} (was ${p.original_price})  {p.name}")
    elif not sims4.newly_on_sale and not sims4.discount_ended:
        lines.append("")
        lines.append("  No packs currently on sale.")
    return lines


def _sims4_section_html(sims4) -> str:
    if sims4 is None:
        return '<div style="color:#888;font-style:italic">(not scanned)</div>'
    if sims4.status != "ok":
        return (
            '<div style="padding:12px;border:1px solid #fecaca;border-radius:8px;'
            'background:#fef2f2;color:#991b1b">'
            f'<b>Sims 4 scan failed:</b> {escape(sims4.status)}'
            + (f' — {escape(sims4.error)}' if sims4.error else '')
            + '</div>'
        )
    html = ['<div style="margin:0 0 20px">']
    if sims4.newly_on_sale:
        html.append(
            '<div style="margin:0 0 12px;padding:12px 16px;background:#ecfdf5;'
            'border:1px solid #10b981;border-radius:8px">'
            f'<div style="font-weight:600;color:#065f46">🆕 Newly on sale ({len(sims4.newly_on_sale)})</div>'
        )
        for p in sims4.newly_on_sale:
            html.append(
                f'<div style="margin-top:6px"><a href="{escape(p.url)}" '
                f'style="color:#2563eb;text-decoration:none">{escape(p.name)}</a>  '
                f'<b style="color:#065f46">{p.discount_percent}% off</b>  '
                f'<span style="color:#666">${p.price} (was ${p.original_price})</span></div>'
            )
        html.append('</div>')
    if sims4.discount_ended:
        html.append(
            '<div style="margin:0 0 12px;padding:12px 16px;background:#fef2f2;'
            'border:1px solid #fca5a5;border-radius:8px">'
            f'<div style="font-weight:600;color:#991b1b">⚠ Discount ended ({len(sims4.discount_ended)})</div>'
        )
        for p in sims4.discount_ended:
            html.append(
                f'<div style="margin-top:6px"><a href="{escape(p.url)}" '
                f'style="color:#2563eb;text-decoration:none">{escape(p.name)}</a>  '
                f'<span style="color:#991b1b">back to ${p.price}</span></div>'
            )
        html.append('</div>')
    if sims4.discount_changed:
        html.append(
            '<div style="margin:0 0 12px;padding:12px 16px;background:#eff6ff;'
            'border:1px solid #93c5fd;border-radius:8px">'
            f'<div style="font-weight:600;color:#1e40af">🔁 Discount % changed</div>'
        )
        for p, old in sims4.discount_changed:
            html.append(
                f'<div style="margin-top:6px"><a href="{escape(p.url)}" '
                f'style="color:#2563eb;text-decoration:none">{escape(p.name)}</a>  '
                f'<span>{old}% → <b>{p.discount_percent}%</b></span>  '
                f'<span style="color:#666">${p.price}</span></div>'
            )
        html.append('</div>')
    if sims4.on_sale_now:
        rows = "".join(
            f'<tr><td style="padding:4px 10px"><a href="{escape(p.url)}" '
            f'style="color:#2563eb;text-decoration:none">{escape(p.name)}</a></td>'
            f'<td style="padding:4px 10px"><b>{p.discount_percent}% off</b></td>'
            f'<td style="padding:4px 10px">${p.price} <s style="color:#888">${p.original_price}</s></td></tr>'
            for p in sorted(sims4.on_sale_now, key=lambda x: -x.discount_percent)
        )
        html.append(
            f'<div style="margin:0;padding:12px 16px;background:#fafafa;'
            f'border:1px solid #e5e7eb;border-radius:8px">'
            f'<div style="font-weight:600;margin-bottom:6px">All current sales ({len(sims4.on_sale_now)})</div>'
            f'<table style="border-collapse:collapse;font-size:13px;width:100%">{rows}</table>'
            f'</div>'
        )
    elif not sims4.newly_on_sale and not sims4.discount_ended:
        html.append('<div style="color:#888;font-style:italic">No packs currently on sale.</div>')
    html.append('</div>')
    return "".join(html)


def _creatine_section_text(creatine) -> list[str]:
    """Plain-text lines for the creatine section of the daily report."""
    lines = ["Creatine (iHerb)", "-" * 50, ""]
    if creatine is None or creatine.status != "ok":
        lines.append(f"  scan failed: {creatine.status if creatine else 'unavailable'}")
        if creatine and creatine.error:
            lines.append(f"  {creatine.error}")
        return lines
    cur = creatine.cheapest
    sym = _currency_symbol(cur.currency)
    lines.append(f"  Cheapest ₪/100g: {sym}{cur.price_per_100g}")
    lines.append(f"    {cur.name}")
    lines.append(f"    {sym}{cur.price}" + ("  (on sale)" if cur.on_sale else ""))
    lines.append(f"    {cur.url}")
    lines.append("")
    if creatine.previous_cheapest is None:
        lines.append("  (first observation)")
    elif creatine.changed:
        prev = creatine.previous_cheapest
        lines.append(f"  Change: {sym}{prev.price_per_100g}/100g → {sym}{cur.price_per_100g}/100g  ({creatine.direction or 'changed'})")
    else:
        lines.append("  No change since last scan.")
    lines.append("")
    lines.append("  Top 5 by ₪/100g:")
    for i, p in enumerate(creatine.products[:5], 1):
        lines.append(f"    {i}. {sym}{p.price_per_100g}/100g — {p.name[:70]}")
    return lines


def _creatine_section_html(creatine) -> str:
    """HTML section for the creatine part of the daily report."""
    if creatine is None or creatine.status != "ok":
        return (
            '<div style="margin:0 0 24px;padding:14px 16px;border:1px solid #fecaca;'
            'border-radius:8px;background:#fef2f2">'
            f'<div style="color:#991b1b"><b>Creatine scan failed:</b> '
            f'{escape(creatine.status if creatine else "unavailable")}'
            + (f' — {escape(creatine.error)}' if (creatine and creatine.error) else '')
            + '</div></div>'
        )
    cur = creatine.cheapest
    sym = _currency_symbol(cur.currency)

    header_bg = "#ecfdf5"
    header_border = "#10b981"
    change_html = ""
    if creatine.previous_cheapest is None:
        change_html = '<span style="color:#065f46">(first observation)</span>'
    elif creatine.changed:
        prev = creatine.previous_cheapest
        arrow_color = "#065f46" if creatine.direction == "dropped" else "#991b1b"
        change_html = (
            f'<div style="margin-top:6px;color:{arrow_color}"><b>'
            f'{sym}{prev.price_per_100g}/100g → {sym}{cur.price_per_100g}/100g '
            f'({creatine.direction or "changed"})</b></div>'
        )
        if creatine.direction == "rose":
            header_bg = "#fef2f2"
            header_border = "#fca5a5"
    else:
        change_html = '<span style="color:#6b7280">No change since last scan.</span>'

    rows_html = "".join(
        f'<tr><td style="padding:4px 10px;color:#666">{i}.</td>'
        f'<td style="padding:4px 10px"><b>{sym}{p.price_per_100g}</b>/100g</td>'
        f'<td style="padding:4px 10px"><a href="{escape(p.url)}" style="color:#2563eb;text-decoration:none">{escape(p.name[:70])}</a></td></tr>'
        for i, p in enumerate(creatine.products[:5], 1)
    )

    return (
        f'<div style="margin:0 0 24px;padding:14px 16px;border:1px solid {header_border};'
        f'border-radius:8px;background:{header_bg}">'
        f'<div style="font-size:16px;font-weight:600">Creatine (iHerb) · cheapest '
        f'<span style="color:#065f46">{sym}{cur.price_per_100g}/100g</span></div>'
        f'<div style="margin-top:6px">{escape(cur.name)}'
        + (' <span style="background:#dc2626;color:#fff;padding:1px 6px;border-radius:3px;font-size:11px">on sale</span>' if cur.on_sale else '')
        + f'</div>'
        f'<a href="{escape(cur.url)}" style="color:#2563eb;font-size:13px">open at iHerb</a>'
        f'{change_html}'
        f'<div style="margin-top:14px;font-size:13px;color:#555"><b>Top 5 by ₪/100g:</b>'
        f'<table style="border-collapse:collapse;margin-top:4px;width:100%">{rows_html}</table></div>'
        f'</div>'
    )


def build_daily_payload(report) -> EmailPayload | None:
    """Combine sales digest + creatine (+ future daily features) into one email."""
    # Headline figure out: biggest promo across sites + any creatine change
    site_digests = report.site_digests
    all_promos = [(d, p) for d in site_digests for p in d.promos]
    best_promo_site, best_promo = (None, None)
    if all_promos:
        best_promo_site, best_promo = max(all_promos, key=lambda dp: dp[1].discount_pct)

    creatine_blurb = ""
    if report.creatine is not None and report.creatine.status == "ok" and report.creatine.changed:
        cur = report.creatine.cheapest
        creatine_blurb = f"creatine {report.creatine.direction or 'changed'} to {_currency_symbol(cur.currency)}{cur.price_per_100g}/100g"

    if best_promo and creatine_blurb:
        subject = (
            f"Daily digest: top {best_promo.discount_pct}% off at {best_promo_site.display_name}"
            + (f" ({best_promo.code})" if best_promo.code else "")
            + f" · {creatine_blurb}"
        )
    elif best_promo:
        subject = (
            f"Daily digest: top {best_promo.discount_pct}% off at {best_promo_site.display_name}"
            + (f" ({best_promo.code})" if best_promo.code else "")
        )
    elif creatine_blurb:
        subject = f"Daily digest: {creatine_blurb}"
    else:
        subject = "Daily digest: nothing new today"

    # ---- Plain text ----
    text_lines = ["Daily digest", "=" * 50, ""]
    text_lines.append("Sales (site-wide promos)")
    text_lines.append("-" * 50)
    text_lines.append("")
    for d in site_digests:
        text_lines.append(f"▼ {d.display_name}  ({d.host})")
        text_lines.append(f"  {d.homepage_url}")
        if d.status != "ok":
            text_lines.append(f"  [{d.status}] {d.error or ''}")
            text_lines.append("")
            continue
        if not d.promos:
            text_lines.append("  (no site-wide promo detected)")
            text_lines.append("")
            continue
        for i, p in enumerate(d.promos, 1):
            code_part = f"code: {p.code}" if p.code else "no code"
            text_lines.append(f"  {i}. {p.discount_pct}% off  —  {code_part}")
            if p.condition:
                text_lines.append(f"     {p.condition[:200]}")
        text_lines.append("")
    text_lines.extend(_creatine_section_text(report.creatine))
    text_lines.append("")
    text_lines.append("— Price Watcher")
    text_body = "\n".join(text_lines)

    # ---- HTML ----
    _F = _FONT_STACK
    html_parts = [
        '<!DOCTYPE html><html><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '</head>',
        f'<body style="margin:0;padding:0;background:#f3f4f6;font-family:{_F};color:{_COLOR_TEXT}">',
        f'<table role="presentation" style="width:100%;background:#f3f4f6;border-collapse:collapse">',
        f'<tr><td style="padding:24px 12px">',
        f'<table role="presentation" style="max-width:720px;margin:0 auto;width:100%;'
        f'background:#ffffff;border-radius:14px;border:1px solid {_COLOR_BORDER};'
        f'border-collapse:separate;overflow:hidden">',
        f'<tr><td style="padding:24px 24px 8px">',
        f'<div style="font-size:22px;font-weight:700;color:{_COLOR_TEXT};'
        f'margin-bottom:4px">Daily digest</div>',
        f'<div style="font-size:13px;color:{_COLOR_MUTED}">Sales · creatine · Sims 4 · supermarket meat</div>',
        f'</td></tr>',
        f'<tr><td style="padding:16px 24px">',
        f'<h3 style="margin:0 0 12px;font-size:16px;color:{_COLOR_TEXT};'
        f'padding:6px 0 4px;border-bottom:2px solid {_COLOR_TEXT}">Sales — site-wide promos</h3>',
    ]
    for d in site_digests:
        html_parts.append(
            f'<div style="margin:0 0 20px;padding:14px 16px;border:1px solid #e5e7eb;'
            f'border-radius:8px;background:#fafafa">'
            f'<div style="font-size:16px;font-weight:600">{escape(d.display_name)}'
            f' <a href="{escape(d.homepage_url)}" style="font-weight:normal;color:#2563eb;'
            f'text-decoration:none;font-size:13px">({escape(d.host)})</a></div>'
        )
        if d.status != "ok":
            html_parts.append(
                f'<div style="margin-top:8px;color:#c33">'
                f'<b>{escape(d.status)}:</b> {escape(d.error or "—")}</div>'
            )
        elif not d.promos:
            html_parts.append(
                '<div style="margin-top:8px;color:#888;font-style:italic">'
                'no site-wide promo detected</div>'
            )
        else:
            for p in d.promos:
                code_html = (
                    f'<code style="background:#111;color:#fff;padding:2px 8px;'
                    f'border-radius:4px;font-size:14px;font-weight:600">{escape(p.code)}</code>'
                    if p.code
                    else '<span style="color:#888;font-size:13px">(no code)</span>'
                )
                html_parts.append(
                    f'<div style="margin:10px 0 0;padding-top:10px;border-top:1px solid #eee">'
                    f'<div style="font-size:15px">'
                    f'<b style="color:#dc2626">{p.discount_pct}% off</b>  &nbsp; {code_html}</div>'
                )
                if p.condition:
                    html_parts.append(
                        f'<div style="margin-top:4px;color:#555;font-size:13px">'
                        f'{escape(p.condition[:300])}</div>'
                    )
                html_parts.append('</div>')
        html_parts.append('</div>')

    html_parts.append(
        f'<h3 style="margin:28px 0 12px;font-size:16px;color:{_COLOR_TEXT};'
        f'padding:6px 0 4px;border-bottom:2px solid {_COLOR_TEXT}">Creatine</h3>'
    )
    html_parts.append(_creatine_section_html(report.creatine))

    html_parts.append(
        f'<h3 style="margin:28px 0 12px;font-size:16px;color:{_COLOR_TEXT};'
        f'padding:6px 0 4px;border-bottom:2px solid {_COLOR_TEXT}">The Sims 4</h3>'
    )
    html_parts.append(_sims4_section_html(getattr(report, "sims4", None)))

    html_parts.append(
        f'<h3 style="margin:28px 0 12px;font-size:16px;color:{_COLOR_TEXT};'
        f'padding:6px 0 4px;border-bottom:2px solid {_COLOR_TEXT}">Supermarket meat discounts</h3>'
    )
    html_parts.append(_supermarkets_section_html(getattr(report, "supermarkets", None)))

    # Append sims4 + supermarkets text lines
    text_body_addendum = "\n".join(_sims4_section_text(getattr(report, "sims4", None)))
    text_body = text_body + "\n" + text_body_addendum + "\n" + "\n".join(
        _supermarkets_section_text(getattr(report, "supermarkets", None))
    )

    html_parts.append(
        f'</td></tr>'
        f'<tr><td style="padding:16px 24px 24px;border-top:1px solid {_COLOR_BORDER};'
        f'background:{_COLOR_BG_SOFT};color:{_COLOR_MUTED};font-size:11px;'
        f'text-align:center">— Price Watcher · daily report</td></tr>'
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
