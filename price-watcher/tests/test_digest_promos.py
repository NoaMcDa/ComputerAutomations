"""Issues #41 (Hebrew code phrasings), #47 (no_promo rendering), #40 (dead code)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from price_watcher import digest as dg
from price_watcher import email_sender as es
from price_watcher.contracts import PromoInfo

ISSUE_41 = [
    ("קוד הנחה: VIP20 – 20% הנחה על כל האתר", "VIP20", 20),
    ("20% הנחה על כל האתר בקוד הנחה VIP20", "VIP20", 20),
    ("10% הנחה עם קוד הטבה: WELCOME10", "WELCOME10", 10),
    ("קוד מבצע SALE15 - 15% הנחה", "SALE15", 15),
    ("15% הנחה | קוד הנחה: SALE15", "SALE15", 15),
    ("20% off with discount code VIP20", "VIP20", 20),
]


@pytest.mark.parametrize("text, code, pct", ISSUE_41)
def test_issue_41_code_and_pct_detected(text, code, pct):
    promos = dg.extract_promos(text)
    assert [(p.code, p.discount_pct) for p in promos] == [(code, Decimal(pct))]


@pytest.mark.parametrize("text, code, pct", ISSUE_41)
def test_issue_41_inside_a_banner_element(text, code, pct):
    html = f'<html><body><div class="promo-bar">{text}</div></body></html>'
    promos = dg.extract_promos(html)
    assert [(p.code, p.discount_pct) for p in promos] == [(code, Decimal(pct))]
    assert all(p.code for p in promos), "must not be reported as a code-less discount"


def test_existing_phrasings_still_work():
    assert [(p.code, int(p.discount_pct)) for p in dg.extract_promos(
        "EXTRA 11% הנחה על כל האתר <div>x</div> קוד קופון: OFF11")] == [("OFF11", 11)]
    assert [(p.code, int(p.discount_pct)) for p in dg.extract_promos(
        "Get 25% off everything")] == [(None, 25)]


def test_hebrew_prefixed_word_is_not_a_keyword():
    # "מקודם" (promoted) contains "קוד" but is not the word "code"
    assert dg.extract_promos("20% הנחה מקודם ABC12") == []


def test_dead_code_removed():
    for name in ("_extract_bare_promos", "_PCT_BARE_PAT", "_COND_PAT_AFTER"):
        assert not hasattr(dg, name), name
    assert "everything!" not in dg._SITEWIDE_BARE_PAT.pattern
    assert not hasattr(es, "build_digest_payload")
    assert not hasattr(es, "send_digest_email")


def test_user_agent_is_shared():
    from price_watcher.http_headers import USER_AGENT
    assert dg._HEADERS["User-Agent"] == USER_AGENT
    assert USER_AGENT.startswith("Mozilla/5.0") and "Chrome/" in USER_AGENT


# ---- #47 no_promo ----------------------------------------------------------

def _report(*digests):
    return dg.DailyReport(site_digests=list(digests))


def _site(status, promos=(), error=None):
    return dg.SiteDigest(host="seestarz.com", display_name="Seestarz",
                         homepage_url="https://seestarz.com/", status=status,
                         promos=list(promos), error=error)


def test_no_promo_is_not_rendered_as_error():
    p = es.build_daily_payload(_report(_site("no_promo")))
    assert "לא זוהה מבצע כלל-אתרי" in p.text_body
    assert "לא זוהה מבצע כלל-אתרי" in p.html_body
    assert "no_promo" not in p.text_body and "no_promo" not in p.html_body
    assert "#c33" not in p.html_body


def test_status_he_has_no_promo():
    assert es._status_he("no_promo") == "לא זוהה מבצע כלל-אתרי"


def test_real_failure_still_red():
    p = es.build_daily_payload(_report(_site("blocked", error="HTTP 403")))
    assert "האתר חסם את הבקשה" in p.text_body
    assert '<div style="margin-top:8px;color:#c33"><b>האתר חסם את הבקשה:</b> HTTP 403</div>' in p.html_body


def test_promos_rendered_and_escaped():
    promo = PromoInfo(code="VIP20", discount_pct=Decimal(20), condition="<b>all</b>")
    p = es.build_daily_payload(_report(_site("ok", [promo])))
    assert "20% הנחה  —  קוד: VIP20" in p.text_body
    assert "&lt;b&gt;all&lt;/b&gt;" in p.html_body
    assert "VIP20" in p.subject


def test_terminal_output_no_promo(capsys):
    dg._print_digests([_site("no_promo")])
    out = capsys.readouterr().out
    assert "לא זוהה מבצע כלל-אתרי" in out and "no_promo" not in out


def test_collect_digest_marks_no_promo(monkeypatch):
    monkeypatch.setattr(dg, "_unique_hosts", lambda: ["seestarz.com"])
    monkeypatch.setattr(dg, "_fetch_homepage", lambda host: ("<html>hi</html>", "ok", None))
    [site] = dg.collect_digest()
    assert site.status == "no_promo" and not site.failed


def test_run_digest_exit_code_on_email_failure(monkeypatch):
    monkeypatch.setattr(dg, "collect_daily_report", lambda: _report(_site("no_promo")))
    monkeypatch.setattr(es, "send_daily_email", lambda report: False)
    assert dg.run_digest() == 1
    monkeypatch.setattr(es, "send_daily_email", lambda report: True)
    assert dg.run_digest() == 0
