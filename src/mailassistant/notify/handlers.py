"""Home Assistant push jobs (run in the worker). Disabled when HA_URL is empty."""
import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from mailassistant.config import get_settings
from mailassistant.db import audit
from mailassistant.db.models import Followup, Setting
from mailassistant.db.session import session_scope, utcnow
from mailassistant.db.status import set_status
from mailassistant.followups.briefing import build_briefing
from mailassistant.jobs.registry import Deferred, handler
from mailassistant.notify import homeassistant as ha
from mailassistant.notify.messages import briefing_message, due_message
from mailassistant.security.allowlist import ActionType

log = logging.getLogger(__name__)
LAST_BRIEFING_KEY = "notify.briefing.last_date"


def _push(title: str, message: str, kind: str, link: str = "") -> bool:
    """True if sent. HANotConfigured -> False (feature off). Unavailable -> Deferred (retried)."""
    s = get_settings()
    if not s.ha_url:
        return False
    try:
        ha.send(s.ha_url, s.ha_notify_service, s.ha_token.get_secret_value() if s.ha_token else "", title, message,
                url=link, ca_file=s.ha_ca_file)
    except ha.HANotConfigured as e:
        with session_scope() as session:
            set_status(session, "homeassistant", False, str(e))
        return False
    except ha.HAUnavailable as e:
        with session_scope() as session:
            set_status(session, "homeassistant", False, f"not reachable: {e}")
        raise Deferred(f"home assistant unavailable: {e}", timedelta(minutes=10)) from e
    except ha.HARefused as e:
        with session_scope() as session:
            set_status(session, "homeassistant", False, f"refused: {e}")
        log.error("home assistant refused the notification: %s", e)
        return False
    with session_scope() as session:
        set_status(session, "homeassistant", True, f"last push: {kind}")
        audit.record(session, actor="system", action="ha_notified", entity_type="notification",
                     detail=f"{ActionType.NOTIFY_USER.value}: {kind}")
    return True


@handler("notify.briefing")
def notify_briefing(payload: dict) -> None:
    """Hourly; pushes today's briefing once, from BRIEFING_HOUR on."""
    s = get_settings()
    if not s.ha_url:
        return
    today = utcnow().astimezone(ZoneInfo(s.timezone))
    if today.hour < s.briefing_hour:
        return
    with session_scope() as session:
        last = session.get(Setting, LAST_BRIEFING_KEY)
        if last is not None and last.value == today.date().isoformat():
            return
        content = build_briefing(session, today.date(), s.timezone, rebuild=True).content
    title, msg = briefing_message(content, s.dashboard_url, s.ha_notify_detail)
    if _push(title, msg, "briefing", s.dashboard_url):
        with session_scope() as session:
            row = session.get(Setting, LAST_BRIEFING_KEY) or Setting(key=LAST_BRIEFING_KEY)
            row.value = today.date().isoformat()
            session.merge(row)


@handler("notify.due")
def notify_due(payload: dict) -> None:
    """Every 15 minutes: confirmed follow-ups whose reminder time has passed, each pushed once."""
    s = get_settings()
    if not s.ha_url:
        return
    with session_scope() as session:
        due = session.scalars(select(Followup).where(
            Followup.status.in_(("open", "waiting")), Followup.remind_at <= utcnow(),
            Followup.notified_at.is_(None)).order_by(Followup.remind_at)).all()
        items = [{"id": f.id, "direction": f.direction, "counterparty": f.counterparty, "action": f.action_text}
                 for f in due]
    if not items:
        return
    title, msg = due_message(items, s.dashboard_url, s.ha_notify_detail)
    if _push(title, msg, f"due followups {','.join(str(i['id']) for i in items)}",
             f"{s.dashboard_url.rstrip('/')}/followups?status=open" if s.dashboard_url else ""):
        with session_scope() as session:
            for f in session.scalars(select(Followup).where(Followup.id.in_([i["id"] for i in items]))):
                f.notified_at = utcnow()


@handler("notify.test")
def notify_test(payload: dict) -> None:
    s = get_settings()
    if not s.ha_url:
        with session_scope() as session:
            set_status(session, "homeassistant", False, "not configured (HA_URL empty)")
        return
    _push("Mail Assistant", "Testmelding: Home Assistant is goed ingesteld.", "test", s.dashboard_url)
