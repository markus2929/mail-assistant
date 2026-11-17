"""Health rows the worker writes so the dashboard can show desktop-side status with database access only."""
from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from mailassistant.db.models import ServiceStatus
from mailassistant.db.session import utcnow


def set_status(session: Session, component: str, ok: bool, detail: str | None = None) -> None:
    """Upsert one component's status. `detail` must never contain mail content (error class / counts only)."""
    now = utcnow()
    stmt = insert(ServiceStatus).values(component=component, ok=ok, detail=(detail or "")[:300], checked_at=now)
    session.execute(stmt.on_conflict_do_update(index_elements=[ServiceStatus.component],
                                               set_={"ok": ok, "detail": (detail or "")[:300], "checked_at": now}))
