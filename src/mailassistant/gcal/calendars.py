"""Turn the calendars named in .env (name or ID) into Google calendar IDs. Pure functions, standard library only.

Never guesses: an unknown or ambiguous name is an error with the alternatives listed, and a calendar the user
cannot edit is refused as a place to create events.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

WRITABLE = ("owner", "writer")


@dataclass
class Resolved:
    value: str                       # what .env says
    id: str | None = None
    name: str | None = None          # display name (the user's own label first)
    access_role: str | None = None
    error: str | None = None

    def ok(self) -> bool:
        return self.id is not None and self.error is None

    def as_dict(self) -> dict:
        return asdict(self)


def looks_like_id(value: str) -> bool:
    v = value.strip()
    return v == "primary" or "@" in v


def display_name(entry: dict) -> str:
    return entry.get("summaryOverride") or entry.get("summary") or entry.get("id", "?")


def _live(calendar_list: list[dict]) -> list[dict]:
    return [c for c in calendar_list if not c.get("deleted")]


def resolve(value: str, calendar_list: list[dict] | None, *, need_write: bool) -> Resolved:
    """calendar_list None = the list could not be read (permission missing): only IDs can be used then."""
    v = (value or "").strip()
    r = Resolved(v)
    if not v:
        r.error = "not configured"
        return r
    if calendar_list is None:
        if looks_like_id(v):
            r.id = v
            r.name = "primary calendar" if v == "primary" else v
            return r                 # access unknown; Google itself refuses a write it does not allow
        r.error = "calendar names need the calendar-list permission: run gcal.authorize again (or use the ID)"
        return r
    cals = _live(calendar_list)
    if v == "primary":
        hit = [c for c in cals if c.get("primary")]
    else:
        hit = [c for c in cals if c.get("id") == v]
        if not hit and not looks_like_id(v):
            low = v.casefold()
            own = [c for c in cals if (c.get("summaryOverride") or "").casefold() == low]
            hit = own or [c for c in cals if (c.get("summary") or "").casefold() == low]
    if not hit:
        if looks_like_id(v):         # a valid ID the list does not show (e.g. not subscribed): trust it
            r.id, r.name = v, v
            return r
        names = ", ".join(sorted({display_name(c) for c in cals})) or "none"
        r.error = f"no calendar named {v!r} (available: {names})"
        return r
    if len(hit) > 1:
        r.error = f"{v!r} matches {len(hit)} calendars ({', '.join(c['id'] for c in hit)}): use the ID"
        return r
    c = hit[0]
    r.id, r.name, r.access_role = c["id"], display_name(c), c.get("accessRole")
    if need_write and r.access_role not in WRITABLE:
        r.error = f"you can only view {r.name!r} ({r.access_role}); events cannot be created there"
    return r


def read_set(work: Resolved, private: Resolved | None, conflict: list[str], calendar_list: list[dict] | None
             ) -> tuple[list[str], list[Resolved]]:
    """IDs to sync and check for conflicts, and the resolved conflict calendars (with their errors).

    conflict == []        -> work + private + every own calendar (owner/writer)
    conflict == ["none"]  -> work + private only
    otherwise             -> work + private + exactly the listed calendars (names or IDs, may be read-only)
    Without the calendar list only IDs can be resolved; own calendars cannot be discovered."""
    ids = [r.id for r in (work, private) if r is not None and r.id]
    listed = [c.strip() for c in conflict if c.strip()]
    resolved: list[Resolved] = []
    if not listed:
        if calendar_list is not None:
            ids += [c["id"] for c in _live(calendar_list) if c.get("accessRole") in WRITABLE]
    elif [c.lower() for c in listed] != ["none"]:
        for c in listed:
            x = resolve(c, calendar_list, need_write=False)
            resolved.append(x)
            if x.ok():
                ids.append(x.id)
    return list(dict.fromkeys(ids)), resolved
