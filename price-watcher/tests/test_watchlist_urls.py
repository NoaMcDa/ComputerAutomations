import hashlib
from datetime import datetime
from types import SimpleNamespace

from price_watcher import watchlist
from price_watcher.contracts import FetchStatus, Watch


def test_make_id_does_not_mangle_hosts():
    assert watchlist._make_id("https://www.example.com/p").startswith("example.com-")
    assert watchlist._make_id("https://wwwidget.com/p").startswith("wwwidget.com-")
    assert watchlist._make_id("https://www.wow.com/p").startswith("wow.com-")
    assert watchlist._make_id("https://WWW.Example.COM/p").startswith("example.com-")


def test_normalise_keeps_product_params_strips_tracking():
    n = watchlist._normalise_url(
        "https://Shop.Example.com/item?model=X1&utm_source=a&UTM_Medium=b&gclid=1&fbclid=2"
        "&ref=home&_ga=3&color=red&msclkid=4&srsltid=5&mc_cid=6&mc_eid=7&igshid=8#frag"
    )
    assert n == "https://shop.example.com/item?model=X1&color=red"


def test_normalise_idempotent():
    u = "https://a.com/p?x=1&y=a%20b&z="
    assert watchlist._normalise_url(u) == u
    assert watchlist._normalise_url(watchlist._normalise_url(u)) == u


def _fake_fetch(monkeypatch, price="5", currency=None):
    import price_watcher.fetcher as f
    calls = []

    def fetch(url, selector=None):
        calls.append(url)
        return SimpleNamespace(status=FetchStatus.OK, price=price, currency=currency, error_message=None)

    monkeypatch.setattr(f, "fetch", fetch)
    return calls


def _old_style(url):
    host = "xample.com"  # what lstrip("www.") produced for www.example.com
    return Watch(id=f"{host}-{hashlib.sha1(url.encode()).hexdigest()[:8]}", url=url,
                 name=None, selector=None, created_at=datetime.now())


def test_add_detects_existing_with_old_style_id(monkeypatch):
    url = "https://www.example.com/p"
    watchlist.save_all([_old_style(url)])
    calls = _fake_fetch(monkeypatch)
    watchlist.add(url)
    assert calls == []
    assert len(watchlist.load_all()) == 1


def test_remove_old_style_id_by_url_and_by_id():
    url = "https://www.example.com/p"
    w = _old_style(url)
    watchlist.save_all([w])
    watchlist.remove(url)
    assert watchlist.load_all() == []
    watchlist.save_all([w])
    watchlist.remove(w.id)
    assert watchlist.load_all() == []


def test_add_prints_no_none_when_currency_unknown(monkeypatch, capsys):
    _fake_fetch(monkeypatch, price="79.9", currency=None)
    watchlist.add("https://example.com/q")
    out = capsys.readouterr().out
    assert "79.9" in out and "None" not in out
