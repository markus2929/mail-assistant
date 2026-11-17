from datetime import timedelta

from mailassistant.db.queue import backoff
from mailassistant.jobs import scheduler


def test_backoff_grows_and_is_capped():
    assert backoff(1) == timedelta(seconds=60)
    assert backoff(2) == timedelta(seconds=120)
    assert backoff(10) == timedelta(seconds=900)


def test_scheduler_fires_once_then_waits(monkeypatch):
    fired = []
    monkeypatch.setattr(scheduler.queue, "enqueue", lambda s, t, *a, **kw: fired.append(t))

    class Ctx:
        def __enter__(self): return None
        def __exit__(self, *a): return False
    monkeypatch.setattr(scheduler, "session_scope", lambda: Ctx())
    last: dict[str, float] = {}
    assert set(scheduler.tick(last, 1000.0)) == {"system.ping", "maintenance.purge_llm_calls",
                                                 "mail.sync", "mail.sync.full",
                                                 "classify.pending", "gcal.sync", "gcal.scan_pending",
                                                 "followups.scan_pending", "followups.briefing",
                                                 "notify.briefing", "notify.due"}
    assert scheduler.tick(last, 1030.0) == []                # nothing due after 30 s
    assert scheduler.tick(last, 1061.0) == ["system.ping", "mail.sync", "classify.pending", "gcal.scan_pending",
                                            "followups.scan_pending"]  # every 60 s
    assert "mail.sync.full" in scheduler.tick(last, 4601.0)  # hourly full reconcile
