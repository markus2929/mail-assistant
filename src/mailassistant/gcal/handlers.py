import logging
import time
from datetime import timedelta

from sqlalchemy import select

from mailassistant.config import get_settings
from mailassistant.db import audit, queue
from mailassistant.db.models import EventProposal
from mailassistant.db.session import session_scope
from mailassistant.db.status import set_status
from mailassistant.gcal.google import GoogleAuthRevoked, GoogleError, GoogleNotConfigured, GoogleUnavailable
from mailassistant.gcal.service import (ConflictsFound, ProposalError, approve_and_create, pending_scan_email_ids,
                                        scan_email, sync_calendar)
from mailassistant.gcal.tokens import calendar_client
from mailassistant.jobs.registry import Deferred, handler
from mailassistant.llm.client import LLMModelMissing, LLMUnavailable, OllamaClient

log = logging.getLogger(__name__)
_down_until = 0.0


def _google_status(ok: bool, detail: str) -> None:
    with session_scope() as session:
        set_status(session, "google", ok, detail)


@handler("gcal.sync")
def gcal_sync(payload: dict) -> None:
    s = get_settings()
    try:
        with session_scope() as session:
            n = sync_calendar(session, calendar_client(session, s), s)
            from mailassistant.gcal.review import calendars
            c = calendars(session) or {}
            problems = [f"{role}: {r['error']}" for role in ("work", "private") if (r := c.get(role)) and r.get("error")]
            set_status(session, "google", not problems and not c.get("note"),
                       "; ".join([*problems, *([c["note"]] if c.get("note") else [])])
                       or f"authorised; {len(c.get('read_ids', []))} calendar(s), {n} events in window")
        log.info("calendar sync: %d events in window", n)
    except GoogleNotConfigured as e:
        _google_status(False, "not authorised (run gcal.authorize)")
        raise Deferred(f"google not configured: {e}", timedelta(minutes=30)) from e
    except GoogleAuthRevoked as e:
        log.error("%s", e)
        _google_status(False, "access revoked or expired (invalid_grant): run gcal.authorize")
        with session_scope() as session:
            audit.record(session, actor="system", action="google_auth_revoked", detail=str(e)[:300])
        raise Deferred(str(e), timedelta(hours=6)) from e
    except GoogleUnavailable as e:
        _google_status(False, "Google not reachable")
        raise Deferred(f"google unavailable: {e}", timedelta(minutes=5)) from e


@handler("gcal.scan_pending")
def gcal_scan_pending(payload: dict) -> None:
    s = get_settings()
    with session_scope() as session:
        for email_id in pending_scan_email_ids(session, s.classify_window_days, 100):
            queue.enqueue(session, "gcal.scan_email", {"email_id": email_id}, dedupe_key=f"gcal-scan:{email_id}")


@handler("gcal.scan_email")
def gcal_scan_email(payload: dict) -> None:
    global _down_until
    if time.monotonic() < _down_until:
        raise Deferred("ollama unavailable (waiting)", timedelta(minutes=3))
    s = get_settings()
    client = OllamaClient.from_settings(s)
    try:
        with session_scope() as session:
            made = scan_email(session, int(payload["email_id"]), client, s)
        if made:
            log.info("event proposals created: %d (email %s)", len(made), payload["email_id"])
    except LLMUnavailable as e:
        _down_until = time.monotonic() + 120
        raise Deferred(f"ollama unavailable: {e}", timedelta(minutes=3)) from e
    except LLMModelMissing as e:
        _down_until = time.monotonic() + 600
        raise Deferred(str(e), timedelta(minutes=30)) from e


def _approval_result(proposal_id: int, result: str) -> None:
    with session_scope() as session:
        p = session.get(EventProposal, proposal_id)
        if p is not None:
            p.approval_result = result[:500]


@handler("gcal.approve")
def gcal_approve(payload: dict) -> None:
    """Approval requested in the dashboard (gcal.review.request_approval). The outcome is written to
    event_proposals.approval_result; conflicts are reported back, never accepted on the user's behalf."""
    s = get_settings()
    pid = int(payload["proposal_id"])
    try:
        with session_scope() as session:
            p = approve_and_create(session, pid, calendar_client(session, s), s,
                                   accept_conflicts=bool(payload.get("accept_conflicts")))
            p.approval_result = "created"
        log.info("event proposal %s created", pid)
    except ConflictsFound as e:
        _approval_result(pid, f"conflicts: {len(e.conflicts)} calendar item(s) overlap; approve anyway to create it")
    except ProposalError as e:
        _approval_result(pid, f"failed: {e}")
    except GoogleNotConfigured as e:
        _approval_result(pid, "waiting: Google is not authorised (run gcal.authorize on the desktop)")
        raise Deferred(f"google not configured: {e}", timedelta(minutes=30)) from e
    except GoogleAuthRevoked as e:
        _approval_result(pid, "waiting: Google access expired (run gcal.authorize on the desktop)")
        raise Deferred(str(e), timedelta(hours=1)) from e
    except GoogleUnavailable as e:
        _approval_result(pid, "waiting: Google not reachable, retrying")
        raise Deferred(f"google unavailable: {e}", timedelta(minutes=5)) from e
    except GoogleError as e:
        _approval_result(pid, f"failed: Google refused: {e}"[:300])
