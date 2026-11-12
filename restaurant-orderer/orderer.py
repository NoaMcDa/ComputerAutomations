"""Optional automated booking with a safe fallback to notification + deep links.

Policy:
* ``mode == "notify_only"`` (default): never books; the email carries deep links.
* ``mode == "auto_order"``: books the best slot ONLY when the slot's platform adapter
  declares that its booking flow needs no SMS one-time code and no card guarantee,
  and contact details are configured. Otherwise it logs why and falls back to
  notification. Ontopo and Tabit adapters both declare an OTP requirement, so in
  practice this falls back for them; the path exists for platforms/venues where
  a plain name/phone/email submission is enough.
* At most one automatic booking is ever made per configuration (state file guard).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Mapping

from searcher import PlatformAdapter, ScanReport, Slot
from settings import Config

log = logging.getLogger("restaurant_orderer.orderer")

NOTIFY = "notify"
BOOKED = "booked"
WOULD_BOOK = "would_book"
FALLBACK = "fallback"


@dataclass
class OrderOutcome:
    action: str
    slot: Slot | None = None
    notes: list[str] = field(default_factory=list)
    confirmation: Mapping[str, Any] | None = None


def contact_from_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    env = os.environ if env is None else env
    return {
        "name": env.get("RO_CONTACT_NAME", "").strip(),
        "phone": env.get("RO_CONTACT_PHONE", "").strip(),
        "email": env.get("RO_CONTACT_EMAIL", "").strip(),
    }


def pick_best_slot(slots: Iterable[Slot]) -> Slot | None:
    """Earliest date first, then earliest time."""
    ordered = sorted(slots, key=lambda s: (s.date, s.time, s.platform, s.area))
    return ordered[0] if ordered else None


def _booking_state(config: Config):
    return config.state_dir / "booking.json"


def existing_booking(config: Config) -> dict | None:
    try:
        return json.loads(_booking_state(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _record_booking(config: Config, slot: Slot, confirmation: Mapping[str, Any], today: date) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    _booking_state(config).write_text(json.dumps({
        "booked_on": today.isoformat(), "platform": slot.platform, "date": slot.date.isoformat(),
        "time": slot.time.strftime("%H:%M"), "area": slot.area, "confirmation": dict(confirmation),
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def _fallback(slot: Slot | None, reason: str) -> OrderOutcome:
    log.warning("auto_order fallback: %s", reason)
    return OrderOutcome(FALLBACK, slot, [f"Automatic booking not attempted: {reason}. "
                                         "Use the reservation links below."])


def process(config: Config, report: ScanReport, adapters: Iterable[PlatformAdapter], today: date, *,
            contact: Mapping[str, str] | None = None, dry_run: bool = False) -> OrderOutcome:
    if config.mode != "auto_order":
        return OrderOutcome(NOTIFY)

    slot = pick_best_slot(report.slots)
    if slot is None:
        return OrderOutcome(NOTIFY)

    prior = existing_booking(config)
    if prior:
        return OrderOutcome(NOTIFY, slot, [
            f"A booking was already made automatically on {prior.get('booked_on')} "
            f"({prior.get('date')} {prior.get('time')} via {prior.get('platform')}); not booking again."])

    adapter = next((a for a in adapters if a.name == slot.platform), None)
    if adapter is None:
        return _fallback(slot, f"no adapter for platform {slot.platform!r}")

    req = adapter.booking_requirements()
    if not req.human_free:
        needs = [w for w, flag in (("an SMS one-time code", req.otp_required),
                                   ("a credit-card guarantee", req.card_required)) if flag]
        return _fallback(slot, f"{slot.platform} requires {' and '.join(needs)}")

    contact = dict(contact_from_env() if contact is None else contact)
    missing = [k for k in ("name", "phone", "email") if not contact.get(k)]
    if missing:
        return _fallback(slot, "contact details missing (" + ", ".join(f"RO_CONTACT_{m.upper()}" for m in missing) + ")")

    if dry_run:
        log.info("dry run - would book %s %s %s via %s", slot.date, slot.time, slot.area, slot.platform)
        return OrderOutcome(WOULD_BOOK, slot, [f"Dry run: would book {slot.date:%d.%m} {slot.time:%H:%M} ({slot.area or 'any'})."])

    try:
        confirmation = adapter.book(slot, contact)
    except NotImplementedError as exc:
        return _fallback(slot, str(exc))
    except Exception as exc:  # booking errors must never break the notification
        log.exception("booking failed")
        return _fallback(slot, f"booking request failed ({type(exc).__name__}: {exc})")

    _record_booking(config, slot, confirmation, today)
    return OrderOutcome(BOOKED, slot, [
        f"BOOKED automatically: {slot.date:%d.%m.%Y} {slot.time:%H:%M} ({slot.area or 'any'}) via {slot.platform}. "
        f"Confirmation: {json.dumps(dict(confirmation), ensure_ascii=False)}"], confirmation)
