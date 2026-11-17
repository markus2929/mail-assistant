"""When a follow-up reminder falls due. Working days are Monday-Friday (no holiday calendar).
Standard library only; all arithmetic in the user's time zone, results in UTC.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def add_working_days(d: date, n: int) -> date:
    step = 1 if n >= 0 else -1
    left = abs(n)
    while left:
        d += timedelta(days=step)
        if d.weekday() < 5:
            left -= 1
    return d


def next_working_day(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _at(d: date, hour: int, tz: str) -> datetime:
    return datetime.combine(d, time(hour), tzinfo=ZoneInfo(tz)).astimezone(UTC)


def remind_at(*, direction: str, sent_at: datetime, deadline_at: datetime | None, tz: str,
              estimate_days_i_owe: int = 2, estimate_days_they_owe: int = 5, hour: int = 8) -> tuple[datetime, str]:
    """(remind_at, basis). Explicit deadline: one working day before it (never before the mail was sent,
    never after the deadline). Otherwise an estimate counted in working days from the mail."""
    z = ZoneInfo(tz)
    sent_day = sent_at.astimezone(z).date()
    if deadline_at is not None:
        due_day = deadline_at.astimezone(z).date()
        day = max(add_working_days(due_day, -1), sent_day)
        when = _at(day, hour, tz)
        if when > deadline_at:  # early-morning deadline: remind at the deadline itself
            when = deadline_at
        return when, "explicit"
    n = estimate_days_i_owe if direction == "I_OWE" else estimate_days_they_owe
    return _at(add_working_days(sent_day, n), hour, tz), "estimated"
