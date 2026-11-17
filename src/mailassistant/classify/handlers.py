import logging
import time
from datetime import timedelta

from sqlalchemy import func, select

from mailassistant.classify.service import classify_email, pending_email_ids
from mailassistant.config import get_settings
from mailassistant.db import queue
from mailassistant.db.models import Job
from mailassistant.db.session import session_scope
from mailassistant.jobs.registry import Deferred, handler
from mailassistant.llm.client import LLMModelMissing, LLMUnavailable, OllamaClient

log = logging.getLogger(__name__)
_down_until = 0.0          # circuit breaker: while Ollama is down, don't hammer it
MAX_QUEUED = 300


@handler("classify.email")
def classify_email_job(payload: dict) -> None:
    global _down_until
    if time.monotonic() < _down_until:
        raise Deferred("ollama unavailable (waiting)", timedelta(minutes=3))
    s = get_settings()
    client = OllamaClient.from_settings(s)
    try:
        with session_scope() as session:
            classify_email(session, int(payload["email_id"]), client, s)
    except LLMUnavailable as e:
        _down_until = time.monotonic() + 120
        raise Deferred(f"ollama unavailable: {e}", timedelta(minutes=3)) from e
    except LLMModelMissing as e:
        _down_until = time.monotonic() + 600
        log.error("%s", e)
        raise Deferred(str(e), timedelta(minutes=30)) from e


@handler("classify.pending")
def classify_pending(payload: dict) -> None:
    """Queue classification for recent, unclassified incoming mail (newest first)."""
    s = get_settings()
    with session_scope() as session:
        queued = session.scalar(select(func.count()).select_from(Job).where(
            Job.type == "classify.email", Job.status == "queued")) or 0
        room = MAX_QUEUED - queued
        if room <= 0:
            return
        for email_id in pending_email_ids(session, s.classify_window_days, room):
            queue.enqueue(session, "classify.email", {"email_id": email_id},
                          dedupe_key=f"classify:{email_id}")
