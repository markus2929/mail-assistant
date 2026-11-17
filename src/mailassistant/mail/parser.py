"""RFC 5322 / MIME parsing into plain dataclasses. Pure standard library; no DB, no network.

The parser never interprets message content: it only extracts text and metadata. Everything it
returns is untrusted data.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime

from mailassistant.mail.htmltext import clean_text, html_to_text

MAX_BODY_CHARS = 200_000
MAX_ICS_BYTES = 512_000
_ID_RE = re.compile(r"<[^<>\s]+>")
KEEP_HEADERS = ("list-id", "list-unsubscribe", "list-unsubscribe-post", "precedence",
                "auto-submitted", "authentication-results", "return-path", "sender",
                "importance", "x-priority")


@dataclass
class ParsedAttachment:
    filename: str | None
    mime_type: str
    size_bytes: int
    sha256: str
    is_ics: bool = False
    ics_text: str | None = None


@dataclass
class ParsedEmail:
    message_id: str
    in_reply_to: str | None
    references: list[str]
    from_addr: str | None
    from_name: str | None
    to_addrs: list[str]
    cc_addrs: list[str]
    reply_to: str | None
    subject: str | None
    sent_at: datetime | None
    body_text: str
    headers: dict
    content_hash: str
    attachments: list[ParsedAttachment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    hidden_html_chars: int = 0

    @property
    def has_attachments(self) -> bool:
        return bool(self.attachments)


def _one_line(v) -> str | None:
    if v is None:
        return None
    return re.sub(r"[\r\n\t]+", " ", str(v).replace("\x00", "")).strip()


def _clean_name(name: str | None) -> str | None:
    return _one_line(name)[:500] or None if name else None


def _decode_ics(data: bytes, charset: str | None) -> str | None:
    """Decode iCalendar text, or None if the bytes are not actually a calendar object."""
    data = data[:MAX_ICS_BYTES]
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = data.decode("utf-16", "replace")
    else:
        try:
            text = data.decode(charset or "utf-8", "replace")
        except LookupError:
            text = data.decode("utf-8", "replace")
    text = text.lstrip("\ufeff \t\r\n")
    if text[:15].upper() != "BEGIN:VCALENDAR":
        return None
    return text.replace("\x00", "")


def _ids(value: str | None) -> list[str]:
    seen: dict[str, None] = {}
    for m in _ID_RE.findall(value or ""):
        seen.setdefault(m.lower(), None)
    return list(seen)


def _addresses(value: str | None) -> list[tuple[str, str]]:
    out = []
    for name, addr in getaddresses([value or ""]):
        addr = addr.strip().lower()
        if "@" in addr:
            out.append((name.strip(), addr))
    return out[:200]


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        d = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if d is None:
        return None
    d = d.replace(tzinfo=UTC) if d.tzinfo is None else d.astimezone(UTC)
    return d if 1990 <= d.year <= 2100 else None


def _part_text(part: EmailMessage, warnings: list[str]) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError, ValueError, KeyError):
        warnings.append("charset_fallback")
        payload = part.get_payload(decode=True) or b""
        try:
            return payload.decode(part.get_content_charset() or "utf-8", "replace")
        except LookupError:
            return payload.decode("latin-1", "replace")


def parse_message(raw: bytes) -> ParsedEmail:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    warnings: list[str] = []

    def hdr(name: str) -> str | None:
        try:
            return _one_line(msg[name])
        except Exception:  # noqa: BLE001 - malformed headers must not abort parsing
            warnings.append(f"bad_header:{name}")
            return None

    from_list = _addresses(hdr("from"))
    from_name, from_addr = from_list[0] if from_list else (None, None)
    to_addrs = [a for _, a in _addresses(hdr("to"))]
    cc_addrs = [a for _, a in _addresses(hdr("cc"))]
    reply_list = _addresses(hdr("reply-to"))
    subject = (hdr("subject") or "")[:1000] or None
    sent_at = _parse_date(hdr("date"))
    if sent_at is None:
        warnings.append("missing_or_bad_date")
    refs = _ids(hdr("references"))[:50]
    reply_ids = _ids(hdr("in-reply-to"))
    in_reply_to = reply_ids[0] if reply_ids else None

    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[ParsedAttachment] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        fname = part.get_filename()
        disp = part.get_content_disposition()
        is_ics = ctype in ("text/calendar", "application/ics") or bool(
            fname and fname.lower().endswith(".ics"))
        ics_text = None
        if is_ics:
            data = part.get_payload(decode=True) or b""
            if len(data) > MAX_ICS_BYTES:
                warnings.append("ics_truncated")
            ics_text = _decode_ics(data, part.get_content_charset())
            if ics_text is None:  # senders sometimes label PDFs etc. as calendar parts
                warnings.append("fake_calendar_attachment")
        if ics_text is not None:
            attachments.append(ParsedAttachment(_clean_name(fname) or "invite.ics", ctype, len(data),
                                                hashlib.sha256(data).hexdigest(), True, ics_text))
        elif not is_ics and ctype.startswith("text/") and disp != "attachment" and not fname:
            (html_parts if ctype == "text/html" else plain_parts).append(_part_text(part, warnings))
        elif disp == "inline" and ctype.startswith("image/") and not fname:
            continue  # inline image (cid:) - not a real attachment
        else:
            data = part.get_payload(decode=True) or b""
            attachments.append(ParsedAttachment(_clean_name(fname), ctype, len(data),
                                                hashlib.sha256(data).hexdigest()))

    plain = clean_text("\n\n".join(plain_parts))
    html_text, hidden = html_to_text("\n".join(html_parts)) if html_parts else ("", 0)
    if hidden:
        warnings.append("hidden_html_text_removed")
    # Prefer plain text unless it is just a stub next to a much richer HTML part.
    if not plain or (len(html_text) > 500 and len(plain) < 0.2 * len(html_text)):
        body = html_text or plain
    else:
        body = plain
    if len(body) > MAX_BODY_CHARS:
        body, _ = body[:MAX_BODY_CHARS], warnings.append("body_truncated")

    ids = _ids(hdr("message-id"))
    if ids:
        message_id = ids[0]
    else:
        warnings.append("synthetic_message_id")
        seed = "|".join([from_addr or "", sent_at.isoformat() if sent_at else "", subject or "",
                         body[:2000]])
        message_id = f"<synthetic-{hashlib.sha256(seed.encode()).hexdigest()[:32]}@mailassistant.local>"

    kept = {k: v[:500] for k in KEEP_HEADERS if (v := hdr(k))}
    kept["_parse"] = {"warnings": warnings, "hidden_html_chars": hidden}
    content_hash = hashlib.sha256("\n".join([
        from_addr or "", subject or "", sent_at.isoformat() if sent_at else "", body[:50000],
    ]).encode()).hexdigest()

    return ParsedEmail(
        message_id=message_id, in_reply_to=in_reply_to, references=refs, from_addr=from_addr,
        from_name=from_name or None, to_addrs=to_addrs, cc_addrs=cc_addrs,
        reply_to=reply_list[0][1] if reply_list else None, subject=subject, sent_at=sent_at,
        body_text=body, headers=kept, content_hash=content_hash, attachments=attachments,
        warnings=warnings, hidden_html_chars=hidden)
