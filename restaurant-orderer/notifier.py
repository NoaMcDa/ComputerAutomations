"""Daily email digest - sent only when at least one open slot was found."""

from __future__ import annotations

import html
import json
import logging
import os
import smtplib
import ssl
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage
from itertools import groupby
from pathlib import Path
from typing import Callable, Mapping, Sequence

from searcher import ScanReport, Slot
from settings import Config

log = logging.getLogger("restaurant_orderer.notifier")

_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass(frozen=True)
class SmtpSettings:
    host: str
    port: int
    user: str
    password: str
    sender: str
    recipients: tuple[str, ...]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SmtpSettings":
        env = os.environ if env is None else env
        user = env.get("RO_SMTP_USER", "").strip()
        try:
            port = int(env.get("RO_SMTP_PORT", "587") or 587)
        except ValueError:
            port = 587
        recipients = tuple(r.strip() for r in env.get("RO_EMAIL_TO", "").split(",") if r.strip())
        return cls(
            host=env.get("RO_SMTP_HOST", "smtp.gmail.com").strip(),
            port=port,
            user=user,
            password=env.get("RO_SMTP_PASSWORD", ""),
            sender=env.get("RO_EMAIL_FROM", "").strip() or user,
            recipients=recipients,
        )

    def missing(self) -> list[str]:
        out = []
        if not self.host:
            out.append("RO_SMTP_HOST")
        if not self.user:
            out.append("RO_SMTP_USER")
        if not self.password:
            out.append("RO_SMTP_PASSWORD")
        if not self.recipients:
            out.append("RO_EMAIL_TO")
        return out


@dataclass(frozen=True)
class Digest:
    subject: str
    text: str
    html: str


@dataclass(frozen=True)
class NotifyOutcome:
    sent: bool
    reason: str
    digest: Digest | None = None


def _fmt_day(d: date) -> str:
    return f"{_DAY_NAMES[d.weekday()]} {d:%d.%m.%Y}"


def build_digest(report: ScanReport, config: Config, notes: Sequence[str] = ()) -> Digest:
    slots = report.slots
    n_days = len({s.date for s in slots})
    subject = (f"{config.email.subject_prefix} {report.restaurant}: {len(slots)} open slot"
               f"{'s' if len(slots) != 1 else ''} on {n_days} day{'s' if n_days != 1 else ''}").strip()

    text_lines = [
        f"Open tables at {report.restaurant} (party of {report.party_size})",
        f"Search range: {config.start:%d.%m.%Y} - {config.end:%d.%m.%Y}, days: {', '.join(config.days_filter)}",
        "",
    ]
    html_parts = [
        "<!doctype html><html><body style=\"font-family:Arial,Helvetica,sans-serif;color:#222\">",
        f"<h2 style=\"margin:0 0 4px\">Open tables at {html.escape(report.restaurant)}</h2>",
        f"<p style=\"margin:0 0 12px;color:#555\">Party of {report.party_size} &middot; "
        f"{config.start:%d.%m.%Y} &ndash; {config.end:%d.%m.%Y} &middot; {html.escape(', '.join(config.days_filter))}</p>",
    ]
    for d, day_slots_iter in groupby(slots, key=lambda s: s.date):
        day_slots = list(day_slots_iter)
        text_lines.append(_fmt_day(d))
        html_parts.append(f"<h3 style=\"margin:16px 0 6px\">{html.escape(_fmt_day(d))}</h3>")
        html_parts.append("<table cellpadding=\"6\" style=\"border-collapse:collapse\">"
                          "<tr style=\"background:#f2f2f2\"><th align=\"left\">Time</th><th align=\"left\">Area</th>"
                          "<th align=\"left\">Platform</th><th align=\"left\">Book</th></tr>")
        for s in day_slots:
            area = s.area or "any"
            text_lines.append(f"  {s.time:%H:%M}  {area:<14} [{s.platform}]  {s.deep_link or '(no link)'}")
            link = (f"<a href=\"{html.escape(s.deep_link, quote=True)}\">Reserve</a>" if s.deep_link else "&ndash;")
            html_parts.append(
                f"<tr><td>{s.time:%H:%M}</td><td>{html.escape(area)}</td>"
                f"<td>{html.escape(s.platform)}</td><td>{link}</td></tr>"
            )
        html_parts.append("</table>")
        text_lines.append("")

    if notes:
        text_lines.append("Notes:")
        text_lines.extend(f"  - {n}" for n in notes)
        html_parts.append("<h4 style=\"margin:18px 0 4px\">Notes</h4><ul>")
        html_parts.extend(f"<li>{html.escape(n)}</li>" for n in notes)
        html_parts.append("</ul>")

    text_lines.append("Slots can disappear quickly - book via the link as soon as possible.")
    html_parts.append("<p style=\"color:#777;font-size:12px\">Slots can disappear quickly &ndash; "
                      "book via the link as soon as possible.</p></body></html>")
    return Digest(subject=subject, text="\n".join(text_lines), html="".join(html_parts))


def build_message(digest: Digest, smtp: SmtpSettings) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = digest.subject
    msg["From"] = smtp.sender
    msg["To"] = ", ".join(smtp.recipients)
    msg.set_content(digest.text)
    msg.add_alternative(digest.html, subtype="html")
    return msg


def send_via_smtp(msg: EmailMessage, smtp: SmtpSettings) -> None:
    ctx = ssl.create_default_context()
    if smtp.port == 465:
        with smtplib.SMTP_SSL(smtp.host, smtp.port, context=ctx, timeout=30) as s:
            s.login(smtp.user, smtp.password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(smtp.host, smtp.port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(smtp.user, smtp.password)
            s.send_message(msg)


# -- once-per-day guard ----------------------------------------------------

def _state_file(config: Config) -> Path:
    return config.state_dir / "last_email.json"


def already_sent_today(config: Config, today: date) -> bool:
    try:
        data = json.loads(_state_file(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return data.get("date") == today.isoformat()


def mark_sent(config: Config, today: date, n_slots: int) -> None:
    try:
        config.state_dir.mkdir(parents=True, exist_ok=True)
        _state_file(config).write_text(json.dumps({"date": today.isoformat(), "slots": n_slots}), encoding="utf-8")
    except OSError as exc:
        log.warning("could not write email state file: %s", exc)


def notify(report: ScanReport, config: Config, today: date, *, notes: Sequence[str] = (),
           smtp: SmtpSettings | None = None, dry_run: bool = False, force: bool = False,
           sender: Callable[[EmailMessage, SmtpSettings], None] = send_via_smtp) -> NotifyOutcome:
    """Send the digest if (and only if) there is something to report."""
    slots = report.slots
    if not slots and config.email.suppress_empty:
        log.info("no open slots - no email (suppress_empty=true)")
        return NotifyOutcome(sent=False, reason="no_slots")
    if not slots:
        digest = Digest(
            subject=f"{config.email.subject_prefix} {report.restaurant}: no open slots today".strip(),
            text=f"No open tables at {report.restaurant} for the configured dates.",
            html=f"<p>No open tables at {html.escape(report.restaurant)} for the configured dates.</p>",
        )
    else:
        digest = build_digest(report, config, notes)

    if dry_run:
        log.info("dry run - email not sent:\n%s\n\n%s", digest.subject, digest.text)
        return NotifyOutcome(sent=False, reason="dry_run", digest=digest)
    if not force and config.email.frequency == "daily" and already_sent_today(config, today):
        log.info("digest already sent today (%s) - skipping", today)
        return NotifyOutcome(sent=False, reason="already_sent_today", digest=digest)

    smtp = smtp or SmtpSettings.from_env()
    missing = smtp.missing()
    if missing:
        log.error("email not sent: missing %s (set them in .env)", ", ".join(missing))
        return NotifyOutcome(sent=False, reason="smtp_not_configured", digest=digest)
    try:
        sender(build_message(digest, smtp), smtp)
    except (smtplib.SMTPException, OSError) as exc:
        log.error("email sending failed: %s", exc)
        return NotifyOutcome(sent=False, reason=f"smtp_error: {exc}", digest=digest)
    mark_sent(config, today, len(slots))
    log.info("digest sent to %s", ", ".join(smtp.recipients))
    return NotifyOutcome(sent=True, reason="sent", digest=digest)


def slot_summary(slots: Sequence[Slot]) -> str:
    return ", ".join(f"{_fmt_day(s.date)} {s.time:%H:%M} ({s.area or 'any'})" for s in slots)
