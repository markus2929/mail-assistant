from datetime import UTC, datetime, timedelta

from mailassistant.gcal.conflicts import (CalEvent, Candidate, find_conflicts, find_duplicates,
                                          hard_conflicts, is_physical_location)

T = datetime(2026, 11, 12, 14, 0, tzinfo=UTC)


def ev(start_min, end_min, **kw):
    return CalEvent(id=kw.pop("id", "e"), calendar_id="primary", summary=kw.pop("summary", "Iets"),
                    start=T + timedelta(minutes=start_min), end=T + timedelta(minutes=end_min), **kw)


def cand(start_min=0, end_min=60, **kw):
    return Candidate(T + timedelta(minutes=start_min), T + timedelta(minutes=end_min), **kw)


def types(c, events, **kw):
    return [x["type"] for x in find_conflicts(c, events, **kw)]


def test_overlaps_and_adjacency():
    assert types(cand(), [ev(30, 90)]) == ["overlap"]
    assert types(cand(), [ev(-30, 10)]) == ["overlap"] and types(cand(), [ev(10, 20)]) == ["overlap"]
    assert types(cand(), [ev(60, 120)]) == [] and types(cand(), [ev(-60, 0)]) == []   # back-to-back is fine


def test_non_blocking_events_are_ignored():
    for kw in ({"transparent": True}, {"declined": True}, {"status": "cancelled"}):
        assert types(cand(), [ev(0, 60, **kw)]) == [], kw


def test_travel_buffer_only_between_different_physical_places():
    a, b = "Kantoor Eindhoven", "Boerderij Bestown"
    assert types(cand(location=a), [ev(-45, -10, location=b)]) == ["tight"]
    assert types(cand(location=a), [ev(-45, -30, location=b)]) == []                 # 30 min gap = enough
    assert types(cand(location=a), [ev(-45, -10, location=a.upper())]) == []         # same place
    assert types(cand(location="Teams-meeting"), [ev(-45, -10, location=b)]) == []   # online: no travel
    assert types(cand(location=a), [ev(-45, -10)]) == []                              # unknown place
    assert types(cand(location=a), [ev(-45, -10, location=b)], buffer_minutes=5) == []
    assert not is_physical_location("https://zoom.us/j/1") and is_physical_location("Markt 1, Eindhoven")


def test_all_day_candidate_only_informs():
    day = Candidate(T.replace(hour=0), T.replace(hour=0) + timedelta(days=1), all_day=True)
    found = find_conflicts(day, [ev(0, 60)])
    assert [c["type"] for c in found] == ["same_day"] and hard_conflicts(found) == []


def test_all_day_busy_event_blocks_timed_candidate():
    block = CalEvent("o", "primary", "Verlof", T.replace(hour=0), T.replace(hour=0) + timedelta(days=1), all_day=True)
    assert types(cand(), [block]) == ["overlap"]


def test_duplicates_by_uid_key_and_similarity():
    c = Candidate(T, T + timedelta(hours=1), title="Projectoverleg fase 2", ics_uid="abc-123@x.com", key="k1")
    assert find_duplicates(c, [ev(0, 60, ical_uid="abc-123@google.com")])
    assert find_duplicates(c, [ev(0, 60, summary="Other", private={"mail_assistant_key": "k1"})])
    assert find_duplicates(c, [ev(2, 62, summary="Projectoverleg fase 2 (kantoor)")])
    assert not find_duplicates(c, [ev(0, 60, summary="Tandarts")])
    assert not find_duplicates(c, [ev(300, 360, summary="Projectoverleg fase 2")])
    assert not find_duplicates(c, [ev(0, 60, summary="Projectoverleg fase 2", status="cancelled")])


def test_same_appointment_across_emails():
    from mailassistant.gcal.conflicts import same_appointment, similar_titles
    tz = "Europe/Amsterdam"
    evening = datetime(2026, 12, 11, 18, 30, tzinfo=UTC)
    day = datetime(2026, 12, 10, 23, 0, tzinfo=UTC)           # 11 Dec 00:00 Amsterdam (all-day)
    timed = Candidate(evening, evening + timedelta(hours=3), title="Tasting clubavond")
    allday = Candidate(day, day + timedelta(days=1), all_day=True, title="Clubavond")
    assert same_appointment(timed, allday, tz) and same_appointment(allday, timed, tz)
    assert not same_appointment(timed, Candidate(evening, evening, title="Tandarts"), tz)
    later = Candidate(evening + timedelta(hours=2), evening + timedelta(hours=3), title="Clubavond")
    assert not same_appointment(timed, later, tz)                # same title, different time: keep both
    next_day = Candidate(day + timedelta(days=1), day + timedelta(days=2), all_day=True, title="Clubavond")
    assert not same_appointment(timed, next_day, tz)
    assert not similar_titles("Afspraak 22 december", "Uitnodiging 22 december")   # only noise words in common
