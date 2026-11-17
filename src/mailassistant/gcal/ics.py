"""iCalendar (RFC 5545) subset parser for invitations. Standard library only.

Handles line folding, quoted parameters, escaped text, all-day events, UTC / TZID / floating times,
Windows timezone names (Outlook), DURATION, METHOD and RRULE (kept as text). Problems are
reported in `issues` rather than guessed away.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mailassistant.mail.htmltext import _is_meeting_link

WINDOWS_TZ = {
    "W. Europe Standard Time": "Europe/Amsterdam", "Romance Standard Time": "Europe/Paris",
    "Central Europe Standard Time": "Europe/Budapest", "Central European Standard Time": "Europe/Warsaw",
    "GMT Standard Time": "Europe/London", "Greenwich Standard Time": "Atlantic/Reykjavik",
    "FLE Standard Time": "Europe/Helsinki", "E. Europe Standard Time": "Europe/Chisinau",
    "Russian Standard Time": "Europe/Moscow", "Turkey Standard Time": "Europe/Istanbul",
    "UTC": "UTC", "Eastern Standard Time": "America/New_York", "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver", "Pacific Standard Time": "America/Los_Angeles",
    "India Standard Time": "Asia/Kolkata", "China Standard Time": "Asia/Shanghai",
    "Tokyo Standard Time": "Asia/Tokyo", "AUS Eastern Standard Time": "Australia/Sydney",
    "South Africa Standard Time": "Africa/Johannesburg", "W. Central Africa Standard Time": "Africa/Lagos",
}
_URL_RE = re.compile(r"https?://[^\s<>\"')\\]+")


@dataclass
class IcsEvent:
    uid: str
    method: str | None
    summary: str | None
    start: datetime | date
    end: datetime | date
    all_day: bool
    tz_name: str | None          # IANA name used for the times (None for UTC/all-day)
    tz_original: str | None      # what the file said ("W. Europe Standard Time", "UTC", "floating"...)
    floating: bool = False
    location: str | None = None
    organizer: dict | None = None
    attendees: list[dict] = field(default_factory=list)
    description: str | None = None
    url: str | None = None
    meeting_url: str | None = None
    status: str | None = None
    sequence: int = 0
    rrule: list[str] = field(default_factory=list)
    recurrence_id: str | None = None
    issues: list[str] = field(default_factory=list)


def _unfold(text: str) -> list[str]:
    return re.sub(r"\r?\n[ \t]", "", text.replace("\r\n", "\n").replace("\r", "\n")).split("\n")


def _split(line: str) -> tuple[str, dict, str] | None:
    in_q, colon = False, -1
    for i, ch in enumerate(line):
        if ch == '"':
            in_q = not in_q
        elif ch == ":" and not in_q:
            colon = i
            break
    if colon < 0:
        return None
    head, value = line[:colon], line[colon + 1:]
    parts, buf, in_q = [], "", False
    for ch in head:
        if ch == '"':
            in_q = not in_q
        if ch == ";" and not in_q:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    parts.append(buf)
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def _unescape(v: str) -> str:
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m[1] in "nN" else m[1], v)


def _zone(tzid: str, default_tz: str, issues: list[str]) -> tuple[ZoneInfo, str]:
    name = WINDOWS_TZ.get(tzid.strip('"'), tzid.strip('"'))
    parts = name.strip("/").split("/")
    for cand in (name, "/".join(parts[-2:]), parts[-1]):
        try:
            return ZoneInfo(cand), cand
        except (ZoneInfoNotFoundError, ValueError, OSError):
            continue
    issues.append(f"unknown_tzid:{tzid}")
    return ZoneInfo(default_tz), default_tz


def _parse_dt(value: str, params: dict, default_tz: str, issues: list[str]):
    """Return (value, all_day, tz_name, tz_original, floating)."""
    v = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", v):
        return datetime.strptime(v[:8], "%Y%m%d").date(), True, None, "date", False
    m = re.fullmatch(r"(\d{8})T(\d{6})(Z?)", v)
    if not m:
        raise ValueError(f"bad date-time {value!r}")
    naive = datetime.strptime(m[1] + m[2], "%Y%m%d%H%M%S")
    if m[3]:
        return naive.replace(tzinfo=ZoneInfo("UTC")), False, "UTC", "UTC", False
    if "TZID" in params:
        zone, name = _zone(params["TZID"], default_tz, issues)
        return naive.replace(tzinfo=zone), False, name, params["TZID"], False
    issues.append("floating_time_assumed_default_tz")
    return naive.replace(tzinfo=ZoneInfo(default_tz)), False, default_tz, "floating", True


def _duration(v: str) -> timedelta | None:
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", v.strip())
    if not m:
        return None
    w, d, h, mi, s = (int(x or 0) for x in m.groups()[1:])
    td = timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)
    return -td if m[1] == "-" else td


def _person(value: str, params: dict) -> dict:
    return {"email": re.sub(r"(?i)^mailto:", "", value).strip().lower(), "name": params.get("CN"),
            "partstat": params.get("PARTSTAT"), "role": params.get("ROLE")}


def _meeting_url(*texts: str | None) -> str | None:
    for t in texts:
        for url in _URL_RE.findall(t or ""):
            if _is_meeting_link(url):
                return url.rstrip(".,;")
    return None


def parse_ics(text: str, default_tz: str = "Europe/Amsterdam") -> list[IcsEvent]:
    lines = _unfold(text)
    method, events, i = None, [], 0
    while i < len(lines):
        parsed = _split(lines[i])
        if parsed and parsed[0] == "METHOD":
            method = parsed[2].strip().upper()
        if lines[i].strip().upper() == "BEGIN:VEVENT":
            props: list[tuple[str, dict, str]] = []
            depth = 0
            i += 1
            while i < len(lines) and not (depth == 0 and lines[i].strip().upper() == "END:VEVENT"):
                up = lines[i].strip().upper()
                if up.startswith("BEGIN:"):
                    depth += 1
                elif up.startswith("END:"):
                    depth -= 1
                elif depth == 0 and (p := _split(lines[i])):
                    props.append(p)
                i += 1
            ev = _build(props, method, default_tz)
            if ev:
                events.append(ev)
        i += 1
    return events


def _build(props, method, default_tz) -> IcsEvent | None:
    first = {}
    multi: dict[str, list] = {}
    for name, params, value in props:
        first.setdefault(name, (params, value))
        multi.setdefault(name, []).append((params, value))
    if "DTSTART" not in first:
        return None
    issues: list[str] = []
    try:
        start, all_day, tz_name, tz_orig, floating = _parse_dt(first["DTSTART"][1], first["DTSTART"][0],
                                                               default_tz, issues)
        if "DTEND" in first:
            end = _parse_dt(first["DTEND"][1], first["DTEND"][0], default_tz, issues)[0]
        elif "DURATION" in first and (d := _duration(first["DURATION"][1])) is not None:
            end = start + d
        else:
            end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
            issues.append("end_missing_default_duration")
    except ValueError as e:
        return IcsEvent(uid=first.get("UID", ({}, ""))[1] or "?", method=method, summary=None,
                        start=date.today(), end=date.today(), all_day=True, tz_name=None, tz_original=None,
                        issues=[f"unparseable:{e}"])
    desc = _unescape(first["DESCRIPTION"][1]) if "DESCRIPTION" in first else None
    loc = _unescape(first["LOCATION"][1]) if "LOCATION" in first else None
    url = first["URL"][1].strip() if "URL" in first else None
    extra = [v for k in ("X-GOOGLE-CONFERENCE", "X-MICROSOFT-SKYPETEAMSMEETINGURL",
                         "X-MICROSOFT-ONLINEMEETINGEXTERNALLINK") for _, v in multi.get(k, [])]
    org = _person(first["ORGANIZER"][1], first["ORGANIZER"][0]) if "ORGANIZER" in first else None
    try:
        seq = int(first.get("SEQUENCE", ({}, "0"))[1])
    except ValueError:
        seq = 0
    return IcsEvent(
        uid=(first.get("UID", ({}, ""))[1] or "").strip(), method=method,
        summary=_unescape(first["SUMMARY"][1]).strip() if "SUMMARY" in first else None,
        start=start, end=end, all_day=all_day, tz_name=tz_name, tz_original=tz_orig, floating=floating,
        location=loc, organizer=org, attendees=[_person(v, p) for p, v in multi.get("ATTENDEE", [])],
        description=desc, url=url, meeting_url=_meeting_url(url, loc, desc, *extra),
        status=first["STATUS"][1].strip().upper() if "STATUS" in first else None, sequence=seq,
        rrule=[v for _, v in multi.get("RRULE", [])],
        recurrence_id=first["RECURRENCE-ID"][1].strip() if "RECURRENCE-ID" in first else None,
        issues=issues)
