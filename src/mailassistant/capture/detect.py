"""Is a mail one the user sent to themself to plan something? Pure functions, standard library only."""
from __future__ import annotations

import re

EVENT_TAGS = {"agenda", "afspraak", "afspraken", "calendar", "event", "plan"}
FOLLOWUP_TAGS = {"todo", "taak", "taken", "followup", "follow-up", "opvolgen", "herinnering"}
_SUBJECT = re.compile(r"^\s*(agenda|afspraak|todo|taak|follow-?up)\s*:", re.I)


def kind_from_address(addr: str) -> str:
    local = addr.split("@", 1)[0].lower()
    tag = local.split("+", 1)[1] if "+" in local else ""
    if tag in EVENT_TAGS:
        return "event"
    if tag in FOLLOWUP_TAGS:
        return "followup"
    return "auto"


def capture_kind(*, to_addrs: list[str], cc_addrs: list[str], subject: str | None,
                 capture_addresses: list[str], own_addresses: list[str]) -> str | None:
    """The kind (event|followup|auto) if this mail - sent BY the user, checked by the caller - is a capture.

    1. sent to one of CAPTURE_ADDRESSES (e.g. me+agenda@…): kind from the plus-tag;
    2. or sent only to own addresses with a subject like "agenda: …" / "todo: …"."""
    rcpt = [a.lower() for a in [*to_addrs, *cc_addrs] if a]
    caps = {a.lower() for a in capture_addresses}
    hit = [a for a in rcpt if a in caps]
    if hit:
        kinds = {kind_from_address(a) for a in hit}
        return kinds.pop() if len(kinds) == 1 else "auto"
    own = {a.lower() for a in own_addresses} | caps
    m = _SUBJECT.match(subject or "")
    if m and rcpt and all(a in own for a in rcpt):
        return "event" if m[1].lower() in ("agenda", "afspraak") else "followup"
    return None


def strip_subject_tag(subject: str | None) -> str:
    return _SUBJECT.sub("", subject or "", count=1).strip()
