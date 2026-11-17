from collections.abc import Callable
from datetime import timedelta
from typing import Any

HANDLERS: dict[str, Callable[[dict[str, Any]], None]] = {}


class Deferred(Exception):
    """Raise from a handler for 'not now' conditions (e.g. LLM unavailable). Costs no attempt."""

    def __init__(self, reason: str, delay: timedelta = timedelta(minutes=5)):
        super().__init__(reason)
        self.reason, self.delay = reason, delay


def handler(job_type: str):
    def deco(fn):
        HANDLERS[job_type] = fn
        return fn
    return deco
