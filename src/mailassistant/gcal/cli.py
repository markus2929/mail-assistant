"""Command-line review of event proposals (until the dashboard exists in Phase 6).

    docker compose run --rm --no-deps worker python -m mailassistant.gcal.cli status|sync|list|show|approve|reject
"""
from __future__ import annotations

import argparse
import sys
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from mailassistant.cliutil import parse_ids
from mailassistant.config import get_settings
from mailassistant.db.models import CalendarCache, Email, EventProposal
from mailassistant.db.session import session_scope
from mailassistant.gcal.google import GoogleError
from mailassistant.gcal.review import ReviewError, reject_proposal
from mailassistant.gcal.service import ConflictsFound, approve_and_create, sync_calendar
from mailassistant.gcal.tokens import calendar_client, load_refresh_token


def _fmt(p: EventProposal, tz: str) -> str:
    z = ZoneInfo(p.tz_name or tz)
    when = p.start_at.astimezone(z).strftime("%a %d %b %Y") if p.all_day else \
        f"{p.start_at.astimezone(z):%a %d %b %Y %H:%M}-{p.end_at.astimezone(z):%H:%M}"
    flags = []
    if any(c["type"] == "overlap" for c in p.conflicts or []):
        flags.append("CONFLICT")
    if any(c["type"] == "tight" for c in p.conflicts or []):
        flags.append("tight-travel")
    cal = f" -> {p.target_calendar}" if p.target_calendar else ""
    return (f"#{p.id:<4} {p.status:<10} {p.kind:<13} {when}  {p.title or '(no title)'}  "
            f"[{float(p.confidence or 0):.2f}]{cal} {' '.join(flags)}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gcal")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("sync")
    lp = sub.add_parser("list")
    lp.add_argument("--status", default="proposed", help="proposed|approved|created|rejected|superseded|failed|all")
    sub.add_parser("show").add_argument("id", type=int)
    ap_ = sub.add_parser("approve")
    ap_.add_argument("id", type=int)
    ap_.add_argument("--accept-conflicts", action="store_true")
    ap_.add_argument("--calendar", choices=["work", "private"], help="default: the suggestion shown by `show`")
    sub.add_parser("calendars", help="list your Google calendars (name, ID, access) and what .env resolves to")
    sub.add_parser("reject").add_argument("ids", nargs="+", help="one or more ids: 2 3, 2,3 or 2, 3")
    a = ap.parse_args(argv)
    if a.cmd == "reject":
        try:
            parse_ids(a.ids)
        except argparse.ArgumentTypeError as e:
            ap.error(str(e))
    s = get_settings()
    try:
        with session_scope() as session:
            if a.cmd == "status":
                print("authorised:", "yes" if load_refresh_token(session, s) else "NO (run gcal.authorize)")
                print("cached events:", session.scalar(select(func.count()).select_from(CalendarCache)))
                for st, n in session.execute(select(EventProposal.status, func.count()).group_by(EventProposal.status)):
                    print(f"proposals {st}: {n}")
            elif a.cmd == "sync":
                print("events in window:", sync_calendar(session, calendar_client(session, s), s))
            elif a.cmd == "list":
                q = select(EventProposal).order_by(EventProposal.start_at)
                if a.status != "all":
                    q = q.where(EventProposal.status == a.status)
                rows = session.scalars(q).all()
                print("\n".join(_fmt(p, s.timezone) for p in rows) or "(none)")
            elif a.cmd == "show":
                p = session.get(EventProposal, a.id)
                e = session.get(Email, p.email_id) if p else None
                if not p:
                    print("no such proposal")
                    return 1
                print(_fmt(p, s.timezone))
                print(f"from: {e.from_addr}  subject: {e.subject}")
                for k in ("location", "organiser", "meeting_url", "evidence_quote"):
                    if getattr(p, k):
                        print(f"{k}: {getattr(p, k)}")
                for n in p.notes or []:
                    print("note:", n)
                for c in p.conflicts or []:
                    print("calendar:", c["type"], c.get("summary"), c.get("start"))
            elif a.cmd == "calendars":
                from mailassistant.gcal.calendars import display_name
                from mailassistant.gcal.google import GoogleScopeMissing
                from mailassistant.gcal.service import resolve_calendars
                client = calendar_client(session, s)
                res = resolve_calendars(session, client, s)
                try:
                    entries = client.list_calendars()
                except GoogleScopeMissing:
                    entries = []
                    print("Cannot list calendars: run `python -m mailassistant.gcal.authorize` again to grant the "
                          "calendar-list permission.\n")
                roles = {r["id"]: role for role in ("work", "private") if (r := res.get(role)) and r.get("id")}
                for c in sorted(entries, key=lambda c: (not c.get("primary"), display_name(c).lower())):
                    mark = roles.get(c["id"], "read for conflicts" if c["id"] in res["read_ids"] else "")
                    print(f"{display_name(c)[:34]:<34} {c.get('accessRole', '?'):<15} {mark:<18} {c['id']}")
                for role in ("work", "private"):
                    r = res.get(role)
                    if r and r.get("error"):
                        print(f"\n{role}: {r['value']!r} -> {r['error']}")
            elif a.cmd == "approve":
                p = session.get(EventProposal, a.id)
                if p is not None and a.calendar:
                    p.target_calendar = a.calendar
                p = approve_and_create(session, a.id, calendar_client(session, s), s, accept_conflicts=a.accept_conflicts)
                print(f"created in Google Calendar ({p.target_calendar or 'work'}): {p.google_event_id}")
            elif a.cmd == "reject":
                failed = 0
                for pid in parse_ids(a.ids):
                    try:
                        with session.begin_nested():  # one bad id does not undo the others
                            reject_proposal(session, pid)
                        print(f"#{pid:<4} rejected")
                    except ReviewError as e:
                        failed += 1
                        print(f"#{pid:<4} error: {e}")
                return 1 if failed else 0
    except ConflictsFound as e:
        for c in e.conflicts:
            print(f"  conflict: {c['type']}: {c.get('summary')} at {c.get('start')}")
        print("Nothing was created. Re-run with --accept-conflicts to create it anyway.")
        return 2
    except (ReviewError, GoogleError) as e:
        print(f"error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
