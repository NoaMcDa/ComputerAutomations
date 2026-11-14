"""Online butchers with home delivery, read alongside the supermarket feeds.

Each butcher returns `MeatPromo` rows like a supermarket chain, so its items
join the same per-cut tables (`supermarkets._finalize_report`): an item on
sale on the butcher's site is a deal (regular -> sale price), and every other
item competes at its regular price, ranked by price per kg. Only items with a
known price per kg can be ranked; the rest are left out.

  האחים אהרון   aharonbros.co.il   WooCommerce Store API (no login). The
                product page says "/ ק"ג" when the price is per kg, so each
                meat item's page is read once per scan to confirm the unit.
  מיטמן         order.meatman.co.il  its ordering site embeds the shop as
                JSON, with units ("קילו", "לק''ג") and multi-kg deals
                ('2 ק"ג ב130₪'), read as a price per kg. Delivers to
                Herzliya (min. order ₪400, fee ₪50 on 2026-11-14).
  מיט פוינט     meatpoint.co.il    Server-rendered category pages; each item
                says "מחיר ל 1.00 ק"ג" or "מחיר ל 1.00 מגש". The site shows
                no old prices, so its items compete at their regular price.

Not included: Zalman's (orders only through Tabit) and A Meat (only on
Wolt); their prices can only be read through those ordering apps.

PW_BUTCHERS=0 turns the butchers off.
"""

from __future__ import annotations

import html
import os
import re
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional
from urllib.parse import quote

import httpx

from price_watcher import logging_setup
from price_watcher.http_headers import USER_AGENT

log = logging_setup.get(__name__)

_HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "he-IL,he;q=0.9"}
DELIVERY = "משלוח עד הבית"
_SALE_NOTE = "מבצע באתר"

AHARON = ("האחים אהרון", "https://aharonbros.co.il")
MEATMAN = ("מיטמן", "https://meatman.co.il")
MEATPOINT = ("מיט פוינט", "https://www.meatpoint.co.il")

# Meat Point's category pages (the others are fish, salads, sauces, spices...).
_MEATPOINT_CATEGORIES = (
    "בקר", "על-עצם", "ללא-עצם", "טלה", "עוףהודו", "טחונים", "קפואים",
    "מיוחדים", "בחירת-הקצב", "מבצעי-החודש", "מוצרי-אווז",
)


def enabled() -> bool:
    return os.environ.get("PW_BUTCHERS", "1").strip().lower() not in ("0", "false", "no", "off")


def _client() -> httpx.Client:
    return httpx.Client(follow_redirects=True, timeout=60, headers=_HEADERS)


def _dec(value) -> Optional[Decimal]:
    try:
        d = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d > 0 else None


def _text(fragment: str) -> str:
    fragment = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", fragment, flags=re.S | re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


_WEIGHT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(ק\"ג|ק״ג|קג|קילו|גרם|גר'|גר|ג')(?![א-ת])"
)


def _kg_in_name(name: str) -> Optional[Decimal]:
    """Pack weight written in a product name ("500 גרם" -> 0.5), or None."""
    m = _WEIGHT_RE.search(name)
    if not m:
        return None
    amount = _dec(m.group(1))
    if amount is None:
        return None
    kg = amount if m.group(2) in ('ק"ג', "ק״ג", "קג", "קילו") else amount / 1000
    return kg if Decimal("0.05") <= kg <= Decimal("10") else None


def _promo(chain: str, name: str, regular: Decimal, sale: Optional[Decimal],
           kg: Optional[Decimal], code: str):
    from price_watcher.supermarkets import MeatPromo
    is_promo = sale is not None and sale < regular
    weighted = kg == Decimal("1")
    return MeatPromo(
        chain=chain, store_id=chain, item_code=code, item_name=name,
        regular_price=regular, discounted_price=sale if is_promo else None,
        discount_rate=None, promotion_description=_SALE_NOTE if is_promo else "",
        stores=[DELIVERY], is_weighted=weighted, unit_kg=kg,
    )


def _is_meat(name: str, context: str = "") -> bool:
    from price_watcher.supermarkets import _is_meat_product
    return _is_meat_product(name, context)


# ---- WooCommerce shops (Aharon Bros, Meatman) ----

def _woo_products(client: httpx.Client, base: str, max_pages: int = 20) -> list[dict]:
    items: list[dict] = []
    for page in range(1, max_pages + 1):
        r = client.get(f"{base}/wp-json/wc/store/v1/products", params={"per_page": 100, "page": page})
        r.raise_for_status()
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        items.extend(b for b in batch if isinstance(b, dict))
        if len(batch) < 100:
            break
    return items


def _woo_prices(item: dict) -> tuple[Optional[Decimal], Optional[Decimal]]:
    """(regular, sale) in shekels; sale is None when not on sale."""
    p = item.get("prices") or {}
    try:
        unit = Decimal(10) ** int(p.get("currency_minor_unit", 2))
    except (TypeError, ValueError):
        unit = Decimal(100)
    regular, price = _dec(p.get("regular_price")), _dec(p.get("price"))
    regular = regular / unit if regular else None
    price = price / unit if price else None
    if regular is None:
        regular, price = price, None
    sale = price if item.get("on_sale") and price is not None and regular and price < regular else None
    return regular, sale


def _woo_candidates(items: list[dict]) -> list[tuple[dict, str, Decimal, Optional[Decimal]]]:
    out = []
    for item in items:
        name = _text(str(item.get("name", "")))
        if not name or item.get("is_in_stock") is False:
            continue
        cats = " ".join(_text(c.get("name", "")) for c in item.get("categories") or [])
        if not _is_meat(name, cats):
            continue
        regular, sale = _woo_prices(item)
        if regular is None:
            continue
        out.append((item, name, regular, sale))
    return out


_PER_KG_PAGE_RE = re.compile(r'/\s*ק"ג|מחיר לק"ג|לק"ג')


def scan_aharon(client: Optional[httpx.Client] = None) -> list:
    chain, base = AHARON
    with (client or _client()) as c:
        candidates = _woo_candidates(_woo_products(c, base))

        def _per_kg(entry) -> bool:
            item = entry[0]
            url = item.get("permalink") or ""
            if not url:
                return False
            try:
                r = c.get(url)
            except httpx.HTTPError:
                return False
            return r.status_code == 200 and bool(_PER_KG_PAGE_RE.search(_text(r.text)))

        with ThreadPoolExecutor(max_workers=6) as pool:
            per_kg = list(pool.map(_per_kg, candidates))
    promos = []
    for (item, name, regular, sale), kg_priced in zip(candidates, per_kg):
        kg = Decimal("1") if kg_priced else _kg_in_name(name)
        promos.append(_promo(chain, name, regular, sale, kg, str(item.get("id", ""))))
    log.info("%s: %d meat items (%d on sale, %d priced per kg)", chain, len(promos),
             sum(p.is_promo for p in promos), sum(1 for p in promos if p.unit_kg is not None))
    return promos


# ---- Meatman (its ordering site, order.meatman.co.il) ----
#
# The catalogue page embeds the whole shop as JSON: `products = [...]` (name,
# price, units "קילו" / "לק''ג" / "יחידה", sell_by_weight, sale_text such as
# '2 ק"ג ב130₪!') and `all_cities = [...]` (delivery cities with minimum
# order and fee; Herzliya: min ₪400, fee ₪50 on 2026-11-14).

_MEATMAN_CATALOG = "https://order.meatman.co.il/front/catalog/1"
_KG_UNITS_RE = re.compile(r"^(?:קילו|ק\"ג|ק״ג|ק''ג|לק\"ג|לק״ג|לק''ג|לקילו)$")
# '2 ק"ג ב130₪!', "מבצע! 4 ק''ג ב50 ש''ח", "2 ק''ג בשר לגולש טרי ב150 ש''ח!"
_KG_DEAL_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*ק(?:''|\"|״|')?ג(?![א-ת]).*?(?:^|\s)ב-?\s*(\d+(?:\.\d+)?)"
)


def _js_var(page: str, name: str):
    """The JSON value assigned to `var/let/const <name> = ...` in a page."""
    import json
    m = re.search(r"(?:var|let|const)\s+" + re.escape(name) + r"\s*=\s*", page)
    if not m:
        return None
    try:
        return json.JSONDecoder().raw_decode(page, m.end())[0]
    except ValueError:
        return None


def _kg_deal(text: str) -> Optional[tuple[Decimal, Decimal]]:
    """(kg, price) of a 'N ק"ג ב-X' deal, or None."""
    m = _KG_DEAL_RE.search(_text(text))
    if not m:
        return None
    kg, price = _dec(m.group(1)), _dec(m.group(2))
    return (kg, price) if kg and price else None


def _meatman_promo(item: dict):
    name = _text(str(item.get("name", "")))
    price = _dec(item.get("price"))
    if not name or price is None or not item.get("is_active") or item.get("hidden"):
        return None
    if not _is_meat(name):
        return None
    units = _text(str(item.get("units") or "")).replace(" ", "")
    kg = Decimal("1") if _KG_UNITS_RE.match(units) else _kg_in_name(name)
    sale = None
    deal_text = _text(str(item.get("sale_text") or ""))
    before = _dec(item.get("price_before_discount"))
    if before and before > price:  # a plain price cut
        price, sale = before, price
    elif kg == Decimal("1") and deal_text:
        deal = _kg_deal(deal_text)
        if deal and deal[1] / deal[0] < price:  # "2 ק"ג ב-130": per kg
            sale = (deal[1] / deal[0]).quantize(Decimal("0.01"))
    p = _promo(MEATMAN[0], name, price, sale, kg, str(item.get("id", "")))
    if p.is_promo and deal_text:
        p.promotion_description = deal_text
    return p


def scan_meatman(client: Optional[httpx.Client] = None) -> list:
    chain = MEATMAN[0]
    with (client or _client()) as c:
        r = c.get(_MEATMAN_CATALOG)
        r.raise_for_status()
    products = _js_var(r.text, "products")
    if not isinstance(products, list):
        raise RuntimeError("product list not found on the ordering site")
    promos = [p for p in (_meatman_promo(i) for i in products if isinstance(i, dict)) if p]
    log.info("%s: %d meat items (%d on sale, %d priced per kg)", chain, len(promos),
             sum(p.is_promo for p in promos), sum(1 for p in promos if p.unit_kg is not None))
    return promos


# ---- Meat Point (server-rendered shop) ----

_MP_ITEM_RE = re.compile(r'<li itemident="(\d+)"')
_MP_NAME_RE = re.compile(r'ee_list_itemname="([^"]*)"')
_MP_PRICE_RE = re.compile(r'class="priceholder"[^>]*price="([\d.]+)"')
_MP_UNIT_RE = re.compile(r'מחיר ל\s*([\d.]+)\s*(ק"ג|ק״ג|גרם|מגש|יחידה|יח\')')


def _meatpoint_items(page: str) -> list[tuple[str, str, Decimal, Optional[Decimal]]]:
    """(id, name, price, kg per priced unit or None) of one category page."""
    starts = [m.start() for m in _MP_ITEM_RE.finditer(page)]
    out = []
    for a, b in zip(starts, starts[1:] + [len(page)]):
        block = page[a:b]
        ident = _MP_ITEM_RE.match(block).group(1)
        name_m, price_m = _MP_NAME_RE.search(block), _MP_PRICE_RE.search(block)
        if not name_m or not price_m:
            continue
        price = _dec(price_m.group(1))
        if price is None:
            continue
        name = html.unescape(name_m.group(1)).strip()
        kg = None
        unit_m = _MP_UNIT_RE.search(_text(block))
        if unit_m:
            amount = _dec(unit_m.group(1))
            if unit_m.group(2) in ('ק"ג', "ק״ג") and amount:
                kg = amount
            elif unit_m.group(2) == "גרם" and amount:
                kg = amount / 1000
        if kg is None:
            kg = _kg_in_name(name)
        out.append((ident, name, price, kg))
    return out


def scan_meatpoint(client: Optional[httpx.Client] = None) -> list:
    chain, base = MEATPOINT
    seen: dict[str, object] = {}
    with (client or _client()) as c:
        for cat in _MEATPOINT_CATEGORIES:
            r = c.get(f"{base}/category/{quote(cat)}")
            if r.status_code != 200:
                log.warning("%s: category %s: HTTP %s", chain, cat, r.status_code)
                continue
            page = r.content.decode("cp1255", errors="ignore")
            for ident, name, price, kg in _meatpoint_items(page):
                if ident in seen or not _is_meat(name):
                    continue
                seen[ident] = _promo(chain, name, price, None, kg, ident)
    if not seen:
        raise RuntimeError("no items read from the shop's category pages")
    promos = list(seen.values())
    log.info("%s: %d meat items (%d priced per kg)", chain, len(promos),
             sum(1 for p in promos if p.unit_kg is not None))
    return promos


SCANNERS: tuple[tuple[str, Callable[[], list]], ...] = (
    (AHARON[0], scan_aharon),
    (MEATMAN[0], scan_meatman),
    (MEATPOINT[0], scan_meatpoint),
)
