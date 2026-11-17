"""Which Bridge mailboxes get synchronised, and what role each has. Pure functions."""
from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatchcase

from mailassistant.mail.source import MailboxInfo


def mailbox_role(name: str, flags: Iterable[str] = ()) -> str:
    f = {x.lower() for x in flags}
    if name.upper() == "INBOX":
        return "inbox"
    if "\\sent" in f:
        return "sent"
    if "\\archive" in f or name == "Archive" or name.startswith("Folders/Archives"):
        return "archive"
    for flag, role in (("\\junk", "spam"), ("\\trash", "trash"), ("\\drafts", "drafts"),
                       ("\\all", "all"), ("\\flagged", "starred")):
        if flag in f:
            return role
    if name.startswith("Labels/"):
        return "label"
    if name.startswith("Folders/"):
        return "folder"
    return "other"


def select_mailboxes(infos: Iterable[MailboxInfo], include: Iterable[str],
                     exclude: Iterable[str] = ()) -> list[MailboxInfo]:
    inc, exc = list(include), list(exclude)
    return [i for i in infos
            if "\\noselect" not in i.flags
            and any(fnmatchcase(i.name, p) for p in inc)
            and not any(fnmatchcase(i.name, p) for p in exc)]


def sync_priority(info: MailboxInfo) -> int:
    return {"inbox": 0, "sent": 1}.get(mailbox_role(info.name, info.flags), 2)
