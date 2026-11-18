"""Incremental, read-only synchronisation: MailSource -> database.

Design:
- A message is identified by its Message-ID; one `emails` row per message, one `email_locations`
  row per (mailbox, UID) where it currently lives (Bridge may show it in several folders).
- "What is new" = server UIDs minus UIDs we already know, so a crash or time-out never loses or
  duplicates anything. `mailboxes.last_uid` is only advanced after a mailbox is fully caught up.
- For each new UID we fetch only the Message-ID header first; messages we already have are just
  given a new location (cheap resync after a UIDVALIDITY change), others are downloaded in full.
- New mail is processed newest-first, so recent mail is available first during the initial backfill.
- Mail is never deleted from our database; locations that vanish on the server get `removed_at`.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mailassistant.config import Settings
from mailassistant.db import audit
from mailassistant.db.models import (Attachment, Email, EmailLocation, MailAccount, Mailbox,
                                     Thread)
from mailassistant.db.session import utcnow
from mailassistant.mail.mailboxes import mailbox_role, select_mailboxes, sync_priority
from mailassistant.mail.parser import ParsedEmail, parse_message
from mailassistant.mail.source import MailboxInfo, MailSource
from mailassistant.mail.subjects import normalise_subject

log = logging.getLogger(__name__)
BATCH = 25


@dataclass
class SyncStats:
    new_messages: int = 0
    linked_existing: int = 0
    removed: int = 0
    errors: int = 0
    mailboxes: int = 0
    remaining_by_mailbox: dict[str, int] = field(default_factory=dict)

    @property
    def remaining(self) -> int:
        return sum(self.remaining_by_mailbox.values())

    def merge(self, other: SyncStats) -> None:
        self.new_messages += other.new_messages
        self.linked_existing += other.linked_existing
        self.removed += other.removed
        self.errors += other.errors
        self.remaining_by_mailbox.update(other.remaining_by_mailbox)


def _chunks(items: list, n: int) -> Iterator[list]:
    for i in range(0, len(items), n):
        yield items[i:i + n]


def ensure_account(session: Session, settings: Settings) -> MailAccount:
    """The Proton account from .env (kept for callers/tests; see mail/accounts.py)."""
    from mailassistant.mail.accounts import proton_account
    return proton_account(session, settings)


def _get_mailbox(session: Session, account: MailAccount, info: MailboxInfo) -> Mailbox:
    mb = session.scalar(select(Mailbox).where(Mailbox.account_id == account.id,
                                              Mailbox.name == info.name))
    if mb is None:
        mb = Mailbox(account_id=account.id, name=info.name, role=mailbox_role(info.name, info.flags),
                     last_uid=0)
        session.add(mb)
        session.flush()
    return mb


# ------------------------------------------------------------------------------ threading
def _participants(p: ParsedEmail, mine: set[str]) -> list[str]:
    return sorted({a for a in (p.from_addr, *p.to_addrs, *p.cc_addrs) if a and a not in mine})


def resolve_thread(session: Session, account: MailAccount, p: ParsedEmail, mine: set[str]) -> Thread:
    """Find or create the thread for a message.

    1. A known message among References/In-Reply-To decides the thread.
    2. Otherwise a thread whose key is the root/parent/own Message-ID (covers replies that were
       synced before their parent, which happens because we process newest-first).
    3. Without reply headers, a reply-looking subject (Re:/Antw:...) joins a recent thread with
       the same normalised subject AND at least one shared correspondent.
    4. Otherwise a new thread. (Threads are never merged automatically; documented limitation.)
    """
    ids = [*p.references, *([p.in_reply_to] if p.in_reply_to else [])]
    root = p.references[0] if p.references else (p.in_reply_to or p.message_id)
    if ids:
        tid = session.scalar(
            select(Email.thread_id)
            .where(Email.account_id == account.id, Email.message_id.in_(ids),
                   Email.thread_id.is_not(None))
            .order_by(Email.thread_id).limit(1))
        if tid:
            return session.get(Thread, tid)
    t = session.scalar(
        select(Thread).where(Thread.account_id == account.id,
                             Thread.thread_key.in_({root, p.message_id, *ids}))
        .order_by(Thread.id).limit(1))
    if t:
        return t
    norm, had_prefix = normalise_subject(p.subject)
    people = _participants(p, mine)
    if not ids and had_prefix and norm and p.sent_at and people:
        from datetime import timedelta
        cands = session.scalars(
            select(Thread).where(Thread.account_id == account.id, Thread.subject_norm == norm,
                                 Thread.last_message_at >= p.sent_at - timedelta(days=30))
            .order_by(Thread.last_message_at.desc()).limit(5)).all()
        for c in cands:
            shared = session.scalar(
                select(Email.id).where(
                    Email.thread_id == c.id,
                    or_(Email.from_addr.in_(people), Email.to_addrs.overlap(people),
                        Email.cc_addrs.overlap(people))).limit(1))
            if shared:
                return c
    if not ids and not had_prefix and norm and p.sent_at and people:
        # 3b. The original of a header-less reply that was synced first (newest-first order): join the
        # thread that starts shortly after it with the same subject and a shared correspondent.
        from datetime import timedelta
        cands = session.scalars(
            select(Thread).where(Thread.account_id == account.id, Thread.subject_norm == norm,
                                 Thread.first_message_at >= p.sent_at,
                                 Thread.first_message_at <= p.sent_at + timedelta(days=30))
            .order_by(Thread.first_message_at).limit(5)).all()
        for c in cands:
            shared = session.scalar(
                select(Email.id).where(
                    Email.thread_id == c.id,
                    or_(Email.from_addr.in_(people), Email.to_addrs.overlap(people),
                        Email.cc_addrs.overlap(people))).limit(1))
            if shared:
                return c
    t = Thread(account_id=account.id, thread_key=root, subject_norm=norm or None)
    session.add(t)
    session.flush()
    return t


def _touch_thread(t: Thread, p: ParsedEmail) -> None:
    if p.sent_at:
        if t.first_message_at is None or p.sent_at < t.first_message_at:
            t.first_message_at = p.sent_at
        if t.last_message_at is None or p.sent_at > t.last_message_at:
            t.last_message_at = p.sent_at


# ------------------------------------------------------------------------------- storing
def _store_message(session: Session, account: MailAccount, mb: Mailbox, mine: set[str],
                   uid: int, raw: bytes) -> bool:
    """Store one downloaded message. Returns True if a new email row was created."""
    p = parse_message(raw)
    thread = resolve_thread(session, account, p, mine)
    direction = "out" if (mb.role == "sent" or p.from_addr in mine) else "in"
    created = True
    try:
        with session.begin_nested():
            email = Email(
                account_id=account.id, thread_id=thread.id, message_id=p.message_id,
                in_reply_to=p.in_reply_to, references_=p.references, direction=direction,
                from_addr=p.from_addr, from_name=p.from_name, to_addrs=p.to_addrs,
                cc_addrs=p.cc_addrs, reply_to=p.reply_to, subject=p.subject, sent_at=p.sent_at,
                body_text=p.body_text, headers=p.headers, content_hash=p.content_hash,
                has_attachments=p.has_attachments)
            session.add(email)
            session.flush()
            for a in p.attachments:
                session.add(Attachment(email_id=email.id, filename=a.filename, mime_type=a.mime_type,
                                       size_bytes=a.size_bytes, sha256=a.sha256, is_ics=a.is_ics,
                                       ics_text=a.ics_text))
    except IntegrityError:  # same Message-ID stored concurrently / reused by a sender
        created = False
        email = session.scalar(select(Email).where(Email.account_id == account.id,
                                                   Email.message_id == p.message_id))
    else:
        _touch_thread(thread, p)
    session.add(EmailLocation(email_id=email.id, mailbox_id=mb.id, uid=uid))
    return created


# ------------------------------------------------------------------------------ mailbox
def sync_mailbox(session: Session, source: MailSource, account: MailAccount, info: MailboxInfo,
                 mine: set[str], *, full: bool, deadline: float,
                 clock: Callable[[], float] = time.monotonic, batch_size: int = BATCH,
                 since: date | None = None) -> SyncStats:
    stats = SyncStats()
    mb = _get_mailbox(session, account, info)
    st = source.status(info.name)

    if mb.uidvalidity is not None and mb.uidvalidity != st.uidvalidity:
        log.warning("UIDVALIDITY changed for %s (%s -> %s): resynchronising by Message-ID",
                    info.name, mb.uidvalidity, st.uidvalidity)
        session.execute(delete(EmailLocation).where(EmailLocation.mailbox_id == mb.id))
        mb.last_uid = 0
        audit.record(session, actor="system", action="uidvalidity_changed", entity_type="mailbox",
                     entity_id=mb.id, before=mb.uidvalidity, after=st.uidvalidity, detail=info.name)
    mb.uidvalidity = st.uidvalidity

    if not full and mb.last_uid and st.uidnext - 1 == mb.last_uid:
        mb.last_synced_at = utcnow()
        return stats  # nothing new since the last complete run

    known = set(session.scalars(select(EmailLocation.uid).where(
        EmailLocation.mailbox_id == mb.id, EmailLocation.removed_at.is_(None))))
    scan_all = full or mb.last_uid == 0
    from_uid = 1 if scan_all else mb.last_uid + 1
    server = set(source.search_uids(info.name, from_uid, since=since) if since
                 else source.search_uids(info.name, from_uid))

    if scan_all:
        gone = sorted(known - server)
        for part in _chunks(gone, 5000):
            session.execute(update(EmailLocation).where(
                EmailLocation.mailbox_id == mb.id, EmailLocation.uid.in_(part),
                EmailLocation.removed_at.is_(None)).values(removed_at=utcnow()))
        stats.removed = len(gone)

    todo = sorted(server - known, reverse=True)  # newest first
    done = 0
    for chunk in _chunks(todo, batch_size):
        ids = source.fetch_message_ids(info.name, chunk)
        norm = {u: m for u, m in ids.items() if m}
        existing = dict(session.execute(
            select(Email.message_id, Email.id).where(Email.account_id == account.id,
                                                     Email.message_id.in_(set(norm.values())))).all()) \
            if norm else {}
        need_full: list[int] = []
        for uid in chunk:
            if norm.get(uid) in existing:
                session.add(EmailLocation(email_id=existing[norm[uid]], mailbox_id=mb.id, uid=uid))
                stats.linked_existing += 1
            else:
                need_full.append(uid)
        for uid, raw in source.fetch_raw(info.name, need_full):
            try:
                with session.begin_nested():
                    if _store_message(session, account, mb, mine, uid, raw):
                        stats.new_messages += 1
                    else:
                        stats.linked_existing += 1
            except Exception:  # noqa: BLE001 - one bad message must not block the mailbox
                stats.errors += 1
                log.exception("failed to store message uid=%s in %s", uid, info.name)  # no content
        session.commit()
        done += len(chunk)
        if clock() >= deadline:
            break

    stats.remaining_by_mailbox[info.name] = max(len(todo) - done, 0)
    if not stats.remaining_by_mailbox[info.name]:
        mb.last_uid = st.uidnext - 1
        mb.last_synced_at = utcnow()
    session.commit()
    return stats


# ------------------------------------------------------------------------------- account
def sync_account(session: Session, source: MailSource, settings: Settings, *, full: bool = False,
                 budget_seconds: float = 120.0, batch_size: int = BATCH,
                 clock: Callable[[], float] = time.monotonic, account: MailAccount | None = None) -> SyncStats:
    """Synchronise one account (default: the Proton account from .env)."""
    from mailassistant.mail.accounts import SKIP_ROLES, backfill_since, own_addresses, sync_patterns

    account = account or ensure_account(session, settings)
    mine = set(own_addresses(session, settings)) | {account.address}
    include, exclude = sync_patterns(account, settings)
    infos = select_mailboxes(source.list_mailboxes(), include, exclude)
    if account.kind != "proton_bridge":   # other servers: also skip Trash/Spam/Drafts/... by their flags
        infos = [i for i in infos if mailbox_role(i.name, i.flags) not in SKIP_ROLES]
    infos = sorted(infos, key=lambda i: (sync_priority(i), i.name))
    since = backfill_since(account, settings)
    total = SyncStats(mailboxes=len(infos))
    overall = clock() + budget_seconds
    share = max(5.0, min(15.0, budget_seconds / max(len(infos), 1)))
    pending: list[MailboxInfo] = []
    # Pass 1: every mailbox gets a short turn (so Sent etc. are not starved by a big INBOX backfill)
    for info in infos:
        st = sync_mailbox(session, source, account, info, mine, full=full,
                          deadline=min(overall, clock() + share), clock=clock, batch_size=batch_size, since=since)
        total.merge(st)
        if st.remaining:
            pending.append(info)
    # Pass 2: spend the remaining time on mailboxes that still have work, in priority order
    for info in pending:
        if clock() >= overall:
            break
        total.merge(sync_mailbox(session, source, account, info, mine, full=False,
                                 deadline=overall, clock=clock, batch_size=batch_size, since=since))
    session.commit()
    return total
