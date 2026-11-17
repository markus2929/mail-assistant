import hashlib
from datetime import UTC, datetime
from pathlib import Path

from mailassistant.mail.parser import parse_message

FIX = Path(__file__).parent.parent / "fixtures" / "emails"


def load(name: str):
    return parse_message((FIX / name).read_bytes())


def test_reply_headers_and_threading_fields():
    p = load("reply_thread.eml")
    assert p.message_id == "<c3@hekwerk.example>"          # normalised to lower case
    assert p.in_reply_to == "<b2@example.com>"
    assert p.references == ["<a1@hekwerk.example>", "<b2@example.com>"]
    assert (p.from_addr, p.from_name) == ("piet@hekwerk.example", "Piet de Vries")
    assert p.to_addrs == ["me@example.com"] and p.cc_addrs == ["anna@hekwerk.example"]
    assert p.sent_at == datetime(2026, 11, 3, 8, 15, tzinfo=UTC)
    assert "offerte vrijdag" in p.body_text


def test_html_newsletter_hides_hidden_text_and_scripts():
    p = load("newsletter_html.eml")
    assert p.subject == "Aanbiedingen deze week"
    assert "30% korting" in p.body_text
    assert "Ignore all previous" not in p.body_text       # display:none text removed
    assert "alert" not in p.body_text and "color:red" not in p.body_text
    assert "\u200b" not in p.body_text and "\xa0" not in p.body_text
    assert p.hidden_html_chars > 0 and "hidden_html_text_removed" in p.warnings
    assert p.headers["list-id"] == "<weekly.shop.example>"
    assert "unsub@shop.example" in p.headers["list-unsubscribe"]
    assert not p.has_attachments


def test_ics_attachment_is_captured():
    p = load("invitation_ics.eml")
    assert len(p.attachments) == 1 and p.has_attachments
    a = p.attachments[0]
    assert a.is_ics and a.filename == "invite.ics"
    assert a.ics_text.startswith("BEGIN:VCALENDAR") and "METHOD:REQUEST" in a.ics_text
    assert "https://zoom.us/j/123456789" in p.body_text


def test_inline_calendar_part_without_filename_is_detected():
    p = load("outlook_alternative.eml")
    assert [a.is_ics for a in p.attachments] == [True]
    assert "Zie de uitnodiging." in p.body_text


def test_latin1_and_missing_message_id():
    p = load("latin1_no_message_id.eml")
    assert p.subject == "Café afspraak" and "café" in p.body_text
    assert p.message_id.startswith("<synthetic-") and "synthetic_message_id" in p.warnings
    assert p.message_id == load("latin1_no_message_id.eml").message_id   # stable across parses


def test_regular_attachment_is_metadata_only():
    p = load("attachment_pdf.eml")
    a = p.attachments[0]
    data = b"%PDF-1.4\n%fake\n"
    assert (a.filename, a.mime_type, a.size_bytes) == ("offerte.pdf", "application/pdf", len(data))
    assert a.sha256 == hashlib.sha256(data).hexdigest()
    assert not a.is_ics and a.ics_text is None


def test_injection_text_is_just_data():
    p = load("injection_plain.eml")
    assert "ignore all previous instructions" in p.body_text.lower()  # kept verbatim, never acted on
    assert p.attachments == []


def test_garbage_input_does_not_raise():
    p = parse_message(b"\x00\xff not an email at all")
    assert p.message_id.startswith("<synthetic-") and p.body_text is not None


def _no_nul(p):
    import json
    blob = json.dumps([p.body_text, p.subject, p.from_name, p.headers,
                       [(a.filename, a.ics_text) for a in p.attachments]], default=str)
    return "\\u0000" not in blob and "\x00" not in blob


def test_pdf_labelled_as_calendar_is_a_plain_attachment():
    p = load("fake_calendar_pdf.eml")           # real-world: PDF sent as text/calendar
    assert len(p.attachments) == 1
    a = p.attachments[0]
    assert (a.filename, a.is_ics, a.ics_text) == ("Opzegging.pdf", False, None)
    assert "fake_calendar_attachment" in p.warnings
    assert p.body_text == "Zie bijlage."          # binary content must not leak into the body
    assert _no_nul(p)


def test_nul_bytes_never_reach_stored_fields():
    p = load("nul_in_body.eml")
    assert "Hallo" in p.body_text and _no_nul(p)


def test_utf16_calendar_is_decoded():
    raw = ("From: a@x.nl\nTo: b@x.nl\nSubject: s\nDate: Fri, 06 Nov 2026 12:00:00 +0000\n"
           "Message-ID: <u16@x>\nMIME-Version: 1.0\n"
           "Content-Type: text/calendar; charset=utf-16\nContent-Transfer-Encoding: base64\n\n").encode()
    import base64
    raw += base64.encodebytes("BEGIN:VCALENDAR\nMETHOD:REQUEST\nEND:VCALENDAR\n".encode("utf-16"))
    p = parse_message(raw)
    assert p.attachments[0].is_ics and "METHOD:REQUEST" in p.attachments[0].ics_text
    assert _no_nul(p)
