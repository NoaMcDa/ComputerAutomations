"""Daily entry point: scan -> (optional) auto-order -> conditional email.

Usage:
    python main.py                 # normal daily run
    python main.py --dry-run       # scan, print the email instead of sending, never book
    python main.py --today 2026-11-20 --config other.json -v
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

import notifier
import orderer
import searcher
from settings import DEFAULT_CONFIG_PATH, DEFAULT_ENV_PATH, ConfigError, load_config, load_dotenv

log = logging.getLogger("restaurant_orderer")


def local_today() -> date:
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        return datetime.now(ZoneInfo("Asia/Jerusalem")).date()
    except Exception:
        return date.today()


def run(argv: list[str] | None = None, *, adapters: list | None = None, smtp=None, sender=None) -> int:
    p = argparse.ArgumentParser(description="Restaurant availability watcher (Ontopo / Tabit)")
    p.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    p.add_argument("--env", default=str(DEFAULT_ENV_PATH), help="path to .env with SMTP settings")
    p.add_argument("--today", type=date.fromisoformat, default=None, help="override today's date (YYYY-MM-DD)")
    p.add_argument("--dry-run", action="store_true", help="do not send email or book; print the digest")
    p.add_argument("--force", action="store_true", help="send even if a digest was already sent today")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv(Path(args.env))
    try:
        config = load_config(args.config)
    except (OSError, ValueError, ConfigError) as exc:
        log.error("invalid configuration (%s): %s", args.config, exc)
        return 2

    today = args.today or local_today()
    if not config.target_dates(today):
        log.info("no remaining target dates between %s and %s (today %s) - nothing to do", config.start, config.end, today)
        return 0

    adapters = adapters if adapters is not None else searcher.build_adapters(config)
    report = searcher.scan(config, adapters, today)
    log.info("scan finished: %s; %d open slot(s)", report.status_counts(), len(report.slots))
    for prob in report.problems():
        log.warning("%s %s: %s %s", prob.platform, prob.date, prob.status, prob.message)

    outcome = orderer.process(config, report, adapters, today, dry_run=args.dry_run)
    notes = list(outcome.notes)
    problems = report.problems()
    if problems:
        kinds = sorted({f"{r.platform}: {r.status}" for r in problems})
        notes.append("Some checks did not complete (" + "; ".join(kinds) + ") - results may be incomplete.")

    kwargs = {"notes": notes, "dry_run": args.dry_run, "force": args.force, "smtp": smtp}
    if sender is not None:
        kwargs["sender"] = sender
    result = notifier.notify(report, config, today, **kwargs)
    if args.dry_run and result.digest:
        print(result.digest.subject)
        print()
        print(result.digest.text)
    log.info("email: %s", result.reason)
    return 0


if __name__ == "__main__":
    sys.exit(run())
