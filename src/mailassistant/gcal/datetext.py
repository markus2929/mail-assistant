"""Deterministic date/time resolution for Dutch and English expressions.

The LLM only copies date/time text out of an email; every calculation happens here, in plain
Python, relative to the date the email was sent. Anything ambiguous is reported in `issues` or
returned as None - the caller then refuses to guess.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, time, timedelta

MONTHS = {
    "januari": 1, "jan": 1, "january": 1, "februari": 2, "feb": 2, "february": 2, "maart": 3, "mrt": 3,
    "march": 3, "mar": 3, "april": 4, "apr": 4, "mei": 5, "may": 5, "juni": 6, "jun": 6, "june": 6,
    "juli": 7, "jul": 7, "july": 7, "augustus": 8, "aug": 8, "august": 8, "september": 9, "sep": 9,
    "sept": 9, "oktober": 10, "okt": 10, "oct": 10, "october": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
WEEKDAYS = {
    "maandag": 0, "dinsdag": 1, "woensdag": 2, "donderdag": 3, "vrijdag": 4, "zaterdag": 5, "zondag": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6,
}
_MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))
_WD_RE = "|".join(sorted(WEEKDAYS, key=len, reverse=True))


@dataclass
class DateResult:
    value: date | None
    issues: list[str] = field(default_factory=list)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _make(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _infer_year(m: int, d: int, ref: date, issues: list[str]) -> date | None:
    cand = _make(ref.year, m, d)
    if cand is None:
        return None
    if cand < ref - timedelta(days=2):
        cand = _make(ref.year + 1, m, d)
    issues.append("year_inferred")
    return cand


def _year(y: str) -> int:
    n = int(y)
    return 2000 + n if n < 100 else n


_RANGE_SEP = r"(?:t/m|tm|tot en met|tot|to|till|until|through|thru|-|–|—|en|and|&|of|or)"
_DAY = r"\d{1,2}(?:st|nd|rd|th|e|ste|de)?"
_RANGES = (
    re.compile(rf"\b{_DAY}\s*{_RANGE_SEP}\s*{_DAY}\s*(?:of\s+)?(?:{_MONTH_RE})\b"),          # 24 t/m 26 november
    re.compile(rf"\b(?:{_MONTH_RE})\b\.?(?:,?\s*\d{{4}})?\s*{_RANGE_SEP}\s*{_DAY}\s*(?:of\s+)?(?:{_MONTH_RE})\b"),
    re.compile(rf"(?<!\d )(?<!\d)\b(?:{_MONTH_RE})\b\.?\s*{_DAY}\s*{_RANGE_SEP}\s*{_DAY}\b"   # november 24-26
               r"(?![:.]\d|\s*(?:uur|u|h)\b)"),
)


def is_date_range(text: str) -> bool:
    """'24 t/m 26 november', '24 november - 2 december', 'november 24-26', '3 of 4 december': several days."""
    t = _norm(text)
    return any(r.search(t) for r in _RANGES)


def resolve_date(text: str, ref: date) -> DateResult:
    """Resolve a date expression relative to `ref` (the day the email was sent).

    A range or a choice of dates is not one date: it returns None with issue 'date_range' (never a guess).
    """
    t = _norm(text)
    issues: list[str] = []
    result: date | None = None

    if is_date_range(t):
        return DateResult(None, ["date_range"])
    if m :=re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t):
        result = _make(int(m[1]), int(m[2]), int(m[3]))
    elif m := re.search(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})\b", t):          # day-first (NL)
        result = _make(_year(m[3]), int(m[2]), int(m[1]))
    elif m := re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th|e|ste|de)?\s*(?:of\s+)?({_MONTH_RE})\b\.?(?:,?\s*(\d{{4}}))?", t):
        mon = MONTHS[m[2]]
        result = _make(int(m[3]), mon, int(m[1])) if m[3] else _infer_year(mon, int(m[1]), ref, issues)
    elif m := re.search(rf"\b({_MONTH_RE})\b\.?\s*(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s*(\d{{4}}))?\b", t):
        mon = MONTHS[m[1]]
        result = _make(int(m[3]), mon, int(m[2])) if m[3] else _infer_year(mon, int(m[2]), ref, issues)
    elif m := re.search(r"\b(\d{1,2})[-/](\d{1,2})(?![\d:])", t):
        result = _infer_year(int(m[2]), int(m[1]), ref, issues)

    wd = re.search(rf"\b({_WD_RE})\b", t)
    if result is None and wd and (m := re.search(
            rf"\b(?:{_WD_RE})\b\.?,?\s+(?:de\s+)?(\d{{1,2}})(?:e|ste|de|st|nd|rd|th)?\b(?![:.]\d|\s*(?:uur|u|h)\b)", t)):
        # "dinsdag 25" without a month: the 25th of this month (or the next one if it has passed) - never
        # silently dropped; the weekday check below reports a contradiction
        n = int(m[1])
        cand = _make(ref.year, ref.month, n)
        if cand is None or cand < ref - timedelta(days=2):
            ny, nm = (ref.year + 1, 1) if ref.month == 12 else (ref.year, ref.month + 1)
            cand = _make(ny, nm, n)
        if cand is not None:
            result = cand
            issues.append("month_inferred")
    if result is not None:
        if wd and WEEKDAYS[wd[1]] != result.weekday():
            issues.append("weekday_mismatch")
        return DateResult(result, issues)

    if re.search(r"\b(overmorgen|day after tomorrow)\b", t):
        return DateResult(ref + timedelta(days=2), issues)
    if re.search(r"\b(morgen|tomorrow)\b", t):
        return DateResult(ref + timedelta(days=1), issues)
    if re.search(r"\b(vandaag|today)\b", t):
        return DateResult(ref, issues)
    if wd:
        target = WEEKDAYS[wd[1]]
        if re.search(r"\b(volgende week|next week|komende week)\b", t):
            monday_next = ref - timedelta(days=ref.weekday()) + timedelta(days=7)
            return DateResult(monday_next + timedelta(days=target), issues)
        if re.search(r"\bnext\b", t):
            issues.append("ambiguous_next_weekday")
        delta = (target - ref.weekday()) % 7 or 7  # strictly after the day the mail was sent
        return DateResult(ref + timedelta(days=delta), issues)
    return DateResult(None, ["no_date_found"])


@dataclass
class TimeResult:
    start: time | None
    end: time | None = None
    issues: list[str] = field(default_factory=list)


_PM = r"(?:'s\s*)?(middags|avonds|namiddags)\b|\bpm\b|\bp\.m\."


def _hm(h: str, mi: str | None) -> time | None:
    hh, mm = int(h), int(mi or 0)
    return time(hh, mm) if 0 <= hh <= 23 and 0 <= mm <= 59 else None


_MORNING_NIGHT = r"ochtend|'s ?morgens|\bmorgens\b|nacht|\bam\b|a\.m\.|\bvroeg\b|morning|night"


def _afternoon(r: "TimeResult", t: str) -> "TimeResult":
    """Captures only: spoken/typed "om 3 uur" means 15:00. 1-6 o'clock without a morning/night word -> +12 h."""
    if r.start is None or re.search(_MORNING_NIGHT, t) or re.search(_PM, t):
        return r
    changed = False
    if 1 <= r.start.hour <= 6:
        r.start, changed = r.start.replace(hour=r.start.hour + 12), True
    if r.end is not None and 1 <= r.end.hour <= 6 and (changed or r.end.hour < (r.start.hour % 12 or 12)):
        r.end, changed = r.end.replace(hour=r.end.hour + 12), True
    if changed:
        r.issues.append("assumed_afternoon")
    return r


def resolve_time(text: str, *, assume_afternoon: bool = False) -> TimeResult:
    """Parse a start (and optional end) time. Bare numbers are not accepted without ':' or 'uur'/'u'.
    assume_afternoon: for the user's own captures only (see _afternoon)."""
    if assume_afternoon:
        return _afternoon(resolve_time(text), _norm(text))
    t = _norm(text)
    if not t:
        return TimeResult(None, None, ["no_time_found"])
    if re.search(r"\bhalf\s+(een|twee|drie|vier|vijf|zes|zeven|acht|negen|tien|elf|twaalf)\b", t):
        return TimeResult(None, None, ["half_hour_ambiguous"])  # Dutch "half drie" = 2:30: do not guess
    rng = re.search(r"\b(\d{1,2})(?:[:.u](\d{2}))?\s*(?:uur|u|h)?\s*(?:-|–|—|tot|to|till|until)\s*"
                    r"(\d{1,2})(?:[:.u](\d{2}))?\s*(?:uur|u|h)?\b", t)
    ampm = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)", t)
    plain = re.search(r"\b([01]?\d|2[0-3])[:.u](\d{2})\b|\b([01]?\d|2[0-3])\s*(?:uur|u)\b", t)
    issues: list[str] = []
    if ampm:
        h = int(ampm[1]) % 12 + (12 if ampm[3].startswith("p") else 0)
        return TimeResult(_hm(str(h), ampm[2]), None, issues)
    if plain or rng:
        if rng and (":" in t or "." in t or "uur" in t or re.search(r"\d u\b|\du\b", t)):
            a, b = _hm(rng[1], rng[2]), _hm(rng[3], rng[4])
            if a and b:
                return TimeResult(a, b, issues)
        if plain:
            h, mi = (plain[1], plain[2]) if plain[1] is not None else (plain[3], None)
            start = _hm(h, mi)
            if start and start.hour < 12 and re.search(_PM, t) and not re.search(r"ochtends", t):
                start = start.replace(hour=start.hour + 12)
            return TimeResult(start, None, issues)
    return TimeResult(None, None, ["no_time_found"])
