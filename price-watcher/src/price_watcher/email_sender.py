"""Email notifications for price / stock / promo changes.

Public API
----------
send_change_notification(events)  -> bool
send_test_email()                 -> None   (raises on failure)
build_payload(events)             -> EmailPayload | None
describe_failure(fetch)           -> dict   (status / detail / hint, Hebrew)
"""

from __future__ import annotations

import smtplib
from decimal import Decimal
from email.message import EmailMessage
from html import escape
from types import SimpleNamespace
from urllib.parse import urlparse

from price_watcher import config as _config_module
from price_watcher import continents as _continents
from price_watcher.contracts import (
    NOTIFIABLE_TYPES,
    ChangeEvent,
    ChangeType,
    EmailPayload,
    FetchStatus,
    PromoInfo,
)
from price_watcher.logging_setup import get as _get_logger

log = _get_logger(__name__)

# Change types that warrant an email.
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
    "parse_failed": "פענוח העמוד נכשל",
    "unavailable": "לא זמין",
    "not_found": "הדף לא נמצא",
    "price_not_found": "לא נמצא מחיר בדף",
    "no_promo": "לא זוהה מבצע",
}

_SIGNATURE = "מעקב מחירים"


def _direction_he(pct: float | None) -> str:
    return _DIRECTION_HE[_direction_word(pct)]


def _status_he(status: str | None) -> str:
    return _STATUS_HE.get(status or "unavailable", status or "")


def failure_detail_he(fetch: "FetchResult") -> str:
    """Hebrew explanation of why a watch's fetch failed (empty if it did not)."""
    from price_watcher.contracts import FetchResult, FetchStatus  # noqa: F401

    msg = (fetch.error_message or "").strip()
    http = f" (HTTP {fetch.http_status})" if fetch.http_status else ""
    if fetch.status == FetchStatus.OK:
        if fetch.price is not None and fetch.price == 0:
            return "המחיר באתר הוצג כ-0 – ייתכן שהמוצר אינו זמין או שהדף השתנה"
        return ""
    if fetch.status == FetchStatus.NOT_FOUND and "variant" in msg.lower():
        return (
            "הווריאציה שנבחרה (מידה/צבע) הוסרה מהמוצר באתר – "
            "מומלץ למחוק את המעקב ולהוסיף אותו מחדש עם הווריאציה הנכונה"
            + (f" ({msg})" if msg else "")
        )
    if fetch.status == FetchStatus.NOT_FOUND:
        base = f"הדף לא נמצא{http} – ייתכן שהמוצר הוסר מהאתר"
    elif fetch.status == FetchStatus.BLOCKED:
        base = f"האתר חסם את הבקשה{http} – כדאי להמתין או להגדיר סלקטור"
    else:
        base = f"{_status_he(fetch.status.value)}{http}"
    return f"{base} ({msg})" if msg else base


def _failure_hint_he(fetch: "FetchResult") -> str | None:
    """What the user can do about a failing watch (None if nothing specific)."""
    msg = (fetch.error_message or "").lower()
    if fetch.status == FetchStatus.NOT_FOUND and "variant" in msg:
        return (
            "המידה / הצבע שבקישור כבר לא קיימים במוצר. יש להסיר את המעקב "
            "ולהוסיף אותו מחדש עם קישור לווריאנט קיים "
            "(price-watcher remove ואז price-watcher add)."
        )
    if fetch.status == FetchStatus.NOT_FOUND:
        return (
            "ייתכן שהמוצר הוסר מהאתר או שהקישור השתנה. בדקו את הקישור בדפדפן; "
            "אם המוצר עבר לכתובת חדשה, הסירו את המעקב והוסיפו את הכתובת החדשה."
        )
    if fetch.status == FetchStatus.BLOCKED:
        return (
            "האתר חוסם בקשות אוטומטיות (הגנה מבוטים). לרוב זה חולף מעצמו; "
            "אם זה נמשך ימים, נסו להוסיף את המעקב מחדש עם \u200e--selector\u200e."
        )
    if fetch.status == FetchStatus.PRICE_NOT_FOUND or (
        fetch.status == FetchStatus.OK and fetch.price is not None and fetch.price == 0
    ):
        return (
            "הדף נטען אבל המחיר לא זוהה (או שזוהה מחיר 0). אם עיצוב הדף השתנה, "
            "הוסיפו את המעקב מחדש עם \u200e--selector\u200e שמצביע על המחיר."
        )
    if fetch.status == FetchStatus.UNREACHABLE:
        return "האתר לא ענה. בדקו את החיבור לאינטרנט ושהאתר עולה בדפדפן."
    return None


def describe_failure(fetch: "FetchResult") -> dict:
    """Explain why a fetch failed, in Hebrew, with what the user can do.

    Returns {"status", "status_he", "http_status", "error_message",
    "detail", "hint"}; ``detail`` is failure_detail_he() (None when the
    fetch did not fail).
    """
    status = fetch.status.value
    detail = failure_detail_he(fetch)
    return {
        "status": status,
        "status_he": _status_he(status),
        "http_status": fetch.http_status,
        "error_message": fetch.error_message,
        "detail": detail or None,
        "hint": _failure_hint_he(fetch) if detail else None,
    }


def _failure_subject_he(fetch: "FetchResult") -> str:
    """Short reason for a failure-alert subject line."""
    if fetch.status == FetchStatus.OK and fetch.price is not None and fetch.price == 0:
        return "המחיר הוצג כ-0"
    return _status_he(fetch.status.value)


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


def _promo_computable(promo: PromoInfo | None) -> bool:
    """A promo with a usable percentage (not tiered "up to", not unreadable)."""
    return promo is not None and promo.discount_pct > 0


def _promo_desc(promo: PromoInfo) -> str:
    pct = f"{promo.discount_pct}%"
    if promo.code:
        return f"הקוד {promo.code} ({pct})"
    return f"הנחה אוטומטית ({pct})"


def _promo_heading(event: ChangeEvent) -> str:
    """Heading of a PROMO_CHANGED email: ended / new / changed."""
    if event.fetch.promo is None:
        return "המבצע הסתיים"
    if event.old_promo is None:
        return "מבצע חדש"
    return "המבצע השתנה"


def _promo_notes(event: ChangeEvent, sym: str) -> list[tuple[str, str]]:
    """(label, text) rows saying what the promotion means for this event.

    - item excluded from promotions: only the displayed price is shown;
    - a tiered / unreadable banner: no estimate, displayed price only;
    - a promotion change: the displayed price did not change;
    - a price change with a readable promotion: whether the promotion
      changed too, and the estimated final price (always marked as such).
    """
    fetch = event.fetch
    promo = fetch.promo
    if event.type not in (ChangeType.PRICE_CHANGED, ChangeType.STOCK_CHANGED,
                          ChangeType.PROMO_CHANGED, ChangeType.RECOVERED):
        return []
    if event.watch.excluded_from_promo:
        return [("מבצעים", "הפריט מסומן כמוחרג ממבצעי האתר — מוצג המחיר המוצג בלבד")]
    if promo is not None and not _promo_computable(promo):
        return [(
            "מבצע",
            "באנר המבצע לא נקרא במלואו או שהמבצע מדורג ('עד X%') — "
            "לא ניתן להעריך מחיר סופי; מוצג המחיר המוצג בלבד",
        )]
    if event.type == ChangeType.PROMO_CHANGED:
        return [("מה השתנה", "המבצע (המחיר המוצג לא השתנה)")]
    if event.type != ChangeType.PRICE_CHANGED or not _promo_computable(promo):
        return []
    rows = [(
        "מה השתנה",
        "המחיר המוצג וגם המבצע" if event.promo_changed else "המחיר המוצג (המבצע לא השתנה)",
    )]
    est = fetch.estimated_final_price
    if est is not None:
        old = event.old_estimated_price
        if old is not None and old != est:
            rows.append(("מחיר סופי משוער", f"{_fmt_price(old, sym)} → {_fmt_price(est, sym)} (הערכה)"))
        else:
            rows.append(("מחיר סופי משוער", f"{_fmt_price(est, sym)} (הערכה)"))
    return rows


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
        promo = event.fetch.promo
        est = event.fetch.estimated_final_price
        if _promo_computable(promo) and est is not None and not event.watch.excluded_from_promo:
            (amount,) = _subject_amounts(est)
            return (
                f"{_promo_heading(event)}: {label} – בערך {_with_sym(amount, sym)} "
                f"עם {_promo_desc(promo)}"
            )
        return f"{_promo_heading(event)}: {label}"

    if event.type == ChangeType.THRESHOLD_FAILURE:
        return f"המעקב הפסיק לעבוד: {label} – {_failure_subject_he(event.fetch)}"

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

    notes = [f"  {key}: {val}" for key, val in _promo_notes(event, sym)]

    if event.type == ChangeType.PRICE_CHANGED:
        lines += [f"המחיר {_direction_he(event.pct_change)}: {label}"]
        lines += price_lines()
        lines += extra_lines()
        lines += notes
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
        lines += notes
        lines += [f"  קישור: {url}"]

    elif event.type == ChangeType.PROMO_CHANGED:
        displayed = _fmt_price(event.fetch.price, sym)
        lines += [f"{_promo_heading(event)}: {label}"]
        lines += promo_lines()
        lines.append(f"  מחיר מוצג: {displayed}")
        if event.fetch.estimated_final_price is not None:
            estimated = _fmt_price(event.fetch.estimated_final_price, sym)
            lines.append(f"  מחיר סופי משוער: {estimated} (הערכה)")
        lines += notes
        lines += [f"  קישור: {url}"]

    elif event.type == ChangeType.THRESHOLD_FAILURE:
        reason = event.reason or "שגיאה לא ידועה"
        lines += [
            f"המעקב הפסיק לעבוד: {label}",
            f"  סיבה: {reason}",
            *([f"  פירוט: {detail}"] if (detail := failure_detail_he(event.fetch)) else []),
            *([f"  מה לעשות: {hint}"] if (hint := _failure_hint_he(event.fetch)) else []),
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
        lines += notes
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

    notes = [row(key, escape(val)) for key, val in _promo_notes(event, sym)]

    if event.type == ChangeType.PRICE_CHANGED:
        heading = (
            f'המחיר <span style="color:{price_color}">{_DIRECTION_HE[direction]}</span>: {label}'
        )
        rows += price_rows()
        rows += extra_rows()
        rows += notes
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
        rows += notes
        rows += [row("קישור", link)]

    elif event.type == ChangeType.PROMO_CHANGED:
        displayed = _fmt_price(event.fetch.price, sym)
        heading = f"{_promo_heading(event)}: {label}"
        rows += promo_rows()
        rows.append(row("מחיר מוצג", _ltr(displayed)))
        if event.fetch.estimated_final_price is not None:
            estimated = _fmt_price(event.fetch.estimated_final_price, sym)
            rows.append(row(
                "מחיר סופי משוער", f'<strong>{_ltr(estimated)}</strong> <em>(הערכה)</em>'
            ))
        rows += notes
        rows += [row("קישור", link)]

    elif event.type == ChangeType.THRESHOLD_FAILURE:
        reason = escape(event.reason or "שגיאה לא ידועה")
        heading = f'המעקב הפסיק לעבוד: {label}'
        rows += [
            row("סיבה", reason),
            *([row("פירוט", escape(detail))] if (detail := failure_detail_he(event.fetch)) else []),
            *([row("מה לעשות", escape(hint))] if (hint := _failure_hint_he(event.fetch)) else []),
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
        rows += notes
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


# Styles of the meat tables. They live in one <style> block in the email's
# <head> (see build_daily_payload) instead of on every cell: with ~200 rows
# the inline version pushed the email over Gmail's ~102 KB clipping limit.
# Without the block (a client that strips <style>) the tables degrade to
# plain black-on-white rows, still readable.
_MEAT_CSS = (
    f".mt{{border-collapse:collapse;width:100%;margin:0 0 16px;border:1px solid {_COLOR_BORDER};"
    f"font-size:13px;color:{_COLOR_TEXT};font-family:{_FONT_STACK}}}"
    f".mt td{{padding:6px;vertical-align:top;border-top:1px solid {_COLOR_BORDER}}}"
    f".mt small{{color:{_COLOR_MUTED}}}"
    f".mt td.h{{background:{_COLOR_TEXT};color:#fff;padding:8px 10px}}"
    ".mt td.h b{font-size:15px}"
    ".mt td.h small{color:#d1d5db}"
    ".mt tr.f{background:#ecfdf5}"
    f".mt b.c{{color:{_COLOR_LINK}}}"
    ".mt b.p{font-size:15px}"
    f".mt b.d{{color:{_COLOR_DANGER}}}"
)


def _meat_style_tag() -> str:
    return f"<style>{_MEAT_CSS}</style>"


def _price_compact_html(p) -> str:
    """Price per kg in bold; for a deal, the regular price per kg struck
    through with the discount and the multi-buy deal; for an item that is not
    on sale, a "regular price" note; then the pack size."""
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
        lines.append(f'<b class="p" dir="ltr">₪{per_kg}</b>' + (f'<small>{unit}</small>' if unit else ''))
    if getattr(p, "is_promo", True):
        # "-30%" when known; a bare dash (not "-—") when it is not.
        pct_txt = f"-{int(p.savings_pct)}%" if p.savings_pct is not None else "—"
        pct = f'<b class="d" dir="ltr">{pct_txt}</b>'
        was = f'<s dir="ltr">₪{was_kg}</s> ' if was_kg else ""
        lines.append(f'<small>{was}{pct}</small>')
    else:
        lines.append('<small>מחיר רגיל</small>')
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
        f"בכל קטגוריה {_top_per_category()} המוצרים הזולים ביותר לפי מחיר לק״ג, מהזול ליקר: "
        "מחיר המבצע, או המחיר הרגיל כשמוצר שאינו במבצע זול יותר "
        "(במוצרים ארוזים המחיר מחושב לפי משקל המארז). "
        "מוצגות קטגוריות שיש בהן לפחות מבצע אחד."
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
        lines.append(f"  ▼ {category}  ({shown} מוצרים)")
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
            if getattr(p, "is_promo", True):
                until = getattr(p, "end_date_display", "")
                lines.append(
                    f"       מחיר רגיל {was} · הנחה {pct}" + (f" · {extra}" if extra else "")
                    + (f" · בתוקף עד {until}" if until else "")
                )
            else:
                lines.append("       מחיר רגיל (לא במבצע)" + (f" · {extra}" if extra else ""))
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
        f'<b>{total_deals}</b> מוצרי בשר · '
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
    for category, items in grouped.items():
        rows_html = []
        for i, p in enumerate(items):
            where = _where_label(p)
            chips = " ".join(_region_chip_html(r) for r in _promo_regions(p)) if show_regions else ""
            below = "<br>".join(x for x in (chips, escape(where)) if x)
            desc = (p.promotion_description or "").strip()
            # Item note: the promotion text and, short, the day the deal ends.
            until = getattr(p, "end_date_display", "")[:5]
            note = " · ".join(x for x in (
                escape(desc[:60]) if desc and desc != p.item_name else "",
                f"עד {until}" if until else "",
            ) if x)
            rows_html.append(
                # The cheapest row is highlighted (class f).
                ('<tr class="f">' if i == 0 else '<tr>')
                # Supermarket, then its branch(es) with the city. One column,
                # so the table still fits a phone screen.
                + ('<td width="34%">' if i == 0 else '<td>')
                + f'<b class="c">{escape(p.chain)}</b>'
                + (f'<br><small>{below}</small>' if below else '')
                + '</td>'
                # Item
                f'<td>{escape(p.item_name[:60])}'
                + (f'<br><small>{note}</small>' if note else '')
                + '</td>'
                # Price per kg (now / was), discount, deal
                f'<td align="left" nowrap>{_price_compact_html(p)}</td>'
                '</tr>'
            )

        total = promos_by_category.get(category, len(items))
        if total > len(items):
            shown_txt = f'{len(items)} הזולים מתוך {total}'
        elif len(items) == 1:
            shown_txt = 'מוצר אחד'
        else:
            shown_txt = f'{len(items)} מוצרים'
        chains_in_table = len({p.chain for p in items})
        chains_txt = 'רשת אחת' if chains_in_table == 1 else f'{chains_in_table} רשתות'
        unpriced = unpriced_by_category.get(category, 0)
        unpriced_txt = f' · עוד {unpriced} בלי מחיר לק״ג' if unpriced else ''
        html.append(
            # cellpadding / width repeat what the stylesheet says, for clients
            # that drop <style>.
            '<table class="mt" dir="rtl" cellpadding="6" cellspacing="0" width="100%">'
            # Header strip: category, counts, and the unit of the price column
            '<tr><td class="h" colspan="3">'
            f'<b>{_category_icon(category)} {escape(category)}</b> '
            f'<small>{shown_txt} · {chains_txt}{unpriced_txt} · מחיר לק״ג</small>'
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


def _continent_sections(groups, flat: list) -> list:
    """The continent groups of one kind. A report without groups (older
    callers) shows its flat list as one untitled section."""
    if groups is None:
        return [SimpleNamespace(continent="", title_he="", deals=list(flat), limit=len(flat),
                                window_days=0, missing_required=[])] if flat else []
    return list(groups)


def _vacation_groups(vac) -> list[tuple[str, str, bool, list]]:
    """(icon, title, is_packages, continent sections) for packages, then flights."""
    return [
        ("🏖️", "חבילות נופש (טיסה + מלון)", True,
         _continent_sections(getattr(vac, "package_groups", None), vac.packages)),
        ("✈️", "טיסות", False,
         _continent_sections(getattr(vac, "flight_groups", None), vac.flights)),
    ]


def _shown_sections(sections: list) -> tuple[list, list[str]]:
    """(sections to render, Hebrew names of continents with nothing to show).

    A continent with no deals is only rendered when it has a note to carry
    (a required Asia country with no deal); otherwise it goes on one short
    "no deals" line instead of an empty table."""
    shown, empty = [], []
    for g in sections:
        if g.deals or g.missing_required:
            shown.append(g)
        elif g.title_he:
            empty.append(g.title_he)
    return shown, empty


def _missing_note(g) -> str:
    """'לא נמצא היום דיל לתאילנד ביציאה ב-365 הימים הקרובים' ('' when none missing)."""
    if not g.missing_required:
        return ""
    names = [_continents.country_name_he(n) or n for n in g.missing_required]
    words = "לא נמצא היום דיל" if len(names) == 1 else "לא נמצאו היום דילים"
    return (f"{words} ל" + " ול".join(names)
            + f" ביציאה ב-{g.window_days} הימים הקרובים")


def _section_caption(g) -> str:
    """'15 דילים · יציאה ב-50 הימים הקרובים'."""
    count = "דיל אחד" if len(g.deals) == 1 else f"{len(g.deals)} דילים"
    return count + (f" · יציאה ב-{g.window_days} הימים הקרובים" if g.window_days else "")


def _windows_summary(vac) -> str:
    """'אירופה: עד 15 דילים, יציאה ב-50 הימים הקרובים · שאר היבשות: ...' ('' without groups)."""
    groups = list(getattr(vac, "package_groups", None) or []) + list(
        getattr(vac, "flight_groups", None) or [])
    europe = next((g for g in groups if g.continent == _continents.EUROPE), None)
    other = next((g for g in groups if g.continent != _continents.EUROPE), None)
    parts = []
    if europe is not None:
        parts.append(f"אירופה: עד {europe.limit} דילים, יציאה ב-{europe.window_days} הימים הקרובים")
    if other is not None:
        parts.append(f"שאר היבשות: עד {other.limit} דילים לכל יבשת, "
                     f"יציאה ב-{other.window_days} הימים הקרובים")
    if parts:
        from price_watcher.vacations import source_windows
        for site, days in source_windows().items():
            parts.append(f"{site}: יציאה ב-{days} הימים הקרובים")
    return " · ".join(parts)


def _unmapped_note(vac) -> str:
    n = len(getattr(vac, "unmapped", None) or [])
    if not n:
        return ""
    return "דיל אחד ליעד שלא זוהתה היבשת שלו לא מוצג" if n == 1 else (
        f"{n} דילים ליעדים שלא זוהתה היבשת שלהם לא מוצגים")


# Category marks on the deal tables: (label, chip text colour, chip background, meaning).
# Soft chips (dark text on a pale tint of the same hue); rows themselves are
# not tinted, so a table with several marks stays calm and readable.
_BADGE_CHEAPEST = ("הכי זול לאדם", "#065f46", "#d1fae5", "המחיר הנמוך ביותר לאדם")
_BADGE_VALUE = ("הכי משתלם", "#1e3a8a", "#dbeafe",
                "המחיר הנמוך ביותר לאדם ללילה מבין החבילות שכוללות כבודה (טרולי או מזוודה)")
_BADGE_PACKAGE = ("החבילה הכי זולה", "#4c1d95", "#ede9fe", "מחיר החבילה הכולל הנמוך ביותר")
_BADGE_LONGEST = ("הכי ארוך", "#78350f", "#fef3c7", "הכי הרבה לילות")
_BADGE_SHORTEST = ("הכי קצר", "#134e4a", "#ccfbf1", "הכי מעט לילות")
_BADGES = (_BADGE_CHEAPEST, _BADGE_VALUE, _BADGE_PACKAGE, _BADGE_LONGEST, _BADGE_SHORTEST)


def _badge_class(badge) -> int:
    return _BADGES.index(badge)


_DEAL_BUTTON = "#1d4ed8"
_DEAL_LINK_LABEL = "לצפייה בדיל ←"


def _deal_link_html(d) -> str:
    """Site name, then a button linking to the deal.

    Styled by `.vt a.go` only (rows carry no inline styles, to keep the daily
    email under Gmail's clipping size); without the <style> block it is a
    plain link.
    """
    return (
        f'<small>{escape(d.source)}</small>'
        f'<a class="go" href="{escape(d.url)}">{_DEAL_LINK_LABEL}</a>'
    )


# Styles of the vacation tables, in one <style> block in the email's <head>
# (like _MEAT_CSS): the per-continent tables hold ~70 rows, and inline styles
# on every cell would push the daily email further past Gmail's ~102 KB
# clipping limit. Without the block the tables degrade to plain rows.
_VAC_CSS = (
    f".vt{{border-collapse:collapse;width:100%;font-family:{_FONT_STACK};color:{_COLOR_TEXT}}}"
    f".vt th{{padding:6px;font-size:11px;font-weight:600;color:{_COLOR_MUTED};text-align:right}}"
    ".vt th.l{text-align:left}.vt th.c{text-align:center}"
    f".vt td{{padding:8px 6px;border-top:1px solid {_COLOR_BORDER};vertical-align:top;"
    "text-align:right;font-size:13px}"
    f".vt td.n{{padding:8px 4px 8px 0;font-size:12px;color:{_COLOR_MUTED};text-align:center}}"
    ".vt td.w{font-size:12px;white-space:nowrap}"
    ".vt td.i{font-size:11px;line-height:1.5}"
    ".vt td.s{font-size:12px}"
    ".vt td.p,.vt td.t{font-size:14px;font-weight:700;text-align:left;white-space:nowrap}"
    f".vt td.t{{color:{_COLOR_ACCENT};white-space:normal}}"
    f".vt td.h{{background:{_COLOR_BG_SOFT};border-top:2px solid {_COLOR_BORDER};"
    "font-size:14px;font-weight:700;padding:10px 8px 6px}"
    f".vt td.h small,.vt td.m{{color:{_COLOR_MUTED};font-size:11px;font-weight:400}}"
    ".vt td.m{font-style:italic;padding:4px 8px}"
    f".vt small{{display:block;font-size:11px;font-weight:400;color:{_COLOR_MUTED};margin-top:2px}}"
    ".vt td.h small{display:inline}"
    f".vt a{{color:{_COLOR_LINK};font-weight:600;text-decoration:none}}"
    f".vt tr.z{{background:{_COLOR_BG_SOFT}}}"
    # The deal link: a solid button, readable in light and dark mode.
    f".vt a.go{{display:inline-block;margin-top:5px;padding:4px 10px;border-radius:6px;"
    f"background:{_DEAL_BUTTON};color:#ffffff;font-size:11px;font-weight:700;"
    "white-space:nowrap;text-decoration:none}"
    ".vk{display:inline-block;font-size:10px;font-weight:700;padding:1px 6px;"
    "border-radius:4px;margin-top:3px;white-space:nowrap;border:1px solid}"
    + "".join(f".vk.k{i}{{color:{b[1]};background:{b[2]};border-color:{b[1]}}}"
              for i, b in enumerate(_BADGES))
)


def _vacation_style_tag() -> str:
    return f"<style>{_VAC_CSS}</style>"


def _vacation_badge_set(is_packages: bool) -> list[tuple[str, str, str, str]]:
    """Categories that apply to a table (per-night value and package total need a hotel stay)."""
    if is_packages:
        return [_BADGE_CHEAPEST, _BADGE_VALUE, _BADGE_PACKAGE, _BADGE_LONGEST, _BADGE_SHORTEST]
    return [_BADGE_CHEAPEST, _BADGE_LONGEST, _BADGE_SHORTEST]


def _per_night(d) -> Decimal | None:
    """Price per person per night, or None when the length of stay is unknown."""
    nights = d.nights_or_computed
    return d.price / nights if nights else None


def _luggage_line(d) -> str:
    """'כבודה: טרולי כלול' / 'ללא כבודה (…)' / 'כבודה: לא צוין באתר'."""
    from price_watcher import vacations as _v
    state = getattr(d, "luggage", _v.LUGGAGE_UNKNOWN)
    if state == _v.LUGGAGE_SUITCASE:
        return "כבודה: מזוודה כלולה"
    if state == _v.LUGGAGE_CARRY_ON:
        return "כבודה: טרולי כלול"
    if state == _v.LUGGAGE_NONE:
        extra = getattr(d, "luggage_extra", "")
        return "ללא כבודה" + (f" ({extra})" if extra else "")
    return "כבודה: לא צוין באתר"


_NO_VALUE_NOTE = 'אין כאן "הכי משתלם": אף חבילה ביבשת הזו לא כוללת כבודה'


def _value_badge_given(deals: list) -> bool:
    """'הכי משתלם' needs at least one row with luggage included."""
    return any(getattr(d, "has_luggage", False) for d in deals)


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
        # Best value only among rows that include luggage (a carry-on is
        # enough); a row that is cheap because it has no luggage can't win.
        _mark(_BADGE_VALUE, _money(
            lambda d: _per_night(d) if getattr(d, "has_luggage", False) else None))
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
    def _cheapest(deals):  # lists are in continent order, not price order
        return min(deals, key=lambda d: (d.currency != "USD", d.price))

    parts = []
    if vac.packages:
        parts.append(f"חבילות מ-{_deal_price(_cheapest(vac.packages))}")
    if vac.flights:
        parts.append(f"טיסות מ-{_deal_price(_cheapest(vac.flights))}")
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
    windows = _windows_summary(vac)
    if windows:
        lines.append(f"  לפי יבשת. {windows}.")
    lines.append(
        f"  נמצאו {vac.total_found} דילים. מחיר לאדם = לכל החופשה (לא ללילה); "
        f"מחיר החבילה = הסכום לכל הנוסעים שהאתר תמחר."
    )
    unmapped = _unmapped_note(vac)
    if unmapped:
        lines.append(f"  {unmapped}.")
    for _icon, title, is_packages, sections in _vacation_groups(vac):
        lines.append("")
        lines.append(f"  ▼ {title}")
        shown, empty = _shown_sections(sections)
        if not shown:
            lines.append("    לא נמצאו היום.")
            continue
        for g in shown:
            if g.title_he:
                lines.append(f"    ■ {g.title_he} — {_section_caption(g)}")
            note = _missing_note(g)
            if note:
                lines.append(f"      ({note})")
            if is_packages and g.deals and not _value_badge_given(g.deals):
                lines.append(f"      ({_NO_VALUE_NOTE})")
            badges = _deal_badges(g.deals, is_packages)
            for i, d in enumerate(g.deals, 1):
                nights = d.nights_or_computed
                when = _deal_dates(d) + (f" ({nights} לילות)" if nights else "")
                tags = " · ".join(b[0] for b in badges.get(i - 1, []))
                lines.append(
                    f"      {i}. {_deal_price(d)} לאדם · {_deal_where(d)} · {when} · {d.source}"
                    + (f"  [{tags}]" if tags else "")
                )
                if d.total is not None:
                    lines.append(f"         מחיר החבילה: {_deal_total(d)} {_party_he(d)}".rstrip())
                if d.includes:
                    lines.append(f"         כולל: {' · '.join(d.includes)[:200]}")
                else:
                    details = _deal_details(d)
                    if details:
                        lines.append(f"         {details[:110]}")
                lines.append(f"         {_luggage_line(d)}")
                lines.append(f"         {d.url}")
        if empty:
            lines.append(f"    אין היום דילים ל: {', '.join(empty)}")
    return lines


def _vacations_section_html(vac) -> str:
    """Vacation deals: one card for packages, one for flights, each split by
    continent (cheapest first within a continent). RTL. Needs _VAC_CSS in the
    email's <head>."""
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
        + (f'<div style="font-size:12px;color:{_COLOR_MUTED};margin-top:2px">'
           f'לפי יבשת. {escape(_windows_summary(vac))}.</div>'
           if _windows_summary(vac) else '')
        + f'<div style="font-size:12px;color:{_COLOR_MUTED};margin-top:2px">'
        f'בכל יבשת מהזול ליקר. "מחיר לאדם" הוא לכל החופשה (לא ללילה), '
        f'כפי שמופיע באתר. "מחיר החבילה" הוא הסכום לכל הנוסעים שהאתר תמחר. '
        f'"מה כלול" מציג רק את מה שהאתר מפרט.'
        f'</div>'
        + (f'<div style="font-size:12px;color:{_COLOR_MUTED};margin-top:2px">'
           f'{escape(_unmapped_note(vac))}.</div>' if _unmapped_note(vac) else '')
        + '</div>'
    ]

    for icon, title, is_packages, sections in _vacation_groups(vac):
        shown, empty = _shown_sections(sections)
        ncols = 6 if is_packages else 5
        rows: list[str] = []
        for g in shown:
            if g.title_he:
                rows.append(
                    f'<tr><td class="h" colspan="{ncols}">{escape(g.title_he)} '
                    f'<small>· {escape(_section_caption(g))}</small></td></tr>'
                )
            note = _missing_note(g)
            if note:
                rows.append(f'<tr><td class="m" colspan="{ncols}">{escape(note)}</td></tr>')
            if is_packages and g.deals and not _value_badge_given(g.deals):
                rows.append(f'<tr><td class="m" colspan="{ncols}">{escape(_NO_VALUE_NOTE)}</td></tr>')
            rows.extend(_vacation_rows_html(g.deals, is_packages))
        if empty:
            rows.append(
                f'<tr><td class="m" colspan="{ncols}">'
                f'אין היום דילים ל: {escape(", ".join(empty))}</td></tr>'
            )
        if not shown:
            body = (
                f'<div style="padding:12px 14px;color:#888;font-style:italic;'
                f'text-align:right">לא נמצאו היום.</div>'
            )
        else:
            headers = (
                '<th class="c">#</th><th>יעד</th><th>תאריכים</th>'
                f'<th>{"מה כלול" if is_packages else "אתר"}</th><th class="l">מחיר לאדם</th>'
                + ('<th class="l">מחיר החבילה</th>' if is_packages else '')
            )
            body = (
                '<table class="vt" dir="rtl" cellpadding="6" cellspacing="0" width="100%">'
                f'<thead><tr>{headers}</tr></thead><tbody>' + "".join(rows) + '</tbody></table>'
            )
        count = sum(len(g.deals) for g in shown)
        html.append(
            f'<div style="margin:0 0 20px;background:#ffffff;border:1px solid {_COLOR_BORDER};'
            f'border-radius:10px;overflow:hidden;font-family:{_FONT_STACK}">'
            f'<div style="padding:10px 14px;background:{_COLOR_BG_SOFT};'
            f'border-bottom:1px solid {_COLOR_BORDER};text-align:right">'
            f'<div style="font-size:15px;font-weight:700;color:{_COLOR_TEXT}">'
            f'{icon} {escape(title)}</div>'
            f'<div style="font-size:11px;color:{_COLOR_MUTED};margin-top:2px">'
            f'{count} דילים, לפי יבשת</div>'
            # Legend for the coloured category marks
            + (f'<div style="font-size:11px;color:{_COLOR_MUTED};margin-top:6px;line-height:2">'
               + " &nbsp; ".join(
                   f'<span class="vk k{_badge_class(b)}">{escape(b[0])}</span> {escape(b[3])}'
                   for b in _vacation_badge_set(is_packages)
               )
               + '<div style="line-height:1.4">הסימונים נבחרים בכל יבשת בנפרד.</div></div>'
               if count else '')
            + '</div>'
            + body + '</div>'
        )
    return "".join(html)


def _vacation_rows_html(deals: list, is_packages: bool) -> list[str]:
    """Table rows of one continent. Styling comes from _VAC_CSS classes."""
    rows = []
    badges = _deal_badges(deals, is_packages)
    for i, d in enumerate(deals):
        row_badges = badges.get(i, [])
        row_class = ' class="z"' if i % 2 else ''
        nights = d.nights_or_computed
        details = _deal_details(d)
        marks = "".join(
            f'<div><span class="vk k{_badge_class(b)}">{escape(b[0])}</span></div>'
            for b in row_badges
        )
        per_night = _per_night(d) if is_packages else None
        deal_link = _deal_link_html(d)
        cells = [
            f'<td class="n">{i + 1}</td>',
            # Destination. Packages: site + deal button underneath. Flights: details underneath.
            f'<td><b>{escape(_deal_where(d))}</b>'
            + (f'<div>{deal_link}</div>' if is_packages
               else (f'<small>{escape(details[:90])}</small>' if details else ''))
            + marks + '</td>',
            # Dates
            f'<td class="w">{_ltr(_deal_dates(d) or "—")}'
            + (f'<small>{nights} לילות</small>' if nights else '') + '</td>',
        ]
        luggage = f'<b>• {escape(_luggage_line(d))}</b>'
        if is_packages:  # what the package includes, luggage last
            cells.append(
                '<td class="i">'
                + "<br>".join([f'• {escape(x[:80])}' for x in d.includes] + [luggage]) + '</td>'
            )
        else:
            cells.append(f'<td class="s">{deal_link}<small>{escape(_luggage_line(d))}</small></td>')
        # Price per person
        cells.append(
            f'<td class="p">{_ltr(_deal_price(d))}'
            + (f'<small>{_ltr(f"{_currency_symbol(d.currency)}{per_night:,.0f}")} ללילה</small>'
               if per_night is not None else '')
            + '</td>'
        )
        if is_packages:  # whole-package price for the party the site priced
            cells.append(
                '<td class="t">'
                + (f'{_ltr(_deal_total(d))}<small>{escape(_party_he(d))}</small>'
                   if d.total is not None else "—")
                + '</td>'
            )
        rows.append(f'<tr{row_class}>' + "".join(cells) + '</tr>')
    return rows


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
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        _vacation_style_tag(), '</head>',
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
        if report.creatine.direction == "changed" and report.creatine.previous_cheapest is not None:
            creatine_blurb = "המוצר הזול ביותר בקריאטין התחלף"
        else:
            creatine_blurb = (
                f"הקריאטין {word} ל-{_currency_symbol(cur.currency)}{cur.price_per_100g} ל-100 גרם"
            )

    sm = getattr(report, "supermarkets", None)
    meat_blurb = ""
    if sm is not None and sm.status == "ok" and sm.promos:
        # The meat tables are ranked by price per kg, not by discount, so the
        # subject names where the deals are rather than a "best discount".
        cities = list(getattr(sm, "cities", None) or [])
        meat_blurb = f"מבצעי בשר ב-{len(sm.chains_scanned)} רשתות"
        if 0 < len(cities) <= 3:
            meat_blurb += " (" + ", ".join(cities) + ")"

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
    vac = getattr(report, "vacations", None)
    vacation_blurb = _vacation_blurb(vac)
    if vacation_blurb:
        headline.append(vacation_blurb)
    if vac is not None and vac.status != "ok":
        failed.append("חופשות")
    mp_report = getattr(report, "marketplace", None)  # None = feature off
    if mp_report is not None and mp_report.status == "failed":
        failed.append("Marketplace")
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
    text_lines.extend(_vacations_section_text(vac))
    text_lines.append("")
    from price_watcher import marketplace as _marketplace
    text_lines.extend(_marketplace.section_text(mp_report))
    text_lines.append(f"— {_SIGNATURE}")
    text_body = "\n".join(text_lines)

    # ---- HTML ----
    _F = _FONT_STACK
    html_parts = [
        '<!DOCTYPE html><html dir="rtl" lang="he"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        _meat_style_tag(),
        _vacation_style_tag() if vac is not None and vac.status == "ok" else "",
        _marketplace.style_tag() if mp_report is not None else "",
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

    if mp_report is not None:
        html_parts.append(_section_heading_html(_marketplace.SECTION_TITLE))
        html_parts.append(_marketplace.section_html(mp_report, heading=False))

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
