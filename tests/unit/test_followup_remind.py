from datetime import UTC, date, datetime

from mailassistant.followups.remind import add_working_days, remind_at

TZ = "Europe/Amsterdam"
FRI = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)      # Friday


def test_working_days_skip_weekends():
    assert add_working_days(date(2026, 11, 13), 2) == date(2026, 11, 17)     # Fri + 2 = Tue
    assert add_working_days(date(2026, 11, 16), -1) == date(2026, 11, 13)    # Mon - 1 = Fri


def test_estimated_reminders_per_direction():
    assert remind_at(direction="I_OWE", sent_at=FRI, deadline_at=None, tz=TZ) == \
        (datetime(2026, 11, 17, 7, 0, tzinfo=UTC), "estimated")              # Tue 08:00 CET
    assert remind_at(direction="THEY_OWE", sent_at=FRI, deadline_at=None, tz=TZ)[0] == \
        datetime(2026, 11, 20, 7, 0, tzinfo=UTC)                             # Fri + 5 working days


def test_explicit_deadline_reminds_one_working_day_before():
    deadline = datetime(2026, 11, 23, 16, 0, tzinfo=UTC)                    # Monday 17:00
    assert remind_at(direction="I_OWE", sent_at=FRI, deadline_at=deadline, tz=TZ) == \
        (datetime(2026, 11, 20, 7, 0, tzinfo=UTC), "explicit")               # the Friday before
    same_day = datetime(2026, 11, 13, 16, 0, tzinfo=UTC)
    assert remind_at(direction="I_OWE", sent_at=FRI, deadline_at=same_day, tz=TZ)[0] == \
        datetime(2026, 11, 13, 7, 0, tzinfo=UTC)                             # never before the mail's day
    early = datetime(2026, 11, 13, 6, 30, tzinfo=UTC)                        # 07:30 local, before 08:00
    assert remind_at(direction="I_OWE", sent_at=datetime(2026, 11, 12, 9, tzinfo=UTC), deadline_at=early,
                     tz=TZ)[0] <= early


def test_summer_time():
    summer = datetime(2026, 6, 5, 10, 0, tzinfo=UTC)                         # Friday, CEST
    assert remind_at(direction="I_OWE", sent_at=summer, deadline_at=None, tz=TZ)[0] == \
        datetime(2026, 6, 9, 6, 0, tzinfo=UTC)                               # 08:00 CEST
