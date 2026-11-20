"""Database side of classification: load an email, classify it, store the decision and proposals."""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from mailassistant.classify.core import ClassifyConfig, classify_view
from mailassistant.classify.policy import CATEGORIES
from mailassistant.classify.prompt import PROMPT_VERSION, EmailView
from mailassistant.config import Settings
from mailassistant.db import audit
from mailassistant.db.models import (Attachment, Classification, ClassificationCorrection, Email,
                                     LlmCall, ProposedAction)
from mailassistant.db.session import utcnow
from mailassistant.llm.client import LLMReply

log = logging.getLogger(__name__)


def build_view(session: Session, email: Email, my_addresses: list[str]) -> EmailView:
    atts = session.execute(select(Attachment.filename, Attachment.mime_type)
                           .where(Attachment.email_id == email.id)).all()
    return EmailView(email.from_addr, email.from_name, list(email.to_addrs or []),
                     list(email.cc_addrs or []), email.subject, email.sent_at,
                     email.body_text or "", dict(email.headers or {}),
                     [(a, m) for a, m in atts], my_addresses)


def is_known_correspondent(session: Session, account_id: int, addr: str | None) -> bool:
    """True if the user has ever written to this address (a person, not a mass mailing)."""
    if not addr:
        return False
    return session.scalar(select(Email.id).where(
        Email.account_id == account_id, Email.direction == "out", Email.to_addrs.any(addr)).limit(1)) is not None


def _recorder(session: Session, settings: Settings, prompt_version: str):
    def record(task: str, messages: list, reply: LLMReply, ok: bool, error: str | None) -> None:
        ctx = settings.ollama_num_ctx
        if ctx and reply.prompt_tokens and reply.prompt_tokens >= ctx - 16:
            log.warning("prompt used %s of %s context tokens: the start of the prompt may have been "
                        "cut off; raise OLLAMA_NUM_CTX or lower CLASSIFY_BODY_CHARS", reply.prompt_tokens, ctx)
        session.add(LlmCall(
            task=task, model=reply.model, prompt_version=prompt_version,
            prompt=json.dumps(messages, ensure_ascii=False) if settings.llm_log_prompts else None,
            response=reply.content if settings.llm_log_prompts else None,
            parsed_ok=ok, validation_error=error, duration_ms=reply.duration_ms))
    return record


def classify_email(session: Session, email_id: int, client, settings: Settings) -> Classification | None:
    """Classify one incoming email. Returns the stored Classification (None if not applicable).
    Raises LLMUnavailable / LLMModelMissing: the caller defers the job."""
    email = session.get(Email, email_id)
    if email is None or email.direction != "in":
        return None
    cfg = ClassifyConfig.from_settings(settings)
    from mailassistant.mail.accounts import own_addresses
    view = build_view(session, email, own_addresses(session, settings))
    result = classify_view(client, view, cfg, _recorder(session, settings, PROMPT_VERSION),
                           known_correspondent=is_known_correspondent(session, email.account_id,
                                                                      email.from_addr))
    d = result.decision

    session.execute(update(Classification).where(Classification.email_id == email_id,
                                                 Classification.is_current.is_(True))
                    .values(is_current=False))
    row = Classification(
        email_id=email_id, category=d.category, importance=d.importance,
        confidence=Decimal(f"{d.confidence:.3f}"), reasons=d.reasons, safety_flags=d.safety_flags,
        injection_flag=d.injection_flag, model=getattr(client, "model", None),
        served_by=getattr(client, "served_by", None),
        prompt_version=result.prompt_version, source=d.source, is_current=True)
    session.add(row)

    # Proposals: expire stale open ones for this email, then record the current ones.
    session.execute(update(ProposedAction).where(
        ProposedAction.email_id == email_id, ProposedAction.status == "proposed")
        .values(status="expired", decided_at=utcnow()))
    for a in d.actions:
        session.add(ProposedAction(email_id=email_id, action_type=a.action_type, payload=a.payload,
                                   reason=a.reason, status="proposed"))
    session.flush()
    audit.record(session, actor="llm-proposal", action="classified", entity_type="email",
                 entity_id=email_id,
                 after={"category": d.category, "confidence": float(d.confidence),
                        "importance": d.importance, "safety_flags": d.safety_flags,
                        "actions": [a.action_type for a in d.actions], "source": d.source})
    return row


def apply_user_correction(session: Session, email_id: int, category: str, note: str | None = None) -> Classification:
    """Manual correction (dashboard, Phase 6). The user's decision supersedes the model's."""
    if category not in CATEGORIES:
        raise ValueError(f"unknown category {category!r}")
    previous = session.scalar(select(Classification).where(
        Classification.email_id == email_id, Classification.is_current.is_(True)))
    session.execute(update(Classification).where(Classification.email_id == email_id,
                                                 Classification.is_current.is_(True))
                    .values(is_current=False))
    session.add(ClassificationCorrection(email_id=email_id, classification_id=previous.id if previous else None,
                                         corrected_category=category, note=note))
    row = Classification(email_id=email_id, category=category, importance=previous.importance if previous else "normal",
                         confidence=Decimal("1.000"), reasons=note or "corrected by user",
                         safety_flags=previous.safety_flags if previous else [], injection_flag=False,
                         source="user", is_current=True)
    session.add(row)
    session.execute(update(ProposedAction).where(
        ProposedAction.email_id == email_id, ProposedAction.status == "proposed")
        .values(status="expired", decided_at=utcnow()))
    session.flush()
    audit.record(session, actor="user", action="classification_corrected", entity_type="email",
                 entity_id=email_id, before=previous.category if previous else None, after=category)
    return row


def pending_email_ids(session: Session, window_days: int, limit: int) -> list[int]:
    """Recent incoming emails without a current classification, newest first."""
    when = func.coalesce(Email.sent_at, Email.received_at)
    cutoff = utcnow() - timedelta(days=window_days)
    has_current = select(Classification.id).where(Classification.email_id == Email.id,
                                                  Classification.is_current.is_(True)).exists()
    return list(session.scalars(select(Email.id).where(Email.direction == "in", when >= cutoff, ~has_current)
                                .order_by(when.desc()).limit(limit)))
