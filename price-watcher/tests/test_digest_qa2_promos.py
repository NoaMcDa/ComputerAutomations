"""QA round 2: Hebrew code wording (#41) and sites without a promo (#47)."""

from __future__ import annotations

import pytest

from price_watcher import digest, email_sender
from price_watcher.digest import SiteDigest


@pytest.mark.parametrize("text, expected", [
    ("קוד הנחה: VIP20 – 20% הנחה על כל האתר", {("VIP20", 20)}),
    ("20% הנחה על כל האתר בקוד הנחה VIP20", {("VIP20", 20)}),
    ("10% הנחה עם קוד הטבה: WELCOME10", {("WELCOME10", 10)}),
    ("קוד מבצע SALE15 - 15% הנחה", {("SALE15", 15)}),
    ("15% הנחה | קוד הנחה: SALE15", {("SALE15", 15)}),
    ("20% off with discount code VIP20", {("VIP20", 20)}),
    ("10% הנחה עם הקוד: ABC10", {("ABC10", 10)}),
])
def test_hebrew_code_qualifier_words(text, expected):
    got = {(p.code, int(p.discount_pct)) for p in digest.parse_offers(text)}
    assert got == expected


def test_qualifier_does_not_make_kodem_a_keyword():
    assert digest.parse_offers("קודם הנחה: VIP20 20% הנחה") == []


def test_no_promo_site_is_ok_with_empty_promos(monkeypatch):
    monkeypatch.setattr(digest, "_unique_hosts", lambda: ["a.example"])
    monkeypatch.setattr(digest, "_fetch_homepage", lambda host: ("<html><body>hi</body></html>", "ok", None))
    (d,) = digest.collect_digest()
    assert d.status == "ok" and d.promos == []


def test_no_promo_site_renders_friendly_line():
    d = SiteDigest(host="a.example", display_name="A", homepage_url="https://a.example/")
    r = type("R", (), dict(site_digests=[d], creatine=None, sims4=None, supermarkets=None))()
    p = email_sender.build_digest_payload([d])
    assert "לא זוהה מבצע כלל-אתרי" in p.text_body and "no_promo" not in p.text_body
    assert "לא זוהה מבצע כלל-אתרי" in p.html_body and "#c33" not in p.html_body
