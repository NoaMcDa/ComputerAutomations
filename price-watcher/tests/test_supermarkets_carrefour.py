"""Carrefour on prices.carrefour.co.il, with a mocked site.

The page layout (a script with `const path` and `const files`) and the UTF-16
Stores file are as the site served them on 2026-11-13 (see `scan_carrefour`)."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import httpx
import pytest

from price_watcher import supermarkets as sm

_FIX = Path(__file__).parent / "fixtures" / "supermarkets"
_CHAIN = "7290055700007"

# Carrefour's Stores file is UTF-16 with a BOM and no XML declaration.
_STORES = """<Root><ChainID>7290055700007</ChainID><ChainName>קרפור</ChainName><SubChains>
 <SubChain><SubChainID>001</SubChainID><Stores>
  <Store><StoreID>460</StoreID><StoreName>קרפור הדר הרצליה (4120)</StoreName><Address>בן גוריון</Address><City>6400</City></Store>
  <Store><StoreID>301</StoreID><StoreName>קרפור באר שבע</StoreName><Address>רגר</Address><City>9000</City></Store>
 </Stores></SubChain></SubChains></Root>""".encode("utf-16")


def _page(day: str, names: list[str]) -> str:
    files = [{"name": n, "size": 1, "modified": "07:00"} for n in names]
    return (f"<html><script>\n    const path = '{day}';\n"
            f"    const files = {json.dumps(files)};\n</script></html>")


def _site(day: str, names: list[str], files: dict[str, bytes], down: bool = False):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if down:
            raise httpx.ConnectError("unreachable", request=request)
        if request.url.path == "/":
            return httpx.Response(200, text=_page(day, names))
        name = request.url.path.rsplit("/", 1)[-1]
        if request.url.path == f"/{day}/{name}" and name in files:
            body = files[name]
            return httpx.Response(200, content=body if name.startswith("Stores") else gzip.compress(body))
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def _names(day: str) -> list[str]:
    return [
        f"Stores{_CHAIN}-000-{day}-000100.xml",
        f"PriceFull{_CHAIN}-001-460-{day}-070003.gz",
        f"PromoFull{_CHAIN}-001-460-{day}-070003.gz",
        f"Price{_CHAIN}-001-460-{day}-080003.gz",    # hourly delta, not used
        f"PriceFull{_CHAIN}-001-301-{day}-070003.gz",
        f"PromoFull{_CHAIN}-001-301-{day}-070003.gz",
    ]


def test_herzliya_store_is_scanned(monkeypatch):
    monkeypatch.delenv("PW_MEAT_CITIES", raising=False)
    day = sm.datetime.now().strftime("%Y%m%d")
    files = {
        f"Stores{_CHAIN}-000-{day}-000100.xml": _STORES,
        f"PriceFull{_CHAIN}-001-460-{day}-070003.gz": (_FIX / "PriceFull_small.xml").read_bytes(),
        f"PromoFull{_CHAIN}-001-460-{day}-070003.gz": (_FIX / "PromoFull_small.xml").read_bytes(),
    }
    client, calls = _site(day, _names(day), files)
    promos, used = sm.scan_carrefour(client=client)
    assert [(s.store_id, s.city) for s in used] == [(460, "הרצליה")]  # Be'er Sheva is out of scope
    assert promos and {p.chain for p in promos} == {"קרפור"}
    assert not any("-301-" in c for c in calls)
    assert not any(f"/Price{_CHAIN}" in c for c in calls)  # PriceFull only


def test_failures_are_reported(monkeypatch):
    monkeypatch.delenv("PW_MEAT_CITIES", raising=False)
    day = sm.datetime.now().strftime("%Y%m%d")

    client, _ = _site(day, [], {}, down=True)
    with pytest.raises(sm.ChainScanError) as exc:
        sm.scan_carrefour(client=client)
    assert exc.value.failed and "אינו זמין" in exc.value.reason

    client, _ = _site(day, [], {})
    with pytest.raises(sm.ChainScanError) as exc:
        sm.scan_carrefour(client=client)
    assert exc.value.failed and "לא נמצאו קבצים" in exc.value.reason

    client, _ = _site(day, _names(day), {})  # Stores listed but not downloadable
    with pytest.raises(sm.ChainScanError) as exc:
        sm.scan_carrefour(client=client)
    assert "הסניפים" in exc.value.reason


def test_no_store_in_the_cities_is_reported_empty(monkeypatch):
    monkeypatch.setenv("PW_MEAT_CITIES", "נתניה")
    day = sm.datetime.now().strftime("%Y%m%d")
    client, _ = _site(day, _names(day), {f"Stores{_CHAIN}-000-{day}-000100.xml": _STORES})
    with pytest.raises(sm.ChainScanError) as exc:
        sm.scan_carrefour(client=client)
    assert not exc.value.failed and exc.value.reason == "אין סניפים בערים שנבחרו"


def test_utf16_feed_is_recognised():
    assert sm._looks_like_feed(_STORES)
    assert not sm._looks_like_feed("<!DOCTYPE html><html>".encode("utf-16"))


def test_carrefour_is_one_of_the_scanned_chains():
    assert sm._CARREFOUR_NAME in [n for n, _fn in sm._chain_tasks()]
