"""Notification texts. Pure functions. detail="counts" (default): numbers and the dashboard link only, because
push notifications to the phone travel through Google's/Apple's push service. detail="full" adds short
action texts and counterparties. Mail bodies are never included."""
from __future__ import annotations

MAX_LINES = 5
MAX_LINE = 90


def _clip(s: str | None, n: int = MAX_LINE) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _line(f: dict) -> str:
    who = "Ik moet" if f.get("direction") == "I_OWE" else "Wacht op"
    return _clip(f"{who} {f.get('counterparty') or '?'}: {f.get('action') or ''}")


def briefing_message(content: dict, url: str = "", detail: str = "counts") -> tuple[str, str]:
    c = content.get("counts", {})
    parts = [f"{len(content.get('calendar', []))} afspraken",
             f"{c.get('overdue', 0)} te laat", f"{c.get('due_today', 0)} vandaag"]
    if content.get("followup_proposals"):
        parts.append(f"{len(content['followup_proposals'])} nieuwe follow-ups")
    if content.get("closure_proposals"):
        parts.append(f"{len(content['closure_proposals'])} afsluitingen te checken")
    if content.get("event_proposals"):
        parts.append(f"{len(content['event_proposals'])} agendavoorstellen")
    lines = [", ".join(parts)]
    if detail == "full":
        lines += [_line(f) for f in [*content.get("overdue", []), *content.get("due_today", [])][:MAX_LINES]]
    if url:
        lines.append(url)
    return f"Briefing {content.get('date', '')}", "\n".join(lines)


def due_message(items: list[dict], url: str = "", detail: str = "counts") -> tuple[str, str]:
    if detail == "full":
        lines = [_line(f) for f in items[:MAX_LINES]]
        if len(items) > MAX_LINES:
            lines.append(f"… en nog {len(items) - MAX_LINES}")
    else:
        owe = sum(f.get("direction") == "I_OWE" for f in items)
        lines = [f"{owe} om zelf te doen, {len(items) - owe} om na te vragen"]
    if url:
        lines.append(f"{url.rstrip('/')}/followups?status=open")
    title = "Follow-up nu aan de beurt" if len(items) == 1 else f"{len(items)} follow-ups aan de beurt"
    return title, "\n".join(lines)
