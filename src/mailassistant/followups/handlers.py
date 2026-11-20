import logging
import time
from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from mailassistant.config import get_settings
from mailassistant.db import queue
from mailassistant.db.models import Job
from mailassistant.db.session import session_scope, utcnow
from mailassistant.followups.briefing import build_briefing
from mailassistant.followups.service import pending_scan_email_ids, scan_email
from mailassistant.jobs.registry import Deferred, handler
from mailassistant.llm.client import LLMModelMissing, LLMUnavailable
from mailassistant.llm.fallback import llm_client

log = logging.getLogger(__name__)
_down_until = 0.0          # circuit breaker: while Ollama is down, don't hammer it
MAX_QUEUED = 100


@handler("followups.scan_pending")
def followups_scan_pending(payload: dict) -> None:
    s = get_settings()
    if s.followup_window_days <= 0:
        return
    with session_scope() as session:
        queued = session.scalar(select(func.count()).select_from(Job).where(
            Job.type == "followups.scan_email", Job.status.in_(("queued", "running")))) or 0
        room = MAX_QUEUED - queued
        if room <= 0:
            return
        for email_id in pending_scan_email_ids(session, s.followup_window_days, room):
            queue.enqueue(session, "followups.scan_email", {"email_id": email_id}, dedupe_key=f"followups:{email_id}")


@handler("followups.scan_email")
def followups_scan_email(payload: dict) -> None:
    global _down_until
    if time.monotonic() < _down_until:
        raise Deferred("ollama unavailable (waiting)", timedelta(minutes=3))
    s = get_settings()
    client = llm_client(s, "followup")
    try:
        with session_scope() as session:
            made, closed = scan_email(session, int(payload["email_id"]), client, s)
        if made or closed:
            log.info("follow-ups: %d proposed, %d closure(s) proposed (email %s)", len(made), len(closed),
                     payload["email_id"])
    except LLMUnavailable as e:
        _down_until = time.monotonic() + 120
        raise Deferred(f"ollama unavailable: {e}", timedelta(minutes=3)) from e
    except LLMModelMissing as e:
        _down_until = time.monotonic() + 600
        log.error("%s", e)
        raise Deferred(str(e), timedelta(minutes=30)) from e


@handler("followups.briefing")
def followups_briefing(payload: dict) -> None:
    """Runs hourly; from BRIEFING_HOUR on it (re)builds today's briefing, so it stays current during the day
    (and appears even if the desktop was off at that hour). Earlier days stay as they were."""
    s = get_settings()
    local = utcnow().astimezone(ZoneInfo(s.timezone))
    if local.hour < s.briefing_hour and not payload.get("force"):
        return
    with session_scope() as session:
        b = build_briefing(session, local.date(), s.timezone, rebuild=True)
        log.info("briefing %s: %s", b.briefing_date, b.content.get("counts"))
