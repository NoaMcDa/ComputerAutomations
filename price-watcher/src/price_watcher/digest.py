"""Daily sales digest.

For each unique host in the watch list, fetch the homepage and extract the
current site-wide promo banner (if any). Email the combined result once a day.

The banner-detection heuristic is reused from the MyProtein plugin, which
already generalises reasonably well across shop sites (class-substring match
on promo/offer/banner/code, header scan, meta description fallback).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from rich.console import Console
from rich.table import Table

from price_watcher import logging_setup
from price_watcher.contracts import PromoInfo


log = logging_setup.get(__name__)
console = Console()


_DISPLAY_OVERRIDES = {
    "terminalx": "Terminal X",
    "myprotein": "MyProtein",
    "seestarz": "Seestarz",
}


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "he,en;q=0.8,en-US;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
}


@dataclass
class SiteDigest:
    host: str
    display_name: str
    homepage_url: str
    status: str = "ok"  # "ok" / "no_banner" / "unreachable" / "blocked"
    promo: Optional[PromoInfo] = None
    error: Optional[str] = None


def _display_name(host: str) -> str:
    base = host.removeprefix("www.").split(".")[0]
    return _DISPLAY_OVERRIDES.get(base.lower(), base.capitalize())


def _unique_hosts() -> list[str]:
    from price_watcher.watchlist import load_all
    hosts: set[str] = set()
    for w in load_all():
        h = urlparse(w.url).hostname
        if h:
            hosts.add(h)
    return sorted(hosts)


def _fetch_homepage(host: str) -> tuple[Optional[str], str, Optional[str]]:
    """Return (html, status, error_message).

    status is one of "ok" / "unreachable" / "blocked".
    """
    url = f"https://{host}/"
    try:
        with httpx.Client(follow_redirects=True, timeout=20, headers=_HEADERS) as client:
            resp = client.get(url)
    except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
        return None, "unreachable", str(exc)
    if resp.status_code in (403, 429):
        return None, "blocked", f"HTTP {resp.status_code}"
    if resp.status_code >= 400:
        return None, "unreachable", f"HTTP {resp.status_code}"
    return resp.text, "ok", None


def _extract_promo(html: str) -> Optional[PromoInfo]:
    """Run the generic promo-banner heuristic on the homepage HTML."""
    from price_watcher.sites.myprotein import (
        _candidate_texts,
        _extract_pct,
        _extract_code,
        _extract_condition,
        _is_up_to,
    )

    soup = BeautifulSoup(html, "lxml")
    for text in _candidate_texts(soup):
        if _is_up_to(text):
            return PromoInfo(
                code=None,
                discount_pct=Decimal("0"),
                condition="up-to (tiered)",
                source_text=text[:200],
            )
        pct = _extract_pct(text)
        if pct is not None and pct > 0:
            code = _extract_code(text)
            condition = _extract_condition(text, code)
            return PromoInfo(
                code=code,
                discount_pct=pct,
                condition=condition,
                source_text=text[:200],
            )
    return None


def collect_digest() -> list[SiteDigest]:
    """For each unique watched host, fetch homepage and extract current promo."""
    digests: list[SiteDigest] = []
    for host in _unique_hosts():
        site = SiteDigest(
            host=host,
            display_name=_display_name(host),
            homepage_url=f"https://{host}/",
        )
        html, status, err = _fetch_homepage(host)
        if status != "ok" or html is None:
            site.status = status
            site.error = err
            digests.append(site)
            continue

        promo = _extract_promo(html)
        if promo is None:
            site.status = "no_banner"
        else:
            site.status = "ok"
            site.promo = promo
        digests.append(site)
    return digests


def _print_digests(digests: list[SiteDigest]) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("Site")
    table.add_column("Promo")
    table.add_column("Code")
    table.add_column("Condition")

    for d in digests:
        if d.status == "ok" and d.promo:
            pct = d.promo.discount_pct
            promo_str = f"{pct}% off" if pct > 0 else f"(tiered: {d.promo.condition or '—'})"
            code = d.promo.code or "—"
            cond = d.promo.condition or "—"
            table.add_row(d.display_name, promo_str, code, cond)
        elif d.status == "no_banner":
            table.add_row(d.display_name, "[dim]no banner detected[/dim]", "—", "—")
        else:
            table.add_row(d.display_name, f"[red]{d.status}[/red]", "—", d.error or "—")

    console.print(table)


def run_digest(dry_run: bool = False) -> int:
    """CLI entry: collect, print, and send the digest email."""
    log.info("Collecting daily digest for %d sites", len(_unique_hosts()))
    digests = collect_digest()
    _print_digests(digests)

    if dry_run:
        from price_watcher.email_sender import build_digest_payload
        payload = build_digest_payload(digests)
        if payload is not None:
            console.print(f"\n[bold]WOULD SEND EMAIL[/bold] — Subject: {payload.subject}")
        return 0

    from price_watcher.email_sender import send_digest_email
    sent = send_digest_email(digests)
    return 0 if sent else 1
