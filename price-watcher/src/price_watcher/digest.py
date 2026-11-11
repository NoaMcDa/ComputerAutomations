"""Daily sales digest.

For each unique host in the watch list, scan the homepage for promo banners
and codes. Produces a detailed per-site report with every detected promo,
code, condition, and a raw source excerpt (so the owner can sanity-check
what the extractor saw).

Two-pass extraction:
  1. Inline <script> content — many sites (Terminal X, Castro) embed banner
     text inside JSON blobs rendered server-side. These aren't visible to
     a BS4 DOM selector-based scan.
  2. DOM elements — class-substring selectors on promo/offer/banner/code
     + the <header> + <meta name="description">.

After collection, promos are deduped by (discount_pct, code) and ranked:
  - having an explicit code wins
  - higher discount wins
  - longer condition wins (more useful context)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
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
    status: str = "ok"  # "ok" / "no_promo" / "unreachable" / "blocked"
    promos: list[PromoInfo] = field(default_factory=list)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Promo extraction (improved)
# ---------------------------------------------------------------------------

# Patterns that indicate a SITE-WIDE sale rather than per-product.
# Match an explicit CODE + percentage near each other.
# Gap is up to 250 chars because Castro's Hebrew banner has the pct and the
# code in separate <div>s with ~120 chars of markup between them.
# Percent NEAR an explicit code keyword. "off/OFF/הנחה" immediately after %
# is optional — Castro's banner says "EXTRA 11% על כל האתר" (no "off" word),
# but has "קוד קופון: OFF11" later. We filter the span for a promo-context
# word afterward to reject noise.
_PCT_CODE_PAT = re.compile(
    r"(\d{1,2})\s*%\s*(?:off|הנחה|OFF)?"
    r"[\s\S]{0,120}?"
    r"\b(?:code|קופון|קוד)\b[^A-Z0-9]{0,20}([A-Z][A-Z0-9]{2,19})",
    re.IGNORECASE | re.UNICODE,
)

_CODE_PCT_PAT = re.compile(
    r"\b(?:code|קופון|קוד)\b[^A-Z0-9]{0,20}([A-Z][A-Z0-9]{2,19})"
    r"[\s\S]{0,120}?"
    r"(\d{1,2})\s*%\s*(?:off|הנחה|OFF)?",
    re.IGNORECASE | re.UNICODE,
)

# Site-wide bare-% promo (no code advertised). Must be adjacent to a strong
# "everything / all items / sitewide" phrase to filter per-product discounts.
_SITEWIDE_BARE_PAT = re.compile(
    r"(\d{1,2})\s*%\s*(?:off|הנחה|OFF)"
    r"[^<>\n]{0,40}"
    r"(?:everything|everything!|all items|site\s*wide|sitewide|הכל|על כל האתר|site|אתר)",
    re.IGNORECASE | re.UNICODE,
)

_CONTEXT_KEYWORDS = re.compile(
    r"\b(off|OFF|הנחה|EXTRA|SALE|סייל|deal|final|sale|end[s]?\s+in|בתוקף)\b",
    re.IGNORECASE | re.UNICODE,
)


def _is_promotional_span(span: str) -> bool:
    """Reject the match if the surrounding text doesn't look promotional."""
    return bool(_CONTEXT_KEYWORDS.search(span))

# Pattern for a bare "N% off" without a visible code (newsletter, first-time, etc.)
_PCT_BARE_PAT = re.compile(
    r"(\d{1,2})\s*%\s*(?:off|הנחה)",
    re.IGNORECASE | re.UNICODE,
)

# "up to N%" / "עד N%" — tiered, not computable
_UP_TO_PAT = re.compile(
    r"(?:up\s+to|עד)\s+(\d{1,2})\s*%",
    re.IGNORECASE | re.UNICODE,
)

# Words to use as condition heuristics (after the match, up to a sentence boundary)
_COND_PAT_AFTER = re.compile(r".{0,120}?(?:[.!?\n\|]|בתוקף|$)", re.UNICODE)


def _extract_promos_from_text(text: str, max_len: int = 250) -> list[PromoInfo]:
    """Find every CODE+% promo in a text blob. Returns dedup'd list."""
    if not text:
        return []
    found: dict[tuple[int, str], PromoInfo] = {}

    def _condition_around(pos_start: int, pos_end: int) -> Optional[str]:
        snippet = text[max(0, pos_start - 50):pos_end + 100]
        snippet = re.sub(r"\s+", " ", snippet).strip()
        return snippet[:max_len]

    # Pattern A: pct ... code
    for m in _PCT_CODE_PAT.finditer(text):
        pct = int(m.group(1))
        code = m.group(2).upper()
        key = (pct, code)
        if not (1 <= pct <= 99) or key in found:
            continue
        if not _is_promotional_span(m.group(0)):
            continue
        found[key] = PromoInfo(
            code=code,
            discount_pct=Decimal(pct),
            condition=_condition_around(m.start(), m.end()),
            source_text=text[max(0, m.start() - 30):m.end() + 60][:max_len],
        )

    # Pattern B: code ... pct
    for m in _CODE_PCT_PAT.finditer(text):
        code = m.group(1).upper()
        pct = int(m.group(2))
        key = (pct, code)
        if not (1 <= pct <= 99) or key in found:
            continue
        if not _is_promotional_span(m.group(0)):
            continue
        found[key] = PromoInfo(
            code=code,
            discount_pct=Decimal(pct),
            condition=_condition_around(m.start(), m.end()),
            source_text=text[max(0, m.start() - 30):m.end() + 60][:max_len],
        )

    # Pattern C: bare pct in site-wide context (no code advertised)
    for m in _SITEWIDE_BARE_PAT.finditer(text):
        pct = int(m.group(1))
        if not (1 <= pct <= 99):
            continue
        # Dedupe against any already-found coded promo at the same pct
        if any(k[0] == pct for k in found):
            continue
        key = (pct, "")
        if key in found:
            continue
        found[key] = PromoInfo(
            code=None,
            discount_pct=Decimal(pct),
            condition=_condition_around(m.start(), m.end()),
            source_text=text[max(0, m.start() - 30):m.end() + 60][:max_len],
        )

    return list(found.values())


def _extract_bare_promos(text: str, with_codes: set[tuple[int, str]]) -> list[PromoInfo]:
    """Find bare 'N% off' promos that DON'T have a code nearby. Lower-ranked."""
    if not text:
        return []
    results: list[PromoInfo] = []
    seen_pcts: set[int] = set()

    # Strip "up to N%" fragments first (they're tiered, handled separately)
    cleaned = _UP_TO_PAT.sub("", text)

    for m in _PCT_BARE_PAT.finditer(cleaned):
        pct = int(m.group(1))
        if not (1 <= pct <= 99):
            continue
        if pct in seen_pcts:
            continue
        # Skip if this same pct was already found paired with a code
        if any(p == pct for p, _ in with_codes):
            seen_pcts.add(pct)
            continue
        seen_pcts.add(pct)
        snippet = cleaned[max(0, m.start() - 60):m.end() + 80]
        snippet = re.sub(r"\s+", " ", snippet).strip()[:200]
        results.append(PromoInfo(
            code=None,
            discount_pct=Decimal(pct),
            condition=snippet,
            source_text=snippet,
        ))
    return results


def _text_sources(html: str, soup: BeautifulSoup) -> list[str]:
    """Return every text source worth scanning for promos."""
    sources: list[str] = []

    # 1. The entire raw HTML — the strict (code + pct) regex is anchored enough
    #    that false positives are rare, and this catches banner JSON embedded
    #    anywhere in a <script> tag (Terminal X's SINGLES20 lives at pos ~333k).
    sources.append(html)

    # 2. Promo/banner DOM elements — stripped text is cleaner for bare-pct scans
    for sel in ('[class*="promo"]', '[class*="offer"]', '[class*="banner"]',
                '[class*="code"]', '[data-component*="promo"]'):
        for el in soup.select(sel):
            t = el.get_text(" ", strip=True)
            if t:
                sources.append(t)

    # 3. Header area
    header = soup.find("header")
    if header:
        t = header.get_text(" ", strip=True)
        if t:
            sources.append(t)

    # 4. Meta description
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        sources.append(str(meta["content"]))

    return sources


def extract_promos(html: str) -> list[PromoInfo]:
    """Find every promo on the page. Returns dedup'd, ranked list."""
    soup = BeautifulSoup(html, "lxml")
    sources = _text_sources(html, soup)

    # Collect coded promos (dedup by code) and site-wide bare promos (dedup by pct).
    all_coded: dict[str, PromoInfo] = {}
    all_bare: dict[int, PromoInfo] = {}
    for src in sources:
        for p in _extract_promos_from_text(src):
            if p.code is None:
                pct_key = int(p.discount_pct)
                if pct_key not in all_bare or len(p.condition or "") > len(all_bare[pct_key].condition or ""):
                    all_bare[pct_key] = p
                continue
            if p.code in _COMMON_NON_CODES:
                continue
            key = p.code
            if key not in all_coded or p.discount_pct > all_coded[key].discount_pct:
                all_coded[key] = p

    # If a coded promo exists at pct X, drop the bare match at pct X (coded is better)
    coded_pcts = {int(p.discount_pct) for p in all_coded.values()}
    merged = list(all_coded.values()) + [
        p for pct, p in all_bare.items() if pct not in coded_pcts
    ]

    # Rank: higher pct first, then longer condition
    merged.sort(
        key=lambda p: (-int(p.discount_pct), -len(p.condition or "")),
    )
    return merged


_COMMON_NON_CODES = {
    "SALE", "OFFER", "FREE", "GIFT", "BUY", "NEW", "TODAY",
    "CODE", "KOD", "CODES", "COUPON", "COLOR", "COLOUR", "STAMPA",
    "MANGO", "BRAND", "TYPE", "SIZE",
}


# ---------------------------------------------------------------------------
# Site scanning
# ---------------------------------------------------------------------------

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
    url = f"https://{host}/"
    try:
        with httpx.Client(follow_redirects=True, timeout=25, headers=_HEADERS) as client:
            resp = client.get(url)
    except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
        return None, "unreachable", str(exc)
    if resp.status_code in (403, 429):
        return None, "blocked", f"HTTP {resp.status_code}"
    if resp.status_code >= 400:
        return None, "unreachable", f"HTTP {resp.status_code}"
    return resp.text, "ok", None


def collect_digest() -> list[SiteDigest]:
    """Scan each unique host; return list of SiteDigest with every detected promo."""
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

        promos = extract_promos(html)
        site.promos = promos
        site.status = "ok" if promos else "no_promo"
        digests.append(site)
    return digests


# ---------------------------------------------------------------------------
# Terminal printing
# ---------------------------------------------------------------------------

def _print_digests(digests: list[SiteDigest]) -> None:
    for d in digests:
        console.print(f"\n[bold cyan]━━━ {d.display_name}[/bold cyan] [dim]({d.host})[/dim]")
        if d.status != "ok":
            console.print(f"  [red]{d.status}[/red] {d.error or ''}")
            continue
        if not d.promos:
            console.print("  [dim]no site-wide promo detected[/dim]")
            continue
        for i, p in enumerate(d.promos, 1):
            code_cell = f"[bold green]{p.code}[/bold green]" if p.code else "[dim]no code[/dim]"
            console.print(f"  {i}. [yellow]{p.discount_pct}% off[/yellow] | code: {code_cell}")
            if p.condition:
                cond = p.condition[:200]
                console.print(f"     [dim]{cond}[/dim]")


@dataclass
class DailyReport:
    """Everything the daily email contains. One section per 'daily feature'."""
    site_digests: list[SiteDigest] = field(default_factory=list)
    creatine: Optional["object"] = None     # CreatineReport
    sims4: Optional["object"] = None        # Sims4Report
    supermarkets: Optional["object"] = None # SupermarketReport


def collect_daily_report() -> DailyReport:
    """Run every daily data-gathering stage and return a combined report."""
    report = DailyReport()
    report.site_digests = collect_digest()
    try:
        from price_watcher import creatine as _creatine
        report.creatine = _creatine.scan_and_report()
    except Exception as exc:
        log.error("Creatine stage failed: %s: %s", type(exc).__name__, exc)
    try:
        from price_watcher import sims4 as _sims4
        report.sims4 = _sims4.scan_and_report()
    except Exception as exc:
        log.error("Sims 4 stage failed: %s: %s", type(exc).__name__, exc)
    try:
        from price_watcher import supermarkets as _supermarkets
        report.supermarkets = _supermarkets.scan_and_report()
    except Exception as exc:
        log.error("Supermarket stage failed: %s: %s", type(exc).__name__, exc)
    return report


def _print_daily_report(report: DailyReport) -> None:
    console.print("\n[bold underline]Sales digest[/bold underline]")
    _print_digests(report.site_digests)
    if report.creatine is not None:
        console.print("\n[bold underline]Creatine (iHerb)[/bold underline]")
        from price_watcher import creatine as _creatine
        _creatine.print_report(report.creatine)
    if report.sims4 is not None:
        console.print("\n[bold underline]The Sims 4 (EA)[/bold underline]")
        from price_watcher import sims4 as _sims4
        _sims4.print_report(report.sims4)
    if report.supermarkets is not None:
        console.print("\n[bold underline]Supermarket meat discounts[/bold underline]")
        from price_watcher import supermarkets as _sm
        _sm.print_report(report.supermarkets)


def run_digest(dry_run: bool = False) -> int:
    """Run the combined daily report: sales digest + creatine + sims4."""
    log.info("Running daily report")
    report = collect_daily_report()
    _print_daily_report(report)

    if dry_run:
        from price_watcher.email_sender import build_daily_payload
        payload = build_daily_payload(report)
        if payload is not None:
            console.print(f"\n[bold]WOULD SEND EMAIL[/bold] — Subject: {payload.subject}")
        return 0

    from price_watcher.email_sender import send_daily_email
    sent = send_daily_email(report)
    if sent:
        if report.creatine is not None:
            from price_watcher import creatine as _creatine
            _creatine.persist(report.creatine)
        if report.sims4 is not None:
            from price_watcher import sims4 as _sims4
            _sims4.persist(report.sims4)
        if report.supermarkets is not None:
            from price_watcher import supermarkets as _sm
            _sm.persist(report.supermarkets)
    return 0 if sent else 1
