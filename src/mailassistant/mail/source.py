"""Interface between the sync engine and a mail server. Nothing here talks to the network."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol


class MailSourceError(Exception):
    """Base class."""


class MailSourceUnavailable(MailSourceError):
    """Transient: Bridge not running / connection lost. Callers defer and retry."""


class MailSourceAuthError(MailSourceError):
    """Login refused (wrong Bridge password). Not transient."""


class BridgeCertMismatch(MailSourceError):
    """The server certificate is not the pinned one. Nothing was sent to it."""


@dataclass(frozen=True)
class MailboxInfo:
    name: str
    flags: frozenset[str]  # lower-cased, e.g. {"\\sent", "\\noselect"}
    delimiter: str | None = None


@dataclass(frozen=True)
class MailboxStatus:
    exists: int
    uidnext: int
    uidvalidity: int


class MailSource(Protocol):
    def list_mailboxes(self) -> list[MailboxInfo]: ...
    def status(self, name: str) -> MailboxStatus: ...
    def search_uids(self, name: str, from_uid: int = 1) -> list[int]: ...
    def fetch_message_ids(self, name: str, uids: list[int]) -> dict[int, str | None]: ...
    def fetch_raw(self, name: str, uids: list[int]) -> Iterator[tuple[int, bytes]]: ...
