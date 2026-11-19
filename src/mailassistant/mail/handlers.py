import logging
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import select

from mailassistant.config import get_settings
from mailassistant.db import audit
from mailassistant.db.models import EmailLocation, Mailbox, MailAccount
from mailassistant.db.session import session_scope, utcnow
from mailassistant.db.status import set_status
from mailassistant.jobs.registry import Deferred, handler
from mailassistant.mail import trash
from mailassistant.mail.accounts import (AccountError, extra_accounts, proton_account, source_for,
                                         status_component)
from mailassistant.mail.imap_bridge import MailWriter
from mailassistant.mail.msauth import MicrosoftAuthError, MicrosoftUnavailable
from mailassistant.mail.source import (BridgeCertMismatch, MailSourceAuthError, MailSourceError,
                                       MailSourceUnavailable)
from mailassistant.mail.sync import sync_account

log = logging.getLogger(__name__)
TRANSIENT = (MailSourceUnavailable, MicrosoftUnavailable)
BLOCKED = (BridgeCertMismatch, MailSourceAuthError, MicrosoftAuthError)


def _status(component: str, ok: bool, detail: str) -> None:
    with session_scope() as session:
        set_status(session, component, ok, detail)


def _accounts(s) -> list[tuple[int, str, str]]:
    """(account id, status component, label): the Proton account (if configured) and the enabled extras."""
    out = []
    with session_scope() as session:
        if s.imap_username:
            out.append((proton_account(session, s).id, "bridge", "bridge"))
        out += [(a.id, status_component(a), a.label) for a in extra_accounts(session)]
    return out


def _sync_one(s, account_id: int, full: bool) -> tuple[str, bool]:
    with session_scope() as session:
        account = session.get(MailAccount, account_id)
        with source_for(session, s, account) as source:
            stats = sync_account(session, source, s, full=full, budget_seconds=120, account=account)
    if stats.new_messages or stats.linked_existing or stats.removed or stats.errors or stats.remaining:
        log.info("mail sync %s: new=%d linked=%d removed=%d errors=%d remaining=%d", account_id,
                 stats.new_messages, stats.linked_existing, stats.removed, stats.errors, stats.remaining)
    return (f"last sync: new={stats.new_messages} errors={stats.errors} remaining={stats.remaining}",
            stats.errors == 0)


@handler("mail.sync")
def mail_sync(payload: dict) -> None:
    """payload: {"full": bool}. Regular runs are cheap; the hourly full run also detects removals.
    Every account is synchronised on its own: one failing account never blocks the others."""
    s = get_settings()
    accounts = _accounts(s)
    if not accounts:
        _status("bridge", False, "not configured")
        raise Deferred("mail sync not configured (IMAP_USERNAME, imap_password, BRIDGE_CERT_SHA256)",
                       timedelta(minutes=10))
    retry: list[str] = []
    for account_id, component, label in accounts:
        try:
            detail, ok = _sync_one(s, account_id, bool(payload.get("full")))
            _status(component, ok, detail)
        except AccountError as e:   # not set up (yet): show it, but do not hold up the other accounts' sync
            _status(component, False, str(e)[:200])
        except TRANSIENT as e:   # Bridge not running yet / server or network down
            _status(component, False, f"unavailable ({type(e).__name__})")
            retry.append(label)
        except BLOCKED as e:
            log.error("mail sync %s blocked: %s", label, e)
            _status(component, False, f"blocked: {type(e).__name__}")
            with session_scope() as session:
                audit.record(session, actor="system", action="mail_sync_blocked", detail=f"{label}: {str(e)[:450]}")
            retry.append(label)
        except MailSourceError as e:
            log.error("mail sync %s failed: %s", label, type(e).__name__)
            _status(component, False, f"error: {type(e).__name__}")
            retry.append(label)
    if retry:
        raise Deferred(f"mail sync incomplete for: {', '.join(retry)}", timedelta(minutes=2))


# ------------------------------------------------------------------ Move to Trash (user's dashboard clicks)
@handler("mail.trash")
def mail_trash(payload: dict) -> None:
    """Carry out the user's Move-to-Trash requests. IMAP MOVE to Trash only; see mail/trash.py."""
    s = get_settings()
    # account id -> mailbox id -> {uid: message-id}
    work: dict[int, dict[int, dict[int, str]]] = defaultdict(lambda: defaultdict(dict))
    box_info: dict[int, tuple[str, int | None]] = {}
    with session_scope() as session:
        reqs = trash.pending(session)
        if not reqs:
            return
        ids = [r.email_id for r in reqs]
        located: set[int] = set()
        for loc, email in trash.live_locations(session, ids):
            if loc.mailbox_id not in box_info:
                mb = session.get(Mailbox, loc.mailbox_id)
                box_info[mb.id] = (mb.name, mb.uidvalidity)
            work[email.account_id][loc.mailbox_id][loc.uid] = email.message_id
            located.add(email.id)
        for r in reqs:
            if r.email_id not in located:   # no longer in any synced folder (moved/deleted elsewhere)
                r.status, r.done_at, r.detail = "skipped", utcnow(), "not in a synced folder any more"

    retry = False
    for account_id, mailboxes in work.items():
        moved: list[tuple[int, int]] = []
        try:
            with session_scope() as session:
                writer = source_for(session, s, session.get(MailAccount, account_id), cls=MailWriter)
            with writer:
                for mb_id, uids in mailboxes.items():
                    name, validity = box_info[mb_id]
                    moved += [(mb_id, u) for u in writer.move_to_trash(name, validity, uids)]
        except TRANSIENT:
            _record(account_id, mailboxes, moved, final=False)
            retry = True
            continue
        except (AccountError, MailSourceError, *BLOCKED) as e:
            log.error("move to trash failed for account %s: %s", account_id, type(e).__name__)
            _record(account_id, mailboxes, moved, final=True, why=f"{type(e).__name__}: {str(e)[:200]}")
            continue
        _record(account_id, mailboxes, moved, final=True)
    if retry:
        raise Deferred("mail server unavailable; Move to Trash will be retried", timedelta(minutes=5))


def _record(account_id: int, mailboxes: dict[int, dict[int, str]], moved: list[tuple[int, int]], *,
            final: bool, why: str | None = None) -> None:
    """Store the outcome. Moved -> done (location removed). Not moved: final -> skipped/failed, else stays
    'requested' for the retry."""
    now = utcnow()
    with session_scope() as session:
        def email_of(mb_id: int, uid: int) -> int | None:
            return session.scalar(select(EmailLocation.email_id).where(EmailLocation.mailbox_id == mb_id,
                                                                        EmailLocation.uid == uid))
        done: set[int] = set()
        for mb_id, uid in moved:
            loc = session.scalar(select(EmailLocation).where(EmailLocation.mailbox_id == mb_id,
                                                              EmailLocation.uid == uid))
            if loc is not None:
                loc.removed_at = now
                done.add(loc.email_id)
        involved = {email_of(mb, u) for mb, uids in mailboxes.items() for u in uids} - {None}
        for r in trash.pending(session):
            if r.email_id in done:
                r.status, r.done_at, r.detail = "done", now, None
            elif r.email_id in involved and final:
                r.status, r.done_at = ("failed" if why else "skipped"), now
                r.detail = why or "not found on the server under the known number (changed since the last sync)"
        if done or (final and involved):
            audit.record(session, actor="system", action="mail_moved_to_trash", entity_type="mail_account",
                         entity_id=account_id, after=sorted(done),
                         detail=f"moved {len(done)} of {len(involved)}" + (f"; error {why}" if why else ""))


