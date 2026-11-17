import ipaddress
import subprocess
import sys

from cryptography import x509

from mailassistant.notify.messages import briefing_message, due_message
from mailassistant.web.app import _host_allowed, is_loopback
from mailassistant.web.auth import LoginThrottle, hash_password, same, token_hash, verify_password
from mailassistant.web.makecert import build


def test_password_hash_roundtrip_and_rejects():
    h = hash_password("correct horse battery")
    assert h.startswith("scrypt$") and "correct" not in h
    assert verify_password("correct horse battery", h)
    assert not verify_password("wrong", h)
    assert not verify_password("x", None) and not verify_password("x", "garbage") and not verify_password("x", "")
    assert hash_password("a") != hash_password("a")                      # salted


def test_tokens_and_compare():
    assert len(token_hash("t")) == 64 and token_hash("t") != "t"
    assert same("abc", "abc") and not same("abc", "abd") and not same("", "") and not same(None, "x")


def test_login_throttle():
    t = LoginThrottle(limit=3, window=60)
    for i in range(3):
        assert not t.blocked("1.2.3.4", now=100 + i)
        t.failed("1.2.3.4", now=100 + i)
    assert t.blocked("1.2.3.4", now=110) and not t.blocked("5.6.7.8", now=110)
    assert not t.blocked("1.2.3.4", now=200)                             # window passed
    t.failed("9.9.9.9", now=1)
    t.succeeded("9.9.9.9")
    assert not t.blocked("9.9.9.9", now=2)


def test_host_check():
    allowed = ["desktop.local", "192.168.1.20", "localhost", "::1"]
    assert _host_allowed("desktop.local:8443", allowed) and _host_allowed("192.168.1.20", allowed)
    assert _host_allowed("[::1]:8443", allowed) and _host_allowed("Desktop.Local", allowed)
    assert not _host_allowed("evil.example:8443", allowed) and not _host_allowed("", allowed)
    assert is_loopback("127.0.0.1") and is_loopback("::1") and not is_loopback("0.0.0.0")


def test_self_signed_certificate_has_the_names():
    cert_pem, key_pem, fp = build(["desktop", "desktop.local"], ["192.168.1.20", "127.0.0.1"])
    cert = x509.load_pem_x509_certificate(cert_pem)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == ["desktop", "desktop.local"]
    assert ipaddress.ip_address("192.168.1.20") in san.get_values_for_type(x509.IPAddress)
    assert b"PRIVATE KEY" in key_pem and len(fp.split(":")) == 32


def test_notification_texts_counts_by_default_and_short_full_texts_on_request():
    f = {"id": 1, "direction": "I_OWE", "counterparty": "piet@x.nl", "action": "Offerte sturen " * 20}
    content = {"date": "2026-11-18", "counts": {"overdue": 1, "due_today": 0}, "calendar": [{}], "overdue": [f],
               "due_today": [], "followup_proposals": [f, f]}
    title, msg = briefing_message(content, "https://desktop.local:8443")
    assert title == "Briefing 2026-11-18" and "1 te laat" in msg and "2 nieuwe follow-ups" in msg
    assert "piet" not in msg and "Offerte" not in msg and msg.endswith(":8443")          # counts only
    _, full = briefing_message(content, "", detail="full")
    assert "piet@x.nl" in full and all(len(line) <= 90 for line in full.splitlines())
    title, msg = due_message([f] * 7)
    assert title == "7 follow-ups aan de beurt" and "Offerte" not in msg and "7 om zelf te doen" in msg
    assert "nog 2" in due_message([f] * 7, detail="full")[1]


def test_web_package_never_imports_secret_bound_clients():
    code = ("import sys, mailassistant.web.app as a; "
            "bad=[m for m in ('mailassistant.gcal.tokens','mailassistant.mail.imap_bridge','mailassistant.gcal.authorize') if m in sys.modules]; "
            "print(bad)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "[]"
    import pathlib
    src = "".join(p.read_text() for p in pathlib.Path("src/mailassistant/web").rglob("*.py"))
    for forbidden in ("OllamaClient", "calendar_client", "ImapBridgeSource", "approve_and_create", "gcal.service"):
        assert forbidden not in src, forbidden
