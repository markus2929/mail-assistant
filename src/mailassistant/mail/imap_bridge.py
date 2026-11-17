"""Read-only IMAP access to Proton Bridge.

- STARTTLS with the Bridge certificate PINNED by SHA-256 fingerprint; the password is only sent
  after the pin matched (so a wrong/hostile listener never sees it).
- Only EXAMINE (read-only) and BODY.PEEK are used: nothing can change flags or mark mail as read.
"""
from __future__ import annotations

import hashlib
import hmac
import imaplib
import re
import ssl
from collections.abc import Iterator

from mailassistant.mail.source import (BridgeCertMismatch, MailboxInfo, MailboxStatus,
                                       MailSourceAuthError, MailSourceError,
                                       MailSourceUnavailable)

_LIST_RE = re.compile(rb'\((?P<flags>[^)]*)\)\s+(?:"(?P<delim>[^"]*)"|NIL)\s+(?P<name>.+?)\s*$')
_UID_RE = re.compile(rb"\bUID (\d+)")
_MSGID_RE = re.compile(rb"(?is)message-id:\s*(<[^<>]+>)")
_STATUS_RE = re.compile(r"(MESSAGES|UIDNEXT|UIDVALIDITY) (\d+)")


def _unquote(raw: bytes) -> str:
    s = raw.strip()
    if s.startswith(b'"') and s.endswith(b'"') and len(s) >= 2:
        s = re.sub(rb"\\(.)", rb"\1", s[1:-1])
    return s.decode("utf-8", errors="replace")


def quote_mailbox(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_list_line(raw: bytes) -> MailboxInfo | None:
    m = _LIST_RE.match(raw)
    if not m:
        return None
    flags = frozenset(f.lower() for f in m["flags"].decode().split())
    delim = m["delim"].decode() if m["delim"] is not None else None
    return MailboxInfo(name=_unquote(m["name"]), flags=flags, delimiter=delim)


def parse_fetch_items(data) -> list[tuple[int, bytes]]:
    """Turn imaplib FETCH data into (uid, literal) pairs.

    The UID may come before the literal (in the tuple's first element) or after it (in the
    trailing bytes item) depending on the server, so both places are checked.
    """
    out: list[tuple[int, bytes]] = []
    for i, item in enumerate(data):
        if not isinstance(item, tuple):
            continue
        m = _UID_RE.search(item[0])
        if not m and i + 1 < len(data) and isinstance(data[i + 1], bytes):
            m = _UID_RE.search(data[i + 1])
        if m:
            out.append((int(m.group(1)), item[1]))
    return out


def extract_message_id(header_bytes: bytes) -> str | None:
    m = _MSGID_RE.search(header_bytes)
    return re.sub(rb"\s+", b"", m.group(1)).decode("ascii", "replace").lower() if m else None


class ImapBridgeSource:
    def __init__(self, host: str, port: int, username: str, password: str,
                 cert_sha256: str | None, *, timeout: float = 120.0):
        self.host, self.port, self.username = host, port, username
        self._password = password
        self._pin = (cert_sha256 or "").replace(":", "").lower()
        self.timeout = timeout
        self._m: imaplib.IMAP4 | None = None
        self._selected: str | None = None

    @classmethod
    def from_settings(cls, s) -> ImapBridgeSource:
        if not (s.imap_username and s.imap_password):
            raise MailSourceError("IMAP_USERNAME / secrets/imap_password not configured")
        return cls(s.imap_host, s.imap_port, s.imap_username, s.imap_password.get_secret_value(),
                   s.bridge_cert_sha256)

    # ---- connection -------------------------------------------------------------------
    def __enter__(self) -> ImapBridgeSource:
        self._connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._m is not None:
            try:
                self._m.logout()
            except Exception:  # noqa: BLE001
                pass
            self._m = None

    def _connect(self) -> None:
        if not self._pin:
            raise MailSourceError(
                "BRIDGE_CERT_SHA256 is not set. Run scripts/imap_probe.py and put the printed "
                "fingerprint in .env")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # replaced by the fingerprint pin below
        try:
            m = imaplib.IMAP4(self.host, self.port, timeout=self.timeout)
        except (OSError, imaplib.IMAP4.error) as e:
            raise MailSourceUnavailable(f"cannot connect to Bridge at {self.host}:{self.port}: {e}") from e
        try:
            m.starttls(ssl_context=ctx)
            fp = hashlib.sha256(m.sock.getpeercert(binary_form=True)).hexdigest()
            if not hmac.compare_digest(fp, self._pin):
                m.shutdown()  # close without sending anything (no credentials leave this process)
                raise BridgeCertMismatch(
                    f"Bridge certificate fingerprint {fp} does not match the pinned value")
            m.login(self.username, self._password)
        except (OSError, imaplib.IMAP4.abort) as e:
            raise MailSourceUnavailable(str(e)) from e
        except imaplib.IMAP4.error as e:
            raise MailSourceAuthError(f"IMAP login failed: {e}") from e
        self._m, self._selected = m, None

    def _run(self, fn, *args, empty_ok: bool = False):
        try:
            typ, data = fn(*args)
        except (OSError, imaplib.IMAP4.abort) as e:
            raise MailSourceUnavailable(str(e)) from e
        except imaplib.IMAP4.error as e:
            raise MailSourceError(str(e)) from e
        if typ != "OK":
            if empty_ok and typ == "NO" and "no such message" in repr(data).lower():
                return []  # Bridge answers NO for a UID range that matches nothing
            raise MailSourceError(f"IMAP {typ}: {data!r}")
        return data

    def _examine(self, name: str) -> None:
        if self._selected != name:
            self._run(self._m.select, quote_mailbox(name), True)  # True = readonly (EXAMINE)
            self._selected = name

    # ---- MailSource -------------------------------------------------------------------
    def list_mailboxes(self) -> list[MailboxInfo]:
        data = self._run(self._m.list)
        infos = (parse_list_line(raw) for raw in data if isinstance(raw, bytes))
        return [i for i in infos if i is not None]

    def status(self, name: str) -> MailboxStatus:
        data = self._run(self._m.status, quote_mailbox(name), "(MESSAGES UIDNEXT UIDVALIDITY)")
        nums = dict(_STATUS_RE.findall(data[0].decode()))
        try:
            return MailboxStatus(int(nums["MESSAGES"]), int(nums["UIDNEXT"]), int(nums["UIDVALIDITY"]))
        except KeyError as e:
            raise MailSourceError(f"unexpected STATUS response for {name!r}: {data!r}") from e

    def search_uids(self, name: str, from_uid: int = 1) -> list[int]:
        self._examine(name)
        data = self._run(self._m.uid, "SEARCH", "UID", f"{from_uid}:*", empty_ok=True)
        raw = (data[0] if data else None) or b""
        uids = [int(x) for x in raw.split()]
        return sorted(u for u in uids if u >= from_uid)  # "n:*" always returns the last message

    def fetch_message_ids(self, name: str, uids: list[int]) -> dict[int, str | None]:
        if not uids:
            return {}
        self._examine(name)
        data = self._run(self._m.uid, "FETCH", ",".join(map(str, uids)),
                         "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])", empty_ok=True)
        return {uid: extract_message_id(body) for uid, body in parse_fetch_items(data)}

    def fetch_raw(self, name: str, uids: list[int]) -> Iterator[tuple[int, bytes]]:
        if not uids:
            return iter(())
        self._examine(name)
        data = self._run(self._m.uid, "FETCH", ",".join(map(str, uids)), "(UID BODY.PEEK[])", empty_ok=True)
        return iter(parse_fetch_items(data))
