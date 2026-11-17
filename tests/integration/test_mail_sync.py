"""Sync engine against a real PostgreSQL (TEST_DATABASE_URL) and an in-memory fake mail server."""
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mailassistant.config import Settings
from mailassistant.db.models import Attachment, Email, EmailLocation, Mailbox, Thread
from mailassistant.mail.imap_bridge import extract_message_id
from mailassistant.mail.source import MailboxInfo, MailboxStatus
from mailassistant.mail.sync import sync_account

ME = "me@example.com"


class FakeSource:
    def __init__(self):
        self.boxes: dict[str, dict] = {}

    def add(self, name, uid, raw, flags=""):
        b = self.boxes.setdefault(name, {"validity": 1000, "flags": flags.lower().split(), "msgs": {}})
        b["msgs"][uid] = raw

    def list_mailboxes(self):
        return [MailboxInfo(n, frozenset(b["flags"])) for n, b in self.boxes.items()]

    def status(self, name):
        b = self.boxes[name]
        return MailboxStatus(len(b["msgs"]), max(b["msgs"], default=0) + 1, b["validity"])

    def search_uids(self, name, from_uid=1):
        return sorted(u for u in self.boxes[name]["msgs"] if u >= from_uid)

    def fetch_message_ids(self, name, uids):
        m = self.boxes[name]["msgs"]
        return {u: extract_message_id(m[u]) for u in uids if u in m}

    def fetch_raw(self, name, uids):
        m = self.boxes[name]["msgs"]
        return iter([(u, m[u]) for u in uids if u in m])


def msg(mid, subject="Onderwerp", frm="piet@example.nl", to=ME, date="Tue, 03 Nov 2026 09:00:00 +0000",
        refs=None, body="Hallo", extra=""):
    h = [f"From: {frm}", f"To: {to}", f"Subject: {subject}", f"Date: {date}", f"Message-ID: <{mid}>"]
    if refs:
        h += [f"In-Reply-To: <{refs[-1]}>", "References: " + " ".join(f"<{r}>" for r in refs)]
    return ("\n".join(h) + "\nContent-Type: text/plain; charset=utf-8\n\n" + body + extra + "\n").encode()


@pytest.fixture
def settings():
    return Settings(_env_file=None, database_url="postgresql+psycopg://x:y@localhost/z",
                    imap_username=ME, my_addresses=f"{ME},info@example-business.com")


@pytest.fixture
def s(engine):
    with engine.begin() as c:
        c.exec_driver_sql("TRUNCATE attachments, email_locations, emails, threads, mailboxes, "
                          "mail_accounts RESTART IDENTITY CASCADE")
    sess = Session(engine)
    yield sess
    sess.rollback()
    sess.close()


def count(s, model):
    return s.scalar(select(func.count()).select_from(model))


def test_initial_sync_then_idempotent(s, settings):
    src = FakeSource()
    for i in range(1, 6):
        src.add("INBOX", i, msg(f"m{i}@x"))
    first = sync_account(s, src, settings, budget_seconds=100)
    assert first.new_messages == 5 and first.remaining == 0 and first.errors == 0
    assert count(s, Email) == 5 and count(s, EmailLocation) == 5
    mb = s.scalar(select(Mailbox).where(Mailbox.name == "INBOX"))
    assert mb.role == "inbox" and mb.last_uid == 5 and mb.uidvalidity == 1000
    again = sync_account(s, src, settings, budget_seconds=100)
    assert again.new_messages == 0 and again.linked_existing == 0
    assert count(s, Email) == 5
    full = sync_account(s, src, settings, full=True, budget_seconds=100)
    assert full.new_messages == 0 and count(s, Email) == 5 and count(s, EmailLocation) == 5


def test_same_message_in_two_folders_is_one_email(s, settings):
    src = FakeSource()
    raw = msg("dup@x")
    src.add("INBOX", 1, raw)
    src.add("Folders/Bonnen", 7, raw)
    st = sync_account(s, src, settings, budget_seconds=100)
    assert st.new_messages == 1 and st.linked_existing == 1
    assert count(s, Email) == 1 and count(s, EmailLocation) == 2 and count(s, Mailbox) == 2


def test_reply_synced_before_parent_joins_same_thread(s, settings):
    src = FakeSource()
    src.add("INBOX", 1, msg("root@x", subject="Offerte", date="Mon, 02 Nov 2026 09:00:00 +0000"))
    src.add("INBOX", 2, msg("re1@x", subject="Re: Offerte", refs=["root@x"],
                            date="Tue, 03 Nov 2026 09:00:00 +0000"))
    src.add("INBOX", 3, msg("re2@x", subject="Re: Offerte", refs=["root@x", "re1@x"],
                            date="Wed, 04 Nov 2026 09:00:00 +0000"))
    sync_account(s, src, settings, budget_seconds=100)   # newest first: re2, re1, root
    ids = set(s.scalars(select(Email.thread_id)))
    assert len(ids) == 1 and count(s, Thread) == 1
    t = s.scalar(select(Thread))
    assert t.thread_key == "<root@x>" and t.first_message_at < t.last_message_at


def test_reply_without_references_joins_by_subject_and_correspondent(s, settings):
    src = FakeSource()
    src.add("INBOX", 1, msg("a@x", subject="Planning", date="Mon, 02 Nov 2026 09:00:00 +0000"))
    src.add("INBOX", 2, msg("b@x", subject="Antw: Planning", date="Tue, 03 Nov 2026 09:00:00 +0000"))
    src.add("INBOX", 3, msg("c@x", subject="Antw: Planning", frm="stranger@other.example",
                            date="Tue, 03 Nov 2026 10:00:00 +0000"))
    sync_account(s, src, settings, budget_seconds=100)
    by_mid = {e.message_id: e.thread_id for e in s.scalars(select(Email))}
    assert by_mid["<a@x>"] == by_mid["<b@x>"] != by_mid["<c@x>"]


def test_removed_location_is_marked_never_deleted(s, settings):
    src = FakeSource()
    for i in (1, 2):
        src.add("INBOX", i, msg(f"m{i}@x"))
    sync_account(s, src, settings, budget_seconds=100)
    del src.boxes["INBOX"]["msgs"][2]
    st = sync_account(s, src, settings, full=True, budget_seconds=100)
    assert st.removed == 1 and count(s, Email) == 2
    gone = s.scalar(select(EmailLocation).where(EmailLocation.uid == 2))
    assert gone.removed_at is not None


def test_uidvalidity_change_relinks_without_duplicates(s, settings):
    src = FakeSource()
    for i in (1, 2, 3):
        src.add("INBOX", i, msg(f"m{i}@x"))
    sync_account(s, src, settings, budget_seconds=100)
    old = src.boxes["INBOX"]
    src.boxes["INBOX"] = {"validity": 2000, "flags": [],
                          "msgs": {u + 100: raw for u, raw in old["msgs"].items()}}
    st = sync_account(s, src, settings, budget_seconds=100)
    assert st.new_messages == 0 and st.linked_existing == 3
    assert count(s, Email) == 3
    uids = sorted(s.scalars(select(EmailLocation.uid)))
    assert uids == [101, 102, 103]


def test_time_budget_limits_work_and_resumes(s, settings):
    src = FakeSource()
    for i in range(1, 6):
        src.add("INBOX", i, msg(f"m{i}@x"))
    st = sync_account(s, src, settings, budget_seconds=0, batch_size=2)
    assert st.new_messages == 2 and st.remaining == 3
    assert s.scalar(select(Mailbox)).last_uid == 0           # not caught up yet
    for _ in range(5):
        if sync_account(s, src, settings, budget_seconds=0, batch_size=2).remaining == 0:
            break
    assert count(s, Email) == 5 and s.scalar(select(Mailbox)).last_uid == 5


def test_direction_ics_and_mailbox_selection(s, settings):
    src = FakeSource()
    src.add("Sent", 1, msg("out@x", frm="info@example-business.com", to="klant@example.nl"), flags="\\Sent")
    ics = ("From: o@x.nl\nTo: " + ME + "\nSubject: Uitnodiging\nDate: Wed, 04 Nov 2026 08:00:00 +0000\n"
           "Message-ID: <ics@x>\nContent-Type: text/calendar; method=REQUEST; charset=utf-8\n\n"
           "BEGIN:VCALENDAR\nMETHOD:REQUEST\nEND:VCALENDAR\n").encode()
    src.add("INBOX", 1, ics)
    src.add("Labels/Ads", 1, msg("lbl@x"))
    src.add("Spam", 1, msg("spam@x"), flags="\\Junk")
    src.add("Folders", 1, msg("nosel@x"), flags="\\Noselect")
    sync_account(s, src, settings, budget_seconds=100)
    assert {m.name for m in s.scalars(select(Mailbox))} == {"INBOX", "Sent"}
    assert s.scalar(select(Email.direction).where(Email.message_id == "<out@x>")) == "out"
    assert s.scalar(select(Email.direction).where(Email.message_id == "<ics@x>")) == "in"
    att = s.scalar(select(Attachment))
    assert att.is_ics and "METHOD:REQUEST" in att.ics_text


def test_original_with_same_subject_from_a_stranger_stays_separate(s, settings):
    src = FakeSource()
    src.add("INBOX", 1, msg("x@x", subject="Planning", frm="stranger@other.example",
                            date="Mon, 02 Nov 2026 09:00:00 +0000"))
    src.add("INBOX", 2, msg("y@x", subject="Antw: Planning", date="Tue, 03 Nov 2026 09:00:00 +0000"))
    sync_account(s, src, settings, budget_seconds=100)
    by_mid = {e.message_id: e.thread_id for e in s.scalars(select(Email))}
    assert by_mid["<x@x>"] != by_mid["<y@x>"]
