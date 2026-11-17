"""Which calendar an approved invitation should go to: the default (work) calendar or the shared (personal)
one (GOOGLE_PRIVATE_CALENDAR). Only a SUGGESTION - the user picks the calendar when approving. Pure function, standard library only."""
from __future__ import annotations

WORK, PRIVATE = "work", "private"


def suggest_calendar(*, to_addrs: list[str], cc_addrs: list[str], work_addresses: list[str],
                     sender_work: int = 0, sender_personal: int = 0) -> tuple[str, str]:
    """(target, reason). Mail to a work address -> work. Otherwise personal, unless earlier mail from this
    sender was mostly classified as work (a known work contact writing to the private address)."""
    rcpt = {a.lower() for a in [*to_addrs, *cc_addrs]}
    hit = sorted(rcpt & {a.lower() for a in work_addresses})
    if hit:
        return WORK, f"sent to {hit[0]}"
    if sender_work > sender_personal:
        return WORK, f"sender is a work contact ({sender_work} work mail(s) earlier)"
    return PRIVATE, "sent to your personal address"
