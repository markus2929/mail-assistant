from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from mailassistant.gcal.ics import parse_ics

AMS = ZoneInfo("Europe/Amsterdam")


def one(text, **kw):
    evs = parse_ics(text, **kw)
    assert len(evs) == 1, evs
    return evs[0]


GOOGLE = """BEGIN:VCALENDAR\r
METHOD:REQUEST\r
BEGIN:VEVENT\r
UID:abc-123@google.com\r
DTSTART;TZID=Europe/Amsterdam:20261112T140000\r
DTEND;TZID=Europe/Amsterdam:20261112T150000\r
SUMMARY:Projectoverleg\\, fase 2\r
LOCATION:Kantoor Eindhoven\r
DESCRIPTION:Agenda volgt.\\nJoin: https://meet.google.com/abc-defg-hij\r
ORGANIZER;CN=Piet de Vries:mailto:Piet@Example.nl\r
ATTENDEE;CN="Alex; Example";PARTSTAT=NEEDS-ACTION;ROLE=REQ-PARTICIPANT:mailto:mail@example.nl\r
SEQUENCE:2\r
STATUS:CONFIRMED\r
END:VEVENT\r
END:VCALENDAR\r
"""


def test_google_style_invitation():
    e = one(GOOGLE)
    assert (e.uid, e.method, e.summary, e.sequence, e.status) == ("abc-123@google.com", "REQUEST",
                                                                  "Projectoverleg, fase 2", 2, "CONFIRMED")
    assert e.start == datetime(2026, 11, 12, 14, 0, tzinfo=AMS) and e.end.hour == 15
    assert (e.tz_name, e.all_day, e.floating) == ("Europe/Amsterdam", False, False)
    assert e.organizer == {"email": "piet@example.nl", "name": "Piet de Vries", "partstat": None, "role": None}
    assert e.attendees[0]["name"] == "Alex; Example" and e.attendees[0]["email"] == "mail@example.nl"
    assert e.meeting_url == "https://meet.google.com/abc-defg-hij" and "\n" in e.description


def test_outlook_windows_timezone_and_line_folding():
    text = ("BEGIN:VCALENDAR\nMETHOD:REQUEST\nBEGIN:VTIMEZONE\nTZID:W. Europe Standard Time\nBEGIN:STANDARD\n"
            "DTSTART:16011028T030000\nTZOFFSETFROM:+0200\nTZOFFSETTO:+0100\nEND:STANDARD\nEND:VTIMEZONE\n"
            "BEGIN:VEVENT\nUID:outlook-1\nDTSTART;TZID=W. Europe Standard Time:20260715T100000\n"
            "DTEND;TZID=W. Europe Standard Time:20260715T110000\nSUMMARY:Een heel lange titel die door Outlo\n"
            " ok wordt afgebroken\nBEGIN:VALARM\nTRIGGER:-PT15M\nEND:VALARM\nEND:VEVENT\nEND:VCALENDAR\n")
    e = one(text)
    assert e.summary == "Een heel lange titel die door Outlook wordt afgebroken"
    assert e.tz_name == "Europe/Amsterdam" and e.tz_original == "W. Europe Standard Time"
    assert e.start.utcoffset().total_seconds() == 7200            # summer time handled by zoneinfo


def test_utc_all_day_floating_and_duration():
    utc = one("BEGIN:VEVENT\nUID:u\nDTSTART:20261112T130000Z\nDURATION:PT90M\nEND:VEVENT")
    assert utc.start == datetime(2026, 11, 12, 13, 0, tzinfo=UTC) and utc.end == datetime(2026, 11, 12, 14, 30, tzinfo=UTC)
    day = one("BEGIN:VEVENT\nUID:d\nDTSTART;VALUE=DATE:20261224\nDTEND;VALUE=DATE:20261226\nEND:VEVENT")
    assert day.all_day and day.start == date(2026, 12, 24) and day.end == date(2026, 12, 26)
    fl = one("BEGIN:VEVENT\nUID:f\nDTSTART:20261112T140000\nEND:VEVENT", default_tz="Europe/Amsterdam")
    assert fl.floating and "floating_time_assumed_default_tz" in fl.issues and "end_missing_default_duration" in fl.issues
    assert fl.start == datetime(2026, 11, 12, 14, 0, tzinfo=AMS) and fl.end.hour == 15


def test_methods_recurrence_and_bad_input():
    assert one(GOOGLE.replace("REQUEST", "CANCEL")).method == "CANCEL"
    r = one("BEGIN:VEVENT\nUID:r\nDTSTART:20261112T130000Z\nRRULE:FREQ=WEEKLY;COUNT=4\nEND:VEVENT")
    assert r.rrule == ["FREQ=WEEKLY;COUNT=4"]
    unknown = one("BEGIN:VEVENT\nUID:x\nDTSTART;TZID=Mars/Olympus:20261112T130000\nEND:VEVENT")
    assert any(i.startswith("unknown_tzid") for i in unknown.issues)
    assert parse_ics("not a calendar") == []
    assert parse_ics("BEGIN:VEVENT\nUID:nodate\nEND:VEVENT") == []
    assert parse_ics("BEGIN:VEVENT\nUID:b\nDTSTART:garbage\nEND:VEVENT")[0].issues[0].startswith("unparseable")
