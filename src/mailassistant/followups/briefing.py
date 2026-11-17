"""Daily briefing: a deterministic list built from the database. No model, so nothing can be invented."""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from mailassistant.db.models import Briefing, CalendarCache, EventProposal, Followup
from mailassistant.db.session import utcnow

LIST_LIMIT = 25


def _iso(d: datetime | None) -> str | None:
    return d.astimezone(UTC).isoformat() if d else None


def _fu(f: Followup) -> dict:
    return {"id": f.id, "direction": f.direction, "status": f.status, "counterparty": f.counterparty,
            "action": f.action_text, "deadline_at": _iso(f.deadline_at), "remind_at": _iso(f.remind_at),
            "remind_basis": f.remind_basis, "closure_email_id": f.closure_email_id}


def build_briefing(session: Session, day: date, tz: str, *, rebuild: bool = False,
                   now: datetime | None = None) -> Briefing:
    """Build (or return the existing) briefing for `day`. Upserts the `briefings` row."""
    existing = session.scalar(select(Briefing).where(Briefing.briefing_date == day))
    if existing is not None and not rebuild:
        return existing
    now = now or utcnow()
    z = ZoneInfo(tz)
    start = datetime.combine(day, time.min, tzinfo=z).astimezone(UTC)
    end = start + timedelta(days=1)

    cal = session.scalars(select(CalendarCache).where(
        CalendarCache.start_at < end, CalendarCache.end_at > start,
        CalendarCache.status != "cancelled").order_by(CalendarCache.start_at)).all()
    due = session.scalars(select(Followup).where(
        Followup.status.in_(("open", "waiting")), Followup.remind_at < end).order_by(Followup.remind_at)).all()
    overdue = [f for f in due if (f.deadline_at and f.deadline_at < start) or (f.remind_at and f.remind_at < start)]
    today = [f for f in due if f not in overdue]
    proposals = session.scalars(select(Followup).where(Followup.status == "proposed")
                                .order_by(Followup.created_at.desc()).limit(LIST_LIMIT)).all()
    closures = session.scalars(select(Followup).where(
        Followup.status.in_(("proposed", "open", "waiting")), Followup.closure_email_id.is_not(None))
        .order_by(Followup.closure_proposed_at)).all()
    events = session.scalars(select(EventProposal).where(
        EventProposal.status == "proposed", EventProposal.kind != "mention", EventProposal.end_at > now)
        .order_by(EventProposal.start_at).limit(LIST_LIMIT)).all()

    content = {
        "date": day.isoformat(), "timezone": tz, "generated_at": now.isoformat(),
        "calendar": [{"summary": c.summary, "start": _iso(c.start_at), "end": _iso(c.end_at), "all_day": c.all_day,
                      "location": c.location, "tentative": c.status == "tentative"} for c in cal],
        "overdue": [_fu(f) for f in overdue[:LIST_LIMIT]],
        "due_today": [_fu(f) for f in today[:LIST_LIMIT]],
        "followup_proposals": [_fu(f) for f in proposals],
        "closure_proposals": [_fu(f) for f in closures[:LIST_LIMIT]],
        "event_proposals": [{"id": p.id, "kind": p.kind, "title": p.title, "start": _iso(p.start_at),
                             "all_day": p.all_day, "conflict": any(c.get("type") == "overlap" for c in p.conflicts or [])}
                            for p in events],
        "counts": {"overdue": len(overdue), "due_today": len(today), "closure_proposals": len(closures)},
    }
    if existing is None:
        existing = Briefing(briefing_date=day, content=content)
        session.add(existing)
    else:
        existing.content = content
    session.flush()
    return existing


def render_text(content: dict) -> str:
    """Plain-text rendering for the CLI (the dashboard renders the same JSON in Phase 6)."""
    z = ZoneInfo(content.get("timezone") or "Europe/Amsterdam")

    def t(iso: str | None, fmt: str = "%a %d %b %H:%M") -> str:
        return datetime.fromisoformat(iso).astimezone(z).strftime(fmt) if iso else "-"

    def who(f: dict) -> str:
        arrow = "I owe" if f["direction"] == "I_OWE" else "waiting for"
        return f"#{f['id']:<4} {arrow} {f['counterparty'] or '?'}: {f['action']}"

    out = [f"Briefing {content['date']}", ""]
    out.append("Today in the calendar:")
    out += [f"  {'all day' if c['all_day'] else t(c['start'], '%H:%M') + '-' + t(c['end'], '%H:%M')}  {c['summary'] or '(no title)'}"
            + (f"  @ {c['location']}" if c.get("location") else "") + ("  (tentative)" if c.get("tentative") else "")
            for c in content["calendar"]] or ["  (nothing)"]
    for key, title in (("overdue", "Overdue"), ("due_today", "Due today")):
        out += ["", f"{title}:"]
        out += [f"  {who(f)}  (deadline {t(f['deadline_at'])})" if f["deadline_at"] else
                f"  {who(f)}  (reminder, {f['remind_basis']})" for f in content[key]] or ["  (none)"]
    out += ["", "Proposed closures (confirm with `close ID`, or `keep ID`):"]
    out += [f"  {who(f)}" for f in content["closure_proposals"]] or ["  (none)"]
    out += ["", "New follow-up proposals (confirm with `confirm ID`, or `reject ID`):"]
    out += [f"  {who(f)}" + (f"  (deadline {t(f['deadline_at'])})" if f["deadline_at"] else "")
            for f in content["followup_proposals"]] or ["  (none)"]
    out += ["", "Calendar proposals waiting for you (gcal list):"]
    out += [f"  #{p['id']:<4} {t(p['start'], '%a %d %b' if p['all_day'] else '%a %d %b %H:%M')}  {p['title'] or '(no title)'}"
            + ("  CONFLICT" if p["conflict"] else "") for p in content["event_proposals"]] or ["  (none)"]
    return "\n".join(out)
