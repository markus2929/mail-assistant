"""Dashboard end to end against PostgreSQL, with only database_url + web secrets configured."""
import json
import re
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mailassistant.config import Settings
from mailassistant.db.models import (AuditLog, Classification, Email, EventProposal, Followup, FollowupEvent, Job,
                                     MailAccount, ServiceStatus, WebSession)
from mailassistant.db.session import utcnow
from mailassistant.web import app as webapp
from mailassistant.web.auth import hash_password

PW = "correct horse battery staple"


@pytest.fixture
def settings():
    # Deliberately NO fernet_key / google_client / imap settings: the web app must not need them.
    return Settings(_env_file=None, database_url="postgresql+psycopg://x:y@localhost/z",
                    web_password_hash=hash_password(PW), web_allowed_hosts="testserver,desktop.local")


@pytest.fixture
def s(engine):
    with engine.begin() as c:
        c.exec_driver_sql("TRUNCATE settings, web_sessions, service_status, followup_events, followups, briefings, attachments, "
                          "event_proposals, calendar_cache, classifications, classification_corrections, "
                          "proposed_actions, audit_log, llm_calls, jobs, email_locations, emails, threads, mailboxes, "
                          "mail_accounts RESTART IDENTITY CASCADE")
    sess = Session(engine)
    sess.acct = MailAccount(address="me@example.com")
    sess.add(sess.acct)
    sess.commit()
    yield sess
    sess.rollback()
    sess.close()


@pytest.fixture
def client(engine, settings, s):
    webapp.app.state.settings = settings
    webapp.app.state.sessionmaker = sessionmaker(engine, expire_on_commit=False)
    webapp.throttle._fails.clear()
    with TestClient(webapp.app, base_url="https://testserver") as c:
        yield c
    webapp.app.state.settings = webapp.app.state.sessionmaker = None


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html)[1]


def login(c) -> str:
    page = c.get("/login")
    r = c.post("/login", data={"csrf": csrf_of(page.text), "password": PW}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    return csrf_of(c.get("/").text)


def mail(s, subject="Offerte", category="work"):
    e = Email(account_id=s.acct.id, message_id=f"<{utcnow().timestamp()}@x>", direction="in",
              from_addr="piet@example.nl", to_addrs=["me@example.com"], subject=subject, body_text="x",
              sent_at=utcnow() - timedelta(days=1))
    s.add(e)
    s.flush()
    s.add(Classification(email_id=e.id, category=category, importance="normal", confidence=0.9, source="llm",
                         is_current=True, injection_flag=False, safety_flags=[]))
    s.commit()
    return e.id


def followup(s, eid, status="proposed", action="Offerte sturen"):
    f = Followup(email_id=eid, direction="I_OWE", counterparty="piet@example.nl", people=["piet@example.nl"],
                 action_text=action, evidence_quote="Kun je de offerte sturen?", remind_at=utcnow() + timedelta(days=1),
                 remind_basis="estimated", status=status, confidence=0.9, dedupe_key=f"k{utcnow().timestamp()}", notes=[])
    s.add(f)
    s.commit()
    return f.id


def proposal(s, eid, **kw):
    start = utcnow() + timedelta(days=5)
    p = EventProposal(email_id=eid, kind="invitation", title="Overleg", start_at=start, end_at=start + timedelta(hours=1),
                      status="proposed", dedupe_key=f"p{utcnow().timestamp()}", conflicts=[], attendees=[], notes=[], **kw)
    s.add(p)
    s.commit()
    return p.id


def test_every_page_requires_login_and_has_security_headers(client):
    for path in ("/", "/followups", "/calendar", "/mail", "/status", "/followups/1"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login", path
    assert client.post("/followups/bulk", data={"action": "reject", "ids": "1"}).status_code == 403
    r = client.get("/login")
    assert "default-src 'self'" in r.headers["content-security-policy"] and r.headers["x-frame-options"] == "DENY"
    assert client.get("/healthz").json()["status"] == "ok"


def test_unknown_host_is_refused(client):
    assert client.get("/login", headers={"host": "evil.example"}).status_code == 400
    assert client.get("/login", headers={"host": "desktop.local:8443"}).status_code == 200


def test_wrong_password_is_throttled_and_audited(client, s):
    for _ in range(5):
        page = client.get("/login")
        client.post("/login", data={"csrf": csrf_of(page.text), "password": "nope"})
    page = client.get("/login")
    assert client.post("/login", data={"csrf": csrf_of(page.text), "password": PW}).status_code == 429
    assert len(s.scalars(select(AuditLog).where(AuditLog.action == "web_login_failed")).all()) == 5
    # login form without its own CSRF cookie/token is refused
    client.cookies.clear()
    assert client.post("/login", data={"csrf": "x", "password": PW}, follow_redirects=False).headers["location"] == "/login"


def test_session_cookie_is_hashed_and_logout_ends_it(client, s):
    token = login(client)
    assert token
    cookie = client.cookies.get("ma_session")
    rows = s.scalars(select(WebSession)).all()
    assert len(rows) == 1 and rows[0].id_hash != cookie
    client.post("/logout", data={"csrf": token})
    assert client.get("/", follow_redirects=False).status_code == 303
    s.expire_all()
    assert s.scalars(select(WebSession)).all() == []


def test_csrf_is_required_for_every_post(client, s):
    login(client)
    fid = followup(s, mail(s))
    assert client.post("/followups/bulk", data={"action": "reject", "ids": str(fid)}).status_code == 403
    assert client.post("/followups/bulk", data={"action": "reject", "ids": str(fid), "csrf": "wrong"}).status_code == 403
    s.expire_all()
    assert s.get(Followup, fid).status == "proposed"


def test_bulk_followup_actions(client, s):
    token = login(client)
    eid = mail(s)
    a, b, c = followup(s, eid, action="Offerte"), followup(s, eid, action="Tekening"), followup(s, eid, "completed", "Factuur")
    r = client.post("/followups/bulk", data={"csrf": token, "action": "reject", "ids": [str(a), str(b), str(c)]})
    assert r.status_code == 200 and f"#{c}: follow-up is already completed" in r.text
    s.expire_all()
    assert [s.get(Followup, i).status for i in (a, b, c)] == ["cancelled", "cancelled", "completed"]
    assert len(s.scalars(select(FollowupEvent).where(FollowupEvent.to_status == "cancelled")).all()) == 2
    d = followup(s, eid, action="Bellen")
    client.post("/followups/bulk", data={"csrf": token, "action": "confirm", "ids": str(d)})
    client.post(f"/followups/{d}/snooze", data={"csrf": token, "days": "3"})
    page = client.get(f"/followups/{d}")
    assert "Bellen" in page.text and "manual" in page.text
    assert "Offerte" in client.get("/followups?status=done").text


def test_calendar_approve_is_queued_for_the_worker_and_bulk_reject(client, s):
    token = login(client)
    eid = mail(s, category="invitation")
    p1, p2, p3 = proposal(s, eid), proposal(s, eid), proposal(s, eid)
    client.post(f"/calendar/{p1}/approve", data={"csrf": token})
    s.expire_all()
    job = s.scalar(select(Job).where(Job.type == "gcal.approve"))
    assert job.payload == {"proposal_id": p1, "accept_conflicts": False}
    assert s.get(EventProposal, p1).approval_requested_at is not None
    page = client.get("/calendar")
    assert "creating…" in page.text and 'http-equiv="refresh"' in page.text
    r = client.post(f"/calendar/{p1}/approve", data={"csrf": token})        # double click
    assert "already being created" in r.text
    client.post("/calendar/bulk", data={"csrf": token, "action": "reject", "ids": [str(p2), str(p3)]})
    s.expire_all()
    assert (s.get(EventProposal, p2).status, s.get(EventProposal, p3).status) == ("rejected", "rejected")
    # conflicts reported by the worker -> the page offers "approve despite conflict"
    s.get(EventProposal, p1).approval_result = "conflicts: 1 calendar item(s) overlap; approve anyway to create it"
    s.commit()
    assert "Approve despite conflict" in client.get("/calendar").text


def test_classification_correction(client, s):
    token = login(client)
    eid = mail(s, subject="Nieuwsbrief", category="manual_review")
    assert "Nieuwsbrief" in client.get("/mail?category=manual_review").text
    client.post(f"/mail/{eid}/category", data={"csrf": token, "category": "newsletter"})
    s.expire_all()
    cur = s.scalar(select(Classification).where(Classification.email_id == eid, Classification.is_current.is_(True)))
    assert (cur.category, cur.source) == ("newsletter", "user")
    assert client.post(f"/mail/{eid}/category", data={"csrf": token, "category": "evil"}).status_code == 200
    s.expire_all()
    assert s.scalar(select(Classification).where(Classification.email_id == eid,
                                                 Classification.is_current.is_(True))).category == "newsletter"


def test_status_and_briefing_pages_read_only_the_database(client, s):
    login(client)
    assert "has not reported yet" in client.get("/status").text
    s.add(ServiceStatus(component="worker", ok=True, detail="queued=0 dead_24h=0", checked_at=utcnow()))
    s.add(ServiceStatus(component="ollama", ok=False, detail="not reachable", checked_at=utcnow()))
    s.commit()
    page = client.get("/status")
    assert "has not reported" not in page.text and "not reachable" in page.text
    followup(s, mail(s), action="Offerte sturen")
    assert "Offerte sturen" in client.get("/").text


# ------------------------------------------------------------------ worker side: gcal.approve + HA push
def test_gcal_approve_handler_writes_the_outcome(s, engine, monkeypatch):
    from mailassistant.gcal import handlers as gh
    from mailassistant.gcal.google import GoogleUnavailable
    from mailassistant.gcal.service import ConflictsFound
    from mailassistant.jobs.registry import Deferred

    factory = sessionmaker(engine, expire_on_commit=False)

    class Scope:
        def __enter__(self):
            self.s = factory()
            return self.s

        def __exit__(self, exc, *a):
            (self.s.rollback if exc else self.s.commit)()
            self.s.close()

    monkeypatch.setattr(gh, "session_scope", Scope)
    monkeypatch.setattr(gh, "get_settings", lambda: Settings(_env_file=None, database_url="postgresql+psycopg://x:y@h/d"))
    monkeypatch.setattr(gh, "calendar_client", lambda session, s: object())
    pid = proposal(s, mail(s, category="invitation"))

    def run(exc=None):
        def fake(session, p, client, st, accept_conflicts=False):
            if exc:
                raise exc
            prop = session.get(EventProposal, p)
            prop.status = "created"
            return prop
        monkeypatch.setattr(gh, "approve_and_create", fake)
        try:
            gh.gcal_approve({"proposal_id": pid})
        except Deferred:
            return "deferred"
        s.expire_all()
        return s.get(EventProposal, pid).approval_result

    assert run(ConflictsFound([{"type": "overlap"}])).startswith("conflicts: 1")
    assert run(GoogleUnavailable("down")) == "deferred"
    s.expire_all()
    assert s.get(EventProposal, pid).approval_result.startswith("waiting")
    assert run() == "created"


class FakeHA:
    def __init__(self, status=200):
        self.calls, outer = [], self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.calls.append((self.path, self.headers.get("Authorization"),
                                    json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(status)
                self.end_headers()
                self.wfile.write(b"[]")

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def test_due_followups_are_pushed_once(s, engine, monkeypatch):
    from mailassistant.jobs.registry import Deferred
    from mailassistant.notify import handlers as nh

    factory = sessionmaker(engine, expire_on_commit=False)

    class Scope:
        def __enter__(self):
            self.s = factory()
            return self.s

        def __exit__(self, exc, *a):
            (self.s.rollback if exc else self.s.commit)()
            self.s.close()

    ha = FakeHA()
    conf = Settings(_env_file=None, database_url="postgresql+psycopg://x:y@h/d", ha_url=ha.url,
                    ha_notify_service="notify.mobile_app_phone", ha_token="T0KEN",
                    dashboard_url="https://desktop.local:8443")
    monkeypatch.setattr(nh, "session_scope", Scope)
    monkeypatch.setattr(nh, "get_settings", lambda: conf)
    eid = mail(s)
    due = followup(s, eid, status="open")
    s.get(Followup, due).remind_at = utcnow() - timedelta(minutes=5)
    followup(s, eid, status="proposed")                          # not confirmed: never pushed
    s.commit()
    nh.notify_due({})
    nh.notify_due({})
    assert len(ha.calls) == 1
    path, auth, body = ha.calls[0]
    assert path == "/api/services/notify/mobile_app_phone" and auth == "Bearer T0KEN"
    assert "1 om zelf te doen" in body["message"] and "Offerte" not in body["message"]   # counts only
    s.expire_all()
    assert s.get(Followup, due).notified_at is not None
    assert s.scalar(select(AuditLog).where(AuditLog.action == "ha_notified"))
    ha.server.shutdown()
    down = Settings(_env_file=None, database_url="postgresql+psycopg://x:y@h/d", ha_url="http://127.0.0.1:9",
                    ha_notify_service="x", ha_token="T")
    monkeypatch.setattr(nh, "get_settings", lambda: down)
    s.get(Followup, due).notified_at = None
    s.commit()
    with pytest.raises(Deferred):
        nh.notify_due({})
    s.expire_all()
    assert s.get(Followup, due).notified_at is None                  # retried later, not lost


def test_calendar_choice_buttons_and_stored_choice(client, s, settings):
    from mailassistant.db.models import Setting
    webapp.app.state.settings = settings.model_copy(update={"google_private_calendar": "Familie"})
    token = login(client)
    pid = proposal(s, mail(s, category="invitation"))       # mail to me@example.com -> suggestion: private
    assert "cannot be used yet" in client.get("/calendar").text   # worker has not resolved the calendars yet
    s.add(Setting(key="gcal.calendars", value={
        "checked_at": utcnow().isoformat(), "list_available": True, "note": None, "extras": [],
        "work": {"value": "primary", "id": "alex@gmail.example", "name": "Agenda", "access_role": "owner", "error": None},
        "private": {"value": "Familie", "id": "fam@g", "name": "Familie", "access_role": "writer", "error": None},
        "read_ids": ["alex@gmail.example", "fam@g"], "read_names": {"alex@gmail.example": "Agenda", "fam@g": "Familie"}}))
    s.commit()
    page = client.get("/calendar")
    assert "Approve → Agenda" in page.text and "Approve → Familie" in page.text and "suggested: private" in page.text
    client.post(f"/calendar/{pid}/approve", data={"csrf": token, "calendar": "work"})
    s.expire_all()
    assert s.get(EventProposal, pid).target_calendar == "work"
    assert "calendar=work" in s.scalar(select(AuditLog.detail).where(AuditLog.action == "event_approval_requested"))
    st = client.get("/status")
    assert "Familie" in st.text and "Checked for conflicts: Agenda, Familie" in st.text


# ------------------------------------------------------------------ Move to Trash
def _locate(s, eid, uid, name="INBOX"):
    from mailassistant.db.models import EmailLocation, Mailbox
    mb = s.scalar(select(Mailbox).where(Mailbox.name == name))
    if mb is None:
        mb = Mailbox(account_id=s.acct.id, name=name, role="inbox", uidvalidity=7)
        s.add(mb)
        s.flush()
    s.add(EmailLocation(email_id=eid, mailbox_id=mb.id, uid=uid))
    s.commit()
    return mb.id


def test_move_to_trash_confirm_then_queue_and_hide(client, s):
    from mailassistant.db.models import MailAction
    token = login(client)
    a, b, c = mail(s, "Sale 1", "marketing"), mail(s, "Sale 2", "marketing"), mail(s, "Sale 3", "marketing")
    followup(s, c, status="open")              # has an open follow-up -> kept
    page = client.get("/mail?category=marketing&days=14")
    assert "Move all 3 shown to Trash" in page.text and 'form="trash"' in page.text
    # step 1: "all shown" -> confirm page, nothing recorded yet
    r = client.post("/mail/trash", data={"csrf": token, "scope": "shown", "category": "marketing", "days": "14"})
    assert r.status_code == 200 and "Move 2 mail(s) to Trash?" in r.text and "has an open follow-up" in r.text
    assert s.scalar(select(MailAction.id)) is None
    # step 2: confirm
    r = client.post("/mail/trash/confirm", data={"csrf": token, "ids": [str(a), str(b)],
                                                  "back": "/mail?category=marketing&days=14"}, follow_redirects=False)
    assert r.status_code == 303
    s.expire_all()
    assert sorted(m.email_id for m in s.scalars(select(MailAction))) == [a, b]
    assert s.scalar(select(Job).where(Job.type == "mail.trash")) is not None
    assert s.scalar(select(AuditLog).where(AuditLog.action == "move_to_trash")) is not None
    page = client.get("/mail?category=marketing&days=14").text
    assert "Sale 1" not in page and "Sale 2" not in page and "Sale 3" in page and "marketing (1)" in page
    # asking again: already requested -> kept, nothing new
    r = client.post("/mail/trash", data={"csrf": token, "scope": "selected", "ids": [str(a)],
                                         "category": "marketing", "days": "14"})
    assert "already moved to Trash" in r.text and "Move 0 mail(s)" in r.text


def test_trash_requires_csrf_and_selection(client, s):
    token = login(client)
    eid = mail(s, "X", "marketing")
    assert client.post("/mail/trash", data={"scope": "selected", "ids": [str(eid)]}).status_code == 403
    assert client.post("/mail/trash/confirm", data={"ids": [str(eid)]}).status_code == 403
    r = client.post("/mail/trash", data={"csrf": token, "scope": "selected", "category": "marketing", "days": "14"},
                    follow_redirects=False)
    assert r.status_code == 303 and "Select" in r.headers.get("set-cookie", "")


def test_restored_mail_shows_again(client, s):
    from mailassistant.db.models import EmailLocation, MailAction
    login(client)
    eid = mail(s, "Restored", "marketing")
    mb = _locate(s, eid, 5)
    s.add(MailAction(email_id=eid, status="done", requested_at=utcnow() - timedelta(hours=2), done_at=utcnow()))
    loc = s.scalar(select(EmailLocation).where(EmailLocation.uid == 5))
    loc.removed_at = utcnow() - timedelta(hours=1)
    s.commit()
    assert "Restored" not in client.get("/mail?category=marketing").text
    s.add(EmailLocation(email_id=eid, mailbox_id=mb, uid=9))   # user moved it back from Trash -> new UID
    s.commit()
    assert "Restored" in client.get("/mail?category=marketing").text


def test_trash_handler_moves_only_verified_uids(s, engine, monkeypatch):
    from mailassistant.db.models import EmailLocation, MailAction
    from mailassistant.mail import handlers as mh
    from mailassistant.mail.trash import request_trash

    factory = sessionmaker(engine, expire_on_commit=False)

    class Scope:
        def __enter__(self):
            self.s = factory()
            return self.s

        def __exit__(self, exc, *a):
            (self.s.rollback if exc else self.s.commit)()
            self.s.close()

    a, b, gone = mail(s, "A", "marketing"), mail(s, "B", "marketing"), mail(s, "Gone", "marketing")
    _locate(s, a, 11)
    _locate(s, b, 12)
    request_trash(s, [a, b, gone])
    s.commit()
    calls = []

    class FakeWriter:
        def __enter__(self):
            return self

        def __exit__(self, *e):
            pass

        def move_to_trash(self, name, validity, expected):
            calls.append((name, validity, dict(expected)))
            return [11]                       # uid 12 had another Message-ID on the server -> not moved

    monkeypatch.setattr(mh, "session_scope", Scope)
    monkeypatch.setattr(mh, "get_settings", lambda: Settings(_env_file=None, database_url="postgresql+psycopg://x:y@h/d"))
    monkeypatch.setattr(mh, "source_for", lambda session, st, acct, cls=None: FakeWriter())
    mh.mail_trash({})
    s.expire_all()
    st = {m.email_id: m.status for m in s.scalars(select(MailAction))}
    assert st == {a: "done", b: "skipped", gone: "skipped"}
    assert calls[0][0] == "INBOX" and calls[0][1] == 7 and set(calls[0][2]) == {11, 12}
    assert s.scalar(select(EmailLocation.removed_at).where(EmailLocation.uid == 11)) is not None
    assert s.scalar(select(EmailLocation.removed_at).where(EmailLocation.uid == 12)) is None


def test_cookies_are_lax_so_links_from_notifications_keep_the_session(client):
    page = client.get("/login")
    assert "samesite=lax" in page.headers["set-cookie"].lower()
    r = client.post("/login", data={"csrf": csrf_of(page.text), "password": PW}, follow_redirects=False)
    session_cookie = next(h for h in r.headers.get_list("set-cookie") if h.startswith("ma_session="))
    assert "samesite=lax" in session_cookie.lower() and "httponly" in session_cookie.lower()


def test_keep_me_signed_in_survives_the_idle_timeout(client, s, settings):
    page = client.get("/login")
    r = client.post("/login", data={"csrf": csrf_of(page.text), "password": PW, "remember": "1"},
                    follow_redirects=False)
    assert f"max-age={settings.web_remember_days * 86400}" in r.headers["set-cookie"].lower()
    ws = s.scalars(select(WebSession)).one()
    assert ws.remember and ws.expires_at > utcnow() + timedelta(days=settings.web_remember_days - 1)
    ws.last_seen_at = utcnow() - timedelta(hours=settings.web_session_idle_hours + 5)   # long idle
    s.commit()
    assert client.get("/", follow_redirects=False).status_code == 200


def test_normal_session_still_has_the_idle_timeout(client, s, settings):
    login(client)
    ws = s.scalars(select(WebSession)).one()
    assert not ws.remember
    ws.last_seen_at = utcnow() - timedelta(hours=settings.web_session_idle_hours + 1)
    s.commit()
    assert client.get("/", follow_redirects=False).status_code == 303


def test_login_page_loaded_twice_still_works(client):
    """Phones preload the page: the form shown is from the first load, the cookie from the second."""
    first = client.get("/login")
    client.get("/login")
    r = client.post("/login", data={"csrf": csrf_of(first.text), "password": PW}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
