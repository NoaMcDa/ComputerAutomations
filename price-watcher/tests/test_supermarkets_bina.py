"""Super Bareket on its Bina Projects portal (#50), with a mocked portal.

The endpoints and the JSON shapes are those of superbareket.binaprojects.com
on 2026-11-11 (see `scan_bina_chain`)."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from price_watcher import supermarkets as sm

_FIX = Path(__file__).parent / "fixtures" / "supermarkets"
_BASE = "https://superbareket.binaprojects.com/"

_STORES = """<?xml version="1.0" encoding="utf-8"?>
<Root><ChainID>7290875100001</ChainID><SubChains>
 <SubChain><SubChainID>002</SubChainID><SubChainName>ברקת שכונתי</SubChainName><Stores>
  <Store><StoreID>061</StoreID><StoreName>רמת השרון</StoreName><Address>סוקלוב 67</Address><City>2650</City></Store>
  <Store><StoreID>063</StoreID><StoreName>רחובות</StoreName><Address>יעקב 3</Address><City>8400</City></Store>
 </Stores></SubChain></SubChains></Root>""".encode("utf-8")


def _portal(files: dict[str, bytes], listing: dict[tuple[str, str], list[str]], down: bool = False):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if down:
            raise httpx.ConnectError("unreachable", request=request)
        if request.url.path == "/MainIO_Hok.aspx":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            names = listing.get((form["WFileType"], form["WStore"]), [])
            rows = [{"FileNm": n, "Store": "", "TypeFile": "", "DateFile": ""} for n in names]
            return httpx.Response(200, json=rows)
        if request.url.path == "/Download.aspx":
            name = request.url.params["FileNm"]
            return httpx.Response(200, json=[{"SPath": f"{_BASE}Download/{name}"}])
        if request.url.path.startswith("/Download/"):
            name = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, content=gzip.compress(files[name])) if name in files \
                else httpx.Response(404)
        return httpx.Response(404)

    return httpx.MockTransport(handler), calls


@pytest.fixture
def mock_portal(monkeypatch):
    real_client = httpx.Client

    def install(*args, **kwargs):
        transport, calls = _portal(*args, **kwargs)
        monkeypatch.setattr(sm.httpx, "Client", lambda **kw: real_client(transport=transport, **kw))
        return calls

    return install


def _day(day: str) -> dict[tuple[str, str], list[str]]:
    return {
        ("1", "0"): [f"Stores7290875100001-000-{day}-040711.GZ", "Stores7290875100001-000-20261101-040711.GZ"],
        ("4", "61"): [f"PriceFull7290875100001-002-061-{day}-040711.GZ",
                      "PriceFull7290875100001-002-061-20261101-040711.GZ"],
        ("5", "61"): [f"PromoFull7290875100001-002-061-{day}-040711.GZ"],
    }


def test_bareket_store_in_ramat_hasharon_is_scanned(monkeypatch, mock_portal):
    monkeypatch.delenv("PW_MEAT_CITIES", raising=False)
    day = sm.datetime.now().strftime("%Y%m%d")
    files = {
        f"Stores7290875100001-000-{day}-040711.GZ": _STORES,
        f"PriceFull7290875100001-002-061-{day}-040711.GZ": (_FIX / "PriceFull_small.xml").read_bytes(),
        f"PromoFull7290875100001-002-061-{day}-040711.GZ": (_FIX / "PromoFull_small.xml").read_bytes(),
    }
    calls = mock_portal(files, _day(day))
    promos, used = sm.scan_bina_chain("superbareket", "סופר ברקת")
    assert [(s.store_id, s.city) for s in used] == [(61, "רמת השרון")]  # Rehovot is out of scope
    assert promos and {p.chain for p in promos} == {"סופר ברקת"}
    assert {p.stores[0] for p in promos} == {"רמת השרון"}
    assert "/Download/PriceFull7290875100001-002-061-20261101-040711.GZ" not in calls  # newest only


def test_bareket_without_a_store_in_the_cities_is_reported_empty(monkeypatch, mock_portal):
    monkeypatch.setenv("PW_MEAT_CITIES", "הרצליה")
    day = sm.datetime.now().strftime("%Y%m%d")
    mock_portal({f"Stores7290875100001-000-{day}-040711.GZ": _STORES}, _day(day))
    with pytest.raises(sm.ChainScanError) as exc:
        sm.scan_bina_chain("superbareket", "סופר ברקת")
    assert not exc.value.failed and exc.value.reason == "אין סניפים בערים שנבחרו"


def test_bareket_portal_failures_are_reported(monkeypatch, mock_portal):
    monkeypatch.delenv("PW_MEAT_CITIES", raising=False)
    mock_portal({}, {}, down=True)
    with pytest.raises(sm.ChainScanError) as exc:
        sm.scan_bina_chain("superbareket", "סופר ברקת")
    assert exc.value.failed

    mock_portal({}, {})  # no files listed
    with pytest.raises(sm.ChainScanError) as exc:
        sm.scan_bina_chain("superbareket", "סופר ברקת")
    assert exc.value.failed and "לא נמצאו קבצים" in exc.value.reason

    # Stores listed, but only stale price files for the store
    day = sm.datetime.now().strftime("%Y%m%d")
    listing = _day(day)
    listing[("4", "61")] = ["PriceFull7290875100001-002-061-20240101-040711.GZ"]
    mock_portal({f"Stores7290875100001-000-{day}-040711.GZ": _STORES}, listing)
    with pytest.raises(sm.ChainScanError) as exc:
        sm.scan_bina_chain("superbareket", "סופר ברקת")
    assert exc.value.failed and "עדכניים" in exc.value.reason


def test_bareket_is_one_of_the_scanned_chains():
    assert ("superbareket", "סופר ברקת") in sm._BINA_CHAINS
    assert json.dumps(sm._BINA_URL.format(sub="superbareket")) == json.dumps(_BASE)
