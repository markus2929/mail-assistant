"""Calendar conflict and duplicate detection. Pure functions, standard library only."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_ONLINE = re.compile(r"\b(teams|zoom|meet\.google|google meet|webex|skype|online|virtueel|virtual|video|"
                     r"call|telefon|phone|https?://)", re.I)


@dataclass(frozen=True)
class CalEvent:
    id: str
    calendar_id: str
    summary: str | None
    start: datetime
    end: datetime
    all_day: bool = False
    location: str | None = None
    status: str | None = "confirmed"
    transparent: bool = False        # "free" events never block
    declined: bool = False           # the user declined it
    ical_uid: str | None = None
    private: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Candidate:
    start: datetime
    end: datetime
    all_day: bool = False
    location: str | None = None
    title: str | None = None
    ics_uid: str | None = None
    key: str | None = None           # our dedupe key


def is_physical_location(loc: str | None) -> bool:
    return bool(loc and loc.strip() and not _ONLINE.search(loc))


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip(" ,.")


def _blocking(ev: CalEvent) -> bool:
    return (ev.status or "confirmed") != "cancelled" and not ev.transparent and not ev.declined


def find_conflicts(cand: Candidate, events: list[CalEvent], buffer_minutes: int = 30) -> list[dict]:
    """Hard 'overlap' conflicts and soft 'tight' ones (different physical places, too little travel time)."""
    out: list[dict] = []
    buf = timedelta(minutes=buffer_minutes)
    for ev in sorted(events, key=lambda e: e.start):
        if not _blocking(ev):
            continue
        info = {"event_id": ev.id, "calendar_id": ev.calendar_id, "summary": ev.summary,
                "start": ev.start.isoformat(), "end": ev.end.isoformat(), "location": ev.location}
        if cand.all_day:  # an all-day proposal only reports what else happens that day
            if ev.start < cand.end and cand.start < ev.end and len(out) < 5:
                out.append({**info, "type": "same_day"})
            continue
        if ev.start < cand.end and cand.start < ev.end:
            out.append({**info, "type": "overlap"})
            continue
        gap = min(abs(cand.start - ev.end), abs(ev.start - cand.end))
        if (gap < buf and is_physical_location(cand.location) and is_physical_location(ev.location)
                and _norm(cand.location) != _norm(ev.location)):
            out.append({**info, "type": "tight", "gap_minutes": int(gap.total_seconds() // 60)})
    return out


def hard_conflicts(conflicts: list[dict]) -> list[dict]:
    return [c for c in conflicts if c["type"] == "overlap"]


def _tokens(s: str | None) -> set[str]:
    return set(re.findall(r"\w{3,}", (s or "").lower()))


def find_duplicates(cand: Candidate, events: list[CalEvent]) -> list[CalEvent]:
    """Events that are very likely the same appointment already in the calendar."""
    dupes = []
    for ev in events:
        if (ev.status or "") == "cancelled":
            continue
        if (cand.key and ev.private.get("mail_assistant_key") == cand.key) or (
                cand.ics_uid and ev.ical_uid and ev.ical_uid.split("@")[0] == cand.ics_uid.split("@")[0]
                and abs(ev.start - cand.start) <= timedelta(minutes=5)):
            dupes.append(ev)
            continue
        close = abs(ev.start - cand.start) <= timedelta(minutes=5)
        a, b = _tokens(cand.title), _tokens(ev.summary)
        similar = bool(a and b and len(a & b) / len(a | b) >= 0.6)
        if close and similar:
            dupes.append(ev)
    return dupes


# Words that say nothing about WHICH appointment it is (dates, mail jargon, filler).
_TITLE_NOISE = {
    "januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september", "oktober", "november",
    "december", "january", "february", "march", "may", "june", "july", "august", "october", "jan", "feb", "mrt",
    "apr", "jun", "jul", "aug", "sep", "okt", "oct", "nov", "dec", "maandag", "dinsdag", "woensdag", "donderdag",
    "vrijdag", "zaterdag", "zondag", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "van", "voor", "het", "een", "der", "den", "met", "aan", "bij", "over", "naar", "uur", "the", "and", "for",
    "with", "your", "jouw", "uw", "you", "uitnodiging", "invitation", "herinnering", "reminder", "bevestigd",
    "bevestiging", "confirmed", "confirmation", "aanmelding", "aanmelden", "inschrijving", "registration",
}


def title_tokens(s: str | None) -> set[str]:
    return {w for w in re.findall(r"[^\W\d_]{3,}", (s or "").lower()) if w not in _TITLE_NOISE}


def similar_titles(a: str | None, b: str | None) -> bool:
    """Most words of the shorter title occur in the longer one ('Clubavond' ~ 'Tasting clubavond')."""
    ta, tb = title_tokens(a), title_tokens(b)
    return bool(ta and tb) and len(ta & tb) / min(len(ta), len(tb)) >= 0.6


def same_appointment(a: Candidate, b: Candidate, tz: str) -> bool:
    """Two proposals (usually from different emails) that describe the same appointment.

    Timed vs timed: start within 5 minutes. If either is all-day: the same local day. Plus similar titles.
    Different times on the same day stay two proposals: better a duplicate to reject than a lost event.
    """
    if a.all_day or b.all_day:
        z = ZoneInfo(tz)
        close = a.start.astimezone(z).date() == b.start.astimezone(z).date()
    else:
        close = abs(a.start - b.start) <= timedelta(minutes=5)
    return close and similar_titles(a.title, b.title)
