"""Database side of Phase 5: extract follow-ups from mail, detect (proposed) resolution, user decisions.

Nothing here closes, confirms or reminds on its own: extraction creates `proposed` follow-ups, resolution
detection only fills the `closure_*` fields, and every status change is a user action with an event + audit row.
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from mailassistant.config import Settings
from mailassistant.db import audit
from mailassistant.db.models import (
    Classification,
    Email,
    EmailLocation,
    Followup,
    FollowupEvent,
    Mailbox,
)
from mailassistant.db.session import utcnow
from mailassistant.followups import prompt as fprompt
from mailassistant.followups.extract import (
    FollowupDraft,
    closure_verified,
    drafts_from_extraction,
    similar_actions,
)
from mailassistant.followups.quoting import own_text
from mailassistant.followups.remind import add_working_days, remind_at
from mailassistant.security.injection import detect as detect_injection

log = logging.getLogger(__name__)

EXTRACT_CATEGORIES = ("personal", "work", "invitation", "manual_review")
OPEN_STATUSES = ("proposed", "open", "waiting")
MAX_CLOSURE_CANDIDATES = 5


class FollowupError(Exception):
    """The requested change is not possible (message is shown to the user)."""


# ------------------------------------------------------------------ helpers
def _mine(session: Session, settings: Settings, email: Email | None = None) -> set[str]:
    # capture addresses (me+agenda@...) are the user's own: such mail is handled by the capture job, not here
    from mailassistant.mail.accounts import own_addresses
    own = {a.lower() for a in [*own_addresses(session, settings), *settings.capture_addresses]}
    if email is not None and email.direction == "out" and email.from_addr:
        own.add(email.from_addr.lower())  # e.g. facturen@ or an old address the user sent from
    return own


def _recipients(email: Email, mine: set[str]) -> list[str]:
    return sorted({a.lower() for a in [*(email.to_addrs or []), *(email.cc_addrs or [])] if a and a.lower() not in mine})


def _is_bulk(email: Email) -> bool:
    h = {k.lower(): str(v).lower() for k, v in (email.headers or {}).items() if not k.startswith("_")}
    return bool(h.get("list-unsubscribe") or h.get("list-id")
                or h.get("precedence") in ("bulk", "list", "junk")
                or h.get("auto-submitted", "no") not in ("", "no"))


_AUTOMATED_SENDER = re.compile(
    r"^_?(?:no-?reply|do-?not-?reply|donotreply|noreply|mailer(?:-daemon)?|bounces?|notifications?|notificaties|"
    r"newsletters?|nieuwsbrief|marketing|mailings?|campaigns?)\b"
    r"|@(?:[\w-]+\.)*(?:mailings?|news(?:letter)?|nieuwsbrief|campaigns?|bounces?)\.[\w-]+\.[\w-]+$", re.I)


def is_automated_sender(addr: str | None) -> bool:
    """Senders that never write personally (no-reply, mailing domains). Their mail cannot hold a personal
    request or promise, even when it lacks List-* headers."""
    return bool(addr and _AUTOMATED_SENDER.search(addr))


def _in_sent(session: Session, email_id: int) -> bool:
    return session.scalar(select(EmailLocation.id).join(Mailbox, Mailbox.id == EmailLocation.mailbox_id).where(
        EmailLocation.email_id == email_id, Mailbox.role == "sent").limit(1)) is not None


def _current_class(session: Session, email_id: int) -> Classification | None:
    return session.scalar(select(Classification).where(Classification.email_id == email_id,
                                                       Classification.is_current.is_(True)))


def extraction_gate(session: Session, email: Email, settings: Settings) -> str | None:
    """None if this mail may be scanned for commitments, else the reason it is skipped."""
    mine = _mine(session, settings, email)
    if _is_bulk(email):
        return "bulk_or_automatic"
    if email.direction == "out":
        if not _in_sent(session, email.id):
            return "out_not_in_sent"            # system copies from own addresses landing in INBOX
        if not _recipients(email, mine):
            return "only_own_recipients"        # note to self
        return None
    if is_automated_sender(email.from_addr):
        return "automated_sender"
    cls = _current_class(session, email.id)
    if cls is None:
        return "not_classified"
    if cls.injection_flag:
        return "prompt_injection_suspected"
    if cls.category not in EXTRACT_CATEGORIES:
        return f"category_{cls.category}"
    if not {a.lower() for a in [*(email.to_addrs or []), *(email.cc_addrs or [])]} & mine:
        return "not_addressed_to_user"          # bcc / mailing list
    return None


def _event(session: Session, f: Followup, to_status: str, actor: str, *, from_status: str | None = None,
           email_id: int | None = None, note: str | None = None) -> None:
    session.add(FollowupEvent(followup_id=f.id, from_status=from_status, to_status=to_status, actor=actor,
                              email_id=email_id, note=note))


def _sender(email: Email) -> str:
    return f"{email.from_name} <{email.from_addr}>" if email.from_name else (email.from_addr or "unknown")


# ------------------------------------------------------------------ extraction
def store_draft(session: Session, email: Email, d: FollowupDraft, reason: str, settings: Settings,
                served_by: str | None = None) -> Followup | None:
    if session.scalar(select(Followup.id).where(Followup.dedupe_key == d.dedupe_key)):
        return None
    mine = _mine(session, settings, email)
    if email.direction == "out":
        people = _recipients(email, mine)
        counterparty = next((a for a in (email.to_addrs or []) if a.lower() not in mine), people[0] if people else None)
    else:
        counterparty = (email.from_addr or "").lower() or None
        people = sorted({*([counterparty] if counterparty else []), *_recipients(email, mine)})
    if email.thread_id is not None:
        for o in session.scalars(select(Followup).where(Followup.thread_id == email.thread_id,
                                                        Followup.direction == d.direction,
                                                        Followup.status.in_(OPEN_STATUSES))):
            if similar_actions(o.action_text, d.action_text):
                audit.record(session, actor="system", action="followup_duplicate_skipped", entity_type="followup",
                             entity_id=o.id, detail=f"same commitment again in email {email.id}")
                return None
    when, basis = remind_at(direction=d.direction, sent_at=email.sent_at or utcnow(), deadline_at=d.deadline_at,
                            tz=settings.timezone, estimate_days_i_owe=settings.followup_estimate_days_i_owe,
                            estimate_days_they_owe=settings.followup_estimate_days_they_owe,
                            hour=settings.followup_remind_hour)
    f = Followup(email_id=email.id, thread_id=email.thread_id, direction=d.direction, counterparty=counterparty,
                 people=people, action_text=d.action_text, evidence_quote=d.evidence_quote,
                 deadline_at=d.deadline_at, remind_at=when, remind_basis=basis, status="proposed",
                 confidence=Decimal(f"{d.confidence:.3f}"), explanation=(reason or "")[:300],
                 dedupe_key=d.dedupe_key, notes=d.notes + ([f"deadline_text: {d.deadline_text}"] if d.deadline_text else []),
                 served_by=served_by)
    session.add(f)
    session.flush()
    _event(session, f, "proposed", "llm-proposal", email_id=email.id)
    audit.record(session, actor="llm-proposal", action="followup_proposed", entity_type="followup", entity_id=f.id,
                 after={"direction": d.direction, "remind_basis": basis, "has_deadline": d.deadline_at is not None,
                        "confidence": d.confidence, "email_id": email.id})
    return f


def extract_followups(session: Session, email: Email, client, settings: Settings) -> list[Followup]:
    from mailassistant.classify.service import _recorder
    from mailassistant.llm.runner import StructuredOutputError, run_structured
    from mailassistant.llm.schemas import FollowupExtraction

    own = own_text(email.body_text)
    if len(own) < 10:
        return []
    outgoing = email.direction == "out"
    mine = _mine(session, settings, email)
    msgs = fprompt.build_extract_messages(
        outgoing=outgoing, sender=_sender(email),
        recipients=", ".join(_recipients(email, mine)) if outgoing else "the user",
        subject=email.subject, sent_at=email.sent_at, own_text=own, body_chars=settings.classify_body_chars)
    version = fprompt.PROMPT_VERSION_OUT if outgoing else fprompt.PROMPT_VERSION_IN
    try:
        res = run_structured(client, msgs, FollowupExtraction, task="extract_followups",
                             record=_recorder(session, settings, version))
    except StructuredOutputError:
        log.info("follow-up extraction: model output unusable for email %s", email.id)
        return []
    drafts, rejected = drafts_from_extraction(
        res.model_dump(), mail_direction="out" if outgoing else "in", own=own, subject=email.subject,
        message_id=email.message_id, sent_at=email.sent_at or utcnow(), tz=settings.timezone,
        min_confidence=settings.followup_min_confidence)
    if rejected:
        log.info("follow-up items rejected for email %s: %s", email.id, ",".join(rejected))
    served_by = getattr(client, "served_by", None)
    return [f for d in drafts if (f := store_draft(session, email, d, res.reason, settings, served_by))]


# ------------------------------------------------------------------ resolution (proposed only)
def closure_candidates(session: Session, email: Email, settings: Settings) -> list[Followup]:
    """Deterministic pre-filter: which open follow-ups could this newer mail fulfil?"""
    mine = _mine(session, settings, email)
    src = select(Email.sent_at).where(Email.id == Followup.email_id).scalar_subquery()
    q = select(Followup).where(Followup.status.in_(OPEN_STATUSES), Followup.closure_email_id.is_(None),
                               Followup.email_id != email.id)
    if email.sent_at is not None:
        q = q.where(src < email.sent_at)
    if email.direction == "out":  # the user did what they owed
        recips = _recipients(email, mine)
        cond = [Followup.people.overlap(recips)] if recips else []
        if email.thread_id is not None:
            cond.append(Followup.thread_id == email.thread_id)
        if not cond:
            return []
        q = q.where(Followup.direction == "I_OWE", or_(*cond))
    else:  # they did what they owed: only mail from one of the people involved counts
        sender = (email.from_addr or "").lower()
        if not sender:
            return []
        q = q.where(Followup.direction == "THEY_OWE", Followup.people.overlap([sender]))
    same_thread = (Followup.thread_id == email.thread_id) if email.thread_id is not None else None
    order = [same_thread.desc()] if same_thread is not None else []
    return list(session.scalars(q.order_by(*order, Followup.id.desc()).limit(MAX_CLOSURE_CANDIDATES)))


def check_resolution(session: Session, email: Email, client, settings: Settings) -> list[Followup]:
    from mailassistant.classify.service import _recorder
    from mailassistant.llm.runner import StructuredOutputError, run_structured
    from mailassistant.llm.schemas import ClosureCheck

    cands = closure_candidates(session, email, settings)
    own = own_text(email.body_text)
    if not cands or len(own) < 5:
        return []
    mine = _mine(session, settings, email)
    proposed: list[Followup] = []
    for f in cands:
        msgs = fprompt.build_close_messages(
            commitment=f.action_text, owed_by="the user owes it" if f.direction == "I_OWE" else "the other person owes it",
            commitment_quote=f.evidence_quote or "", sender=_sender(email),
            recipients=", ".join(_recipients(email, mine)) or "the user", subject=email.subject,
            sent_at=email.sent_at, own_text=own)
        try:
            res = run_structured(client, msgs, ClosureCheck, task="check_followup_closure",
                                 record=_recorder(session, settings, fprompt.PROMPT_VERSION_CLOSE))
        except StructuredOutputError:
            continue
        ok, why = closure_verified(res.model_dump(), own=own, subject=email.subject,
                                   min_confidence=settings.followup_close_min_confidence)
        if not ok:
            log.info("closure not proposed for follow-up %s by email %s: %s", f.id, email.id, why)
            continue
        f.closure_email_id, f.closure_quote, f.closure_proposed_at = email.id, res.evidence_quote.strip()[:600], utcnow()
        _event(session, f, f.status, "llm-proposal", from_status=f.status, email_id=email.id, note="closure proposed")
        audit.record(session, actor="llm-proposal", action="followup_closure_proposed", entity_type="followup",
                     entity_id=f.id, after={"email_id": email.id, "confidence": res.confidence})
        proposed.append(f)
    return proposed


# ------------------------------------------------------------------ one email, end to end
def scan_email(session: Session, email_id: int, client, settings: Settings, now: datetime | None = None
               ) -> tuple[list[Followup], list[Followup]]:
    """(new follow-ups, follow-ups with a new closure proposal). LLMUnavailable propagates: nothing is
    marked scanned, the job is deferred and repeated (dedupe keys make the repeat harmless)."""
    now = now or utcnow()
    email = session.get(Email, email_id)
    if email is None:
        return [], []
    signals = detect_injection(own_text(email.body_text))
    cls = _current_class(session, email.id) if email.direction == "in" else None
    if signals or (cls and cls.injection_flag):  # never a source of follow-ups nor evidence of closure
        audit.record(session, actor="system", action="followup_scan_skipped", entity_type="email",
                     entity_id=email.id, detail="prompt injection suspected: " + ",".join(signals or ["classifier"]))
        email.followups_scanned_at = now
        return [], []
    gate = extraction_gate(session, email, settings)
    made = extract_followups(session, email, client, settings) if gate is None else []
    session.commit()
    closed = check_resolution(session, email, client, settings)
    email.followups_scanned_at = now
    return made, closed


def pending_scan_email_ids(session: Session, window_days: int, limit: int) -> list[int]:
    """Oldest first, so that during the backfill a commitment exists before the mail that fulfils it.
    Incoming mail waits until it is classified."""
    when = func.coalesce(Email.sent_at, Email.received_at)
    classified = exists(select(Classification.id).where(Classification.email_id == Email.id,
                                                        Classification.is_current.is_(True)))
    return list(session.scalars(
        select(Email.id).where(Email.followups_scanned_at.is_(None), when >= utcnow() - timedelta(days=window_days),
                               or_(Email.direction == "out", classified))
        .order_by(when.asc()).limit(limit)))


# ------------------------------------------------------------------ user decisions
def _get(session: Session, fid: int) -> Followup:
    f = session.get(Followup, fid, with_for_update=True)
    if f is None:
        raise FollowupError(f"no follow-up {fid}")
    return f


def _change(session: Session, f: Followup, to_status: str, action: str, note: str | None = None,
            email_id: int | None = None) -> Followup:
    before = f.status
    f.status, f.updated_at = to_status, utcnow()
    _event(session, f, to_status, "user", from_status=before, email_id=email_id, note=note)
    audit.record(session, actor="user", action=action, entity_type="followup", entity_id=f.id,
                 before=before, after=to_status, detail=note)
    return f


def confirm(session: Session, fid: int) -> Followup:
    f = _get(session, fid)
    if f.status != "proposed":
        raise FollowupError(f"follow-up is {f.status}, not proposed")
    return _change(session, f, "open" if f.direction == "I_OWE" else "waiting", "followup_confirmed")


def reject(session: Session, fid: int) -> Followup:
    f = _get(session, fid)
    if f.status not in OPEN_STATUSES:
        raise FollowupError(f"follow-up is already {f.status}")
    return _change(session, f, "cancelled", "followup_rejected", note="rejected by user")


def mark_done(session: Session, fid: int) -> Followup:
    f = _get(session, fid)
    if f.status not in OPEN_STATUSES:
        raise FollowupError(f"follow-up is already {f.status}")
    return _change(session, f, "completed", "followup_completed", note="marked done by user")


def accept_closure(session: Session, fid: int) -> Followup:
    f = _get(session, fid)
    if f.closure_email_id is None:
        raise FollowupError("no closure is proposed for this follow-up")
    if f.status not in OPEN_STATUSES:
        raise FollowupError(f"follow-up is already {f.status}")
    f.resolved_by_email_id = f.closure_email_id
    return _change(session, f, "completed", "followup_closure_accepted", email_id=f.closure_email_id)


def dismiss_closure(session: Session, fid: int) -> Followup:
    f = _get(session, fid)
    if f.closure_email_id is None:
        raise FollowupError("no closure is proposed for this follow-up")
    email_id = f.closure_email_id
    f.closure_email_id = f.closure_quote = f.closure_proposed_at = None
    f.notes = [*(f.notes or []), f"closure_dismissed:email {email_id}"]
    return _change(session, f, f.status, "followup_closure_dismissed", note="closure proposal dismissed",
                   email_id=email_id)


def snooze(session: Session, fid: int, working_days: int, settings: Settings, now: datetime | None = None) -> Followup:
    f = _get(session, fid)
    if f.status not in ("open", "waiting"):
        raise FollowupError("only confirmed follow-ups (open/waiting) have a reminder to snooze")
    z = ZoneInfo(settings.timezone)
    day = add_working_days((now or utcnow()).astimezone(z).date(), max(working_days, 1))
    f.remind_at = datetime.combine(day, time(settings.followup_remind_hour), tzinfo=z).astimezone(UTC)
    f.remind_basis, f.user_edited = "manual", True
    return _change(session, f, f.status, "followup_snoozed", note=f"until {day.isoformat()}")


def set_deadline(session: Session, fid: int, day: date, settings: Settings) -> Followup:
    f = _get(session, fid)
    if f.status not in OPEN_STATUSES:
        raise FollowupError(f"follow-up is already {f.status}")
    z = ZoneInfo(settings.timezone)
    f.deadline_at = datetime.combine(day, time(17), tzinfo=z).astimezone(UTC)
    when, _ = remind_at(direction=f.direction, sent_at=utcnow(), deadline_at=f.deadline_at, tz=settings.timezone,
                        hour=settings.followup_remind_hour)
    f.remind_at, f.remind_basis, f.user_edited = when, "manual", True
    return _change(session, f, f.status, "followup_deadline_set", note=day.isoformat())


def edit_action(session: Session, fid: int, text: str) -> Followup:
    """Correct the description (e.g. garbled by speech recognition)."""
    f = _get(session, fid)
    if f.status not in OPEN_STATUSES:
        raise FollowupError(f"follow-up is already {f.status}")
    text = " ".join((text or "").split())[:200]
    if not text:
        raise FollowupError("the description cannot be empty")
    old, f.action_text, f.user_edited = f.action_text, text, True
    return _change(session, f, f.status, "followup_edited", note=f"was: {old}"[:300])
