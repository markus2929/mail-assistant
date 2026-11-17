import logging
from datetime import timedelta

from sqlalchemy import delete, func, select

from mailassistant.config import get_settings
from mailassistant.db import audit
from mailassistant.db.models import Job, LlmCall
from mailassistant.db.session import session_scope, utcnow
from mailassistant.db.status import set_status
from mailassistant.jobs.registry import handler

log = logging.getLogger(__name__)


@handler("system.ping")
def ping(payload: dict) -> None:
    """Heartbeat for the dashboard (which only reads the database): worker alive, Ollama reachable."""
    from mailassistant.llm.client import OllamaClient

    s = get_settings()
    client = OllamaClient.from_settings(s)
    ollama_ok = client.healthy()
    models = ", ".join(dict.fromkeys(m for m in (s.ollama_model, s.followup_ollama_model) if m))
    with session_scope() as session:
        queued = session.scalar(select(func.count()).select_from(Job).where(Job.status == "queued")) or 0
        dead = session.scalar(select(func.count()).select_from(Job).where(
            Job.status == "dead", Job.finished_at > utcnow() - timedelta(days=1))) or 0
        set_status(session, "worker", True, f"queued={queued} dead_24h={dead}")
        set_status(session, "ollama", ollama_ok, f"models: {models}" if ollama_ok else "not reachable")
    log.info("ping ok")


@handler("maintenance.purge_llm_calls")
def purge_llm_calls(payload: dict) -> None:
    cutoff = utcnow() - timedelta(days=get_settings().llm_log_retention_days)
    with session_scope() as s:
        n = s.execute(delete(LlmCall).where(LlmCall.created_at < cutoff)).rowcount
        audit.record(s, actor="system", action="purge_llm_calls", detail=f"deleted {n} rows")
