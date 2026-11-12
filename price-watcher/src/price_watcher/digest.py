"""Daily sales digest.

For each unique host in the watch list, scan the homepage for promo banners
and codes. Produces a detailed per-site report with every detected promo,
code, condition, and a raw source excerpt (so the owner can sanity-check
what the extractor saw).

Text sources (see "Promo extraction" below for the offer/pairing rules):
  1. String values inside <script> — some sites (Terminal X) embed banner
     text inside JSON blobs rendered server-side.
  2. The page's visible text (with <img alt>) + meta descriptions.

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

from price_watcher import logging_setup
from price_watcher.contracts import PromoInfo
from price_watcher.http_headers import USER_AGENT


log = logging_setup.get(__name__)
console = Console()


_DISPLAY_OVERRIDES = {
    "terminalx": "Terminal X",
    "myprotein": "MyProtein",
    "seestarz": "Seestarz",
}


_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "he,en;q=0.8,en-US;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
}


# Shown (terminal, email text and HTML) for a site that was read fine but has
# no site-wide offer today.
NO_PROMO_HE = "לא זוהה מבצע כלל-אתרי"


@dataclass
class SiteDigest:
    host: str
    display_name: str
    homepage_url: str
    status: str = "ok"  # "ok" (fetched; empty promos = no promo) / "unreachable" / "blocked"
    promos: list[PromoInfo] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def failed(self) -> bool:
        """True only when the homepage could not be read (no promo is not a failure)."""
        return self.status != "ok"


# ---------------------------------------------------------------------------
# Promo extraction
# ---------------------------------------------------------------------------
#
# Pipeline:
#   1. Text sources: the page's *visible* text (scripts/styles/svg removed,
#      <img alt> kept) plus every JSON/JS string *value* found in <script>
#      tags (Terminal X's SINGLES20 banner only exists inside a script blob).
#      Raw markup, CSS and JSON keys are never scanned, and each script string
#      is scanned on its own, so neighbouring JSON fields cannot be combined.
#   2. Offers: each source is split into lines (<br>, block elements, list
#      items, newlines inside strings) and each line into segments on "|".
#      One segment ~ one offer ("APP EXCLUSIVE | EXTRA 5% OFF | USE CODE: APP5"
#      is three segments).
#   3. Pairing: a code is paired with the nearest discount percentage in its
#      own segment (at most _MAX_PAIR_GAP chars away). A segment holding only
#      a code ("USE CODE: APP5", Castro's "בהקלדת קוד קופון: OFF11" <div>)
#      borrows the percentage of the directly preceding segment, else the
#      directly following one, but only if that neighbour has no code of its
#      own and both segments are short. Nothing pairs across further segments.
#   4. A percentage counts only if it has a left boundary ("150%" is not 50%),
#      is not "up to N%" (tiered) and has a discount word next to it
#      ("off", "extra", "הנחה", ...). Codes must be upper-case in the source
#      and follow a code keyword ("code", "קוד", "קופון", ...); only the
#      keyword is case-insensitive.
#   5. A percentage without a code is reported only when a site-wide phrase
#      ("everything", "sitewide", "על כל האתר", ...) follows it.

# Max distance (chars) between a percentage and a code in the same segment.
_MAX_PAIR_GAP = 120
# Max length of each segment when a code borrows its neighbour's percentage.
_MAX_NEIGHBOUR_LEN = 120

# Percentage: left boundary (no digit / decimal point before), 1-2 digits.
_PCT_PAT = re.compile(r"(?<![\d.,])(\d{1,2})\s*%")

_UP_TO_PAT = re.compile(r"(?:\bup\s+to|(?<!\w)עד)\s*$", re.IGNORECASE)

# Discount words that must sit right before or right after the percentage.
_DISCOUNT_WORD_PAT = re.compile(
    r"\b(?:off|extra|discount|save|savings|sale)\b|הנחה|הנחת",
    re.IGNORECASE,
)
_DISCOUNT_BEFORE_CHARS = 20
_DISCOUNT_AFTER_CHARS = 15

# Code keyword (case-insensitive) + short punctuation gap + UPPER-CASE code.
# English keywords need a word boundary ("postcode" is not "code"); Hebrew
# ones may carry a prefix letter ("בקוד") but must end there ("קודם" is not).
_CODE_PAT = re.compile(
    r"(?:(?i:\b(?:promo\s*code|code|coupon|voucher))"
    r"|קוד(?:\s+(?:הנחה|הטבה|מבצע|קופון|קידום))?|קופון)(?!\w)"
    r"[\s:：\-–—\"'״“”«»()\[\]]{0,6}"
    r"([A-Z][A-Z0-9]{2,19})(?![A-Za-z0-9])"
)

# Site-wide phrases that must follow a bare (code-less) percentage.
_SITEWIDE_PAT = re.compile(
    r"\b(?:everything|all\s+items|all\s+products|site\s*-?\s*wide"
    r"|store\s*-?\s*wide|entire\s+(?:site|store|order))\b"
    r"|(?<!\w)(?:על\s+|ב)?כל\s+האתר(?!\w)|(?<!\w)על\s+הכל(?!\w)",
    re.IGNORECASE,
)
_SITEWIDE_AFTER_CHARS = 40

# Upper-case words that follow "code" in banners but are not codes
# ("USE CODE BELOW", "CODE APPLIED AT CHECKOUT").
_NON_CODES = {
    "SALE", "SAVE", "OFFER", "FREE", "GIFT", "BUY", "NEW", "NOW", "TODAY",
    "CODE", "CODES", "COUPON", "BELOW", "HERE", "AT", "CHECKOUT", "APPLIED",
    "SHOP", "OFF", "THE",
}

_LINE_BREAK = ""  # private-use sentinel for structural breaks (not matched by \s)
_SKIP_TAGS = ["script", "style", "noscript", "template", "svg", "iframe", "head"]
_BLOCK_TAGS = [
    "p", "div", "li", "ul", "ol", "dl", "dt", "dd", "h1", "h2", "h3", "h4",
    "h5", "h6", "section", "article", "aside", "header", "footer", "nav",
    "main", "table", "tr", "td", "th", "form", "button", "blockquote",
    "figure", "figcaption", "option",
]

# JSON / JS double-quoted string literal that is a value, not an object key.
_JS_STRING_PAT = re.compile(r'"((?:[^"\\\n]|\\.){4,})"(?!\s*:)')
_JS_ESCAPE_PAT = re.compile(r"\\(u[0-9a-fA-F]{4}|.)", re.DOTALL)
_JS_SIMPLE_ESCAPES = {"n": "\n", "r": "\n", "t": " ", "b": "", "f": ""}


def _render_text(soup: BeautifulSoup) -> str:
    """Visible text of *soup* with structural breaks as "\\n".

    Mutates *soup* (callers pass a soup they own).
    """
    for tag in soup.find_all(_SKIP_TAGS):
        tag.decompose()
    for img in soup.find_all("img"):
        alt = img.get("alt")
        img.replace_with(f"{_LINE_BREAK}{alt}{_LINE_BREAK}" if alt else "")
    for br in soup.find_all("br"):
        br.replace_with(_LINE_BREAK)
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before(_LINE_BREAK)
        tag.insert_after(_LINE_BREAK)
    # " " between text nodes so inline items (flex <a>/<span>) don't fuse.
    text = re.sub(r"\s+", " ", soup.get_text(" "))
    lines = (ln.strip() for ln in text.split(_LINE_BREAK))
    return "\n".join(ln for ln in lines if ln)


def _html_fragment_text(fragment: str) -> str:
    """Visible text of an HTML (or plain-text) fragment, lines kept."""
    if "<" not in fragment:
        lines = (re.sub(r"\s+", " ", ln).strip() for ln in fragment.splitlines())
        return "\n".join(ln for ln in lines if ln)
    return _render_text(BeautifulSoup(fragment, "lxml"))


def _unescape_js(raw: str) -> str:
    def _one(m: re.Match) -> str:
        esc = m.group(1)
        if esc[0] == "u" and len(esc) == 5:
            return chr(int(esc[1:], 16))
        return _JS_SIMPLE_ESCAPES.get(esc, esc)
    return _JS_ESCAPE_PAT.sub(_one, raw)


def _script_strings(script_text: str, depth: int = 0) -> list[str]:
    """String values inside a script that could hold a banner (contain '%')."""
    out: list[str] = []
    for m in _JS_STRING_PAT.finditer(script_text):
        raw = m.group(1)
        if "%" not in raw:
            continue
        value = _unescape_js(raw)
        if depth == 0 and '":"' in value:
            # A JSON document serialised inside a JS string: look inside it.
            out.extend(_script_strings(value, depth + 1))
            continue
        out.append(value)
    return out


def _segments(text: str) -> list[str]:
    segs: list[str] = []
    for line in text.split("\n"):
        for part in line.split("|"):
            part = part.strip(" \t-–—•·")
            if part:
                segs.append(part)
    return segs


def _discount_pcts(seg: str) -> list[tuple[int, int, int]]:
    """(pct, start, end) for each real discount percentage in *seg*."""
    found: list[tuple[int, int, int]] = []
    for m in _PCT_PAT.finditer(seg):
        pct = int(m.group(1))
        if not 1 <= pct <= 99:
            continue
        before = seg[max(0, m.start() - _DISCOUNT_BEFORE_CHARS):m.start()]
        if _UP_TO_PAT.search(before):
            continue
        after = seg[m.end():m.end() + _DISCOUNT_AFTER_CHARS]
        if not (_DISCOUNT_WORD_PAT.search(before) or _DISCOUNT_WORD_PAT.search(after)):
            continue
        found.append((pct, m.start(), m.end()))
    return found


def _codes(seg: str) -> list[tuple[str, int]]:
    return [
        (m.group(1), m.start())
        for m in _CODE_PAT.finditer(seg)
        if m.group(1) not in _NON_CODES
    ]


def _nearest_pct(pcts: list[tuple[int, int, int]], code_pos: int) -> Optional[int]:
    """Nearest preceding percentage, else nearest following, within the gap."""
    before = [p for p in pcts if p[2] <= code_pos and code_pos - p[2] <= _MAX_PAIR_GAP]
    if before:
        return max(before, key=lambda p: p[2])[0]
    after = [p for p in pcts if p[1] > code_pos and p[1] - code_pos <= _MAX_PAIR_GAP]
    if after:
        return min(after, key=lambda p: p[1])[0]
    return None


def parse_offers(text: str, max_len: int = 250) -> list[PromoInfo]:
    """Every coded or code-less site-wide offer in one banner text.

    Lines are separated by "\\n"; segments within a line by "|" (see the
    pipeline above). Also used by the site plugins (MyProtein) so product
    pages and the digest read banners the same way.
    """
    segs = _segments(text)
    pcts = [_discount_pcts(s) for s in segs]
    codes = [_codes(s) for s in segs]
    offers: list[PromoInfo] = []
    used: set[tuple[int, int]] = set()  # (segment index, pct) consumed by a code

    def _make(code: Optional[str], pct: int, idxs: list[int]) -> PromoInfo:
        lo, hi = min(idxs), max(idxs)
        condition = " | ".join(segs[lo:hi + 1])[:max_len]
        source = " | ".join(segs[max(0, lo - 1):hi + 2])[:max_len]
        return PromoInfo(code=code, discount_pct=Decimal(pct),
                         condition=condition, source_text=source)

    for i, seg in enumerate(segs):
        for code, pos in codes[i]:
            if pcts[i]:
                pct = _nearest_pct(pcts[i], pos)
                if pct is not None:
                    used.add((i, pct))
                    offers.append(_make(code, pct, [i]))
                continue
            if len(seg) > _MAX_NEIGHBOUR_LEN:
                continue
            for j in (i - 1, i + 1):
                if (0 <= j < len(segs) and pcts[j] and not codes[j]
                        and len(segs[j]) <= _MAX_NEIGHBOUR_LEN):
                    pct = pcts[j][-1][0] if j < i else pcts[j][0][0]
                    used.add((j, pct))
                    offers.append(_make(code, pct, [i, j]))
                    break

    for i, seg in enumerate(segs):
        for pct, _start, end in pcts[i]:
            if (i, pct) in used:
                continue
            if _SITEWIDE_PAT.search(seg[end:end + _SITEWIDE_AFTER_CHARS]):
                offers.append(_make(None, pct, [i]))
    return offers


def _text_sources(soup: BeautifulSoup) -> list[str]:
    """Every text source worth scanning: script string values, visible text,
    meta descriptions. *soup* is consumed (scripts are removed)."""
    sources: list[str] = []
    for script in soup.find_all("script"):
        for value in _script_strings(script.string or script.get_text() or ""):
            text = _html_fragment_text(value)
            if text:
                sources.append(text)
    for attrs in ({"name": "description"}, {"property": "og:description"}):
        meta = soup.find("meta", attrs=attrs)
        if meta and meta.get("content"):
            sources.append(_html_fragment_text(str(meta["content"])))
    sources.append(_render_text(soup))
    return sources


def extract_promos(html: str) -> list[PromoInfo]:
    """Find every promo on the page. Returns dedup'd, ranked list."""
    soup = BeautifulSoup(html, "lxml")
    sources = _text_sources(soup)

    # Collect coded promos (dedup by code) and site-wide bare promos (dedup by pct).
    all_coded: dict[str, PromoInfo] = {}
    all_bare: dict[int, PromoInfo] = {}
    for src in sources:
        for p in parse_offers(src):
            if p.code is None:
                pct_key = int(p.discount_pct)
                if pct_key not in all_bare or len(p.condition or "") > len(all_bare[pct_key].condition or ""):
                    all_bare[pct_key] = p
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


def element_text(tag) -> str:
    """Visible text of one element with <br>/block breaks kept as "\\n"."""
    return _render_text(BeautifulSoup(str(tag), "lxml"))


def has_up_to_discount(text: str) -> bool:
    """True if *text* advertises an 'up to N% off' (tiered) discount."""
    for seg in _segments(text):
        for m in _PCT_PAT.finditer(seg):
            before = seg[max(0, m.start() - _DISCOUNT_BEFORE_CHARS):m.start()]
            after = seg[m.end():m.end() + _DISCOUNT_AFTER_CHARS]
            if _UP_TO_PAT.search(before) and _DISCOUNT_WORD_PAT.search(after):
                return True
    return False


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
            if resp.status_code in (403, 429):
                return None, "blocked", f"HTTP {resp.status_code}"
            if resp.status_code >= 400:
                return None, "unreachable", f"HTTP {resp.status_code}"
            return resp.text, "ok", None
    # HTTPError covers TooManyRedirects / DecodingError / protocol errors too;
    # InvalidURL is not an HTTPError subclass.
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        return None, "unreachable", f"{type(exc).__name__}: {exc}"


def collect_digest() -> list[SiteDigest]:
    """Scan each unique host; return list of SiteDigest with every detected promo."""
    digests: list[SiteDigest] = []
    for host in _unique_hosts():
        site = SiteDigest(
            host=host,
            display_name=_display_name(host),
            homepage_url=f"https://{host}/",
        )
        try:
            html, status, err = _fetch_homepage(host)
            if status != "ok" or html is None:
                site.status = status
                site.error = err
                digests.append(site)
                continue

            promos = extract_promos(html)
            site.promos = promos
            site.status = "ok"  # fetched fine; an empty `promos` list means "no promo"
        except Exception as exc:
            # One misbehaving homepage must not cancel the other sites.
            log.error("Digest scan failed for %s: %s: %s", host, type(exc).__name__, exc)
            site.promos = []
            site.status = "unreachable"
            site.error = f"{type(exc).__name__}: {exc}"
        digests.append(site)
    return digests


# ---------------------------------------------------------------------------
# Terminal printing
# ---------------------------------------------------------------------------

def _print_digests(digests: list[SiteDigest]) -> None:
    for d in digests:
        console.print(f"\n[bold cyan]━━━ {d.display_name}[/bold cyan] [dim]({d.host})[/dim]")
        if d.failed:
            console.print(f"  [red]{d.status}[/red] {d.error or ''}")
            continue
        if not d.promos:
            console.print(f"  [dim]{NO_PROMO_HE}[/dim]")
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
    vacations: Optional["object"] = None    # VacationReport
    marketplace: Optional["object"] = None  # MarketplaceReport; None = feature off


def collect_daily_report() -> DailyReport:
    """Run every daily data-gathering stage and return a combined report."""
    report = DailyReport()
    try:
        report.site_digests = collect_digest()
    except Exception as exc:
        log.error("Sales digest stage failed: %s: %s", type(exc).__name__, exc)
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
    report.vacations = collect_vacations()
    # Facebook Marketplace (off unless PW_FB_QUERIES is set); logic in marketplace.py.
    try:
        from price_watcher import marketplace as _marketplace
        report.marketplace = _marketplace.scan_and_report()
    except Exception as exc:
        log.error("Marketplace stage failed: %s: %s", type(exc).__name__, exc)
    return report


def collect_vacations():
    """Run the vacation-deal stage. Returns a VacationReport, or None on a crash."""
    try:
        from price_watcher import vacations as _vacations
        return _vacations.scan_and_report()
    except Exception as exc:
        log.error("Vacation stage failed: %s: %s", type(exc).__name__, exc)
        return None


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
    if report.vacations is not None:
        console.print("\n[bold underline]Vacation deals[/bold underline]")
        from price_watcher import vacations as _vac
        _vac.print_report(report.vacations)
    if getattr(report, "marketplace", None) is not None:
        console.print("\n[bold underline]Facebook Marketplace[/bold underline]")
        from price_watcher import marketplace as _marketplace
        _marketplace.print_report(report.marketplace)


def _deals_only_recipients(require_email: bool = True) -> tuple[str, ...]:
    from price_watcher import config as _config
    return _config.load(require_email=require_email).email.deals_email_to


def run_digest(dry_run: bool = False, test: bool = False) -> int:
    """Run the combined daily report and email it.

    The full report goes to PW_EMAIL_TO. The vacation deals also go, as a
    separate deals-only email, to PW_DEALS_EMAIL_TO. With `test=True` only
    PW_EMAIL_TO is emailed and nothing is saved.
    """
    log.info("Running daily report%s", " (test: owner only)" if test else "")
    report = collect_daily_report()
    _print_daily_report(report)

    if dry_run:
        from price_watcher.email_sender import build_daily_payload, build_deals_payload
        payload = build_daily_payload(report)
        if payload is not None:
            console.print(f"\n[bold]WOULD SEND EMAIL[/bold] — Subject: {payload.subject}")
        deals_payload = build_deals_payload(report.vacations)
        if deals_payload is not None:
            console.print(
                f"[bold]WOULD SEND DEALS-ONLY EMAIL[/bold] to "
                f"{len(_deals_only_recipients(require_email=False))} extra recipient(s) — "
                f"Subject: {deals_payload.subject}"
            )
        return 0

    from price_watcher.email_sender import send_daily_email, send_deals_email
    sent = send_daily_email(report)
    if test:
        console.print("[yellow]Test run: deals-only recipients were not emailed.[/yellow]")
        return 0 if sent else 1
    # Independent of the main email: a failure there must not hold back the
    # deals-only recipients.
    send_deals_email(report.vacations, _deals_only_recipients())
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
        if getattr(report, "marketplace", None) is not None:
            from price_watcher import marketplace as _marketplace
            _marketplace.persist(report.marketplace)
    return 0 if sent else 1


def run_deals(dry_run: bool = False, test: bool = False, days: Optional[int] = None) -> int:
    """Scan vacation deals only and send the deals-only email.

    Normal run: goes to PW_DEALS_EMAIL_TO. `test=True`: goes to PW_EMAIL_TO only.
    `days` limits the scan to departures within that many days (overrides
    PW_DEALS_DAYS for this run).
    """
    import os
    if days is not None:
        os.environ["PW_DEALS_DAYS"] = str(max(0, days))
    from price_watcher import config as _config
    from price_watcher import vacations as _vac
    from price_watcher.email_sender import build_deals_payload, send_deals_email

    report = collect_vacations()
    if report is None:
        console.print("[red]Vacation scan crashed — see the log.[/red]")
        return 1
    _vac.print_report(report)
    payload = build_deals_payload(report)
    if payload is None:
        console.print("[yellow]No deals found — nothing to send.[/yellow]")
        return 1
    cfg = _config.load(require_email=not dry_run).email
    recipients = (cfg.email_to,) if test else cfg.deals_email_to
    if dry_run:
        console.print(
            f"\n[bold]WOULD SEND DEALS-ONLY EMAIL[/bold] to {len(recipients)} "
            f"recipient(s) — Subject: {payload.subject}"
        )
        return 0
    if not recipients:
        console.print("[yellow]PW_DEALS_EMAIL_TO is empty — nothing sent.[/yellow]")
        return 1
    sent = send_deals_email(report, recipients)
    console.print(
        f"[green]Deals email sent to {len(recipients)} recipient(s).[/green]" if sent
        else "[red]Deals email failed — see the log.[/red]"
    )
    return 0 if sent else 1
