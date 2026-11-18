"""IMAP access: Proton Bridge and other IMAP servers (e.g. Microsoft 365).

- Bridge (tls="starttls_pin"): STARTTLS with the Bridge certificate PINNED by SHA-256 fingerprint; the
  password is only sent after the pin matched (so a wrong/hostile listener never sees it).
- Other servers (tls="imaps"): implicit TLS with normal certificate + host name verification (system CAs);
  login with a password or OAuth2 (XOAUTH2, access token from a callable).
- ImapBridgeSource (sync) only uses EXAMINE (read-only) and BODY.PEEK: nothing can change flags or mark
  mail as read. MailWriter adds exactly one write operation: UID MOVE to the Trash folder (never EXPUNGE,
  never \\Deleted), used only for the user's own "Move to Trash" clicks (mail/trash.py).
"""
from __future__ import annotations

import hashlib
import hmac
import imaplib
import re
import ssl
from collections.abc import Callable, Iterator
from datetime import date

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


def imap_date(d: date) -> str:
    """IMAP date (RFC 3501): 03-Nov-2026 - month names are always English, independent of the locale."""
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    return f"{d.day:02d}-{months[d.month - 1]}-{d.year}"


def xoauth2_string(user: str, access_token: str) -> bytes:
    """SASL XOAUTH2 initial response (imaplib base64-encodes it)."""
    return f"user={user}\x01auth=Bearer {access_token}\x01\x01".encode()


def find_trash(infos: list[MailboxInfo]) -> str | None:
    for i in infos:
        if "\\trash" in i.flags:
            return i.name
    names = {i.name.lower(): i.name for i in infos}
    for cand in ("trash", "deleted items", "deleted messages", "prullenbak", "verwijderde items"):
        if cand in names:
            return names[cand]
    return None


def extract_message_id(header_bytes: bytes) -> str | None:
    m = _MSGID_RE.search(header_bytes)
    return re.sub(rb"\s+", b"", m.group(1)).decode("ascii", "replace").lower() if m else None


class ImapBridgeSource:
    def __init__(self, host: str, port: int, username: str, password: str | None,
                 cert_sha256: str | None, *, timeout: float = 120.0, tls: str = "starttls_pin",
                 token_provider: Callable[[], str] | None = None):
        if tls not in ("starttls_pin", "imaps"):
            raise ValueError(f"unknown tls mode {tls!r}")
        self.host, self.port, self.username = host, port, username
        self._password = password
        self._pin = (cert_sha256 or "").replace(":", "").lower()
        self.timeout = timeout
        self.tls = tls
        self._token_provider = token_provider   # OAuth2: returns a fresh access token
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
        if self.tls == "starttls_pin":
            self._m = self._connect_bridge()
        else:
            self._m = self._connect_imaps()
        self._selected = None

    def _connect_bridge(self) -> imaplib.IMAP4:
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
            self._login(m)
        except (OSError, imaplib.IMAP4.abort) as e:
            raise MailSourceUnavailable(str(e)) from e
        except imaplib.IMAP4.error as e:
            raise MailSourceAuthError(f"IMAP login failed: {e}") from e
        return m

    def _connect_imaps(self) -> imaplib.IMAP4:
        ctx = ssl.create_default_context()   # certificate chain + host name are verified
        try:
            m = imaplib.IMAP4_SSL(self.host, self.port, ssl_context=ctx, timeout=self.timeout)
        except ssl.SSLCertVerificationError as e:
            raise BridgeCertMismatch(f"certificate of {self.host} not trusted: {e}") from e
        except (OSError, imaplib.IMAP4.error) as e:
            raise MailSourceUnavailable(f"cannot connect to {self.host}:{self.port}: {e}") from e
        try:
            if self._pin:   # optional extra pin on top of normal verification
                fp = hashlib.sha256(m.sock.getpeercert(binary_form=True)).hexdigest()
                if not hmac.compare_digest(fp, self._pin):
                    m.shutdown()
                    raise BridgeCertMismatch(f"certificate fingerprint {fp} does not match the pinned value")
            self._login(m)
        except (OSError, imaplib.IMAP4.abort) as e:
            raise MailSourceUnavailable(str(e)) from e
        except imaplib.IMAP4.error as e:
            raise MailSourceAuthError(f"IMAP login failed: {e}") from e
        return m

    def _login(self, m: imaplib.IMAP4) -> None:
        if self._token_provider is not None:
            token = self._token_provider()
            raw = xoauth2_string(self.username, token)
            m.authenticate("XOAUTH2", lambda _challenge: raw)
        else:
            m.login(self.username, self._password or "")

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

    def search_uids(self, name: str, from_uid: int = 1, *, since: date | None = None) -> list[int]:
        self._examine(name)
        crit = ["UID", f"{from_uid}:*"]
        if since is not None:
            crit += ["SINCE", imap_date(since)]
        data = self._run(self._m.uid, "SEARCH", *crit, empty_ok=True)
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


class MailWriter(ImapBridgeSource):
    """The ONE write path: move messages to the account's Trash with UID MOVE (RFC 6851).

    No EXPUNGE, no \\Deleted flag, no fallback: a server without MOVE gets an error instead. Only UIDs whose
    Message-ID on the server equals the one in our database are moved (see mail/trash.matching_uids).
    """

    def capabilities(self) -> set[str]:
        data = self._run(self._m.capability)
        return {c.upper() for c in b" ".join(x for x in data if isinstance(x, bytes)).decode().split()}

    def move_to_trash(self, name: str, uidvalidity: int | None, expected: dict[int, str]) -> list[int]:
        from mailassistant.mail.trash import matching_uids

        if not expected:
            return []
        if "MOVE" not in self.capabilities():
            raise MailSourceError("this mail server does not support IMAP MOVE; nothing was changed")
        trash = find_trash(self.list_mailboxes())
        if trash is None:
            raise MailSourceError("no Trash folder found on the server; nothing was changed")
        if trash == name:
            return []
        st = self.status(name)
        if uidvalidity is not None and st.uidvalidity != uidvalidity:
            raise MailSourceError(f"{name!r} was renumbered (UIDVALIDITY changed); run a sync first")
        self._run(self._m.select, quote_mailbox(name), False)   # read-write: needed for MOVE
        self._selected = None   # force EXAMINE again for any later read
        data = self._run(self._m.uid, "FETCH", ",".join(map(str, sorted(expected))),
                         "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])", empty_ok=True)
        on_server = {uid: extract_message_id(body) for uid, body in parse_fetch_items(data)}
        uids = matching_uids(expected, on_server)
        if uids:
            self._run(self._m.uid, "MOVE", ",".join(map(str, uids)), quote_mailbox(trash))
        return uids
