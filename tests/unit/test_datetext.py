from datetime import date, time

from mailassistant.gcal.datetext import resolve_date, resolve_time

REF = date(2026, 11, 9)   # a Monday


def d(text, ref=REF):
    r = resolve_date(text, ref)
    return r.value, r.issues


def test_explicit_dates_dutch_and_english():
    assert d("donderdag 12 november 2026") == (date(2026, 11, 12), [])
    assert d("12 nov 2026")[0] == date(2026, 11, 12)
    assert d("12-11-2026")[0] == date(2026, 11, 12)           # day-first
    assert d("2026-11-12")[0] == date(2026, 11, 12)
    assert d("November 12th, 2026")[0] == date(2026, 11, 12)
    assert d("1e december 2026")[0] == date(2026, 12, 1)
    assert d("31-02-2026")[0] is None                          # impossible date


def test_year_is_inferred_forward_and_flagged():
    assert d("12 november") == (date(2026, 11, 12), ["year_inferred"])
    assert d("5 januari", date(2026, 12, 20))[0] == date(2027, 1, 5)
    assert d("12/11")[0] == date(2026, 11, 12)


def test_relative_expressions():
    assert d("morgen")[0] == date(2026, 11, 10) and d("tomorrow")[0] == date(2026, 11, 10)
    assert d("overmorgen")[0] == date(2026, 11, 11) and d("vandaag")[0] == REF
    assert d("donderdag")[0] == date(2026, 11, 12)             # next occurrence after the mail
    assert d("maandag")[0] == date(2026, 11, 16)                # same weekday = a week later
    assert d("volgende week donderdag")[0] == date(2026, 11, 19)
    assert d("next week friday")[0] == date(2026, 11, 20)
    assert d("next thursday") == (date(2026, 11, 12), ["ambiguous_next_weekday"])


def test_weekday_mismatch_is_reported_not_corrected():
    value, issues = d("vrijdag 12 november 2026")             # the 12th is a Thursday
    assert value == date(2026, 11, 12) and "weekday_mismatch" in issues


def test_no_date():
    assert d("binnenkort") == (None, ["no_date_found"]) and d("")[0] is None


def t(text):
    r = resolve_time(text)
    return r.start, r.end, r.issues


def test_times():
    assert t("14:00")[0] == time(14, 0) and t("om 14.30 uur")[0] == time(14, 30)
    assert t("14u")[0] == time(14, 0) and t("14u30")[0] == time(14, 30) and t("9 uur")[0] == time(9, 0)
    assert t("2pm")[0] == time(14, 0) and t("2:30 PM")[0] == time(14, 30) and t("12am")[0] == time(0, 0)
    assert t("3 uur 's middags")[0] == time(15, 0)
    assert t("14:00-15:30")[:2] == (time(14, 0), time(15, 30))
    assert t("van 10.00 tot 11.00 uur")[:2] == (time(10, 0), time(11, 0))


def test_ambiguous_or_missing_times_are_not_guessed():
    assert t("half drie")[0] is None and "half_hour_ambiguous" in t("half drie")[2]
    assert t("12")[0] is None and t("")[0] is None and t("ergens volgende week")[0] is None


def test_date_ranges_and_choices_are_not_resolved_to_one_day():
    ref = date(2026, 10, 30)
    for t in ("24 t/m 26 november", "24 tot en met 26 november", "24-26 november", "van 24 tot 26 november",
              "24 november t/m 26 november", "30 november - 2 december", "november 24-26", "november 24 to 26",
              "3 of 4 december", "dec 3 & 4"):
        r = resolve_date(t, ref)
        assert (r.value, r.issues) == (None, ["date_range"]), t
    # a time range after a single date is not a date range
    for t in ("12 november 14-16 uur", "november 12 14-16h", "15 december 2026 20:00 - 22:00",
              "1 december tot 14:00", "24-11-2026"):
        assert resolve_date(t, ref).value is not None, t


def test_weekday_with_bare_day_number_is_not_dropped():
    ref = date(2026, 11, 17)                                   # Tuesday
    r = resolve_date("dinsdag 25", ref)                        # the 25th is a Wednesday
    assert r.value == date(2026, 11, 25) and r.issues == ["month_inferred", "weekday_mismatch"]
    assert resolve_date("woensdag 25", ref).issues == ["month_inferred"]
    assert resolve_date("vrijdag 3", ref).value == date(2026, 12, 3)          # passed this month -> next month
    assert resolve_date("dinsdag", ref).value == date(2026, 11, 24)
    assert resolve_date("dinsdag om 15 uur", ref).value == date(2026, 11, 24)  # a time, not a day number
