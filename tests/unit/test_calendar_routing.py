from mailassistant.gcal.routing import PRIVATE, WORK, suggest_calendar

WORK_ADDR = ["info@example-business.com", "orders@example-business.com"]


def test_work_address_means_work_calendar():
    assert suggest_calendar(to_addrs=["Info@Example-Business.com"], cc_addrs=[], work_addresses=WORK_ADDR)[0] == WORK
    assert suggest_calendar(to_addrs=["me@example.com"], cc_addrs=["info@example-business.com"],
                            work_addresses=WORK_ADDR)[0] == WORK


def test_personal_address_is_private_unless_sender_is_a_work_contact():
    kw = dict(to_addrs=["me@example.com"], cc_addrs=[], work_addresses=WORK_ADDR)
    assert suggest_calendar(**kw) == (PRIVATE, "sent to your personal address")
    assert suggest_calendar(**kw, sender_work=3, sender_personal=1)[0] == WORK
    assert suggest_calendar(**kw, sender_work=1, sender_personal=1)[0] == PRIVATE


# ------------------------------------------------------------------ names and IDs in .env
from mailassistant.config import Settings  # noqa: E402
from mailassistant.gcal.calendars import looks_like_id, read_set, resolve  # noqa: E402

CALS = [{"id": "alex@gmail.example", "primary": True, "summary": "alex@gmail.example", "accessRole": "owner"},
        {"id": "fam@group.calendar.google.com", "summary": "Gezin", "summaryOverride": "Familie", "accessRole": "writer"},
        {"id": "nl#holiday@group.v.calendar.google.com", "summary": "Feestdagen", "accessRole": "reader"},
        {"id": "old@group.calendar.google.com", "summary": "Oud", "accessRole": "owner", "deleted": True}]


def test_resolve_names_and_ids():
    assert looks_like_id("primary") and looks_like_id("x@group.calendar.google.com") and not looks_like_id("Familie")
    assert resolve("primary", CALS, need_write=True).id == "alex@gmail.example"
    r = resolve("familie", CALS, need_write=True)                   # own label, any case
    assert (r.id, r.name, r.error) == ("fam@group.calendar.google.com", "Familie", None)
    assert resolve("Gezin", CALS, need_write=True).id == "fam@group.calendar.google.com"   # Google's own name too
    assert "only view" in resolve("Feestdagen", CALS, need_write=True).error
    assert resolve("Feestdagen", CALS, need_write=False).ok()
    assert "available: Familie, Feestdagen, alex@gmail.example" in resolve("Werk", CALS, need_write=True).error
    assert resolve("Oud", CALS, need_write=False).error                # deleted calendars are ignored
    assert resolve("x@group.calendar.google.com", CALS, need_write=True).id == "x@group.calendar.google.com"
    assert "permission" in resolve("Familie", None, need_write=True).error
    assert resolve("primary", None, need_write=True).id == "primary"


def test_conflict_calendars_default_none_and_explicit_list():
    work, private = resolve("primary", CALS, need_write=True), resolve("Familie", CALS, need_write=True)
    assert read_set(work, private, [], CALS)[0] == ["alex@gmail.example", "fam@group.calendar.google.com"]
    assert read_set(work, None, ["none"], CALS)[0] == ["alex@gmail.example"]
    ids, res = read_set(work, None, ["Feestdagen", " ", "Bestaat niet"], CALS)   # exactly the listed ones
    assert ids == ["alex@gmail.example", "nl#holiday@group.v.calendar.google.com"]
    assert [r.ok() for r in res] == [True, False] and "no calendar named" in res[1].error
    assert read_set(resolve("primary", None, need_write=True), None, ["a@group.calendar.google.com"], None)[0] == \
        ["primary", "a@group.calendar.google.com"]


def test_old_setting_names_still_work():
    base = dict(_env_file=None, database_url="postgresql+psycopg://x:y@h/d")
    s = Settings(**base, google_calendar_ids="w@g,x@g", google_shared_calendar_id="p@g")
    assert (s.google_work_calendar, s.google_private_calendar, s.google_conflict_calendars) == ("w@g", "p@g", ["x@g"])
    s = Settings(**base, google_conflict_calendars="Family, Shared calendar,Club")
    assert s.google_conflict_calendars == ["Family", "Shared calendar", "Club"]
    assert Settings(**base, google_extra_calendars="a@g").google_conflict_calendars == ["a@g"]
    s = Settings(**base, google_calendar_ids="w@g", google_work_calendar="Werk")
    assert s.google_work_calendar == "Werk"
    assert Settings(**base).google_work_calendar == "primary" and Settings(**base).google_private_calendar == ""
