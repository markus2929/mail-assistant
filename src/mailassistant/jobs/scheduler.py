"""Enqueues periodic jobs. Dedupe keys make this safe to restart or run twice."""
import logging
import signal
import time

from mailassistant.db import queue
from mailassistant.db.session import session_scope

log = logging.getLogger("scheduler")
_stop = False

# (job type, interval seconds, payload, dedupe key).
PERIODIC = [
    ("system.ping", 60, {}, "system.ping"),
    ("maintenance.purge_llm_calls", 3600, {}, "maintenance.purge_llm_calls"),
    ("mail.sync", 60, {}, "mail.sync"),
    ("mail.sync", 3600, {"full": True}, "mail.sync.full"),
    ("classify.pending", 60, {}, "classify.pending"),
    ("gcal.sync", 900, {}, "gcal.sync"),
    ("gcal.scan_pending", 60, {}, "gcal.scan_pending"),
    ("followups.scan_pending", 60, {}, "followups.scan_pending"),
    ("followups.briefing", 3600, {}, "followups.briefing"),
    ("notify.briefing", 900, {}, "notify.briefing"),
    ("notify.due", 900, {}, "notify.due"),
]


def tick(last_run: dict[str, float], now: float) -> list[str]:
    fired = []
    for jtype, interval, payload, key in PERIODIC:
        if key not in last_run or now - last_run[key] >= interval:
            with session_scope() as s:
                queue.enqueue(s, jtype, payload, dedupe_key=key)
            last_run[key] = now
            fired.append(key)
    return fired


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: globals().__setitem__("_stop", True))
    last: dict[str, float] = {}  # empty -> every periodic job fires once at startup
    while not _stop:
        tick(last, time.monotonic())
        time.sleep(5)


if __name__ == "__main__":
    main()
