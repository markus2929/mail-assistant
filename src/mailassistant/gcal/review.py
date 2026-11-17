"""Database-only decisions on event proposals, used by the dashboard (which never talks to Google).

Approving is asynchronous: the request is stored and a `gcal.approve` job is queued; the worker on the
desktop runs `service.approve_and_create` and writes the outcome to `approval_result`.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mailassistant.config import Settings
from mailassistant.db import audit, queue
from mailassistant.db.models import Classification, Email, EventProposal, Setting
from mailassistant.db.session import utcnow
from mailassistant.gcal.routing import PRIVATE, WORK, suggest_calendar

CALENDARS_KEY = "gcal.calendars"   # resolved calendars, written by the worker (gcal.service.resolve_calendars)


class ReviewError(Exception):
    """The request is not possible (message is shown to the user)."""


def reject_proposal(session: Session, proposal_id: int) -> EventProposal:
    p = session.get(EventProposal, proposal_id, with_for_update=True)
    if p is None:
        raise ReviewError(f"no proposal {proposal_id}")
    if p.status == "created":
        raise ReviewError("already created in Google Calendar; delete it there")
    p.status, p.decided_at = "rejected", utcnow()
    audit.record(session, actor="user", action="event_rejected", entity_type="event_proposal", entity_id=p.id)
    return p


def approval_pending(p: EventProposal) -> bool:
    return p.approval_requested_at is not None and (p.approval_result is None or p.approval_result.startswith("waiting"))


def calendars(session: Session) -> dict | None:
    """The worker's last resolution of the configured calendars (None until the first calendar sync)."""
    row = session.get(Setting, CALENDARS_KEY)
    return row.value if row is not None else None


def private_calendar_usable(session: Session, settings: Settings) -> bool:
    if not settings.google_private_calendar:
        return False
    c = calendars(session)
    p = (c or {}).get("private") or {}
    return bool(p.get("id")) and not p.get("error")


def suggestion_for(session: Session, email: Email, settings: Settings) -> tuple[str, str]:
    """Suggested calendar for an invitation in this mail: (work|private, reason). Private only if configured."""
    if not settings.google_private_calendar:
        return WORK, "no private calendar configured"
    counts = dict(session.execute(
        select(Classification.category, func.count()).join(Email, Email.id == Classification.email_id)
        .where(Email.from_addr == email.from_addr, Email.id != email.id, Classification.is_current.is_(True),
               Classification.category.in_(("work", "personal")))
        .group_by(Classification.category)).all()) if email.from_addr else {}
    return suggest_calendar(to_addrs=list(email.to_addrs or []), cc_addrs=list(email.cc_addrs or []),
                            work_addresses=settings.calendar_work_addresses,
                            sender_work=counts.get("work", 0), sender_personal=counts.get("personal", 0))


def request_approval(session: Session, proposal_id: int, *, accept_conflicts: bool = False,
                     calendar: str | None = None, settings: Settings | None = None) -> EventProposal:
    """calendar: work | private (None = keep the suggestion / work)."""
    p = session.get(EventProposal, proposal_id, with_for_update=True)
    if p is None:
        raise ReviewError(f"no proposal {proposal_id}")
    if calendar not in (None, WORK, PRIVATE):
        raise ReviewError(f"unknown calendar {calendar!r}")
    if calendar == PRIVATE and not (settings and private_calendar_usable(session, settings)):
        raise ReviewError("the private calendar is not configured or not usable (see the status page)")
    if p.kind == "mention":
        raise ReviewError("this is only a mention of an event, not an appointment; it cannot be created")
    if p.status not in ("proposed", "failed", "approved"):
        raise ReviewError(f"proposal is already {p.status}")
    if approval_pending(p):
        raise ReviewError("already being created")
    p.approval_requested_at, p.approval_result = utcnow(), None
    p.target_calendar = calendar or p.target_calendar or WORK
    queue.enqueue(session, "gcal.approve", {"proposal_id": p.id, "accept_conflicts": bool(accept_conflicts)},
                  dedupe_key=f"gcal.approve:{p.id}", priority=10)
    audit.record(session, actor="user", action="event_approval_requested", entity_type="event_proposal",
                 entity_id=p.id, detail=f"accept_conflicts={bool(accept_conflicts)} calendar={p.target_calendar}")
    return p
