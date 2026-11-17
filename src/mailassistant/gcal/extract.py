"""Turn calendar files and model extractions into event drafts. No database, no network.

Principles (to avoid false calendar events):
- ICS files are parsed deterministically; the model is not involved.
- For plain-text mail the model only COPIES text (date, time, quote). Python does all the date
  arithmetic, and a draft is rejected if the quoted evidence or the date/time text does not appear
  verbatim in the email, if no date can be resolved, or if no time is found (unless all-day).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from mailassistant.gcal.datetext import resolve_date, resolve_time
from mailassistant.gcal.ics import IcsEvent

IGNORED_METHODS = {"REPLY", "COUNTER", "DECLINECOUNTER", "REFRESH"}
_URL = re.compile(r"https?://[^\s<>\"')]+")
_TZ_TEXT = {"cet": None, "cest": None, "amsterdam": None, "nederlandse tijd": None, "dutch time": None,
            "utc": "UTC", "gmt": "UTC", "london": "Europe/London", "uk time": "Europe/London",
            "new york": "America/New_York", "est": "America/New_York", "edt": "America/New_York"}


@dataclass
class Draft:
    kind: str                       # invitation | proposed_date | mention
    title: str | None
    start_at: datetime              # timezone-aware, UTC
    end_at: datetime
    all_day: bool
    tz_name: str
    tz_original: str | None
    confidence: float
    dedupe_key: str
    location: str | None = None
    organiser: str | None = None
    attendees: list[dict] = field(default_factory=list)
    meeting_url: str | None = None
    evidence_quote: str | None = None
    ics_uid: str | None = None
    notes: list[str] = field(default_factory=list)
    cancel: bool = False


def _key(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:40]


def _local_midnight(d: date, tz: str) -> datetime:
    return datetime.combine(d, time.min, tzinfo=ZoneInfo(tz)).astimezone(UTC)


def draft_from_ics(ev: IcsEvent, *, subject: str | None, now: datetime, default_tz: str) -> tuple[Draft | None, str | None]:
    if ev.method in IGNORED_METHODS:
        return None, f"ignored_method:{ev.method}"
    if any(i.startswith("unparseable") for i in ev.issues):
        return None, "unparseable"
    cancel = ev.method == "CANCEL" or ev.status == "CANCELLED"
    if ev.all_day:
        start, end, tz = _local_midnight(ev.start, default_tz), _local_midnight(ev.end, default_tz), default_tz
    else:
        start, end, tz = ev.start.astimezone(UTC), ev.end.astimezone(UTC), ev.tz_name or default_tz
    if end <= start:
        end = start + timedelta(hours=1)
        ev.issues.append("end_not_after_start")
    if not cancel and end < now:
        return None, "in_the_past"
    conf = {"REQUEST": 0.95, "PUBLISH": 0.85, "ADD": 0.85, None: 0.8}.get(ev.method, 0.8)
    conf -= 0.1 * ev.floating + 0.15 * any(i.startswith("unknown_tzid") for i in ev.issues)
    notes = list(ev.issues) + [f"recurring: RRULE:{r} (only the first occurrence is proposed)" for r in ev.rrule]
    org = None
    if ev.organizer:
        org = f"{ev.organizer.get('name') or ''} <{ev.organizer['email']}>".strip()
    uid = ev.uid or _key("nouid", ev.summary or "", start.isoformat())
    return Draft(
        kind="invitation", title=ev.summary or subject, start_at=start, end_at=end, all_day=ev.all_day,
        tz_name=tz, tz_original=ev.tz_original, confidence=round(max(conf, 0.1), 2),
        dedupe_key=_key("ics", uid, ev.recurrence_id or "", start.isoformat()), location=ev.location,
        organiser=org, attendees=ev.attendees, meeting_url=ev.meeting_url, ics_uid=uid, notes=notes,
        cancel=cancel), None


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower().replace("’", "'")).strip()


def quote_in_text(quote: str, text: str) -> bool:
    segments = [x.strip() for x in re.split(r"\.\.\.|…", _squash(quote)) if len(x.strip()) >= 8]
    t = _squash(text)
    return bool(segments) and all(seg in t for seg in segments)


_W = r"(?:\w+ ){0,3}"  # up to three filler words ("schrijf je dan snel in")
_SIGNUP_REQUEST = re.compile(
    r"\b(?:schrijf|meld) (?:je|jij|jullie|u) " + _W + r"(?:in|aan)\b"
    r"|\bverzoek (?:tot|om) (?:in te schrijven|inschrijv\w*|aan te melden|aanmeld\w*)"
    r"|\b(?:inschrijven|aanmelden) (?:kan|is|via|graag|voor|vóór|uiterlijk|bij|op)\b"
    r"|\bgraag (?:inschrijven|aanmelden)\b|\bopen diensten\b"
    r"|\b(?:please )?(?:sign up|register|enrol+)\b(?! (?:was|is|has been) (?:successful|confirmed))"
    r"|\bregistration (?:is )?(?:required|open|now open|closes)\b")
_SIGNUP_CONFIRMED = re.compile(
    r"\b(?:je|jouw|uw|your) (?:aanmelding|inschrijving|registratie|reservering|registration|booking|reservation)\b"
    r"[^.\n]{0,80}?\b(?:is |has been |werd )?(?:geslaagd|gelukt|ontvangen|bevestigd|definitief|verwerkt|"
    r"successful|confirmed|received)\b"
    r"|\b(?:je|u) (?:bent|staat) (?:nu |hierbij )?(?:aangemeld|ingeschreven|geregistreerd|ingedeeld|ingeroosterd)\b"
    r"|\bbedankt voor (?:je|jouw|uw) (?:aanmelding|inschrijving|registratie|reservering)\b"
    r"|\b(?:you(?:'re| are)|you have been) (?:now )?(?:registered|signed up|enrolled|booked)\b"
    r"|\bthank(?:s| you) for (?:registering|signing up|your registration|your booking)\b")


def signup_request_unconfirmed(text: str) -> bool:
    """The mail asks the user to sign up / register, and does not confirm that they already did.

    Such a mail is not (yet) an appointment: the later confirmation mail is. Deterministic, so the
    model cannot talk its way past it.
    """
    t = _squash(text)
    return bool(_SIGNUP_REQUEST.search(t)) and not _SIGNUP_CONFIRMED.search(t)


def draft_from_extraction(data: dict, *, subject: str | None, body: str, sent_at: datetime, now: datetime,
                          default_tz: str, default_minutes: int, source_key: str,
                          min_confidence: float = 0.5, capture: bool = False,
                          intent_event: bool = False) -> tuple[Draft | None, str | None]:
    """`data` is the validated model output as a dict (see EventExtraction).

    capture=True: the text was written or forwarded by the user themself (dashboard, voice, +agenda mail):
    a bare 1-6 o'clock means afternoon (noted), and with intent_event=True ("zet in mijn agenda", +agenda)
    a sign-up request or a mere mention still becomes an appointment proposal (noted). All other checks stay."""
    # some models write "null"/"none" instead of an empty string: treat as empty
    data = {k: ("" if isinstance(v, str) and v.strip().lower() in ("null", "none", "n/a") else v) for k, v in data.items()}
    kind = data.get("kind")
    if capture and intent_event and kind == "mention":
        kind = "invitation"
        data = {**data, "_upgraded": True}
    if kind not in ("invitation", "proposed_date", "mention"):
        return None, "no_event"
    text = f"{subject or ''}\n{body}"
    if not quote_in_text(data.get("evidence_quote", ""), text):
        return None, "quote_not_found"
    sq = _squash(text)
    date_text = _squash(data.get("date_text", ""))
    if not date_text or date_text not in sq:
        return None, "date_text_not_found"
    ref = sent_at.astimezone(ZoneInfo(default_tz)).date()
    dr = resolve_date(date_text, ref)
    if dr.value is None:
        return None, "date_range" if "date_range" in dr.issues else "date_unresolved"
    notes, conf = list(dr.issues), min(float(data.get("confidence", 0)), 0.95)
    if data.get("_upgraded"):
        notes.append("added_by_you")
    if kind != "mention" and signup_request_unconfirmed(text):
        if capture and intent_event:
            notes.append("added_by_you")        # the user asked for it explicitly
        else:
            kind = "mention"  # shown for information, can never be created; the confirmation mail can
            notes.append("signup_request_not_confirmed")
    conf -= 0.05 * sum(i in ("year_inferred", "month_inferred", "ambiguous_next_weekday") for i in dr.issues)
    conf -= 0.35 * ("weekday_mismatch" in dr.issues)

    tz = default_tz
    tz_text = _squash(data.get("timezone_text", ""))
    if tz_text:
        if tz_text in _TZ_TEXT:
            tz = _TZ_TEXT[tz_text] or default_tz
        else:
            notes.append("unknown_timezone_text")
    start_txt = _squash(data.get("start_time_text", ""))
    all_day = bool(data.get("all_day"))
    if all_day:
        start_at, end_at = _local_midnight(dr.value, tz), _local_midnight(dr.value + timedelta(days=1), tz)
    else:
        if not start_txt or start_txt not in sq:
            return None, "time_text_not_found"
        tr = resolve_time(start_txt if not data.get("end_time_text") else f"{start_txt} - {data['end_time_text']}",
                          assume_afternoon=capture)
        if tr.start is None:
            return None, "time_unresolved"
        notes += [i for i in tr.issues if i == "assumed_afternoon"]
        local_start = datetime.combine(dr.value, tr.start, tzinfo=ZoneInfo(tz))
        end_t = tr.end
        if end_t is None and data.get("end_time_text"):
            end_t = resolve_time(_squash(data["end_time_text"]), assume_afternoon=capture).start
            if capture and end_t is not None and end_t <= tr.start and end_t.hour + 12 < 24:
                end_t = end_t.replace(hour=end_t.hour + 12)
        if end_t is not None:
            local_end = datetime.combine(dr.value, end_t, tzinfo=ZoneInfo(tz))
            if local_end <= local_start:
                local_end = local_start + timedelta(minutes=default_minutes)
                notes.append("end_before_start_ignored")
        elif data.get("duration_minutes"):
            local_end = local_start + timedelta(minutes=int(data["duration_minutes"]))
        else:
            local_end = local_start + timedelta(minutes=default_minutes)
            notes.append("default_duration")
        start_at, end_at = local_start.astimezone(UTC), local_end.astimezone(UTC)
    if end_at < now:
        return None, "in_the_past"
    if conf < min_confidence:
        return None, "low_confidence"
    loc = (data.get("location") or "").strip()
    if loc and _squash(loc) not in sq:
        loc, _ = "", notes.append("location_not_in_email")
    url = (data.get("meeting_url") or "").strip()
    if url and (url not in text or not _URL.fullmatch(url)):
        url, _ = "", notes.append("meeting_url_not_in_email")
    title = (data.get("title") or subject or "").strip()[:200] or None
    return Draft(
        kind=kind, title=title, start_at=start_at, end_at=end_at, all_day=all_day, tz_name=tz,
        tz_original=data.get("date_text") or None, confidence=round(max(conf, 0.0), 2),
        dedupe_key=_key("llm", source_key, start_at.isoformat(), _squash(title or "")), location=loc or None,
        organiser=(data.get("organiser") or "").strip() or None, meeting_url=url or None,
        evidence_quote=data["evidence_quote"].strip()[:500], notes=notes), None
