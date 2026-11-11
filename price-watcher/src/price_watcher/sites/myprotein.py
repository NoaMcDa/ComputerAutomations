"""MyProtein site-specific extractor plugin (issue #11).

Reads the promo/offer banner from MyProtein pages and computes an
estimated final price by applying the discount percentage.
"""

from __future__ import annotations

import re
from dataclasses import replace
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlparse
from typing import Optional

from bs4 import BeautifulSoup

from price_watcher.contracts import FetchResult, PromoInfo
from price_watcher.logging_setup import get

log = get(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# "Up to N%" or the Hebrew equivalent "עד N%"
_RE_UP_TO = re.compile(
    r"(?:up\s+to|עד)\s+\d{1,2}\s*%",
    re.IGNORECASE | re.UNICODE,
)

# Standalone discount percentage (not preceded by "up to" / "עד")
_RE_PCT = re.compile(r"(\d{1,2})\s*%", re.UNICODE)

# Promo code — word "code"/"קוד" followed by up to 10 non-code chars, then
# the actual code (3-15 uppercase alphanumeric chars).
_RE_CODE = re.compile(
    r"(?:code|קוד)[^A-Z0-9]{0,10}([A-Z0-9]{3,15})",
    re.IGNORECASE | re.UNICODE,
)

# Common false-positive tokens that should be rejected when found on their own
# (without "code"/"קוד" nearby).
_FALSE_POSITIVES = {"SAVE", "OFFER", "SALE", "FREE", "NEW", "BUY"}

# Optional trailing condition after the code (up to ~100 chars, stopping at
# sentence boundaries).
_RE_CONDITION = re.compile(
    r"[A-Z0-9]{3,15}"           # the code itself
    r"[^A-Z0-9]{0,5}"           # small gap
    r"(.{1,100}?)(?:[.!?\n]|$)",
    re.UNICODE,
)


def _extract_pct(text: str) -> Optional[Decimal]:
    """Return the first non-'up-to' percentage found in *text*, or None."""
    # First remove all "up to N%" / "עד N%" spans so they don't match below.
    cleaned = _RE_UP_TO.sub("", text)
    m = _RE_PCT.search(cleaned)
    if m:
        return Decimal(m.group(1))
    return None


def _is_up_to(text: str) -> bool:
    """Return True if the only/first percentage match is an 'up to' one."""
    return bool(_RE_UP_TO.search(text)) and not bool(
        _RE_PCT.search(_RE_UP_TO.sub("", text))
    )


def _extract_code(text: str) -> Optional[str]:
    """Return the promo code extracted from *text*, or None."""
    m = _RE_CODE.search(text)
    if m:
        code = m.group(1).upper()
        return code
    return None


def _extract_condition(text: str, code: Optional[str]) -> Optional[str]:
    """Return the condition clause that follows *code* in *text*, or None."""
    if not code:
        return None
    idx = text.upper().find(code)
    if idx == -1:
        return None
    after = text[idx + len(code):]
    m = re.match(r"[^A-Z0-9]{0,5}(.{1,100}?)(?:[.!?\n]|$)", after, re.UNICODE)
    if m:
        cond = m.group(1).strip()
        return cond if cond else None
    return None


def _candidate_texts(soup: BeautifulSoup) -> list[str]:
    """Return candidate text snippets in priority order."""
    candidates: list[str] = []

    # 1. data-component attributes containing promo/offer/code
    for tag in soup.find_all(attrs={"data-component": True}):
        val: str = tag.get("data-component", "")
        if any(kw in val.lower() for kw in ("promo", "offer", "code")):
            txt = tag.get_text(" ", strip=True)
            if txt:
                candidates.append(txt)

    # 2. CSS class substring selectors
    for selector in (
        '[class*="promo"]',
        '[class*="offer"]',
        '[class*="banner"]',
        '[class*="code"]',
    ):
        for tag in soup.select(selector):
            txt = tag.get_text(" ", strip=True)
            if txt:
                candidates.append(txt)

    # 3. The header area
    header = soup.find("header")
    if header:
        txt = header.get_text(" ", strip=True)
        if txt:
            candidates.append(txt)

    # 4. Meta description
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        candidates.append(str(meta["content"]))

    return candidates


# ---------------------------------------------------------------------------
# Extractor class
# ---------------------------------------------------------------------------

class MyProteinExtractor:
    """SiteExtractor implementation for MyProtein."""

    # Hosts we claim to handle
    _HOSTS = {"myprotein.co.il", "myprotein.com"}

    def applies_to(self, url: str) -> bool:
        try:
            host = urlparse(url).hostname or ""
            # exact match or any *.myprotein.* subdomain
            if host in self._HOSTS:
                return True
            parts = host.split(".")
            return "myprotein" in parts
        except Exception:
            return False

    def extract(self, html: str, url: str, base_result: FetchResult) -> FetchResult:
        try:
            return self._extract(html, url, base_result)
        except Exception as exc:
            log.exception("MyProtein extractor failed for %s: %s", url, exc)
            return base_result

    def _extract(self, html: str, url: str, base_result: FetchResult) -> FetchResult:
        soup = BeautifulSoup(html, "lxml")
        candidates = _candidate_texts(soup)

        for raw_text in candidates:
            # Truncate source_text to 200 chars as spec requires
            source_text = raw_text[:200]

            # Check for "up to N%" — log and return a no-compute PromoInfo
            if _is_up_to(raw_text):
                log.info(
                    "MyProtein: 'up to' promo detected, not computing discount. "
                    "url=%s source=%r",
                    url,
                    source_text,
                )
                promo = PromoInfo(
                    code=None,
                    discount_pct=Decimal("0"),
                    condition=None,
                    source_text=source_text,
                )
                return replace(
                    base_result,
                    promo=promo,
                    estimated_final_price=base_result.price,
                )

            pct = _extract_pct(raw_text)
            if pct is None:
                # No parseable percentage in this candidate — try next
                continue

            code = _extract_code(raw_text)
            condition = _extract_condition(raw_text, code)

            promo = PromoInfo(
                code=code,
                discount_pct=pct,
                condition=condition,
                source_text=source_text,
            )

            # Compute estimated final price only when base price is available
            estimated: Optional[Decimal] = None
            if base_result.price is not None:
                estimated = (
                    base_result.price
                    * (Decimal("100") - pct)
                    / Decimal("100")
                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            log.info(
                "MyProtein: promo applied. code=%s pct=%s estimated=%s url=%s",
                code,
                pct,
                estimated,
                url,
            )
            return replace(base_result, promo=promo, estimated_final_price=estimated)

        # No promo found — return base result unchanged
        return base_result


# ---------------------------------------------------------------------------
# Module-level singleton required by sites/__init__.py
# ---------------------------------------------------------------------------

extractor = MyProteinExtractor()
