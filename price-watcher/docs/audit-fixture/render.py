"""Render the daily email from invented fixtures through the real pipeline.

usage: render.py <price-watcher dir> <out dir>
"""
from __future__ import annotations

import json
import os
import random
import sys
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "src"))
os.environ["PW_DATA_DIR"] = tempfile.mkdtemp()
for k in list(os.environ):
    if k.startswith("PW_") and k != "PW_DATA_DIR":
        del os.environ[k]

from price_watcher import supermarkets as sm  # noqa: E402
from price_watcher import vacations as V  # noqa: E402
from price_watcher import sims4, creatine, digest, email_sender  # noqa: E402
from price_watcher.contracts import PromoInfo  # noqa: E402

TODAY = date.today()
FIX = ROOT / "tests" / "fixtures"

# ---------------------------------------------------------------- meat ----
# (code, name, weighted, price, extra item fields)
W = {"bIsWeighted": "1", "UnitQty": "קילוגרם", "Quantity": "1.00", "UnitOfMeasure": 'ק"ג'}


def pack(grams):
    return {"bIsWeighted": "0", "UnitQty": "גרם", "Quantity": f"{grams}.00", "UnitOfMeasure": "100 גרם"}


ITEMS = [
    ("1001", "חזה עוף טרי", 39.90, W),
    ("1002", "כרעיים עוף טרי", 24.90, W),
    ("1003", "פרגיות עוף טרי", 64.90, W),
    ("1004", "שוקיים עוף טרי", 27.90, W),
    ("1005", "כנפיים עוף טרי", 16.90, W),
    ("1006", "עוף שלם טרי", 19.90, W),
    ("1007", "כבד עוף טרי", 22.90, W),
    ("1008", "לבבות עוף קפוא", 29.90, W),
    ("1009", "שניצל עוף טרי דק", 49.90, W),
    ("1010", "טחון עוף טרי 500 גרם", 21.90, pack(500)),
    ("1011", "חזה הודו טרי", 44.90, W),
    ("1012", "גרון הודו קפוא", 17.90, W),
    ("1013", "שווארמה הודו נקבה טרי", 54.90, W),
    ("1014", "אנטריקוט טרי", 169.90, W),
    ("1015", "סינטה בקר טרי", 129.90, W),
    ("1016", "שייטל בקר טרי", 92.90, W),
    ("1017", "אסאדו בקר טרי עם עצם", 82.90, W),
    ("1018", "בשר בקר טחון טרי", 61.90, W),
    ("1019", "בשר טחון בקר טרי 500 גרם", 34.90, pack(500)),
    ("1020", "פילה מדומה בקר טרי מס 6", 87.90, W),
    ("1021", "צלי כתף בקר מס 5 טרי", 79.90, W),
    ("1022", "שריר בקר מס 8 טרי", 74.90, W),
    ("1023", "אוסובוקו בקר טרי", 69.90, W),
    ("1024", "גולש בקר טרי", 72.90, W),
    ("1025", "לשון בקר קפוא", 99.90, W),
    ("1026", "כבד בקר טרי", 34.90, W),
    ("1027", "עצמות מוח בקר קפוא", 19.90, W),
    ("1028", "עצמות בקר למרק קפוא", 14.90, W),
    ("1029", "זנב בקר מח. טרי", 59.90, W),
    ("1030", "פיקניה בקר טרי", 119.90, W),
    ("1031", "דנבר בקר טרי", 109.90, W),
    ("1032", "בריסקט בקר טרי מס 3", 69.90, W),
    ("1033", "צלעות טלה טרי", 189.90, W),
    ("1034", "כתף כבש טרי", 119.90, W),
    ("1035", "מח עצם בקר קפוא 400 גרם", 24.90, pack(400)),
    ("1036", "אנטריקוט בקר טרי מיושן 21 יום פרימיום אנגוס כשר למהדרין בד״ץ משק יש לנו", 219.90, W),
    # non-meat noise the classifier must reject
    ("2001", "עצם לעיסה לכלב 3 יח", 12.90, {"bIsWeighted": "0", "UnitQty": "יחידה", "Quantity": "3"}),
    ("2002", "ציר עצמות בקר 500 מל", 18.90, {"bIsWeighted": "0", "UnitQty": "מיליליטר", "Quantity": "500"}),
]

# chain -> (price factor, [(store id, name, city)], promos)
# promo: (description, [codes], fields, days until end, branches or None for all)
P = lambda desc, codes, fields, end=5, only=None, clubs=None: (desc, codes, fields, end, only, clubs)  # noqa: E731

CHAINS = {
    "שופרסל": (1.00, [(11, "שלי הרצליה סוקולוב", "הרצליה"), (12, "דיל הרצליה פיתוח", "הרצליה"), (13, "שלי רמת השרון", "רמת השרון")], [
        P('מעל 99 ש"ח פרגיות 39.90 לקג מוג 2', ["1003"], {"DiscountedPrice": "39.90", "MinQty": "1"}, 2),
        P("חזה עוף טרי מוג 3", ["1001"], {"DiscountedPrice": "29.90", "MinQty": "1"}, 3, only=[11]),
        P("כרעיים עוף טרי 2 קג ב 40 ע.מלאי", ["1002"], {"DiscountedPrice": "40.00", "MinQty": "2"}, 1),
        P("אנטריקוט טרי 2 ב110 ס.בודדים", ["1014"], {"DiscountedPrice": "110.00", "MinQty": "2"}, 4),
        P("אסאדו 1+1", ["1017"], {"MinQty": "2"}, 6),
        P("עצמות מוח בקר קפוא 2 קג ב 30", ["1027"], {"DiscountedPrice": "30.00", "MinQty": "2"}, 6),
        P("מועדון: שייטל ב 69.90", ["1016"], {"DiscountedPrice": "69.90", "MinQty": "1"}, 6, clubs=["2"]),
        P("לשון בקר קפוא", ["1025"], {"DiscountedPrice": "84.90", "MinQty": "1"}, 9),
    ]),
    "קרפור": (1.04, [(21, "קרפור מרקט הרצליה", "הרצליה"), (22, "קרפור סיטי רמת השרון", "רמת השרון")], [
        P("ת.קצר בשר טחון בקר טרי 500 גרם", ["1019"], {"DiscountedPrice": "60.00", "MinQty": "2"}, 1),
        P("סינטה בקר טרי מוג 2 קג ס.בודדים ע.מלאי ת.קצר", ["1015"], {"DiscountedPrice": "99.90", "MinQty": "1"}, 1),
        P("אסאדו 1+1", ["1017"], {"DiscountRate": "50", "MinQty": "2"}, 6),
        P("כרעיים עוף טרי 2 קג ב 40", ["1002"], {"DiscountedPrice": "40.00", "MinQty": "2"}, 3),
        P("מח עצם בקר קפוא 400 גרם 2 ב 40", ["1035"], {"DiscountedPrice": "40.00", "MinQty": "2"}, 5),
        P("פיקניה בקר טרי מבצע", ["1030"], {"DiscountedPrice": "99.90", "MinQty": "1"}, 7),
        P("בריסקט בקר טרי מס 3", ["1032"], {"DiscountedPrice": "59.90", "MinQty": "1"}, 7),
    ]),
    "רמי לוי": (0.93, [(31, "רמי לוי הרצליה", "הרצליה")], [
        P("בשר בקר טחון טרי 2 קג ב 100", ["1018"], {"DiscountedPrice": "100.00", "MinQty": "2"}, 2),
        P("עוף שלם טרי מוג 2 יח ע.מלאי", ["1006"], {"DiscountedPrice": "14.90", "MinQty": "1"}, 2),
        P("כרעיים עוף טרי 2 קג ב 40", ["1002"], {"DiscountedPrice": "40.00", "MinQty": "2"}, 3),
        P("שייטל בקר טרי", ["1016"], {"DiscountRate": "2310", "MinQty": "1"}, 4),
        P("בקניית 2 קג ומעלה שוקיים 19.90", ["1004"], {"DiscountedPrice": "19.90", "MinQty": "2"}, 4),
        P("גולש בקר טרי", ["1024"], {"DiscountedPrice": "59.90", "MinQty": "1"}, 8),
        P("כתף כבש טרי ע.מלאי", ["1034"], {"DiscountedPrice": "99.90", "MinQty": "1"}, 2),
    ]),
    "יוחננוף": (0.97, [(41, "יוחננוף הרצליה", "הרצליה")], [
        P("10% הנחה על כל מחלקת הבשר", ["1014", "1015", "1016", "1020", "1021", "1022", "1023", "1030", "1031"], {"DiscountRate": "10", "MinQty": "1"}, 3),
        P("פילה מדומה מס 6 טרי", ["1020"], {"DiscountedPrice": "72.90", "MinQty": "1"}, 3),
        P("גרון הודו קפוא ס.בודדים", ["1012"], {"DiscountedPrice": "12.90", "MinQty": "1"}, 5),
        P("כנפיים עוף טרי 3 קג ב 40", ["1005"], {"DiscountedPrice": "40.00", "MinQty": "3"}, 5),
        P("חזה הודו טרי מוג 4 קג", ["1011"], {"DiscountedPrice": "36.90", "MinQty": "1"}, 5),
        P("עצמות מוח בקר קפוא", ["1027"], {"DiscountedPrice": "15.90", "MinQty": "1"}, 5),
    ]),
    "אושר עד": (0.90, [(51, "אושר עד הרצליה", "הרצליה")], [
        P("כרעיים עוף טרי 2 קג ב 40", ["1002"], {"DiscountedPrice": "40.00", "MinQty": "2"}, 3),
        P("שניצל עוף טרי דק מוג 3 קג", ["1009"], {"DiscountedPrice": "39.90", "MinQty": "1"}, 3),
        P("כבד עוף טרי", ["1007"], {"DiscountedPrice": "17.90", "MinQty": "1"}, 3),
        P("אוסובוקו בקר טרי", ["1023"], {"DiscountedPrice": "54.90", "MinQty": "1"}, 6),
        P("צלעות טלה טרי ת.קצר", ["1033"], {"DiscountedPrice": "159.90", "MinQty": "1"}, 1),
        P("טחון עוף טרי 500 גרם 3 ב 50", ["1010"], {"DiscountedPrice": "50.00", "MinQty": "3"}, 6),
    ]),
    "טיב טעם": (1.12, [(61, "טיב טעם הרצליה", "הרצליה"), (62, "טיב טעם אין דה סיטי רמת השרון", "רמת השרון")], [
        P("אנטריקוט בקר מיושן פרימיום", ["1036"], {"DiscountedPrice": "189.90", "MinQty": "1"}, 6),
        P("דנבר בקר טרי", ["1031"], {"DiscountedPrice": "89.90", "MinQty": "1"}, 6),
        P("שריר בקר מס 8 טרי 2 קג ב 130", ["1022"], {"DiscountedPrice": "130.00", "MinQty": "2"}, 6),
        P("שווארמה הודו נקבה טרי", ["1013"], {"DiscountedPrice": "44.90", "MinQty": "1"}, 6),
        P("לבבות עוף קפוא", ["1008"], {"DiscountedPrice": "19.90", "MinQty": "1"}, 6),
        P("זנב בקר טרי", ["1029"], {"DiscountedPrice": "49.90", "MinQty": "1"}, 6),
    ]),
    "סופר ברקת": (0.88, [(71, "ברקת הרצליה", "הרצליה"), (72, "ברקת רמת השרון", "רמת השרון")], [
        P("כבד בקר טרי", ["1026"], {"DiscountedPrice": "27.90", "MinQty": "1"}, 4),
        P("צלי כתף בקר מס 5 טרי מוג 3 קג", ["1021"], {"DiscountedPrice": "64.90", "MinQty": "1"}, 4),
        P("חזה עוף טרי 2 קג ב 60", ["1001"], {"DiscountedPrice": "60.00", "MinQty": "2"}, 4),
        P("פרגיות עוף טרי", ["1003"], {"DiscountedPrice": "49.90", "MinQty": "1"}, 4),
        P("עצמות בקר למרק קפוא", ["1028"], {"DiscountedPrice": "9.90", "MinQty": "1"}, 4),
        P("סינטה בקר טרי", ["1015"], {"DiscountedPrice": "104.90", "MinQty": "1"}, 4),
    ]),
}


def _price_xml(factor: float) -> bytes:
    rows = []
    for code, name, price, extra in ITEMS:
        p = round(price * factor, 1) - 0.0
        p = f"{(int(p) + 0.9):.2f}" if factor != 1 else f"{price:.2f}"
        fields = {"ItemCode": code, "ItemName": name, "ItemPrice": p, "ManufactureName": "משק", **extra}
        if extra.get("UnitOfMeasure") == "100 גרם":
            fields["UnitOfMeasurePrice"] = f"{float(p) / (float(extra['Quantity']) / 100):.2f}"
        rows.append("<Item>" + "".join(f"<{k}>{v}</{k}>" for k, v in fields.items()) + "</Item>")
    return ("<Root><Items>" + "".join(rows) + "</Items></Root>").encode()


def _promo_xml(promos, store_id: int) -> bytes:
    out = []
    for i, (desc, codes, fields, end, only, clubs) in enumerate(promos):
        if only and store_id not in only:
            continue
        club = "".join(f"<ClubId>{c}</ClubId>" for c in (clubs or ["0"]))
        out.append(
            f"<Promotion><PromotionId>{i}</PromotionId><PromotionDescription>{desc}</PromotionDescription>"
            f"<PromotionEndDate>{TODAY + timedelta(days=end)}</PromotionEndDate>"
            + "".join(f"<{k}>{v}</{k}>" for k, v in fields.items())
            + f"<Clubs>{club}</Clubs><PromotionItems>"
            + "".join(f"<Item><ItemCode>{c}</ItemCode></Item>" for c in codes)
            + "</PromotionItems></Promotion>"
        )
    return ("<Root><Promotions>" + "".join(out) + "</Promotions></Root>").encode()


def _chain_task(name):
    factor, stores, promos = CHAINS[name]
    infos = [sm.StoreInfo(sid, sname, city, "השרון") for sid, sname, city in stores]
    return lambda: sm._scan_chain(name, infos, lambda s: (_price_xml(factor), _promo_xml(promos, s.store_id)))


def _fail():
    raise sm.ChainScanError("הורדת הקבצים נכשלה ב-2 סניפים")


def _nobranch():
    return sm._scan_chain("פרש מרקט", [], lambda s: None)


def meat_tasks():
    tasks = [(n, _chain_task(n)) for n in CHAINS]
    tasks.append(("קשת טעמים", _fail))
    tasks.append(("פרש מרקט", _nobranch))
    return tasks


# ------------------------------------------------------------ vacations ----
EUROPE = [("פראג", "צ׳כיה"), ("בודפשט", "הונגריה"), ("אתונה", "יוון"), ("רומא", "איטליה"), ("ברצלונה", "ספרד"),
          ("וינה", "אוסטריה"), ("בוקרשט", "רומניה"), ("סופיה", "בולגריה"), ("לרנקה", "קפריסין"), ("רודוס", "יוון"),
          ("מילאנו", "איטליה"), ("ליסבון", "פורטוגל"), ("לונדון", "אנגליה"), ("פריז", "צרפת"), ("אמסטרדם", "הולנד"),
          ("ברלין", "גרמניה"), ("ורשה", "פולין"), ("קרקוב", "פולין"), ("בלגרד", "סרביה"), ("טביליסי", "גאורגיה")]
FAR = [("בנגקוק", "תאילנד"), ("פוקט", "תאילנד"), ("טוקיו", "יפן"), ("דובאי", ""), ("ניו יורק", "ארצות הברית"),
       ("מיאמי", "ארהב"), ("מרקש", "מרוקו"), ("זנזיבר", "טנזניה"), ("ריו דה ז׳נרו", "ברזיל"), ("סידני", "אוסטרליה")]
HOTELS = ["Grand Central", "Marriott Old Town", "Hilton Garden Inn City Centre", "Leonardo Royal",
          "The Old Town Residence Luxury Collection by Example Hotels", "NH Collection", "Ibis Styles",
          "Radisson Blu Plaza", "Golden Sands Resort & Spa", "Art Hotel"]
BOARDS = ["ארוחת בוקר", "חצי פנסיון", "הכל כלול", "לינה בלבד"]
AIRLINES = ["אל על", "ארקיע", "ישראייר", "Wizz Air", "Ryanair", "Blue Bird"]
LUG = [V.LUGGAGE_SUITCASE, V.LUGGAGE_CARRY_ON, V.LUGGAGE_NONE, V.LUGGAGE_UNKNOWN]
SOURCES_OK = ["איסתא", "אשת טורס", "השטיח המעופף", "גוליבר", "חוליו", "טיסות סודיות", V._HOLIDAY_FINDER]


def _deals(source: str, rnd: random.Random) -> list:
    out = []
    for i in range(40):
        far = rnd.random() < 0.3
        dest, country = rnd.choice(FAR if far else EUROPE)
        start = TODAY + timedelta(days=rnd.randint(0, 300 if far else 60))
        nights = rnd.choice([2, 3, 3, 4, 4, 5, 5, 6, 7])
        is_pkg = source != "טיסות סודיות" and rnd.random() < 0.6
        base = rnd.randint(700, 1900) if far else rnd.randint(79, 620)
        lug = rnd.choice(LUG)
        if is_pkg:
            price = Decimal(base + rnd.randint(150, 500))
            stars = rnd.choice([None, Decimal("2"), Decimal("3"), Decimal("3"), Decimal("4"), Decimal("4"), Decimal("4.5"), Decimal("5")])
            adults, children = rnd.choice([(2, 0)] * 6 + [(2, 2), (0, 0), (1, 0)])
            hotel = rnd.choice(HOTELS)
            board = rnd.choice(BOARDS)
            inc = V._includes("טיסה הלוך ושוב", V._hotel_line(hotel, stars), board,
                              "העברות" if rnd.random() < 0.4 else "", "חדר דלוקס זוגי עם נוף לים ומרפסת" if rnd.random() < 0.3 else "")
            out.append(V.VacationDeal(
                source=source, kind=V.KIND_PACKAGE, destination=dest, country=country, price=price,
                url=f"https://example-travel.co.il/deal/{source}/{i}?utm_source=pw&utm_medium=email&fdate={start}&nights={nights}&adt1={adults}",
                date_from=start, date_to=start + timedelta(days=nights), nights=nights, hotel=hotel,
                note=" · ".join([board, rnd.choice(AIRLINES)]), includes=inc, adults=adults, children=children,
                total_price=price * (adults + children) if adults else None, stars=stars, luggage=lug,
                luggage_extra="טרולי $25 לכיוון" if lug == V.LUGGAGE_NONE and rnd.random() < 0.5 else "",
            ))
        else:
            out.append(V.VacationDeal(
                source=source, kind=V.KIND_FLIGHT, destination=dest, country=country, price=Decimal(base),
                url=f"https://example-travel.co.il/flight/{source}/{i}?utm_source=pw&utm_medium=email&fdate={start}",
                date_from=start, date_to=start + timedelta(days=nights), nights=nights,
                note=" · ".join([rnd.choice(AIRLINES), rnd.choice(["טיסה ישירה", "עצירה אחת", ""])]).strip(" ·"),
                luggage=lug,
            ))
    return out


def vacation_sources():
    def ok(name):
        return lambda: _deals(name, random.Random("v-" + name))

    def bad():
        raise RuntimeError("HTTP 503 from travelist")
    srcs = []
    for name, _fn in V._SOURCES:
        srcs.append((name, ok(name) if name in SOURCES_OK else bad))
    return tuple(srcs)


# ------------------------------------------------------- other sections ----
def site_digests():
    out = []
    for host, disp, fname in [("www.myprotein.co.il", "MyProtein", "myprotein_home.html"),
                              ("www.castro.com", "Castro", "castro_home.html"),
                              ("www.terminalx.com", "Terminal X", "terminalx_home.html"),
                              ("seestarz.com", "Seestarz", "seestarz_home.html")]:
        promos = digest.extract_promos((FIX / "promos" / fname).read_text(encoding="utf-8"))
        out.append(digest.SiteDigest(host=host, display_name=disp, homepage_url=f"https://{host}/", promos=promos))
    out.append(digest.SiteDigest(host="www.adikastyle.com", display_name="Adika", homepage_url="https://www.adikastyle.com/",
                                 promos=[PromoInfo(code=None, discount_pct=Decimal("30"), condition="30% OFF EVERYTHING"),
                                         PromoInfo(code="EXTRA11", discount_pct=Decimal("11"), condition="EXTRA 11% על כל האתר בקנייה מעל 299 ש״ח, ללא כפל מבצעים")]))
    out.append(digest.SiteDigest(host="www.fox.co.il", display_name="Fox", homepage_url="https://www.fox.co.il/",
                                 status="blocked", error="HTTP 403"))
    return out


def creatine_report():
    html = "".join((FIX / f).read_text(encoding="utf-8") for f in ("creatine_iherb_search_p1.html", "creatine_iherb_search_p2.html"))
    products, cells = creatine._parse_cells(html)
    powders = sorted([p for p in products if creatine._is_powder(p)], key=lambda p: p.price_per_100g)
    cheapest = creatine.pick_cheapest(products)
    return creatine.CreatineReport(status="ok", products=powders[:10], cheapest=cheapest, previous_cheapest=cheapest,
                                   changed=False, cells_seen=cells, parsed=len(products), powders=len(powders))


def sims_report():
    names = ["Seasons", "Cats & Dogs", "City Living", "Get to Work", "Get Together", "Island Living", "Discover University",
             "Eco Lifestyle", "Snowy Escape", "Cottage Living", "High School Years", "Growing Together", "Horse Ranch",
             "For Rent", "Lovestruck", "Life & Death", "Businesses & Hobbies", "Enchanted by Nature", "Parenthood",
             "Vampires", "Realm of Magic", "Jungle Adventure", "Dine Out", "Spa Day", "Outdoor Retreat", "Strangerville",
             "Dream Home Decorator", "My Wedding Stories", "Werewolves", "Journey to Batuu"]
    rnd = random.Random("sims")
    packs = []
    for i, n in enumerate(names):
        orig = Decimal("39.99") if i < 19 else Decimal("19.99")
        pct = rnd.choice([25, 30, 40, 50, 60]) if i < 22 else 0
        price = (orig * (100 - pct) / 100).quantize(Decimal("0.01"))
        packs.append(sims4.Sims4Pack(slug="the-sims-4-" + n.lower().replace(" & ", "-and-").replace(" ", "-"),
                                     name="The Sims 4 " + n, price=price, original_price=orig, discount_percent=pct))
    return sims4.Sims4Report(status="ok", packs=packs, on_sale_now=[p for p in packs if p.is_on_sale])


# ------------------------------------------------------------- render ----
def main():
    sm._chain_tasks = meat_tasks
    extra = {}
    try:
        from price_watcher import butchers as B  # only on feat/butchers
        extra["butchers_module"] = True
        globals()["B"] = B
    except ImportError:
        B = None
    if B is not None:
        D = Decimal
        rnd = random.Random("butchers")
        def butcher(chain, factor, sale_codes):
            def scan():
                rows = []
                for code, name, price, extra_f in ITEMS[:36]:
                    if rnd.random() < 0.25:
                        continue
                    kg = D(1) if extra_f is W else D(extra_f["Quantity"].split(".")[0]) / 1000
                    reg = D(str(round(price * factor))) + D("0.00")
                    if kg != 1:
                        reg = (reg * kg).quantize(D("1"))
                    sale = (reg * D("0.85")).quantize(D("1")) if code in sale_codes else None
                    rows.append(B._promo(chain, name.replace(" טרי", "") + (" פרימיום" if factor > 1.3 else ""), reg, sale,
                                         None if code in ("1007", "1026") else kg, code))
                return rows
            return scan
        base_tasks = meat_tasks
        def tasks_with_butchers():
            t = base_tasks()
            t.append(("האחים אהרון", lambda s=butcher("האחים אהרון", 1.25, {"1014", "1018", "1001"}): (s(), [])))
            t.append(("מיטמן", lambda s=butcher("מיטמן", 1.4, {"1015", "1017", "1033", "1002"}): (s(), [])))
            t.append(("מיט פוינט", lambda s=butcher("מיט פוינט", 1.15, set()): (s(), [])))
            return t
        sm._chain_tasks = tasks_with_butchers
    meat = sm.scan_and_report()
    V._SOURCES = vacation_sources()
    vac = V.scan_and_report()
    report = digest.DailyReport(site_digests=site_digests(), creatine=creatine_report(), sims4=sims_report(),
                                supermarkets=meat, vacations=vac)
    payload = email_sender.build_daily_payload(report)
    (OUT / "email.html").write_text(payload.html_body, encoding="utf-8")
    (OUT / "email.txt").write_text(payload.text_body, encoding="utf-8")
    (OUT / "subject.txt").write_text(payload.subject, encoding="utf-8")

    # per-section byte sizes
    html = payload.html_body
    marks = ["מבצעים כלל-אתריים", "קריאטין", "הסימס 4", "מבצעי בשר בסופרמרקטים", email_sender._VACATION_TITLE]
    pos = []
    for m in marks:
        needle = email_sender._section_heading_html(m, first=(m == marks[0]))
        pos.append(html.find(needle))
    stats = {
        "html_bytes": len(html.encode()), "text_bytes": len(payload.text_body.encode()),
        "text_lines": payload.text_body.count("\n") + 1, "subject_len": len(payload.subject),
        "section_offsets": dict(zip(marks, [len(html[:p].encode()) if p >= 0 else -1 for p in pos])),
        "meat": {
            "rows": len(meat.promos), "tables": len({p.category for p in meat.promos}),
            "regular_rows": sum(1 for p in meat.promos if not p.is_promo),
            "chains_scanned": meat.chains_scanned, "failed": meat.chains_failed, "empty": meat.chains_empty,
            "by_category": meat.promos_by_category, "unpriced": meat.unpriced_by_category,
            "unranked": meat.unranked_by_category, "stores": len(meat.stores_scanned),
        },
        "vac": {"packages": len(vac.packages), "flights": len(vac.flights), "total_found": vac.total_found,
                "dropped_length": vac.dropped_length, "dropped_party": vac.dropped_party,
                "dropped_low_star": vac.dropped_low_star, "unmapped": vac.unmapped,
                "sources": [(s.name, s.ok, s.count) for s in vac.sources]},
        "sims_on_sale": len(report.sims4.on_sale_now), "creatine_rows": len(report.creatine.products),
        **extra,
    }
    (OUT / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False, indent=1, default=str))
    print("SUBJECT:", payload.subject)
    # meat table dump
    cur = None
    lines = []
    for p in meat.promos:
        if p.category != cur:
            cur = p.category
            lines.append(f"\n## {cur}  ({meat.promos_by_category.get(cur)})")
        lines.append(f"{p.chain:10} | {p.item_name[:40]:40} | {p.price_per_kg} | reg {p.regular_per_kg} | "
                     f"{'PROMO' if p.is_promo else 'regular'} | {p.savings_pct} | {p.promotion_description} | deal={p.deal_text} | {p.stores}")
    (OUT / "meat_dump.txt").write_text("\n".join(lines), encoding="utf-8")
    import re
    tag = re.compile(r"מוג\s*\d|ע\.מלאי|ת\.קצר|ס\.בודדים|מעל \d+ ש|\d\s*(?:קג\s*)?ב\s*\d|1\+1|ומעלה")
    firsts, seen = [], set()
    for p in meat.promos:
        if p.category not in seen:
            seen.add(p.category); firsts.append(p)
    st = {"rows_with_raw_tag": sum(1 for p in meat.promos if tag.search(p.promotion_description) or tag.search(p.item_name)),
          "deal_rows": sum(1 for p in meat.promos if p.is_promo),
          "first_rows_conditional": sum(1 for p in firsts if tag.search(p.promotion_description)),
          "first_rows_conditional_no_deal_text": sum(1 for p in firsts if tag.search(p.promotion_description) and not p.deal_text),
          "first_rows": len(firsts)}
    vl = []
    for title, groups, is_pkg in (("PACKAGES", vac.package_groups, True), ("FLIGHTS", vac.flight_groups, False)):
        for g in groups:
            if not g.deals: continue
            marks = email_sender._deal_badges(g.deals, is_pkg)
            nights = [d.nights_or_computed for d in g.deals]
            vl.append(f"\n## {title} {g.continent} rows={len(g.deals)} limit={g.limit} nights max x{nights.count(max(nights))} min x{nights.count(min(nights))} missing={g.missing_required}")
            for i, d in enumerate(g.deals):
                pn = email_sender._package_per_night(d) if is_pkg else None
                vl.append(f"{i+1:2} {d.source:14} {d.destination:10} {d.nights_or_computed}n price={d.price} total={d.total} pernight={round(pn) if pn else ''} lug={d.luggage:8} stars={d.stars} {[b[0] for b in marks.get(i, [])]} | {email_sender._deal_price(d)} | {'; '.join(d.includes)[:90]}")
    (OUT / "vac_dump.txt").write_text("\n".join(vl), encoding="utf-8")
    print(json.dumps(st, ensure_ascii=False))


main()
