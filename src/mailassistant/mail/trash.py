"""Move mail to the mailbox's Trash - the only change this system ever makes to a mailbox.

Flow: the dashboard (DB only) records the user's request in `mail_actions` and queues `mail.trash`; the worker
moves the messages with IMAP MOVE to the account's Trash folder (recoverable there; nothing is expunged).
Email rows stay in the database (mail is never deleted from the DB); the dashboard just hides them.

Safety:
- only incoming mail; at most MAX_PER_REQUEST per click;
- mail with an open follow-up or a pending/approved calendar proposal is kept (listed on the confirm page);
- before moving, the worker re-reads the Message-ID of every UID on the server and only moves UIDs whose
  Message-ID matches the database (so a renumbered mailbox can never cause the wrong mail to be moved).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import and_, exists, select
from sqlalchemy.orm import Session, aliased

from mailassistant.db import audit, queue
from mailassistant.db.models import Email, EmailLocation, EventProposal, Followup, MailAction
from mailassistant.security.allowlist import ActionType

MAX_PER_REQUEST = 500
JOB = "mail.trash"
OPEN_FOLLOWUP = ("proposed", "open", "waiting")
OPEN_EVENT = ("proposed", "approved")


class TrashError(Exception):
    pass


def hidden_clause():
    """SQL condition: the email has a trash request (requested or done) and has NOT shown up again in a synced
    folder since then (a mail the user restored from Trash gets a new location and is visible again)."""
    ma = aliased(MailAction)
    loc = aliased(EmailLocation)
    reappeared = exists().where(loc.email_id == ma.email_id, loc.removed_at.is_(None),
                                loc.first_seen_at > ma.requested_at)
    return exists().where(ma.email_id == Email.id, ma.action == "trash",
                          ma.status.in_(("requested", "done")), ~reappeared)


@dataclass
class TrashSelection:
    eligible: list[Email] = field(default_factory=list)
    kept: list[tuple[Email, str]] = field(default_factory=list)   # (email, reason)


def select_for_trash(session: Session, email_ids: Iterable[int]) -> TrashSelection:
    ids = sorted({int(i) for i in email_ids})
    out = TrashSelection()
    if not ids:
        return out
    if len(ids) > MAX_PER_REQUEST:
        raise TrashError(f"at most {MAX_PER_REQUEST} mails at a time (you selected {len(ids)})")
    emails = session.scalars(select(Email).where(Email.id.in_(ids)).order_by(Email.id)).all()
    open_fu = set(session.scalars(select(Followup.email_id).where(Followup.email_id.in_(ids),
                                                                  Followup.status.in_(OPEN_FOLLOWUP))))
    open_ev = set(session.scalars(select(EventProposal.email_id).where(EventProposal.email_id.in_(ids),
                                                                       EventProposal.status.in_(OPEN_EVENT))))
    already = set(session.scalars(select(Email.id).where(Email.id.in_(ids), hidden_clause())))
    for e in emails:
        if e.direction != "in":
            out.kept.append((e, "sent by you"))
        elif e.id in open_fu:
            out.kept.append((e, "has an open follow-up"))
        elif e.id in open_ev:
            out.kept.append((e, "has a calendar proposal"))
        elif e.id in already:
            out.kept.append((e, "already moved to Trash"))
        else:
            out.eligible.append(e)
    return out


def request_trash(session: Session, email_ids: Iterable[int], *, actor: str = "user") -> int:
    """Record the user's request and queue the worker job. Returns the number of mails queued."""
    sel = select_for_trash(session, email_ids)
    for e in sel.eligible:
        session.add(MailAction(email_id=e.id, action="trash", status="requested"))
    if sel.eligible:
        audit.record(session, actor=actor, action=ActionType.MOVE_TO_TRASH.value, entity_type="emails",
                     after=[e.id for e in sel.eligible],
                     detail=f"{len(sel.eligible)} mail(s) requested via dashboard; kept {len(sel.kept)}")
        session.flush()
        queue.enqueue(session, JOB, {}, dedupe_key=JOB, priority=20)
    return len(sel.eligible)


# ------------------------------------------------------------------ worker side (pure + DB, no network)
def matching_uids(expected: dict[int, str], on_server: dict[int, str | None]) -> list[int]:
    """UIDs that may be moved: present on the server with exactly the Message-ID the database has for them."""
    return sorted(u for u, mid in expected.items() if mid and on_server.get(u) == mid)


def pending(session: Session) -> list[MailAction]:
    return list(session.scalars(select(MailAction).where(MailAction.action == "trash",
                                                         MailAction.status == "requested")
                                .order_by(MailAction.id)))


def live_locations(session: Session, email_ids: list[int]) -> list[tuple[EmailLocation, Email]]:
    return list(session.execute(
        select(EmailLocation, Email).join(Email, Email.id == EmailLocation.email_id)
        .where(and_(EmailLocation.email_id.in_(email_ids), EmailLocation.removed_at.is_(None)))).all())
