"""PostgreSQL-backed job queue (FOR UPDATE SKIP LOCKED).

Lifecycle: queued -> running -> done | queued (retry/deferred) | dead.
- fail():  consumes an attempt; exponential backoff; 'dead' after max_attempts.
- defer(): for "not now" conditions (e.g. Ollama unreachable); does NOT consume an attempt.
Callers own the transaction: commit right after claim() so the row lock is released.
"""
from datetime import timedelta
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from mailassistant.db.models import Job
from mailassistant.db.session import utcnow

ACTIVE_PREDICATE = "dedupe_key IS NOT NULL AND status IN ('queued','running')"


def backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(60 * 2 ** max(attempts - 1, 0), 900))


def enqueue(session: Session, type: str, payload: dict[str, Any] | None = None, *,
            dedupe_key: str | None = None, run_after=None, priority: int = 100,
            max_attempts: int = 5) -> int | None:
    """Insert a job. Returns its id, or None if an active job with the same dedupe_key exists."""
    stmt = (
        pg_insert(Job)
        .values(type=type, payload=payload or {}, dedupe_key=dedupe_key, priority=priority,
                max_attempts=max_attempts, run_after=run_after or utcnow(), status="queued")
        .on_conflict_do_nothing(index_elements=[Job.dedupe_key],
                                index_where=text(ACTIVE_PREDICATE))
        .returning(Job.id)
    )
    return session.execute(stmt).scalar_one_or_none()


def claim(session: Session, worker_id: str, types: list[str] | None = None) -> Job | None:
    now = utcnow()
    stmt = (select(Job).where(Job.status == "queued", Job.run_after <= now)
            .order_by(Job.priority, Job.run_after, Job.id).limit(1)
            .with_for_update(skip_locked=True))
    if types:
        stmt = stmt.where(Job.type.in_(types))
    job = session.execute(stmt).scalar_one_or_none()
    if job is None:
        return None
    job.status, job.locked_by, job.locked_at = "running", worker_id, now
    job.attempts += 1
    session.flush()
    return job


def complete(session: Session, job_id: int) -> None:
    session.execute(update(Job).where(Job.id == job_id).values(
        status="done", finished_at=utcnow(), locked_by=None, locked_at=None, last_error=None))


def fail(session: Session, job_id: int, error: str) -> str:
    job = session.get(Job, job_id, with_for_update=True)
    job.last_error = error
    job.locked_by = job.locked_at = None
    if job.attempts >= job.max_attempts:
        job.status, job.finished_at = "dead", utcnow()
    else:
        job.status, job.run_after = "queued", utcnow() + backoff(job.attempts)
    session.flush()
    return job.status


def defer(session: Session, job_id: int, reason: str, delay: timedelta) -> None:
    job = session.get(Job, job_id, with_for_update=True)
    job.status, job.run_after, job.defer_reason = "queued", utcnow() + delay, reason
    job.attempts = max(job.attempts - 1, 0)
    job.locked_by = job.locked_at = None
    session.flush()


def reclaim_stale(session: Session, lease: timedelta = timedelta(minutes=15)) -> int:
    """Return jobs whose worker died (lease expired) to the queue."""
    res = session.execute(
        update(Job).where(Job.status == "running", Job.locked_at < utcnow() - lease)
        .values(status="queued", locked_by=None, locked_at=None, last_error="worker lease expired"))
    return res.rowcount
