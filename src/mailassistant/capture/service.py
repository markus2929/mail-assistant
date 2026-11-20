"""Captures: appointments and follow-ups the user adds themself (dashboard, Home Assistant Assist, +agenda mail).

Each capture is an Email row - the real one for forwarded mail, a synthetic one (never in the mailbox) for the
dashboard and voice - so proposals reference it like any mail and go through exactly the same storage,
duplicate and conflict logic. Everything remains a proposal the user approves.
"""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from mailassistant.capture import prompt as cprompt
from mailassistant.capture.detect import capture_kind, strip_subject_tag
from mailassistant.config import Settings
from mailassistant.db import audit, queue
from mailassistant.db.models import (Attachment, Capture, Email, EmailLocation, EventProposal, Followup,
                                     MailAccount, Mailbox)
from mailassistant.db.session import utcnow
from mailassistant.security.injection import detect as detect_injection

log = logging.getLogger(__name__)
MAX_TEXT = 4000
LABEL = {"dashboard": "dashboard", "assist": "spraak", "mail": "mail"}


class CaptureError(Exception):
    pass


def _enqueue(session: Session, cap: Capture) -> None:
    queue.enqueue(session, "capture.process", {"capture_id": cap.id}, dedupe_key=f"capture:{cap.id}", priority=5)


def create_capture(session: Session, text: str, kind: str, source: str, settings: Settings) -> Capture:
    """Database only (used by the dashboard and the Assist API)."""
    text = (text or "").strip()
    if not text:
        raise CaptureError("empty text")
    if kind not in ("event", "followup", "auto") or source not in ("dashboard", "assist"):
        raise CaptureError("unknown kind or source")
    text = text[:MAX_TEXT]
    account = session.scalar(select(MailAccount).order_by(MailAccount.id).limit(1))
    if account is None:
        raise CaptureError("no mail account yet (start mail sync first)")
    now = utcnow()
    words = " ".join(text.split()[:6])
    email = Email(account_id=account.id, message_id=f"<capture-{uuid.uuid4().hex}@mail-assistant.local>",
                  direction="out", from_addr=(settings.my_addresses or [account.address])[0], to_addrs=[],
                  cc_addrs=[], subject=f"Toegevoegd via {LABEL[source]}: {words}"[:200], body_text=text,
                  sent_at=now, headers={"_capture": {"source": source, "kind": kind}},
                  events_scanned_at=now, followups_scanned_at=now)   # the normal scanners skip it
    session.add(email)
    session.flush()
    cap = Capture(email_id=email.id, source=source, kind=kind, status="queued", result={})
    session.add(cap)
    session.flush()
    _enqueue(session, cap)
    audit.record(session, actor="user", action="capture_received", entity_type="capture", entity_id=cap.id,
                 detail=f"source={source} kind={kind} chars={len(text)}")
    return cap


def register_mail_captures(session: Session, settings: Settings, days: int = 7, limit: int = 50) -> list[int]:
    """Mail the user SENT (it is in a Sent mailbox, so nobody else can inject captures) to a capture address
    (me+agenda@…) or to themself with "agenda: …" / "todo: …" in the subject."""
    from mailassistant.mail.accounts import own_addresses
    own = own_addresses(session, settings)
    if not own:
        return []
    in_sent = exists(select(EmailLocation.id).join(Mailbox, Mailbox.id == EmailLocation.mailbox_id).where(
        EmailLocation.email_id == Email.id, Mailbox.role == "sent"))
    done = exists(select(Capture.id).where(Capture.email_id == Email.id))
    rows = session.scalars(select(Email).where(
        Email.direction == "out", Email.from_addr.in_(own), in_sent, ~done,
        Email.sent_at >= utcnow() - timedelta(days=days)).order_by(Email.sent_at).limit(500)).all()
    made: list[int] = []
    for e in rows:
        kind = capture_kind(to_addrs=list(e.to_addrs or []), cc_addrs=list(e.cc_addrs or []), subject=e.subject,
                            capture_addresses=settings.capture_addresses, own_addresses=own)
        if kind is None:
            continue
        now = utcnow()
        e.events_scanned_at, e.followups_scanned_at = now, now
        e.headers = {**(e.headers or {}), "_capture": {"source": "mail", "kind": kind}}
        cap = Capture(email_id=e.id, source="mail", kind=kind, status="queued", result={})
        session.add(cap)
        session.flush()
        _enqueue(session, cap)
        audit.record(session, actor="user", action="capture_received", entity_type="capture", entity_id=cap.id,
                     detail=f"source=mail kind={kind} email={e.id}")
        made.append(cap.id)
        if len(made) >= limit:
            break
    return made


# ------------------------------------------------------------------ processing (worker)
def _event_record(p: EventProposal) -> dict:
    overlap = next((c.get("summary") or "een andere afspraak" for c in p.conflicts or [] if c.get("type") == "overlap"), None)
    return {"id": p.id, "title": p.title, "start": p.start_at.isoformat(), "end": p.end_at.isoformat(),
            "all_day": p.all_day, "status": p.status, "kind": p.kind, "notes": list(p.notes or []), "conflict": overlap}


def _followup_record(f: Followup) -> dict:
    return {"id": f.id, "direction": f.direction, "action": f.action_text,
            "deadline": f.deadline_at.isoformat() if f.deadline_at else None}


def _events(session: Session, email: Email, cap: Capture, client, settings: Settings, text: str, subject: str,
            result: dict) -> None:
    from mailassistant.classify.service import _recorder
    from mailassistant.gcal.extract import draft_from_extraction, draft_from_ics
    from mailassistant.gcal.ics import parse_ics
    from mailassistant.gcal.service import store_draft
    from mailassistant.llm.runner import StructuredOutputError, run_structured
    from mailassistant.llm.schemas import EventExtraction

    now, tz = utcnow(), settings.timezone
    drafts = []
    for att in session.scalars(select(Attachment).where(Attachment.email_id == email.id, Attachment.is_ics.is_(True))):
        for ev in parse_ics(att.ics_text or "", tz):
            d, why = draft_from_ics(ev, subject=subject, now=now, default_tz=tz)
            if d is not None and not d.cancel:
                drafts.append((d, att.id, None))
            elif why:
                result["reasons"].setdefault("event", why)
    if not drafts:
        try:
            res = run_structured(client, cprompt.event_messages(text, subject=subject, created=email.sent_at,
                                                                body_chars=settings.classify_body_chars),
                                 EventExtraction, task="capture_event",
                                 record=_recorder(session, settings, cprompt.EVENT_VERSION))
        except StructuredOutputError:
            result["reasons"]["event"] = "unreadable"
            return
        d, why = draft_from_extraction(
            res.model_dump(), subject=subject, body=text, sent_at=email.sent_at or now, now=now, default_tz=tz,
            default_minutes=settings.calendar_default_event_minutes, source_key=f"{email.message_id}:capture",
            min_confidence=settings.calendar_min_confidence, capture=True, intent_event=cap.kind == "event")
        if d is None:
            result["reasons"]["event"] = why
            return
        drafts.append((d, None, getattr(client, "served_by", None)))
    for d, att_id, served_by in drafts:
        p = store_draft(session, email, d, att_id, settings, served_by) or session.scalar(
            select(EventProposal).where(EventProposal.dedupe_key == d.dedupe_key))
        if p is not None:
            result["events"].append(_event_record(p))


def _followups(session: Session, email: Email, client, settings: Settings, text: str, subject: str,
               result: dict) -> None:
    from mailassistant.classify.service import _recorder
    from mailassistant.followups.extract import drafts_from_extraction
    from mailassistant.followups.service import store_draft
    from mailassistant.llm.runner import StructuredOutputError, run_structured
    from mailassistant.llm.schemas import FollowupExtraction

    try:
        res = run_structured(client, cprompt.followup_messages(text, subject=subject, created=email.sent_at,
                                                               body_chars=settings.classify_body_chars),
                             FollowupExtraction, task="capture_followup",
                             record=_recorder(session, settings, cprompt.FOLLOWUP_VERSION))
    except StructuredOutputError:
        result["reasons"]["followup"] = "unreadable"
        return
    drafts, rejected = drafts_from_extraction(
        res.model_dump(), mail_direction="capture", own=text, subject=subject, message_id=email.message_id,
        sent_at=email.sent_at or utcnow(), tz=settings.timezone, min_confidence=settings.followup_min_confidence)
    for d in drafts:
        f = store_draft(session, email, d, res.reason, settings, getattr(client, "served_by", None)) or session.scalar(
            select(Followup).where(Followup.dedupe_key == d.dedupe_key))
        if f is not None:
            result["followups"].append(_followup_record(f))
    if not drafts:
        result["reasons"]["followup"] = rejected[0] if rejected else "nothing_found"


def process_capture(session: Session, capture_id: int, event_client, followup_client, settings: Settings,
                    voice_followup_client=None) -> Capture | None:
    """Turn a capture into proposals. LLMUnavailable propagates (job deferred; dedupe keys make a retry safe)."""
    cap = session.get(Capture, capture_id, with_for_update=True)
    if cap is None or cap.status != "queued":
        return cap
    email = session.get(Email, cap.email_id)
    text = (email.body_text or "").strip()     # full text: a forwarded message IS the content here
    subject = strip_subject_tag(email.subject)
    result: dict = {"events": [], "followups": [], "reasons": {}}
    if detect_injection(text) or detect_injection(subject):
        result["reasons"]["blocked"] = "prompt_injection_suspected"
    else:
        if cap.kind in ("event", "auto"):
            _events(session, email, cap, event_client, settings, text, subject, result)
        if cap.kind in ("followup", "auto") and not (cap.kind == "auto" and result["events"]):
            client = voice_followup_client if cap.source == "assist" and voice_followup_client else followup_client
            _followups(session, email, client, settings, text, subject, result)
    cap.result, cap.processed_at = result, utcnow()
    cap.status = "done" if result["events"] or result["followups"] else "nothing"
    audit.record(session, actor="system", action="capture_processed", entity_type="capture", entity_id=cap.id,
                 after={"status": cap.status, "events": [e["id"] for e in result["events"]],
                        "followups": [f["id"] for f in result["followups"]], "reasons": result["reasons"]})
    return cap
