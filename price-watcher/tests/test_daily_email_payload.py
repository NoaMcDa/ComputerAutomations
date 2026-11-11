"""Daily email: subject reflects every section, footer is last, links and notes (offline)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from price_watcher import creatine, email_sender, sims4
from price_watcher.supermarkets import SupermarketReport


def _pack(slug="the-sims-4-seasons", pct=30):
    return sims4.Sims4Pack(slug=slug, name="The Sims 4 Seasons", price=Decimal("28"),
                           original_price=Decimal("40"), discount_percent=pct)


def _sims(**kw):
    base = dict(status="ok", packs=[_pack()], on_sale_now=[], newly_on_sale=[])
    base.update(kw)
    return sims4.Sims4Report(**base)


def _cprod(name="Brand A", per100="9.49"):
    return creatine.CreatineProduct(name=name, url="https://il.iherb.com/pr/a", price=Decimal("90"),
                                    currency="ILS", price_per_100g=Decimal(per100), unit="100g",
                                    on_sale=False, seller="iHerb")


def _report(**kw):
    base = dict(site_digests=[], creatine=None, sims4=None, supermarkets=SupermarketReport(status="ok"))
    base.update(kw)
    return SimpleNamespace(**base)


def test_subject_mentions_sims_deals_instead_of_nothing_new():
    pack = _pack()
    r = _report(sims4=_sims(on_sale_now=[pack], newly_on_sale=[pack]))
    subject = email_sender.build_daily_payload(r).subject
    assert "אין חדש" not in subject and "סימס 4" in subject and "30%" in subject


def test_subject_nothing_new_only_when_everything_is_quiet():
    assert email_sender.build_daily_payload(_report(sims4=_sims())).subject == "דוח יומי: אין חדש היום"


def test_subject_flags_failed_sections():
    failed_creatine = creatine.CreatineReport(status="blocked", error="x")
    failed_sims = sims4.Sims4Report(status="fetch_failed", error="y")
    r = _report(creatine=failed_creatine, sims4=failed_sims,
                supermarkets=SupermarketReport(status="fetch_failed", error="z"))
    subject = email_sender.build_daily_payload(r).subject
    assert "אין חדש" not in subject
    for name in ("קריאטין", "סימס 4", "בשר"):
        assert name in subject


def test_subject_for_same_price_different_product_does_not_say_rose():
    cur = _cprod("Brand B")
    rep = creatine.CreatineReport(status="ok", products=[cur], cheapest=cur,
                                  previous_cheapest=_cprod("Brand A"), changed=True, direction="changed")
    payload = email_sender.build_daily_payload(_report(creatine=rep))
    assert "עלה" not in payload.subject and "התחלף" in payload.subject
    assert "עלה" not in payload.text_body and "התחלף" in payload.text_body
    assert "עלה" not in payload.html_body


def test_signature_is_the_last_line_of_the_text_body():
    pack = _pack()
    cur = _cprod()
    r = _report(creatine=creatine.CreatineReport(status="ok", products=[cur], cheapest=cur),
                sims4=_sims(on_sale_now=[pack], newly_on_sale=[pack]))
    text = email_sender.build_daily_payload(r).text_body
    sig = "— " + email_sender._SIGNATURE
    assert text.rstrip().endswith(sig) and text.count(sig) == 1


def test_sims_links_and_scan_note_in_both_parts():
    pack = _pack()
    r = _report(sims4=_sims(on_sale_now=[pack], newly_on_sale=[pack], page_limited=True))
    payload = email_sender.build_daily_payload(r)
    assert "/buy/addon/the-sims-4-seasons" in payload.html_body
    assert "/store/the-sims-4-seasons" not in payload.html_body
    assert "נסרקו 1 חבילות" in payload.text_body and "העמוד הראשון בלבד" in payload.text_body
    assert "העמוד הראשון בלבד" in payload.html_body


def test_creatine_failure_reason_is_in_the_email():
    rep = creatine.CreatineReport(status="blocked", error="iHerb חסם את הבקשה (Cloudflare, HTTP 403)")
    payload = email_sender.build_daily_payload(_report(creatine=rep))
    assert "Cloudflare" in payload.text_body and "Cloudflare" in payload.html_body
