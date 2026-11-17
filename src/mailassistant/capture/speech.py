"""Sentences Home Assistant Assist reads back. Built by code from the stored result - never by the model."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

DAYS = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
MONTHS = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september", "oktober",
          "november", "december"]

WHY = {
    "time_text_not_found": "Ik mis een tijd. Zeg het nog eens met een tijd, bijvoorbeeld om 3 uur.",
    "time_unresolved": "Ik begreep de tijd niet. Zeg het nog eens, bijvoorbeeld om half vier of om 15 uur.",
    "date_text_not_found": "Ik mis een datum. Zeg het nog eens met een dag of datum.",
    "date_unresolved": "Ik begreep de datum niet. Zeg het nog eens met een dag of datum.",
    "date_range": "Dat zijn meerdere dagen. Noem één dag.",
    "in_the_past": "Die datum is al voorbij.",
    "quote_not_found": "Ik kon het niet goed volgen. Zeg het nog eens wat korter.",
    "low_confidence": "Ik weet niet zeker wat je bedoelt. Zeg het nog eens wat duidelijker.",
    "no_event": "Ik hoorde geen afspraak.",
    "unreadable": "Dat lukte niet, probeer het nog eens.",
    "prompt_injection_suspected": "Dat heb ik niet verwerkt, omdat het verdacht leek.",
}


def when_text(iso: str, all_day: bool, tz: str) -> str:
    d = datetime.fromisoformat(iso).astimezone(ZoneInfo(tz))
    day = f"{DAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]}"
    return day if all_day else f"{day} om {d:%H:%M}"


def speech(status: str, result: dict, tz: str) -> str:
    if status == "queued":
        return "Opgeslagen. Ik verwerk het zodra de computer klaar is; je vindt het daarna in het dashboard."
    parts: list[str] = []
    for e in result.get("events", []):
        s = f"{e.get('title') or 'Afspraak'}, {when_text(e['start'], e.get('all_day', False), tz)}"
        if e.get("status") == "superseded":
            parts.append(f"{s} stond al in je agenda of was al voorgesteld.")
            continue
        s += ", staat klaar om te bevestigen."
        if "assumed_afternoon" in e.get("notes", []):
            s += " Ik heb middag aangenomen."
        if "weekday_mismatch" in e.get("notes", []):
            s += " Let op: de dag en de datum kloppen niet met elkaar, controleer het voorstel."
        if e.get("conflict"):
            s += f" Let op: dat botst met {e['conflict']}."
        if e.get("kind") == "mention":
            s = f"{e.get('title') or 'Dat'} heb ik alleen als vermelding genoteerd."
        parts.append(s)
    for f in result.get("followups", []):
        s = f"Genoteerd: {f['action']}"
        if f.get("deadline"):
            s += f", uiterlijk {when_text(f['deadline'], True, tz)}"
        parts.append(s + ".")
    if parts:
        return " ".join(parts)
    reasons = result.get("reasons", {})
    for key in ("blocked", "event", "followup"):
        if reasons.get(key) in WHY:
            return WHY[reasons[key]]
    return "Ik hoorde geen afspraak of taak."
