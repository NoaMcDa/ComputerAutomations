"""MyProtein site-specific extractor plugin (issue #11).

Reads the promo/offer banner from MyProtein pages and computes an
estimated final price by applying the discount percentage.

Banner parsing is shared with the daily digest (``digest.parse_offers``):
the banner is split into offers on ``|`` and line breaks, and a code is only
paired with a percentage from the same offer. A percentage needs a discount
word next to it ("off", "extra", "הנחה"), so "100% Whey" or "80% protein"
are never discounts, and codes must be upper-case after a code keyword.

Which offer becomes the PromoInfo when a banner lists several, e.g.
"30% Off EVERYTHING | Applies as Basket | Extra 5% on App using code: APP5":
  1. A site-wide offer without a code (30%, code=None) wins: it applies
     automatically at the basket, so it is what the shopper actually pays.
     Extra coded offers on top (app-only / first-order) are not applied.
  2. Otherwise the first offer with a code, paired from the same segment.
  3. Otherwise an "up to N%" banner gives discount 0 (not computable).
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlparse
from typing import Optional

from bs4 import BeautifulSoup

from price_watcher.contracts import FetchResult, PromoInfo
from price_watcher.digest import element_text, has_up_to_discount, parse_offers
from price_watcher.logging_setup import get

log = get(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _candidate_texts(soup: BeautifulSoup) -> list[str]:
    """Return candidate text snippets in priority order."""
    candidates: list[str] = []

    # 1. data-component attributes containing promo/offer/code
    for tag in soup.find_all(attrs={"data-component": True}):
        val: str = tag.get("data-component", "")
        if any(kw in val.lower() for kw in ("promo", "offer", "code")):
            txt = element_text(tag)
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
            txt = element_text(tag)
            if txt:
                candidates.append(txt)

    # 3. The header area
    header = soup.find("header")
    if header:
        txt = element_text(header)
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

        sitewide: Optional[PromoInfo] = None
        coded: Optional[PromoInfo] = None
        up_to_source: Optional[str] = None
        for raw_text in candidates:
            # Truncate source_text to 200 chars as spec requires
            source_text = raw_text[:200]
            for offer in parse_offers(raw_text):
                offer = replace(offer, source_text=source_text)
                if offer.code is None and sitewide is None:
                    sitewide = offer
                elif offer.code is not None and coded is None:
                    coded = offer
            if up_to_source is None and has_up_to_discount(raw_text):
                up_to_source = source_text
            if sitewide is not None:
                break

        promo = sitewide or coded
        if promo is None and up_to_source is not None:
            # "Up to N%" — log and return a no-compute PromoInfo
            log.info(
                "MyProtein: 'up to' promo detected, not computing discount. "
                "url=%s source=%r",
                url,
                up_to_source,
            )
            promo = PromoInfo(
                code=None,
                discount_pct=Decimal("0"),
                condition=None,
                source_text=up_to_source,
            )
            return replace(
                base_result,
                promo=promo,
                estimated_final_price=base_result.price,
            )
        if promo is None:
            # No promo found — return base result unchanged
            return base_result

        pct = promo.discount_pct
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
            promo.code,
            pct,
            estimated,
            url,
        )
        return replace(base_result, promo=promo, estimated_final_price=estimated)


# ---------------------------------------------------------------------------
# Module-level singleton required by sites/__init__.py
# ---------------------------------------------------------------------------

extractor = MyProteinExtractor()
