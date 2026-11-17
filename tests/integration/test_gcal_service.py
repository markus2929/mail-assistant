"""Calendar service against real PostgreSQL, a fake model and a stateful fake Google Calendar."""
import json
import re
import threading
import urllib.parse
from datetime import UTC, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mailassistant.config import Settings
from mailassistant.db.models import (Attachment, AuditLog, CalendarCache, Classification, Email, EventProposal,
                                     MailAccount)
from mailassistant.db.session import utcnow
from mailassistant.gcal.google import GoogleAuth, GoogleCalendarClient
from mailassistant.gcal.service import (ConflictsFound, ProposalError, approve_and_create, build_event_body,
                                        event_id_for, pending_scan_email_ids, reject_proposal, scan_email,
                                        sync_calendar)
from mailassistant.llm.client import LLMReply, LLMUnavailable


OWN = [{"id": "primary", "primary": True, "summary": "alex@gmail.example", "accessRole": "owner"}]


class FakeCalendar:
    def __init__(self, events=(), calendars=None, list_forbidden=False):
        self.events = {e["id"]: e for e in events}
        self.calendars = list(OWN if calendars is None else calendars)
        self.list_forbidden = list_forbidden
        self.inserts: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def _send(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                if self.path.startswith("/token"):
                    return self._send(200, {"access_token": "AT", "expires_in": 3600})
                ev = json.loads(body)
                outer.inserts.append({"path": self.path, "event": ev})
                if ev["id"] in outer.events:
                    return self._send(409, {"error": {"message": "The requested identifier already exists."}})
                outer.events[ev["id"]] = {**ev, "status": ev.get("status", "confirmed")}
                self._send(200, outer.events[ev["id"]])

            def do_GET(self):
                path = urllib.parse.urlparse(self.path).path
                if path.endswith("/users/me/calendarList"):
                    if outer.list_forbidden:
                        return self._send(403, {"error": {"message": "Request had insufficient authentication scopes.",
                                                          "errors": [{"reason": "insufficientPermissions"}]}})
                    return self._send(200, {"items": outer.calendars})
                if m := re.search(r"/events/([^/]+)$", path):
                    ev = outer.events.get(urllib.parse.unquote(m[1]))
                    return self._send(200, ev) if ev else self._send(404, {"error": {"message": "Not Found"}})
                self._send(200, {"items": list(outer.events.values())})

            def log_message(self, *a):
                pass

        class Quiet(ThreadingHTTPServer):
            def handle_error(self, *a):
                pass

        self.server = Quiet(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def client(self):
        return GoogleCalendarClient(GoogleAuth("c", "s", "r", token_url=self.url + "/token"),
                                    base_url=self.url + "/calendar/v3")

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeModel:
    model = "fake"

    def __init__(self, *contents):
        self.contents = list(contents)

    def chat(self, messages, schema=None, **kw):
        c = self.contents.pop(0)
        if isinstance(c, Exception):
            raise c
        return LLMReply(c, 5, "fake")


def sentence():
    d = utcnow() + timedelta(days=20)
    date_text = f"{d.day} {d.strftime('%B').lower()}"
    return date_text, f"Overleg op {date_text} om 14:00 uur"


def extraction(**kw):
    date_text, quote = sentence()
    base = dict(reason="r", kind="invitation", title="Overleg", date_text=date_text, start_time_text="14:00",
                end_time_text="", duration_minutes=None, timezone_text="", location="", meeting_url="",
                organiser="", evidence_quote=quote, all_day=False, confidence=0.9)
    base.update(kw)
    return json.dumps(base)


@pytest.fixture
def settings():
    return Settings(_env_file=None, database_url="postgresql+psycopg://x:y@localhost/z",
                    my_addresses="me@example.com")


@pytest.fixture
def s(engine):
    with engine.begin() as c:
        c.exec_driver_sql("TRUNCATE attachments, event_proposals, calendar_cache, classifications, audit_log, "
                          "llm_calls, emails, threads, mailboxes, mail_accounts RESTART IDENTITY CASCADE")
    sess = Session(engine)
    sess.acct = MailAccount(address="me@example.com")
    sess.add(sess.acct)
    sess.commit()
    yield sess
    sess.rollback()
    sess.close()


@pytest.fixture
def cal():
    made = []

    def make(*events, **kw):
        c = FakeCalendar(events, **kw)
        made.append(c)
        return c
    yield make
    for c in made:
        c.close()


def when(days=10, hour=13):
    return (utcnow() + timedelta(days=days)).astimezone(UTC).replace(hour=hour, minute=0, second=0, microsecond=0)


def gev(id, start, end, **kw):
    return {"id": id, "status": "confirmed", "summary": kw.pop("summary", "Busy"),
            "start": {"dateTime": start.isoformat().replace("+00:00", "Z")},
            "end": {"dateTime": end.isoformat().replace("+00:00", "Z")}, **kw}


def ics(start, uid="u-1", method="REQUEST", summary="Projectoverleg"):
    f = lambda d: d.strftime("%Y%m%dT%H%M%SZ")  # noqa: E731
    return (f"BEGIN:VCALENDAR\nMETHOD:{method}\nBEGIN:VEVENT\nUID:{uid}\nDTSTART:{f(start)}\n"
            f"DTEND:{f(start + timedelta(hours=1))}\nSUMMARY:{summary}\nORGANIZER;CN=Piet:mailto:p@x.nl\n"
            "ATTENDEE;CN=Alex:mailto:me@example.com\nEND:VEVENT\nEND:VCALENDAR\n")


def add_email(s, mid="m1", body="Hallo", subject="Uitnodiging", ics_text=None, category=None, injection=False):
    e = Email(account_id=s.acct.id, message_id=f"<{mid}@x>", direction="in", from_addr="p@x.nl", subject=subject,
              body_text=body, sent_at=utcnow())
    s.add(e)
    s.flush()
    if ics_text:
        s.add(Attachment(email_id=e.id, filename="invite.ics", mime_type="text/calendar", is_ics=True, ics_text=ics_text))
    if category:
        s.add(Classification(email_id=e.id, category=category, importance="normal", confidence=0.9, source="llm",
                             is_current=True, injection_flag=injection, safety_flags=[]))
    s.commit()
    return e.id


def proposals(s):
    return s.scalars(select(EventProposal).order_by(EventProposal.id)).all()


def test_sync_caches_events_and_drops_vanished_ones(s, settings, cal):
    g = cal(gev("a", when(3), when(3) + timedelta(hours=1)), gev("b", when(4), when(4) + timedelta(hours=1)))
    assert sync_calendar(s, g.client(), settings) == 2
    s.commit()
    del g.events["b"]
    sync_calendar(s, g.client(), settings)
    s.commit()
    assert [r.google_event_id for r in s.scalars(select(CalendarCache))] == ["a"]


def test_ics_invitation_becomes_a_proposal_with_conflict_info(s, settings, cal):
    start = when()
    g = cal(gev("busy", start + timedelta(minutes=30), start + timedelta(minutes=90)))
    sync_calendar(s, g.client(), settings)
    eid = add_email(s, ics_text=ics(start))
    made = scan_email(s, eid, FakeModel(), settings)
    s.commit()
    p = made[0]
    assert (p.kind, p.status, p.ics_uid, p.organiser) == ("invitation", "proposed", "u-1", "Piet <p@x.nl>")
    assert [c["type"] for c in p.conflicts] == ["overlap"] and p.start_at == start
    assert s.get(Email, eid).events_scanned_at is not None
    assert scan_email(s, eid, FakeModel(), settings) == []          # idempotent
    assert len(proposals(s)) == 1


def test_updated_invitation_supersedes_and_cancel_withdraws(s, settings):
    start = when()
    scan_email(s, add_email(s, "m1", ics_text=ics(start)), FakeModel(), settings)
    scan_email(s, add_email(s, "m2", ics_text=ics(start + timedelta(hours=2))), FakeModel(), settings)
    s.commit()
    assert [p.status for p in proposals(s)] == ["superseded", "proposed"]
    scan_email(s, add_email(s, "m3", ics_text=ics(start, method="CANCEL")), FakeModel(), settings)
    s.commit()
    assert [p.status for p in proposals(s)] == ["superseded", "superseded"]
    assert s.scalar(select(AuditLog).where(AuditLog.action == "invitation_cancelled_by_organizer"))


def test_event_already_in_calendar_is_not_proposed(s, settings, cal):
    start = when()
    g = cal(gev("x", start, start + timedelta(hours=1), summary="Projectoverleg", iCalUID="u-1@google.com"))
    sync_calendar(s, g.client(), settings)
    p = scan_email(s, add_email(s, ics_text=ics(start)), FakeModel(), settings)[0]
    assert p.status == "superseded" and p.conflicts[0]["type"] == "duplicate_of"


def test_injection_flagged_and_spam_mail_is_not_scanned(s, settings):
    eid = add_email(s, ics_text=ics(when()), category="invitation", injection=True)
    assert scan_email(s, eid, FakeModel(), settings) == [] and proposals(s) == []
    assert s.get(Email, eid).events_scanned_at is not None
    assert s.scalar(select(AuditLog).where(AuditLog.action == "event_scan_skipped"))


def test_plain_text_invitation_via_model_with_verification(s, settings):
    quote = sentence()[1]
    ok = add_email(s, "ok", body=f"Hoi, {quote}. Groet", category="invitation")
    made = scan_email(s, ok, FakeModel(extraction()), settings)
    s.commit()
    assert len(made) == 1 and made[0].evidence_quote == quote
    fake = add_email(s, "fake", body="Wij zien elkaar binnenkort wel", category="invitation")
    assert scan_email(s, fake, FakeModel(extraction()), settings) == []          # quote not in the email
    assert s.get(Email, fake).events_scanned_at is not None


def test_unavailable_model_does_not_mark_the_email_scanned(s, settings):
    eid = add_email(s, body=sentence()[1], category="invitation")
    with pytest.raises(LLMUnavailable):
        scan_email(s, eid, FakeModel(LLMUnavailable("down")), settings)
    s.rollback()
    assert s.get(Email, eid).events_scanned_at is None and eid in pending_scan_email_ids(s, 60, 10)


def test_approve_creates_one_event_without_attendees_or_invitations(s, settings, cal):
    g = cal()
    p = scan_email(s, add_email(s, ics_text=ics(when())), FakeModel(), settings)[0]
    s.commit()
    done = approve_and_create(s, p.id, g.client(), settings)
    ev = g.inserts[0]["event"]
    assert done.status == "created" and done.google_event_id == ev["id"] == event_id_for(p.dedupe_key)
    assert re.fullmatch(r"[0-9a-v]{5,}", ev["id"]) and "sendUpdates=none" in g.inserts[0]["path"]
    assert "attendees" not in ev and ev["extendedProperties"]["private"]["mail_assistant_key"] == p.dedupe_key
    assert "No invitations were sent" in ev["description"] and ev["status"] == "confirmed"
    with pytest.raises(ProposalError):
        approve_and_create(s, p.id, g.client(), settings)       # cannot create twice
    assert len(g.inserts) == 1


def test_retry_after_a_lost_response_is_idempotent(s, settings, cal):
    g = cal()
    p = scan_email(s, add_email(s, ics_text=ics(when())), FakeModel(), settings)[0]
    s.commit()
    email = s.get(Email, p.email_id)
    g.events[event_id_for(p.dedupe_key)] = {**build_event_body(p, email, settings.timezone), "status": "confirmed"}
    # the live duplicate check sees our own earlier event -> nothing new is created
    with pytest.raises(ProposalError):
        approve_and_create(s, p.id, g.client(), settings)
    assert g.inserts == [] and s.get(EventProposal, p.id).status == "superseded"


def test_hard_conflict_blocks_until_accepted(s, settings, cal):
    start = when()
    g = cal(gev("busy", start, start + timedelta(hours=1)))
    p = scan_email(s, add_email(s, ics_text=ics(start)), FakeModel(), settings)[0]
    s.commit()
    with pytest.raises(ConflictsFound) as e:
        approve_and_create(s, p.id, g.client(), settings)
    assert e.value.conflicts[0]["type"] == "overlap" and g.inserts == []
    assert s.get(EventProposal, p.id).status == "proposed"
    assert approve_and_create(s, p.id, g.client(), settings, accept_conflicts=True).status == "created"


def test_mentions_cannot_be_created_and_rejection_works(s, settings, cal):
    g = cal()
    m = EventProposal(email_id=add_email(s), kind="mention", title="Beurs", start_at=when(),
                      end_at=when() + timedelta(hours=1), status="proposed", dedupe_key="mention-1",
                      conflicts=[], attendees=[], notes=[])
    s.add(m)
    s.commit()
    with pytest.raises(ProposalError):
        approve_and_create(s, m.id, g.client(), settings)
    assert reject_proposal(s, m.id).status == "rejected"


def test_proposed_date_is_created_as_tentative(s, settings, cal):
    g = cal()
    quote = "Kunnen we " + sentence()[1] + " doen?"
    eid = add_email(s, body=quote, category="invitation")
    made = scan_email(s, eid, FakeModel(extraction(kind="proposed_date", evidence_quote=quote)), settings)
    s.commit()
    assert len(made) == 1 and made[0].kind == "proposed_date"
    approve_and_create(s, made[0].id, g.client(), settings)
    assert g.inserts[0]["event"]["status"] == "tentative"
    assert "not yet confirmed" in g.inserts[0]["event"]["description"]
    assert s.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "event_created")) == 1


def test_same_appointment_from_two_emails_is_proposed_once(s, settings, cal):
    quote = sentence()[1]
    allday = add_email(s, "a", body=f"Je aanmelding is geslaagd. {quote}.", category="invitation")
    first = scan_email(s, allday, FakeModel(extraction(all_day=True, start_time_text="", title="Overleg")), settings)[0]
    s.commit()
    timed = add_email(s, "b", body=f"Hoi, {quote}. Groet", category="invitation")
    second = scan_email(s, timed, FakeModel(extraction(title="Overleg bestuur")), settings)[0]
    s.commit()
    s.refresh(first)
    # the version with a real time wins; the all-day one is replaced (visible, audited), not deleted
    assert (first.status, second.status) == ("superseded", "proposed")
    assert f"replaced_by_proposal:#{second.id}" in first.notes
    assert s.scalar(select(AuditLog).where(AuditLog.action == "event_proposal_replaced"))

    # a third mail about the same appointment (e.g. someone else's sign-up) is not proposed again
    third = add_email(s, "c", body=f"Ik meld me ook aan. {quote}.", category="invitation")
    p3 = scan_email(s, third, FakeModel(extraction(title="Overleg")), settings)[0]
    assert p3.status == "superseded" and p3.conflicts[0] == {**p3.conflicts[0], "type": "duplicate_of_proposal",
                                                             "proposal_id": second.id}

    # once created in Google, a better-looking later proposal still never replaces it
    g = cal()
    approve_and_create(s, second.id, g.client(), settings)
    fourth = add_email(s, "d", ics_text=ics(second.start_at, uid="other-uid", summary="Overleg bestuur"))
    p4 = scan_email(s, fourth, FakeModel(), settings)[0]
    s.refresh(second)
    assert (second.status, p4.status) == ("created", "superseded")


def test_different_appointments_on_the_same_day_stay_separate(s, settings):
    quote = sentence()[1]
    a = scan_email(s, add_email(s, "a", body=f"Hoi, {quote}.", category="invitation"),
                   FakeModel(extraction(title="Overleg")), settings)[0]
    s.commit()
    b = scan_email(s, add_email(s, "b", body=f"Hoi, {quote}.", category="invitation"),
                   FakeModel(extraction(title="Tandarts")), settings)[0]
    s.commit()
    assert (a.status, b.status) == ("proposed", "proposed")


FAMILY = {"id": "fam@group.calendar.google.com", "summary": "Familie", "accessRole": "writer"}


def test_private_calendar_by_name_gets_minimal_event_and_work_stays_default(s, cal):
    conf = Settings(_env_file=None, database_url="postgresql+psycopg://x:y@localhost/z",
                    my_addresses="me@example.com", google_private_calendar="familie")   # name, any case
    g = cal(calendars=[*OWN, FAMILY])
    sync_calendar(s, g.client(), conf)
    s.commit()
    start = when()
    p = scan_email(s, add_email(s, "fam", ics_text=ics(start, uid="fam-1", summary="Verjaardag")), FakeModel(), conf)[0]
    s.commit()
    assert p.target_calendar == "private" and any("calendar suggestion: private" in n for n in p.notes)
    approve_and_create(s, p.id, g.client(), conf)
    ins = g.inserts[-1]
    assert "/calendars/fam%40group.calendar.google.com/events" in ins["path"]
    ev = ins["event"]
    assert "description" not in ev and "source_message_id" not in ev["extendedProperties"]["private"]
    assert ev["summary"] == "Verjaardag" and p.created_calendar_id == "fam@group.calendar.google.com"
    e = Email(account_id=s.acct.id, message_id="<w@x>", direction="in", from_addr="p@x.nl", subject="Overleg",
              to_addrs=["info@example-business.com"], body_text="x", sent_at=utcnow())
    s.add(e)
    s.flush()
    s.add(Attachment(email_id=e.id, filename="i.ics", mime_type="text/calendar", is_ics=True,
                     ics_text=ics(start + timedelta(days=1), uid="w-1")))
    s.commit()
    w = scan_email(s, e.id, FakeModel(), conf)[0]
    s.commit()
    assert w.target_calendar == "work"
    approve_and_create(s, w.id, g.client(), conf)
    assert "/calendars/primary/events" in g.inserts[-1]["path"] and "Source: email" in g.inserts[-1]["event"]["description"]


def test_own_calendars_are_read_for_conflicts_but_not_subscriptions(s, cal):
    from mailassistant.gcal.review import calendars
    other_own = {"id": "club@group.calendar.google.com", "summary": "Club", "accessRole": "owner"}
    holidays = {"id": "nl.dutch#holiday@group.v.calendar.google.com", "summary": "Feestdagen", "accessRole": "reader"}
    colleague = {"id": "jan@example-business.com", "summary": "Jan", "accessRole": "freeBusyReader"}
    conf = Settings(_env_file=None, database_url="postgresql+psycopg://x:y@localhost/z")
    g = cal(calendars=[*OWN, FAMILY, other_own, holidays, colleague])
    sync_calendar(s, g.client(), conf)
    assert calendars(s)["read_ids"] == ["primary", "fam@group.calendar.google.com", "club@group.calendar.google.com"]
    sync_calendar(s, g.client(), conf.model_copy(update={"google_conflict_calendars": ["Club", "Feestdagen"]}))
    assert calendars(s)["read_ids"] == ["primary", "club@group.calendar.google.com", holidays["id"]]
    sync_calendar(s, g.client(), conf.model_copy(update={"google_conflict_calendars": ["none"]}))
    assert calendars(s)["read_ids"] == ["primary"]


def test_ambiguous_or_unknown_name_creates_nothing(s, cal):
    from mailassistant.gcal.review import calendars
    twin = {"id": "fam2@group.calendar.google.com", "summary": "Familie", "accessRole": "owner"}
    conf = Settings(_env_file=None, database_url="postgresql+psycopg://x:y@localhost/z",
                    my_addresses="me@example.com", google_private_calendar="Familie")
    g = cal(calendars=[*OWN, FAMILY, twin])
    sync_calendar(s, g.client(), conf)
    assert "matches 2 calendars" in calendars(s)["private"]["error"]
    p = scan_email(s, add_email(s, ics_text=ics(when())), FakeModel(), conf)[0]
    s.commit()
    with pytest.raises(ProposalError, match="matches 2 calendars"):
        approve_and_create(s, p.id, g.client(), conf)
    assert g.inserts == []
    g.calendars = [*OWN]
    sync_calendar(s, g.client(), conf)
    assert "no calendar named 'Familie' (available: alex@gmail.example)" == calendars(s)["private"]["error"]


def test_without_list_permission_ids_still_work_and_names_explain(s, cal):
    from mailassistant.gcal.review import calendars
    g = cal(list_forbidden=True)
    conf = Settings(_env_file=None, database_url="postgresql+psycopg://x:y@localhost/z",
                    google_private_calendar="Familie")
    sync_calendar(s, g.client(), conf)
    c = calendars(s)
    assert c["read_ids"] == ["primary"] and "permission missing" in c["note"]
    assert "run gcal.authorize again" in c["private"]["error"]
    sync_calendar(s, g.client(), conf.model_copy(update={"google_private_calendar": "fam@group.calendar.google.com"}))
    assert calendars(s)["read_ids"] == ["primary", "fam@group.calendar.google.com"]


def test_private_choice_without_configured_calendar_is_refused(s, settings, cal):
    from mailassistant.gcal.review import ReviewError, request_approval
    p = scan_email(s, add_email(s, ics_text=ics(when())), FakeModel(), settings)[0]
    s.commit()
    assert p.target_calendar == "work"
    with pytest.raises(ReviewError):
        request_approval(s, p.id, calendar="private", settings=settings)
