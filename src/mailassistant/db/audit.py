from typing import Any

from sqlalchemy.orm import Session

from mailassistant.db.models import AuditLog


def record(session: Session, *, actor: str, action: str, entity_type: str | None = None,
           entity_id: str | int | None = None, before: Any = None, after: Any = None,
           detail: str | None = None) -> None:
    """Append an audit entry. actor: 'system' | 'user' | 'llm-proposal'."""
    session.add(AuditLog(actor=actor, action=action, entity_type=entity_type,
                         entity_id=None if entity_id is None else str(entity_id),
                         before=before, after=after, detail=detail))
