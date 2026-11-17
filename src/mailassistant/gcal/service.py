"""Database side of Phase 4: calendar cache, event proposals, approval and (idempotent) creation."""
from __future__ import annotations

import base64
import hashlib
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, exists, func, or_, select, update
from sqlalchemy.orm import Session

from mailassistant.config import Settings
from mailassistant.db import audit
from mailassistant.db.models import (Attachment, CalendarCache, Classification, Email, EventProposal, LlmCall,
                                     Setting)
from mailassistant.db.session import utcnow
from mailassistant.gcal import prompt as event_prompt
from mailassistant.gcal.conflicts import (CalEvent, Candidate, find_conflicts, find_duplicates, hard_conflicts,
                                          same_appointment)
from mailassistant.gcal.extract import Draft, draft_from_extraction, draft_from_ics
from mailassistant.gcal.google import GoogleApiError, GoogleCalendarClient, GoogleConflict, GoogleScopeMissing
from mailassistant.gcal.ics import parse_ics
from mailassistant.gcal.review import ReviewError, reject_proposal, suggestion_for  # noqa: F401  (re-export)
from mailassistant.gcal.calendars import display_name, looks_like_id, read_set, resolve
from mailassistant.gcal.review import CALENDARS_KEY
from mailassistant.gcal.routing import PRIVATE

log = logging.getLogger(__name__)


class ProposalError(ReviewError):
    """The proposal cannot be approved/created (message is shown to the user)."""


class ConflictsFound(ProposalError):
    def __init__(self, conflicts: list[dict]):
        super().__init__(f"{len(conflicts)} conflicting calendar item(s); approve with accept_conflicts=True to proceed")
        self.conflicts = conflicts


# ------------------------------------------------------------------ calendar cache
def cal_event_from_google(item: dict, calendar_id: str, default_tz: str) -> CalEvent | None:
    if item.get("status") == "cancelled" or "start" not in item:
        return None
    tz = ZoneInfo(default_tz)

    def when(x: dict) -> datetime:
        if "dateTime" in x:
            return datetime.fromisoformat(x["dateTime"].replace("Z", "+00:00"))
        return datetime.fromisoformat(x["date"]).replace(tzinfo=tz)

    declined = any(a.get("self") and a.get("responseStatus") == "declined" for a in item.get("attendees", []))
    return CalEvent(
        id=item["id"], calendar_id=calendar_id, summary=item.get("summary"), start=when(item["start"]),
        end=when(item.get("end", item["start"])), all_day="date" in item["start"],
        location=item.get("location"), status=item.get("status", "confirmed"),
        transparent=item.get("transparency") == "transparent", declined=declined,
        ical_uid=item.get("iCalUID"),
        private=(item.get("extendedProperties") or {}).get("private") or {})


def resolve_calendars(session: Session, client: GoogleCalendarClient, settings: Settings) -> dict:
    """Resolve the configured calendars (names or IDs) and store the result for the dashboard (DB-only)."""
    work_v, priv_v = settings.google_work_calendar or "primary", settings.google_private_calendar
    conflict = settings.google_conflict_calendars
    listed = [e.strip() for e in conflict if e.strip()]
    # The calendar list is needed for names and for "all own calendars" (empty list); not for IDs only.
    need_list = (not looks_like_id(work_v) or bool(priv_v and not looks_like_id(priv_v)) or not listed
                 or any(e.lower() != "none" and not looks_like_id(e) for e in listed))
    cal_list, list_note = None, None
    if need_list:
        try:
            cal_list = client.list_calendars()
        except GoogleScopeMissing:
            list_note = ("calendar-list permission missing: names cannot be used and only the configured calendars "
                         "are checked for conflicts; run gcal.authorize again")
    work = resolve(work_v, cal_list, need_write=True)
    private = resolve(priv_v, cal_list, need_write=True) if priv_v else None
    if private is not None and private.ok() and work.ok() and private.id == work.id:
        private.error = "the private calendar is the same as the work calendar"
    ids, conflict_res = read_set(work, private, conflict, cal_list)
    names = {c["id"]: display_name(c) for c in cal_list or []}
    data = {"checked_at": utcnow().isoformat(), "list_available": cal_list is not None, "note": list_note,
            "work": work.as_dict(), "private": private.as_dict() if private else None,
            "extras": [x.as_dict() for x in conflict_res], "read_ids": ids,
            "read_names": {i: names.get(i, i) for i in ids}}
    session.merge(Setting(key=CALENDARS_KEY, value=data))
    return data


def sync_calendar(session: Session, client: GoogleCalendarClient, settings: Settings,
                  now: datetime | None = None) -> int:
    """Refresh the local copy of the window [now - past, now + future] for every calendar in the read set."""
    now = now or utcnow()
    w0, w1 = now - timedelta(days=settings.calendar_sync_past_days), now + timedelta(days=settings.calendar_sync_future_days)
    read_ids = resolve_calendars(session, client, settings)["read_ids"]
    session.execute(delete(CalendarCache).where(CalendarCache.google_calendar_id.not_in(read_ids or [""])))
    total = 0
    for cid in read_ids:
        seen: list[str] = []
        for item in client.list_events(cid, w0, w1):
            ev = cal_event_from_google(item, cid, settings.timezone)
            if ev is None:
                continue
            seen.append(ev.id)
            row = session.scalar(select(CalendarCache).where(CalendarCache.google_calendar_id == cid,
                                                             CalendarCache.google_event_id == ev.id))
            if row is None:
                row = CalendarCache(google_calendar_id=cid, google_event_id=ev.id)
                session.add(row)
            row.ical_uid, row.summary, row.start_at, row.end_at = ev.ical_uid, ev.summary, ev.start, ev.end
            row.all_day, row.location, row.status = ev.all_day, ev.location, ev.status
            row.transparency = "transparent" if ev.transparent else "opaque"
            row.raw = {"declined": ev.declined, "private": ev.private}
            row.updated_at = now
            total += 1
        stale = delete(CalendarCache).where(CalendarCache.google_calendar_id == cid, CalendarCache.start_at >= w0)
        if seen:
            stale = stale.where(CalendarCache.google_event_id.not_in(seen))
        session.execute(stale)
        session.flush()
    return total


def load_events(session: Session, start: datetime, end: datetime) -> list[CalEvent]:
    rows = session.scalars(select(CalendarCache).where(CalendarCache.end_at > start, CalendarCache.start_at < end)).all()
    return [CalEvent(r.google_event_id, r.google_calendar_id, r.summary, r.start_at, r.end_at, r.all_day, r.location,
                     r.status, r.transparency == "transparent", bool((r.raw or {}).get("declined")), r.ical_uid,
                     (r.raw or {}).get("private") or {}) for r in rows]


# ------------------------------------------------------------------ proposals from email
def _candidate(d: Draft) -> Candidate:
    return Candidate(d.start_at, d.end_at, d.all_day, d.location, d.title, d.ics_uid, d.dedupe_key)


_KIND_RANK = {"invitation": 2, "proposed_date": 1, "mention": 0}


def _quality(kind: str, from_ics: bool, all_day: bool, confidence) -> tuple:
    """Which of two proposals for the same appointment to keep: invitation > proposed date > mention,
    then calendar file over model extraction, then a real time over all-day, then confidence."""
    return _KIND_RANK.get(kind, 0), from_ics, not all_day, float(confidence or 0)


def _same_as_open_proposal(session: Session, d: Draft, from_ics: bool, settings: Settings
                           ) -> tuple[EventProposal | None, bool]:
    """(other, new_wins) for an open/created proposal - typically from another email - for the same appointment."""
    others = session.scalars(select(EventProposal).where(
        EventProposal.status.in_(("proposed", "approved", "created")),
        EventProposal.start_at < d.start_at + timedelta(days=1),
        EventProposal.end_at > d.start_at - timedelta(days=1)).order_by(EventProposal.id)).all()
    cand = _candidate(d)
    for o in others:
        if same_appointment(cand, Candidate(o.start_at, o.end_at, o.all_day, o.location, o.title), settings.timezone):
            wins = o.status == "proposed" and (
                _quality(d.kind, from_ics, d.all_day, d.confidence)
                > _quality(o.kind, o.attachment_id is not None, o.all_day, o.confidence))
            return o, wins
    return None, False


def store_draft(session: Session, email: Email, d: Draft, attachment_id: int | None, settings: Settings) -> EventProposal | None:
    if session.scalar(select(EventProposal.id).where(EventProposal.dedupe_key == d.dedupe_key)):
        return None  # already proposed (same invitation / same extraction)
    if d.ics_uid:  # an updated invitation replaces still-open older proposals for the same UID
        session.execute(update(EventProposal).where(
            EventProposal.ics_uid == d.ics_uid, EventProposal.status.in_(("proposed", "approved", "failed")))
            .values(status="superseded", decided_at=utcnow()))
    events = load_events(session, d.start_at - timedelta(days=1), d.end_at + timedelta(days=1))
    cand = _candidate(d)
    dupes = find_duplicates(cand, events)
    conflicts = ([{"type": "duplicate_of", "event_id": e.id, "summary": e.summary, "start": e.start.isoformat()}
                  for e in dupes] if dupes else find_conflicts(cand, events, settings.calendar_travel_buffer_minutes))
    other, new_wins = (None, False) if dupes else _same_as_open_proposal(session, d, attachment_id is not None, settings)
    notes = list(d.notes)
    target_cal, why_cal = suggestion_for(session, email, settings)
    if settings.google_private_calendar:
        notes.append(f"calendar suggestion: {target_cal} ({why_cal})")
    if other is not None and not new_wins:  # the same appointment is already proposed (or created) from another email
        conflicts = [{"type": "duplicate_of_proposal", "proposal_id": other.id, "summary": other.title,
                      "start": other.start_at.isoformat()}, *conflicts]
        notes.append(f"duplicate_of_proposal:#{other.id}")
    p = EventProposal(
        email_id=email.id, attachment_id=attachment_id, kind=d.kind, ics_uid=d.ics_uid, title=d.title,
        start_at=d.start_at, end_at=d.end_at, all_day=d.all_day, tz_name=d.tz_name, tz_original=d.tz_original,
        location=d.location, organiser=d.organiser, attendees=d.attendees, meeting_url=d.meeting_url,
        conflicts=conflicts, confidence=d.confidence, evidence_quote=d.evidence_quote,
        status="superseded" if dupes or (other is not None and not new_wins) else "proposed",
        google_event_id=None, dedupe_key=d.dedupe_key, notes=notes, target_calendar=target_cal)
    session.add(p)
    session.flush()
    if other is not None and new_wins:  # the new one is better (e.g. has a time): it replaces the open one
        other.status, other.decided_at = "superseded", utcnow()
        other.notes = [*(other.notes or []), f"replaced_by_proposal:#{p.id}"]
        audit.record(session, actor="system", action="event_proposal_replaced", entity_type="event_proposal",
                     entity_id=other.id, detail=f"same appointment, better proposal #{p.id} (email {email.id})")
    audit.record(session, actor="llm-proposal" if d.evidence_quote else "system", action="event_proposed",
                 entity_type="event_proposal", entity_id=p.id,
                 after={"kind": d.kind, "title": d.title, "start": d.start_at.isoformat(), "status": p.status,
                        "confidence": d.confidence, "email_id": email.id,
                        "duplicate_of": other.id if other is not None and not new_wins else None})
    return p


def scan_email(session: Session, email_id: int, llm_client, settings: Settings, now: datetime | None = None) -> list[EventProposal]:
    """Extract event proposals from one incoming email. May raise LLMUnavailable (nothing is marked scanned)."""
    from mailassistant.classify.prompt import EmailView
    from mailassistant.classify.service import _recorder
    from mailassistant.llm.runner import StructuredOutputError, run_structured
    from mailassistant.llm.schemas import EventExtraction

    now = now or utcnow()
    email = session.get(Email, email_id)
    if email is None:
        return []
    cls = session.scalar(select(Classification).where(Classification.email_id == email_id,
                                                      Classification.is_current.is_(True)))
    if email.direction != "in" or (cls and (cls.injection_flag or cls.category == "spam_suspicious")):
        if cls and cls.injection_flag:
            audit.record(session, actor="system", action="event_scan_skipped", entity_type="email",
                         entity_id=email_id, detail="prompt injection suspected")
        email.events_scanned_at = now
        return []
    out: list[EventProposal] = []
    tz = settings.timezone
    found_ics = False
    for att in session.scalars(select(Attachment).where(Attachment.email_id == email_id, Attachment.is_ics.is_(True))):
        for ev in parse_ics(att.ics_text or "", tz):
            found_ics = True
            d, why = draft_from_ics(ev, subject=email.subject, now=now, default_tz=tz)
            if d is None:
                log.info("ics event skipped: %s", why)
                continue
            if d.cancel:
                n = session.execute(update(EventProposal).where(
                    EventProposal.ics_uid == d.ics_uid, EventProposal.status.in_(("proposed", "approved", "failed")))
                    .values(status="superseded", decided_at=now)).rowcount
                created = session.scalar(select(EventProposal.id).where(EventProposal.ics_uid == d.ics_uid,
                                                                        EventProposal.status == "created"))
                audit.record(session, actor="system", action="invitation_cancelled_by_organizer",
                             entity_type="email", entity_id=email_id,
                             detail=f"uid={d.ics_uid} withdrawn={n} already_created_proposal={created}")
                continue
            if p := store_draft(session, email, d, att.id, settings):
                out.append(p)
    session.commit()  # ICS results are safe to keep even if the model step below has to be retried

    if not found_ics and cls and cls.category == "invitation" and float(cls.confidence or 0) >= 0.6:
        view = EmailView(email.from_addr, email.from_name, list(email.to_addrs or []), list(email.cc_addrs or []),
                         email.subject, email.sent_at, email.body_text or "", dict(email.headers or {}), [], [])
        try:
            res = run_structured(llm_client, event_prompt.build_messages(view, body_chars=settings.classify_body_chars),
                                 EventExtraction, task="extract_event",
                                 record=_recorder(session, settings, event_prompt.PROMPT_VERSION))
            d, why = draft_from_extraction(
                res.model_dump(), subject=email.subject, body=email.body_text or "",
                sent_at=email.sent_at or now, now=now, default_tz=tz,
                default_minutes=settings.calendar_default_event_minutes, source_key=email.message_id,
                min_confidence=settings.calendar_min_confidence)
            if d is None:
                log.info("model extraction rejected: %s", why)
            elif p := store_draft(session, email, d, None, settings):
                out.append(p)
        except StructuredOutputError:
            log.info("event extraction: model output unusable for email %s", email_id)
    email.events_scanned_at = now
    return out


def pending_scan_email_ids(session: Session, window_days: int, limit: int) -> list[int]:
    when = func.coalesce(Email.sent_at, Email.received_at)
    has_ics = exists(select(Attachment.id).where(Attachment.email_id == Email.id, Attachment.is_ics.is_(True)))
    invite = exists(select(Classification.id).where(
        Classification.email_id == Email.id, Classification.is_current.is_(True), Classification.category == "invitation"))
    return list(session.scalars(
        select(Email.id).where(Email.direction == "in", Email.events_scanned_at.is_(None),
                               when >= utcnow() - timedelta(days=window_days), or_(has_ics, invite))
        .order_by(when.desc()).limit(limit)))


# ------------------------------------------------------------------ approval and creation
def event_id_for(dedupe_key: str) -> str:
    """Deterministic Google event id (base32hex, a-v and 0-9): inserting twice cannot create two events."""
    return base64.b32hexencode(hashlib.sha256(dedupe_key.encode()).digest()).decode().lower().rstrip("=")


def build_event_body(p: EventProposal, email: Email, default_tz: str, *, minimal: bool = False) -> dict:
    """minimal=True (private calendar, possibly shared with family): title, time and location only - no mail details."""
    tz = p.tz_name or default_tz
    zone = ZoneInfo(tz)
    if p.all_day:
        first = p.start_at.astimezone(zone).date()
        last = max(p.end_at.astimezone(zone).date(), first + timedelta(days=1))
        start, end = {"date": first.isoformat()}, {"date": last.isoformat()}
    else:
        start = {"dateTime": p.start_at.astimezone(zone).isoformat(), "timeZone": tz}
        end = {"dateTime": p.end_at.astimezone(zone).isoformat(), "timeZone": tz}
    lines = []
    if p.kind == "proposed_date":
        lines.append("Proposed date - not yet confirmed.")
    if p.meeting_url:
        lines.append(f"Meeting link: {p.meeting_url}")
    if p.organiser:
        lines.append(f"Organiser: {p.organiser}")
    people = [a.get("name") or a.get("email") for a in (p.attendees or []) if a.get("email")]
    if people:
        lines.append("Invited: " + ", ".join(people[:15]))
    lines.append(f"Source: email '{email.subject or ''}' from {email.from_addr}, sent "
                 f"{email.sent_at.date().isoformat() if email.sent_at else '?'}.")
    lines.append("Created by Mail Assistant after your approval. No invitations were sent.")
    if minimal:
        lines = ["Proposed date - not yet confirmed."] if p.kind == "proposed_date" else []
    body = {
        "id": event_id_for(p.dedupe_key), "summary": p.title or email.subject or "(no title)",
        "start": start, "end": end, "description": "\n".join(lines),
        "status": "tentative" if p.kind == "proposed_date" else "confirmed",
        "extendedProperties": {"private": {"mail_assistant_key": p.dedupe_key, "proposal_id": str(p.id),
                                           **({} if minimal else {"source_message_id": (email.message_id or "")[:200]})}},
    }
    if not body["description"]:
        del body["description"]
    if p.location:
        body["location"] = p.location
    return body


def approve_and_create(session: Session, proposal_id: int, client: GoogleCalendarClient, settings: Settings, *,
                       accept_conflicts: bool = False, now: datetime | None = None) -> EventProposal:
    now = now or utcnow()
    p = session.get(EventProposal, proposal_id, with_for_update=True)
    if p is None:
        raise ProposalError(f"no proposal {proposal_id}")
    if p.kind == "mention":
        raise ProposalError("this is only a mention of an event, not an appointment; it cannot be created")
    if p.status in ("created", "rejected", "superseded"):
        raise ProposalError(f"proposal is already {p.status}")
    if p.start_at is None or p.end_at is None:
        raise ProposalError("no resolved date/time")
    if p.end_at < now:
        raise ProposalError("the event is in the past")
    email = session.get(Email, p.email_id)
    private = p.target_calendar == PRIVATE
    cals = resolve_calendars(session, client, settings)   # live: a renamed/removed calendar is caught here
    chosen = cals["private"] if private else cals["work"]
    if chosen is None:
        raise ProposalError("no private calendar is configured (GOOGLE_PRIVATE_CALENDAR); approve to work instead")
    if chosen["error"] or not chosen["id"]:
        raise ProposalError(f"{'private' if private else 'work'} calendar: {chosen['error']}")
    target = chosen["id"]

    # Fresh look at every configured calendar right now (the local copy may be up to 15 min old).
    live: list[CalEvent] = []
    for cid in cals["read_ids"]:
        for item in client.list_events(cid, p.start_at - timedelta(days=1), p.end_at + timedelta(days=1)):
            if ev := cal_event_from_google(item, cid, settings.timezone):
                live.append(ev)
    cand = Candidate(p.start_at, p.end_at, p.all_day, p.location, p.title, p.ics_uid, p.dedupe_key)
    if find_duplicates(cand, live):
        p.status, p.decided_at = "superseded", now
        audit.record(session, actor="system", action="event_already_in_calendar", entity_type="event_proposal", entity_id=p.id)
        session.commit()
        raise ProposalError("this appointment is already in your calendar; nothing was created")
    p.conflicts = find_conflicts(cand, live, settings.calendar_travel_buffer_minutes)
    if hard_conflicts(p.conflicts) and not accept_conflicts:
        session.commit()
        raise ConflictsFound(p.conflicts)

    p.status, p.decided_at = "approved", now
    audit.record(session, actor="user", action="event_approved", entity_type="event_proposal", entity_id=p.id,
                 detail=f"accept_conflicts={accept_conflicts}")
    session.commit()
    body = build_event_body(p, email, settings.timezone, minimal=private)
    try:
        created = client.insert_event(target, body)
    except GoogleConflict:  # same id already exists: our earlier attempt succeeded
        created = client.get_event(target, body["id"])
        if created.get("status") == "cancelled":
            p.status = "failed"
            audit.record(session, actor="system", action="event_create_failed", entity_type="event_proposal",
                         entity_id=p.id, detail="an event with this id was created earlier and later deleted")
            session.commit()
            raise ProposalError("this event was created before and then deleted in Google Calendar; reject this proposal and add it manually")
    except GoogleApiError as e:
        p.status = "failed"
        audit.record(session, actor="system", action="event_create_failed", entity_type="event_proposal",
                     entity_id=p.id, detail=str(e)[:300])
        session.commit()
        raise ProposalError(f"Google refused the event: {e}") from e
    # GoogleUnavailable propagates: the proposal stays 'approved' and can be retried with the same command.
    p.status, p.google_event_id, p.created_calendar_id = "created", created.get("id"), target
    audit.record(session, actor="system", action="event_created", entity_type="event_proposal", entity_id=p.id,
                 after={"google_event_id": p.google_event_id, "calendar": "private" if private else "work"})
    session.commit()
    return p
