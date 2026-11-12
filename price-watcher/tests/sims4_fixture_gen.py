"""Builds the synthetic EA store pages used by test_sims4.py.

The live all-packs page embeds its data as escaped JSON inside JS string
literals (``self.__next_f.push([1,"..."])``), which is what ``rsc_page`` mimics.
Run ``python tests/sims4_fixture_gen.py`` to regenerate the fixture files.
"""
from __future__ import annotations

import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def price(amount, original, pct, currency="USD"):
    return {
        "__typename": "PricingInfo",
        "amount": amount,
        "originalAmount": original,
        "discountPercentage": pct,
        "currency": currency,
    }


def pack(id_, slug, title, type_, non_sub=None, sub=None, sub_first=False, extra=None, option=True):
    d = {"__typename": "Product", "id": id_, "title": title, "type": type_}
    if slug is not None:
        d["slug"] = slug
    d["image"] = {"url": f"https://media.contentapi.ea.com/content/dam/{slug or 'x'}.jpg"}
    if option:
        opt = {}
        blocks = [("nonSubscriberPricingInfo", non_sub), ("subscriberPricingInfo", sub)]
        if sub_first:
            blocks.reverse()
        for k, v in blocks:
            if v is not None:
                opt[k] = v
        d["lowestPricePurchaseOption"] = opt
    else:
        d["lowestPricePurchaseOption"] = None
    if extra:
        d.update(extra)
    return d


def rsc_page(items, has_next, cursor, total):
    data = {
        "data": {
            "products": {
                "items": items,
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                "totalCount": total,
            }
        }
    }
    chunk = "5:" + json.dumps(["$", "div", None, data], ensure_ascii=False, separators=(",", ":")) + "\n"
    js = "self.__next_f.push(" + json.dumps([1, chunk], ensure_ascii=False, separators=(",", ":")) + ")"
    return (
        "<!doctype html><html><head><title>All Packs | The Sims 4</title>"
        '<script>self.__next_f=self.__next_f||[];</script></head><body>'
        '<div id="root"><button>Show more</button></div>'
        f"<script>{js}</script>"
        '<script>self.__next_f.push([1,"2:I[\\"123\\",[\\"static/chunks/app.js\\"],\\"default\\"]\\n"])</script>'
        "</body></html>"
    )


PAGE1 = [
    # Expansion pack on sale; non-subscriber block first.
    pack("en-us_the-sims-4-seasons_il_pc", "the-sims-4-seasons",
         "The Sims™ 4 Seasons Expansion Pack", "EXPANSION_PACK",
         price(19.99, 39.99, 50), price(17.99, 39.99, 55)),
    # Subscriber (EA Play 10%) block listed FIRST: must still read the non-subscriber price.
    pack("en-gb_the-sims-4-home-chef-hustles_il_pc", "the-sims-4-home-chef-hustles",
         "The Sims™ 4 Home Chef Hustle Stuff Pack", "MICRO_CONTENT",
         price(9.99, 9.99, 0), price(8.99, 9.99, 10), sub_first=True),
    # No purchase option at all: must be skipped, not given the next pack's price.
    pack("en-us_the-sims-4-coming-soon_il_pc", "the-sims-4-coming-soon",
         "The Sims™ 4 Coming Soon Kit", "MICRO_CONTENT", option=False),
    # originalAmount null (not on sale); slug only available from a non en-us id.
    pack("en-gb_the-sims-4-romantic-garden-stuff_il_pc", None,
         "The Sims™ 4 Romantic Garden Stuff Pack", "MICRO_CONTENT",
         price(9.99, None, 0)),
    # Game pack with its own link in the JSON.
    pack("en-us_the-sims-4-star-wars-journey-to-batuu-game-pack_il_pc",
         "the-sims-4-star-wars-journey-to-batuu-game-pack",
         "The Sims™ 4 Star Wars™: Journey to Batuu Game Pack", "MICRO_CONTENT",
         price(13.99, 19.99, 30), price(12.59, 19.99, 37),
         extra={"link": {"url": "/games/the-sims/the-sims-4/packs/game-packs/the-sims-4-star-wars-journey-to-batuu-game-pack"}}),
]

PAGE2 = [
    pack("en-us_the-sims-4-cats-and-dogs_il_pc", "the-sims-4-cats-and-dogs",
         "The Sims™ 4 Cats & Dogs Expansion Pack", "EXPANSION_PACK",
         price(39.99, 39.99, 0), price(35.99, 39.99, 10)),
    pack("en-us_the-sims-4-cozy-kitsch-kit_il_pc", "the-sims-4-cozy-kitsch-kit",
         "The Sims™ 4 Cozy Kitsch Kit", "MICRO_CONTENT",
         price(2.49, 4.99, 50)),
]


def page1() -> str:
    return rsc_page(PAGE1, True, "cursor-50", 6)


def page2() -> str:
    return rsc_page(PAGE2, False, None, 6)


if __name__ == "__main__":
    (FIXTURES / "sims4_all_packs_p1.html").write_text(page1(), encoding="utf-8")
    (FIXTURES / "sims4_all_packs_p2.html").write_text(page2(), encoding="utf-8")
