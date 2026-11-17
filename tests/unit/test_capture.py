from datetime import UTC, datetime, time

from mailassistant.capture.detect import capture_kind, kind_from_address, strip_subject_tag
from mailassistant.capture.speech import speech
from mailassistant.gcal.datetext import resolve_time
from mailassistant.gcal.extract import draft_from_extraction

OWN = ["me@example.com", "info@example-business.com"]
CAPS = ["me+agenda@example.com", "me+todo@example.com"]


def test_capture_detection():
    kw = dict(cc_addrs=[], subject="", capture_addresses=CAPS, own_addresses=OWN)
    assert capture_kind(to_addrs=["Me+Agenda@example.com"], **kw) == "event"
    assert capture_kind(to_addrs=["me+todo@example.com"], **kw) == "followup"
    assert capture_kind(to_addrs=CAPS, **kw) == "auto"
    assert capture_kind(to_addrs=["piet@example.nl"], **kw) is None
    # subject tag only when sent to yourself
    assert capture_kind(to_addrs=["me@example.com"], cc_addrs=[], subject="Agenda: tandarts dinsdag",
                        capture_addresses=CAPS, own_addresses=OWN) == "event"
    assert capture_kind(to_addrs=["piet@example.nl"], cc_addrs=[], subject="todo: offerte",
                        capture_addresses=CAPS, own_addresses=OWN) is None
    assert kind_from_address("me+x@y.nl") == "auto" and strip_subject_tag("TODO: Piet bellen") == "Piet bellen"


def test_afternoon_only_for_captures():
    assert resolve_time("om 3 uur").start == time(3) and resolve_time("om 3 uur", assume_afternoon=True).start == time(15)
    assert "assumed_afternoon" in resolve_time("3 uur", assume_afternoon=True).issues
    for t in ("3 uur 's nachts", "'s ochtends om 6 uur", "10:00", "5 uur 's middags"):
        assert "assumed_afternoon" not in resolve_time(t, assume_afternoon=True).issues, t
    r = resolve_time("2-4 uur", assume_afternoon=True)
    assert (r.start, r.end) == (time(14), time(16))


SENT = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)          # Wednesday
NOW = datetime(2026, 11, 18, 9, 5, tzinfo=UTC)


def ext(text, **kw):
    base = dict(reason="r", kind="invitation", title="Tandarts", date_text="vrijdag", start_time_text="3 uur",
                end_time_text="", duration_minutes=None, timezone_text="", location="", meeting_url="", organiser="",
                evidence_quote=text, all_day=False, confidence=0.9)
    base.update(kw)
    return base


def draft(text, capture, intent_event=False, **kw):
    return draft_from_extraction(ext(text, **kw), subject=None, body=text, sent_at=SENT, now=NOW,
                                 default_tz="Europe/Amsterdam", default_minutes=60, source_key="k",
                                 capture=capture, intent_event=intent_event)


def test_capture_time_and_intent():
    text = "tandarts vrijdag om 3 uur"
    d, _ = draft(text, capture=True)
    assert d.start_at == datetime(2026, 11, 20, 14, 0, tzinfo=UTC) and "assumed_afternoon" in d.notes   # 15:00 CET
    assert draft(text, capture=False)[0].start_at.hour == 2                                            # mail: 03:00
    sign = "schrijf je in voor de borrel vrijdag om 3 uur"
    assert draft(sign, capture=False, title="Borrel")[0].kind == "mention"
    d, _ = draft(sign, capture=True, intent_event=True, title="Borrel")
    assert d.kind == "invitation" and "added_by_you" in d.notes
    d, _ = draft(text, capture=True, intent_event=True, kind="mention")
    assert d.kind == "invitation" and "added_by_you" in d.notes
    assert draft(text, capture=True, intent_event=False, kind="mention")[0].kind == "mention"
    assert draft("tandarts vrijdag", capture=True, start_time_text="")[1] == "time_text_not_found"


def test_read_back_sentences():
    tz = "Europe/Amsterdam"
    ev = {"title": "Tandarts", "start": "2026-11-20T14:00:00+00:00", "all_day": False, "status": "proposed",
          "kind": "invitation", "notes": ["assumed_afternoon"], "conflict": "Overleg"}
    s = speech("done", {"events": [ev], "followups": [], "reasons": {}}, tz)
    assert s.startswith("Tandarts, vrijdag 20 november om 15:00, staat klaar om te bevestigen.")
    assert "middag aangenomen" in s and "botst met Overleg" in s
    f = {"action": "Piet bellen", "deadline": "2026-11-20T16:00:00+00:00"}
    assert speech("done", {"events": [], "followups": [f]}, tz) == "Genoteerd: Piet bellen, uiterlijk vrijdag 20 november."
    assert speech("nothing", {"reasons": {"event": "time_text_not_found"}}, tz).startswith("Ik mis een tijd")
    assert speech("nothing", {"reasons": {"event": "date_range"}}, tz) == "Dat zijn meerdere dagen. Noem één dag."
    assert speech("queued", {}, tz).startswith("Opgeslagen")
    assert speech("nothing", {"reasons": {}}, tz) == "Ik hoorde geen afspraak of taak."


def test_null_strings_from_the_model_count_as_empty_and_mismatch_is_spoken():
    d, _ = draft("tandarts vrijdag om 3 uur", capture=True, location="null", timezone_text="null", meeting_url="None")
    assert not any(n in d.notes for n in ("location_not_in_email", "unknown_timezone_text", "meeting_url_not_in_email"))
    ev = {"title": "Tandarts", "start": "2026-11-25T14:00:00+00:00", "all_day": False, "status": "proposed",
          "kind": "invitation", "notes": ["weekday_mismatch"], "conflict": None}
    assert "kloppen niet" in speech("done", {"events": [ev]}, "Europe/Amsterdam")


def test_own_notes_keep_the_users_words():
    from mailassistant.followups.extract import drafts_from_extraction, own_words_action
    assert own_words_action("herinner me vrijdag piet te bellen over de offerte") == "Vrijdag piet te bellen over de offerte"
    assert own_words_action("ik moet nog boodschappen doen") == "Boodschappen doen"
    assert own_words_action("remind me to email the accountant on monday") == "Email the accountant on monday"
    assert own_words_action("woord " * 20) is None
    item = dict(who_owes="user", action="Call Piet on Friday", deadline_text="vrijdag",
                evidence_quote="herinner me vrijdag piet te bellen", confidence=0.9)
    d = drafts_from_extraction({"reason": "r", "items": [item]}, mail_direction="capture",
                               own="herinner me vrijdag piet te bellen", subject=None, message_id="m", sent_at=SENT,
                               tz="Europe/Amsterdam")[0][0]
    assert d.action_text == "Vrijdag piet te bellen"                 # not the model's English translation
    m = drafts_from_extraction({"reason": "r", "items": [item]}, mail_direction="in",
                               own="herinner me vrijdag piet te bellen", subject=None, message_id="m2", sent_at=SENT,
                               tz="Europe/Amsterdam")[0][0]
    assert m.action_text == "Call Piet on Friday"                    # mail behaviour unchanged
