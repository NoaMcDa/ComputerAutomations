import smtplib

from price_watcher import config, email_sender
from price_watcher.email_sender import EmailPayload


class _Rec:
    inst = []
    kind = ""

    def __init__(self, host, port, **kw):
        self.kw, self.calls = kw, []
        _Rec.inst.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __getattr__(self, n):
        return lambda *a, **k: self.calls.append(n)


class _Plain(_Rec):
    kind = "plain"


class _Ssl(_Rec):
    kind = "ssl"


def _send(monkeypatch, port):
    _Rec.inst = []
    monkeypatch.setattr(smtplib, "SMTP", _Plain)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _Ssl)
    cfg = config.EmailConfig("h", port, "u", "p", "a@x.com", "b@x.com")
    email_sender._send_via_smtp(EmailPayload(subject="s", text_body="t", html_body="<p>h</p>"), cfg)
    return _Rec.inst[0]


def test_port_587_uses_starttls_with_timeout(monkeypatch):
    c = _send(monkeypatch, 587)
    assert c.kind == "plain" and c.kw.get("timeout")
    assert "starttls" in c.calls


def test_port_465_uses_implicit_tls_with_timeout(monkeypatch):
    c = _send(monkeypatch, 465)
    assert c.kind == "ssl" and c.kw.get("timeout")
    assert "starttls" not in c.calls and "login" in c.calls
