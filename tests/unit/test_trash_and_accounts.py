"""Move to Trash (IMAP writer) and extra accounts (Microsoft sign-in, folders) without network."""
import base64
import io
import json
import urllib.error
from datetime import date
from types import SimpleNamespace

import pytest

from mailassistant.mail import msauth
from mailassistant.mail.accounts import IMAP_DEFAULT_EXCLUDE, SKIP_ROLES, sync_patterns
from mailassistant.mail.imap_bridge import (ImapBridgeSource, MailWriter, find_trash, imap_date, parse_list_line,
                                            xoauth2_string)
from mailassistant.mail.mailboxes import mailbox_role, select_mailboxes
from mailassistant.mail.source import MailboxInfo, MailSourceError
from mailassistant.mail.trash import matching_uids
from mailassistant.security.allowlist import ActionType, AutomationLevel, effective_level


# ------------------------------------------------------------------ rules
def test_trash_is_user_approval_only_and_delete_does_not_exist():
    assert effective_level("move_to_trash", AutomationLevel.AUTO) == AutomationLevel.APPLY_AFTER_APPROVAL
    assert "delete_email" not in {a.value for a in ActionType}


def test_only_uids_with_the_same_message_id_are_moved():
    expected = {1: "<a@x>", 2: "<b@x>", 3: "<c@x>", 4: ""}
    on_server = {1: "<a@x>", 2: "<other@x>", 4: None}       # 2 renumbered, 3 gone, 4 unknown
    assert matching_uids(expected, on_server) == [1]


# ------------------------------------------------------------------ IMAP writer with a fake server
class FakeImap:
    def __init__(self, caps=b"IMAP4rev1 MOVE UIDPLUS", validity=7, server=None, trash_flag=True):
        self.caps, self.validity, self.server = caps, validity, server or {}
        self.trash_flag = trash_flag
        self.commands: list[tuple] = []

    def capability(self):
        return "OK", [self.caps]

    def list(self):
        t = rb'(\HasNoChildren \Trash) "/" "Deleted Items"' if self.trash_flag else rb'(\HasNoChildren) "/" "Deleted Items"'
        return "OK", [rb'(\HasNoChildren) "/" "INBOX"', t]

    def status(self, name, what):
        return "OK", [f'{name} (MESSAGES 3 UIDNEXT 20 UIDVALIDITY {self.validity})'.encode()]

    def select(self, name, readonly):
        self.commands.append(("SELECT", name, readonly))
        return "OK", [b"3"]

    def uid(self, cmd, *args):
        self.commands.append((cmd, *args))
        if cmd == "FETCH":
            out = []
            for u in args[0].split(","):
                if int(u) in self.server:
                    out += [(f"1 (UID {u} BODY[HEADER.FIELDS (MESSAGE-ID)] {{30}}".encode(),
                             f"Message-ID: {self.server[int(u)]}\r\n\r\n".encode()), b")"]
            return "OK", out
        return "OK", [None]


def writer(fake) -> MailWriter:
    w = MailWriter("h", 1, "u", "p", "aa")
    w._m = fake
    return w


def test_writer_moves_verified_uids_to_trash_and_never_expunges():
    fake = FakeImap(server={11: "<a@x>", 12: "<other@x>"})
    moved = writer(fake).move_to_trash("INBOX", 7, {11: "<a@x>", 12: "<b@x>"})
    assert moved == [11]
    assert ("SELECT", '"INBOX"', False) in fake.commands
    assert ("MOVE", "11", '"Deleted Items"') in fake.commands
    flat = " ".join(str(c) for c in fake.commands).upper()
    assert "EXPUNGE" not in flat and "STORE" not in flat and "DELETED)" not in flat


def test_writer_refuses_without_move_support():
    fake = FakeImap(caps=b"IMAP4rev1 UIDPLUS", server={11: "<a@x>"})
    with pytest.raises(MailSourceError, match="MOVE"):
        writer(fake).move_to_trash("INBOX", 7, {11: "<a@x>"})
    assert not any(c[0] in ("MOVE", "SELECT") for c in fake.commands)


def test_writer_refuses_when_mailbox_was_renumbered():
    fake = FakeImap(validity=8, server={11: "<a@x>"})
    with pytest.raises(MailSourceError, match="UIDVALIDITY"):
        writer(fake).move_to_trash("INBOX", 7, {11: "<a@x>"})
    assert not any(c[0] == "MOVE" for c in fake.commands)


def test_trash_folder_by_flag_or_name():
    assert find_trash([MailboxInfo("Prullenbak", frozenset({"\\trash"})), MailboxInfo("Trash", frozenset())]) == "Prullenbak"
    assert find_trash([MailboxInfo("INBOX", frozenset()), MailboxInfo("Deleted Items", frozenset())]) == "Deleted Items"
    assert find_trash([MailboxInfo("INBOX", frozenset())]) is None


# ------------------------------------------------------------------ IMAP details
def test_xoauth2_and_imap_date():
    raw = xoauth2_string("me@company.com", "TOKEN")
    assert raw == b"user=me@company.com\x01auth=Bearer TOKEN\x01\x01"
    assert base64.b64encode(raw)   # imaplib encodes it
    assert imap_date(date(2026, 11, 3)) == "03-Nov-2026"


def test_source_modes():
    with pytest.raises(ValueError):
        ImapBridgeSource("h", 1, "u", "p", None, tls="plain")
    src = ImapBridgeSource("outlook.office365.com", 993, "u", None, None, tls="imaps", token_provider=lambda: "t")
    assert src.tls == "imaps"


def test_exchange_folders():
    lines = [rb'(\HasNoChildren) "/" INBOX', rb'(\HasNoChildren \Sent) "/" "Sent Items"',
             rb'(\HasNoChildren \Trash) "/" "Deleted Items"', rb'(\HasNoChildren \Junk) "/" "Junk Email"',
             rb'(\HasNoChildren \Drafts) "/" Drafts', rb'(\HasNoChildren \Archive) "/" Archive',
             rb'(\HasChildren) "/" Calendar', rb'(\HasNoChildren) "/" "Calendar/Birthdays"',
             rb'(\HasNoChildren) "/" "Conversation History"', rb'(\HasNoChildren) "/" Klanten']
    infos = [parse_list_line(x) for x in lines]
    acct = SimpleNamespace(kind="imap", sync_include=[], sync_exclude=[])
    inc, exc = sync_patterns(acct, SimpleNamespace())
    assert inc == ["*"] and exc == IMAP_DEFAULT_EXCLUDE
    chosen = [i.name for i in select_mailboxes(infos, inc, exc) if mailbox_role(i.name, i.flags) not in SKIP_ROLES]
    assert chosen == ["INBOX", "Sent Items", "Archive", "Klanten"]
    assert mailbox_role("Sent Items", {"\\sent"}) == "sent"


# ------------------------------------------------------------------ Microsoft device-code sign-in
class Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def opener_for(responses):
    calls = []

    def opener(req, timeout=30):
        calls.append((req.full_url, req.data.decode()))
        code, body = responses.pop(0)
        if code == 200:
            return Resp(json.dumps(body).encode())
        raise urllib.error.HTTPError(req.full_url, code, "err", {}, io.BytesIO(json.dumps(body).encode()))
    return opener, calls


ST = SimpleNamespace(microsoft_client_id="cid-123", microsoft_tenant="common")


def test_device_login_polls_until_signed_in():
    opener, calls = opener_for([
        (200, {"device_code": "DC", "user_code": "ABCD", "verification_uri": "https://microsoft.com/devicelogin",
               "interval": 1, "expires_in": 60, "message": "go sign in"}),
        (400, {"error": "authorization_pending"}),
        (400, {"error": "slow_down"}),
        (200, {"access_token": "AT", "refresh_token": "RT", "scope": "IMAP.AccessAsUser.All"})])
    said, slept = [], []
    tok = msauth.device_login(ST, said.append, opener=opener, sleep=slept.append, clock=lambda: 0.0)
    assert tok["refresh_token"] == "RT" and said == ["go sign in"]
    assert slept == [1.0, 1.0, 6.0]
    assert "/common/oauth2/v2.0/devicecode" in calls[0][0] and "IMAP.AccessAsUser.All" in calls[0][1]
    assert "client_secret" not in "".join(c[1] for c in calls)


def test_device_login_declined_and_missing_client_id():
    opener, _ = opener_for([(200, {"device_code": "DC", "interval": 1, "expires_in": 60}),
                            (400, {"error": "authorization_declined", "error_description": "no"})])
    with pytest.raises(msauth.MicrosoftAuthError, match="declined"):
        msauth.device_login(ST, lambda m: None, opener=opener, sleep=lambda s: None, clock=lambda: 0.0)
    with pytest.raises(msauth.MicrosoftAuthError, match="MICROSOFT_CLIENT_ID"):
        msauth.refresh(SimpleNamespace(microsoft_client_id="", microsoft_tenant="common"), "RT")


def test_refresh_errors_are_classified():
    opener, _ = opener_for([(400, {"error": "invalid_grant"})])
    with pytest.raises(msauth.MicrosoftAuthError, match="accounts authorize"):
        msauth.refresh(ST, "RT", opener=opener)
    opener, _ = opener_for([(503, {})])
    with pytest.raises(msauth.MicrosoftUnavailable):
        msauth.refresh(ST, "RT", opener=opener)
    opener, _ = opener_for([(200, {"access_token": "AT2", "refresh_token": "RT2"})])
    assert msauth.refresh(ST, "RT", opener=opener)["access_token"] == "AT2"
