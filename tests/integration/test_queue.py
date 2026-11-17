from datetime import timedelta

from sqlalchemy.orm import Session

from mailassistant.db import queue
from mailassistant.db.session import utcnow


def test_enqueue_dedupes_active_jobs(session):
    a = queue.enqueue(session, "x", dedupe_key="k")
    b = queue.enqueue(session, "x", dedupe_key="k")
    session.commit()
    assert a is not None and b is None
    job = queue.claim(session, "w")
    queue.complete(session, job.id)
    session.commit()
    assert queue.enqueue(session, "x", dedupe_key="k") is not None  # allowed again once done


def test_claim_respects_priority_and_run_after(session):
    later = queue.enqueue(session, "x", run_after=utcnow() + timedelta(hours=1))
    low = queue.enqueue(session, "x", priority=200)
    high = queue.enqueue(session, "x", priority=10)
    session.commit()
    assert queue.claim(session, "w").id == high
    assert queue.claim(session, "w").id == low
    assert queue.claim(session, "w") is None  # `later` not due yet
    assert later is not None


def test_concurrent_workers_never_get_same_job(engine, session):
    queue.enqueue(session, "x"); queue.enqueue(session, "x"); session.commit()
    s1, s2 = Session(engine), Session(engine)
    try:
        j1 = queue.claim(s1, "w1")   # row locked, transaction still open
        j2 = queue.claim(s2, "w2")
        assert j1 is not None and j2 is not None and j1.id != j2.id
    finally:
        s1.rollback(); s2.rollback(); s1.close(); s2.close()


def test_fail_backs_off_then_goes_dead(session):
    queue.enqueue(session, "x", max_attempts=2); session.commit()
    job = queue.claim(session, "w")
    assert queue.fail(session, job.id, "boom") == "queued"
    assert job.run_after > utcnow()
    job.run_after = utcnow() - timedelta(seconds=1); session.commit()
    job = queue.claim(session, "w")
    assert queue.fail(session, job.id, "boom again") == "dead"


def test_defer_does_not_consume_attempts(session):
    queue.enqueue(session, "x", max_attempts=1); session.commit()
    job = queue.claim(session, "w")
    assert job.attempts == 1
    queue.defer(session, job.id, "ollama unreachable", timedelta(minutes=5))
    assert job.status == "queued" and job.attempts == 0
    assert job.defer_reason == "ollama unreachable"


def test_stale_running_jobs_are_reclaimed(session):
    queue.enqueue(session, "x"); session.commit()
    job = queue.claim(session, "w")
    job.locked_at = utcnow() - timedelta(hours=1); session.commit()
    assert queue.reclaim_stale(session) == 1
    session.refresh(job)
    assert job.status == "queued"
