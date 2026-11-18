"""Extra IMAP accounts: per-account sync, backfill limit, own addresses."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from mailassistant.config import Settings
from mailassistant.db.models import Email, MailAccount, Mailbox
from mailassistant.mail.accounts import backfill_since, own_addresses
from mailassistant.mail.source import MailboxInfo, MailboxStatus
from mailassistant.mail.sync import sync_account

from mailassistant.mail.imap_bridge import extract_message_id


class SinceSource:
    """Exchange-like server with SINCE support; UIDs below 100 count as 'older than the backfill date'."""
    def __init__(self):
        self.boxes: dict[str, dict] = {}
        self.since_calls = []

    def add(self, name, uid, raw, flags=""):
        b = self.boxes.setdefault(name, {"validity": 1000, "flags": flags.lower().split(), "msgs": {}})
        b["msgs"][uid] = raw

    def list_mailboxes(self):
        return [MailboxInfo(n, frozenset(b["flags"])) for n, b in self.boxes.items()]

    def status(self, name):
        b = self.boxes[name]
        return MailboxStatus(len(b["msgs"]), max(b["msgs"], default=0) + 1, b["validity"])

    def search_uids(self, name, from_uid=1, *, since=None):
        self.since_calls.append(since)
        uids = sorted(u for u in self.boxes[name]["msgs"] if u >= from_uid)
        return [u for u in uids if u >= 100] if since else uids

    def fetch_message_ids(self, name, uids):
        m = self.boxes[name]["msgs"]
        return {u: extract_message_id(m[u]) for u in uids if u in m}

    def fetch_raw(self, name, uids):
        m = self.boxes[name]["msgs"]
        return iter([(u, m[u]) for u in uids if u in m])


def msg(mid, frm="piet@example.nl", to="me@company.com"):
    return (f"From: {frm}\nTo: {to}\nSubject: S\nDate: Tue, 03 Nov 2026 09:00:00 +0000\nMessage-ID: <{mid}>\n"
            "Content-Type: text/plain; charset=utf-8\n\nHallo\n").encode()


@pytest.fixture
def settings():
    return Settings(_env_file=None, database_url="postgresql+psycopg://x:y@localhost/z",
                    imap_username="me@proton.me", my_addresses="me@proton.me", mail_backfill_days=30)


@pytest.fixture
def s(engine):
    with engine.begin() as c:
        c.exec_driver_sql("TRUNCATE attachments, email_locations, emails, threads, mailboxes, "
                          "mail_accounts RESTART IDENTITY CASCADE")
    sess = Session(engine)
    yield sess
    sess.rollback()
    sess.close()


def test_extra_account_syncs_its_own_folders_with_backfill_limit(s, settings):
    work = MailAccount(kind="imap", label="work", address="me@company.com", auth="microsoft_oauth",
                       host="outlook.office365.com", port=993, aliases=["me@company.nl"])
    s.add(work)
    s.commit()
    src = SinceSource()
    src.add("INBOX", 5, msg("old@x", to="me@company.com"))            # before the backfill date
    src.add("INBOX", 100, msg("new@x", to="me@company.com"))
    src.add("Sent Items", 101, msg("sent@x", frm="me@company.com", to="klant@example.nl"), flags="\\Sent")
    src.add("Deleted Items", 102, msg("del@x"), flags="\\Trash")
    src.add("Junk Email", 103, msg("junk@x"), flags="\\Junk")
    src.add("Calendar", 104, msg("cal@x"))
    st = sync_account(s, src, settings, account=work, budget_seconds=100)
    assert st.errors == 0
    ids = set(s.scalars(select(Email.message_id)))
    assert ids == {"<new@x>", "<sent@x>"}
    assert all(c == backfill_since(work, settings) for c in src.since_calls)
    assert {m.name for m in s.scalars(select(Mailbox))} == {"INBOX", "Sent Items"}
    sent = s.scalar(select(Email).where(Email.message_id == "<sent@x>"))
    assert sent.direction == "out" and sent.account_id == work.id
    # the Proton account (no limit) still uses the plain search
    from mailassistant.mail.accounts import proton_account
    assert backfill_since(proton_account(s, settings), settings) is None


def test_own_addresses_include_every_account(s, settings):
    s.add(MailAccount(kind="imap", label="work", address="me@company.com", aliases=["Me@Company.nl"]))
    s.commit()
    own = own_addresses(s, settings)
    assert own[0] == "me@proton.me" and "me@company.com" in own and "me@company.nl" in own


def test_backfill_date_is_fixed_to_when_the_account_was_added(s, settings):
    a = MailAccount(kind="imap", label="w", address="w@x.nl")
    s.add(a)
    s.commit()
    s.refresh(a)
    assert backfill_since(a, settings) == a.created_at.date() - timedelta(days=30)
    assert isinstance(backfill_since(a, settings), date)

