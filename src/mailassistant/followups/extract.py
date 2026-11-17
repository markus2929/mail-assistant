"""Turn validated model output into follow-up drafts. No database, no network.

Principles (false reminders are the worst outcome):
- The model only copies text. Python checks that the quote and the deadline text literally occur in the
  sender's OWN text (quoted history removed), resolves dates, and decides who owes whom.
- Counterparty and people come from the mail headers, never from the model.
- Anything that does not verify is dropped; an unresolvable deadline becomes "no deadline" (estimated
  reminder) with a note - it is never guessed.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from mailassistant.gcal.datetext import resolve_date, resolve_time
from mailassistant.gcal.extract import _squash, quote_in_text

DEFAULT_DEADLINE_HOUR = 17


@dataclass
class FollowupDraft:
    direction: str                  # I_OWE | THEY_OWE
    action_text: str
    evidence_quote: str
    confidence: float
    dedupe_key: str
    deadline_at: datetime | None = None
    deadline_text: str | None = None
    notes: list[str] = field(default_factory=list)


def _key(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:40]


def action_tokens(s: str | None) -> set[str]:
    return {w for w in re.findall(r"[^\W\d_]{4,}", (s or "").lower())}


def similar_actions(a: str | None, b: str | None) -> bool:
    """Same commitment worded slightly differently ('send the quote' ~ 'send quote for the barn')."""
    ta, tb = action_tokens(a), action_tokens(b)
    return bool(ta and tb) and len(ta & tb) / min(len(ta), len(tb)) >= 0.6


def resolve_deadline(text: str, *, own: str, sent_at: datetime, tz: str) -> tuple[datetime | None, list[str]]:
    """Explicit deadline from copied text. (None, notes) when absent, not verbatim, a range or unresolvable."""
    t = _squash(text)
    if not t:
        return None, []
    if t not in _squash(own):
        return None, ["deadline_text_not_found"]
    z = ZoneInfo(tz)
    dr = resolve_date(t, sent_at.astimezone(z).date())
    if dr.value is None:
        return None, ["deadline_range" if "date_range" in dr.issues else "deadline_unresolved"]
    notes = [i for i in dr.issues if i != "year_inferred"]
    if "weekday_mismatch" in dr.issues:
        return None, ["deadline_weekday_mismatch"]
    if "year_inferred" in dr.issues and (dr.value - sent_at.astimezone(z).date()).days > 183:
        # "vóór 23 september" in an October mail is a past date, not one next year: don't guess
        return None, ["deadline_year_unclear"]
    tr = resolve_time(t)
    local = datetime.combine(dr.value, tr.start or time(DEFAULT_DEADLINE_HOUR), tzinfo=z)
    if tr.start is None:
        notes.append("deadline_time_default")
        if local.astimezone(UTC) < sent_at:  # "vandaag" in a mail sent after 17:00: end of that day
            local = datetime.combine(dr.value, time(23, 59), tzinfo=z)
    if local.astimezone(UTC) < sent_at:
        return None, ["deadline_before_mail"]
    return local.astimezone(UTC), notes


def drafts_from_extraction(data: dict, *, mail_direction: str, own: str, subject: str | None, message_id: str,
                           sent_at: datetime, tz: str, min_confidence: float = 0.6
                           ) -> tuple[list[FollowupDraft], list[str]]:
    """`data` is a validated FollowupExtraction as dict. Returns (drafts, rejection reasons)."""
    drafts: list[FollowupDraft] = []
    rejected: list[str] = []
    text = f"{subject or ''}\n{own}"
    for item in data.get("items") or []:
        quote = (item.get("evidence_quote") or "").strip()
        action = re.sub(r"\s+", " ", (item.get("action") or "")).strip()[:200]
        conf = min(float(item.get("confidence") or 0), 0.95)
        if not action or not quote:
            rejected.append("empty")
            continue
        if not quote_in_text(quote, text):
            rejected.append("quote_not_found")
            continue
        if conf < min_confidence:
            rejected.append("low_confidence")
            continue
        direction = "I_OWE" if item.get("who_owes") == "user" else "THEY_OWE"
        deadline, notes = resolve_deadline(item.get("deadline_text") or "", own=text, sent_at=sent_at, tz=tz)
        if any(d.direction == direction and similar_actions(d.action_text, action) for d in drafts):
            rejected.append("duplicate_in_mail")
            continue
        drafts.append(FollowupDraft(
            direction=direction, action_text=action, evidence_quote=quote[:600], confidence=round(conf, 2),
            dedupe_key=_key("fu", message_id, direction, _squash(action)), deadline_at=deadline,
            deadline_text=(item.get("deadline_text") or "").strip() or None,
            notes=[f"from_{mail_direction}going_mail", *notes]))
    return drafts, rejected


def closure_verified(data: dict, *, own: str, subject: str | None, min_confidence: float = 0.7) -> tuple[bool, str]:
    """Is a validated ClosureCheck strong enough to PROPOSE closing a follow-up?"""
    if data.get("fulfils") != "yes":
        return False, f"fulfils_{data.get('fulfils')}"
    if float(data.get("confidence") or 0) < min_confidence:
        return False, "low_confidence"
    if not quote_in_text(data.get("evidence_quote") or "", f"{subject or ''}\n{own}"):
        return False, "quote_not_found"
    return True, "ok"
