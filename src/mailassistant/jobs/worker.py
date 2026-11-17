import logging
import os
import signal
import socket
import time

from mailassistant.db import queue
from mailassistant.db.session import session_scope
from mailassistant.jobs import handlers  # noqa: F401  (registers handlers)
from mailassistant.classify import handlers as _classify_handlers  # noqa: F401
from mailassistant.capture import handlers as _capture_handlers  # noqa: F401
from mailassistant.followups import handlers as _followup_handlers  # noqa: F401
from mailassistant.gcal import handlers as _gcal_handlers  # noqa: F401
from mailassistant.mail import handlers as _mail_handlers  # noqa: F401
from mailassistant.notify import handlers as _notify_handlers  # noqa: F401
from mailassistant.jobs.registry import HANDLERS, Deferred

log = logging.getLogger("worker")
_stop = False


def run_once(worker_id: str) -> bool:
    """Claim and run one job. Returns False if nothing was ready."""
    with session_scope() as s:
        queue.reclaim_stale(s)
        job = queue.claim(s, worker_id, types=list(HANDLERS))
        if job is None:
            return False
        job_id, jtype, payload = job.id, job.type, dict(job.payload)
    try:
        HANDLERS[jtype](payload)
    except Deferred as d:
        log.warning("job %s deferred: %s", job_id, d.reason)
        with session_scope() as s:
            queue.defer(s, job_id, d.reason, d.delay)
    except Exception as e:  # noqa: BLE001 - any handler failure is retried with backoff
        log.exception("job %s (%s) failed", job_id, jtype)
        with session_scope() as s:
            queue.fail(s, job_id, repr(e)[:2000])
    else:
        with session_scope() as s:
            queue.complete(s, job_id)
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: globals().__setitem__("_stop", True))
    log.info("worker %s started; handlers=%s", worker_id, sorted(HANDLERS))
    while not _stop:
        if not run_once(worker_id):
            time.sleep(2)


if __name__ == "__main__":
    main()
