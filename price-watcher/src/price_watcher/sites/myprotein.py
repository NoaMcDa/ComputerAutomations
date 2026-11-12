"""MyProtein site-specific extractor plugin (issue #11).

Reads the promo/offer banner from MyProtein pages and computes an
estimated final price by applying the discount percentage.

Outcomes, in order of preference across the banner candidates:

- a readable percentage (with or without a code): ``PromoInfo`` with that
  percentage and ``estimated_final_price = price × (1 − pct)``;
- a tiered "up to N%" banner: ``PromoInfo(discount_pct=0)`` — not computable,
  the estimate stays at the displayed price;
- a code is advertised but no percentage can be read (unreadable banner):
  ``PromoInfo(code=..., discount_pct=0)`` — same fallback;
- nothing: the base result unchanged.

A ``discount_pct`` of 0 therefore means "a promotion exists but no estimate
can be made"; the state store never treats it as a change and the email says
the displayed price is shown as is.
"""

from __future__ import annotations

import re
from dataclasses import replace
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from price_watcher.contracts import FetchResult, PromoInfo
from price_watcher.digest import _CODE_KEYWORD
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

# Standalone discount percentage. Not part of a bigger number ("100%").
_RE_PCT = re.compile(r"(?<![\d.,])(\d{1,2})\s*%", re.UNICODE)

# Promo code: a code keyword (shared with the digest: "code", "קוד",
# "קוד הנחה", "בקוד הטבה", ...) then the code itself in capitals.
_RE_CODE = re.compile(
    r"(?i:" + _CODE_KEYWORD + r")[^A-Za-z0-9]{0,20}([A-Z][A-Z0-9]{2,19})(?![A-Za-z0-9])",
    re.UNICODE,
)

# Words that follow "code" in ordinary sentences and are not codes.
_FALSE_POSITIVES = {"SAVE", "OFFER", "SALE", "FREE", "NEW", "BUY", "CODE", "THE", "AT", "HERE"}

# Stated conditions of a promotion: minimum spend, selected items, end date.
_RE_CONDITIONS = [
    re.compile(p, re.IGNORECASE | re.UNICODE)
    for p in (
        r"(?:on\s+)?(?:orders?|spend(?:ing)?)\s+(?:over|of|above)\s+[₪$€£]?\s*\d+(?:[.,]\d+)*\s*[₪$€£]?",
        r"min(?:imum|\.)?\s+(?:spend|order)\s+(?:of\s+)?[₪$€£]?\s*\d+(?:[.,]\d+)*\s*[₪$€£]?",
        r"(?:on\s+)?select(?:ed)?\s+(?:items|products|lines|ranges)",
        r"(?:ends?|until|expires?)\s+(?:on\s+)?(?:\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?|midnight|tonight|today|sunday|monday|tuesday|wednesday|thursday|friday|saturday)",
        r"(?:בקנייה|בהזמנה|בקניה)\s+(?:של\s+)?מעל\s+[₪]?\s*\d+(?:[.,]\d+)*\s*(?:₪|ש\"ח|שח)?",
        r"(?:על\s+)?(?:מוצרים|פריטים)\s+נבחרים",
        r"(?:בתוקף\s+)?עד\s+(?:ה-?|לתאריך\s+)?\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?",
    )
]


def _extract_pct(text: str) -> Optional[Decimal]:
    """Return the first non-'up-to' percentage found in *text*, or None."""
    # First remove all "up to N%" / "עד N%" spans so they don't match below.
    cleaned = _RE_UP_TO.sub("", text)
    for m in _RE_PCT.finditer(cleaned):
        pct = int(m.group(1))
        if 1 <= pct <= 99:
            return Decimal(pct)
    return None


def _is_up_to(text: str) -> bool:
    """True if the banner only advertises an "up to N%" (tiered) discount."""
    return bool(_RE_UP_TO.search(text)) and _extract_pct(text) is None


def _extract_code(text: str) -> Optional[str]:
    """Return the promo code extracted from *text*, or None."""
    for m in _RE_CODE.finditer(text):
        code = m.group(1)
        if code not in _FALSE_POSITIVES:
            return code
    return None


def _extract_condition(text: str) -> Optional[str]:
    """Return the stated conditions (minimum spend, selected items, end date)."""
    found: list[str] = []
    for pat in _RE_CONDITIONS:
        for m in pat.finditer(text):
            cond = re.sub(r"\s+", " ", m.group(0)).strip()
            if cond and cond not in found:
                found.append(cond)
    return "; ".join(found)[:200] if found else None


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
        fallback: Optional[PromoInfo] = None  # tiered or unreadable banner

        for raw_text in _candidate_texts(soup):
            source_text = raw_text[:200]
            pct = _extract_pct(raw_text)
            code = _extract_code(raw_text)

            if pct is None:
                if fallback is None and _is_up_to(raw_text):
                    log.info(
                        "MyProtein: 'up to' promo detected, not computing discount. "
                        "url=%s source=%r", url, source_text,
                    )
                    fallback = PromoInfo(
                        code=code,
                        discount_pct=Decimal("0"),
                        condition=_extract_condition(raw_text),
                        source_text=source_text,
                    )
                elif fallback is None and code is not None:
                    log.info(
                        "MyProtein: code %s advertised but no percentage readable. "
                        "url=%s source=%r", code, url, source_text,
                    )
                    fallback = PromoInfo(
                        code=code,
                        discount_pct=Decimal("0"),
                        condition=_extract_condition(raw_text),
                        source_text=source_text,
                    )
                continue

            promo = PromoInfo(
                code=code,
                discount_pct=pct,
                condition=_extract_condition(raw_text),
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
                code, pct, estimated, url,
            )
            return replace(base_result, promo=promo, estimated_final_price=estimated)

        if fallback is not None:
            return replace(
                base_result, promo=fallback, estimated_final_price=base_result.price
            )
        # No promo found — return base result unchanged
        return base_result


# ---------------------------------------------------------------------------
# Module-level singleton required by sites/__init__.py
# ---------------------------------------------------------------------------

extractor = MyProteinExtractor()
