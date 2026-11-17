"""Command-line review of follow-ups and the daily briefing (until the dashboard exists in Phase 6).

    docker compose run --rm --no-deps web python -m mailassistant.followups.cli status|list|show|confirm|...
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from mailassistant.cliutil import parse_ids
from mailassistant.config import get_settings
from mailassistant.db.models import Email, Followup, FollowupEvent
from mailassistant.db.session import session_scope, utcnow
from mailassistant.followups import service
from mailassistant.followups.briefing import build_briefing, render_text


BULK = {"confirm": service.confirm, "reject": service.reject, "done": service.mark_done,
        "close": service.accept_closure, "keep": service.dismiss_closure}


def _fmt(f: Followup, tz: str) -> str:
    z = ZoneInfo(tz)
    who = "I owe" if f.direction == "I_OWE" else "waiting for"
    when = (f"deadline {f.deadline_at.astimezone(z):%a %d %b %H:%M}" if f.deadline_at else
            f"remind {f.remind_at.astimezone(z):%a %d %b}" if f.remind_at else "")
    flags = " CLOSURE?" if f.closure_email_id else ""
    return (f"#{f.id:<4} {f.status:<9} {who} {f.counterparty or '?'}: {f.action_text}  "
            f"[{when}, {f.remind_basis}] [{float(f.confidence or 0):.2f}]{flags}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="followups")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    lp = sub.add_parser("list")
    lp.add_argument("--status", default="active", help="active (proposed+open+waiting)|proposed|open|waiting|completed|cancelled|all")
    sub.add_parser("show").add_argument("id", type=int)
    for name in BULK:
        sub.add_parser(name).add_argument("ids", nargs="+", help="one or more ids: 89 90, 89,90 or 89, 90")
    sp = sub.add_parser("snooze")
    sp.add_argument("id", type=int)
    sp.add_argument("days", type=int, help="working days")
    dp = sub.add_parser("deadline")
    dp.add_argument("id", type=int)
    dp.add_argument("day", type=date.fromisoformat, help="YYYY-MM-DD")
    bp = sub.add_parser("briefing")
    bp.add_argument("--date", type=date.fromisoformat, default=None)
    bp.add_argument("--rebuild", action="store_true", help="rebuild from the current state")
    a = ap.parse_args(argv)
    if a.cmd in BULK:
        try:
            parse_ids(a.ids)
        except argparse.ArgumentTypeError as e:
            ap.error(str(e))
    s = get_settings()
    tz = s.timezone
    try:
        with session_scope() as session:
            if a.cmd == "status":
                for st, n in session.execute(select(Followup.status, func.count()).group_by(Followup.status)):
                    print(f"follow-ups {st}: {n}")
                print("closures proposed:", session.scalar(select(func.count()).select_from(Followup).where(
                    Followup.closure_email_id.is_not(None), Followup.status.in_(service.OPEN_STATUSES))))
                print("emails scanned:", session.scalar(select(func.count()).select_from(Email).where(
                    Email.followups_scanned_at.is_not(None))))
            elif a.cmd == "list":
                q = select(Followup).order_by(Followup.remind_at)
                if a.status == "active":
                    q = q.where(Followup.status.in_(service.OPEN_STATUSES))
                elif a.status != "all":
                    q = q.where(Followup.status == a.status)
                print("\n".join(_fmt(f, tz) for f in session.scalars(q)) or "(none)")
            elif a.cmd == "show":
                f = session.get(Followup, a.id)
                if f is None:
                    print("no such follow-up")
                    return 1
                e = session.get(Email, f.email_id)
                print(_fmt(f, tz))
                print(f"from mail: {e.direction} {e.from_addr} -> {', '.join(e.to_addrs or [])}  subject: {e.subject}")
                print(f"evidence: {f.evidence_quote}")
                if f.explanation:
                    print(f"model reason: {f.explanation}")
                print(f"people: {', '.join(f.people or [])}")
                for n in f.notes or []:
                    print("note:", n)
                if f.closure_email_id:
                    c = session.get(Email, f.closure_email_id)
                    print(f"PROPOSED CLOSURE by mail {c.id} ({c.direction} {c.from_addr}, {c.subject}): {f.closure_quote}")
                for ev in session.scalars(select(FollowupEvent).where(FollowupEvent.followup_id == f.id)
                                          .order_by(FollowupEvent.id)):
                    print(f"  {ev.created_at.astimezone(ZoneInfo(tz)):%d %b %H:%M} {ev.actor}: "
                          f"{ev.from_status or '-'} -> {ev.to_status}  {ev.note or ''}")
            elif a.cmd in BULK:
                failed = 0
                for fid in parse_ids(a.ids):
                    try:
                        with session.begin_nested():  # one bad id does not undo the others
                            f = BULK[a.cmd](session, fid)
                        print(_fmt(f, tz))
                    except service.FollowupError as e:
                        failed += 1
                        print(f"#{fid:<4} error: {e}")
                return 1 if failed else 0
            elif a.cmd == "snooze":
                print(_fmt(service.snooze(session, a.id, a.days, s), tz))
            elif a.cmd == "deadline":
                print(_fmt(service.set_deadline(session, a.id, a.day, s), tz))
            elif a.cmd == "briefing":
                day = a.date or utcnow().astimezone(ZoneInfo(tz)).date()
                print(render_text(build_briefing(session, day, tz, rebuild=a.rebuild or a.date is None).content))
    except service.FollowupError as e:
        print(f"error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
