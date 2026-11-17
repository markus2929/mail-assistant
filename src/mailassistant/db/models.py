"""SQLAlchemy models (Phase 1 schema). Mail is never hard-deleted by the application."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index,
                        Integer, LargeBinary, Numeric, String, Text, UniqueConstraint, func, text)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TZ = DateTime(timezone=True)
NOW = func.now()
EMPTY_ARR = text("'{}'")
EMPTY_OBJ = text("'{}'::jsonb")


class Base(DeclarativeBase):
    pass


def _in(col: str, values: list[str]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


CATEGORIES = ["personal", "work", "newsletter", "automated_notification", "marketing",
              "invitation", "spam_suspicious", "manual_review"]


class MailAccount(Base):
    __tablename__ = "mail_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    address: Mapped[str] = mapped_column(String(320), unique=True)
    aliases: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=EMPTY_ARR)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)


class Mailbox(Base):
    __tablename__ = "mailboxes"
    __table_args__ = (UniqueConstraint("account_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("mail_accounts.id"))
    name: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20), server_default="other")  # inbox/sent/archive/ads/other
    uidvalidity: Mapped[int | None] = mapped_column(BigInteger)
    last_uid: Mapped[int] = mapped_column(BigInteger, server_default="0")
    last_synced_at: Mapped[datetime | None] = mapped_column(TZ)


class Thread(Base):
    __tablename__ = "threads"
    __table_args__ = (UniqueConstraint("account_id", "thread_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("mail_accounts.id"))
    thread_key: Mapped[str] = mapped_column(Text)  # root Message-ID (or fallback key)
    subject_norm: Mapped[str | None] = mapped_column(Text)
    first_message_at: Mapped[datetime | None] = mapped_column(TZ)
    last_message_at: Mapped[datetime | None] = mapped_column(TZ)
    summary: Mapped[str | None] = mapped_column(Text)
    summary_email_count: Mapped[int | None] = mapped_column(Integer)
    summary_updated_at: Mapped[datetime | None] = mapped_column(TZ)


class Email(Base):
    __tablename__ = "emails"
    __table_args__ = (
        UniqueConstraint("account_id", "message_id"),
        CheckConstraint(_in("direction", ["in", "out"])),
        Index("ix_emails_thread", "thread_id"),
        Index("ix_emails_sent_at", "sent_at"),
        Index("ix_emails_from", "from_addr"),
        Index("ix_emails_content_hash", "content_hash"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("mail_accounts.id"))
    thread_id: Mapped[int | None] = mapped_column(ForeignKey("threads.id"))
    message_id: Mapped[str] = mapped_column(Text)
    in_reply_to: Mapped[str | None] = mapped_column(Text)
    references_: Mapped[list[str]] = mapped_column("references", ARRAY(Text), server_default=EMPTY_ARR)
    direction: Mapped[str] = mapped_column(String(3))
    from_addr: Mapped[str | None] = mapped_column(String(320))
    from_name: Mapped[str | None] = mapped_column(Text)
    to_addrs: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=EMPTY_ARR)
    cc_addrs: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=EMPTY_ARR)
    reply_to: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(TZ)
    received_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    body_text: Mapped[str | None] = mapped_column(Text)
    body_html_sanitised: Mapped[str | None] = mapped_column(Text)
    headers: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_OBJ)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    has_attachments: Mapped[bool] = mapped_column(Boolean, server_default="false")
    events_scanned_at: Mapped[datetime | None] = mapped_column(TZ)  # calendar extraction done (Phase 4)
    followups_scanned_at: Mapped[datetime | None] = mapped_column(TZ)  # follow-up extraction done (Phase 5)


class EmailLocation(Base):
    """Where a message currently lives (Bridge may expose one message in several folders)."""
    __tablename__ = "email_locations"
    __table_args__ = (UniqueConstraint("mailbox_id", "uid"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id"))
    mailbox_id: Mapped[int] = mapped_column(ForeignKey("mailboxes.id"))
    uid: Mapped[int] = mapped_column(BigInteger)
    first_seen_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    removed_at: Mapped[datetime | None] = mapped_column(TZ)


class Attachment(Base):
    __tablename__ = "attachments"
    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id"), index=True)
    filename: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    storage_path: Mapped[str | None] = mapped_column(Text)
    is_ics: Mapped[bool] = mapped_column(Boolean, server_default="false")
    ics_text: Mapped[str | None] = mapped_column(Text)  # raw iCalendar text (Phase 4 parses it)


class Classification(Base):
    __tablename__ = "classifications"
    __table_args__ = (
        CheckConstraint(_in("category", CATEGORIES)),
        CheckConstraint(_in("source", ["llm", "rule", "user"])),
        Index("uq_classification_current", "email_id", unique=True, postgresql_where=text("is_current")),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id"))
    category: Mapped[str] = mapped_column(String(30))
    importance: Mapped[str] = mapped_column(String(10), server_default="normal")
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    reasons: Mapped[str | None] = mapped_column(Text)
    safety_flags: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=EMPTY_ARR)
    injection_flag: Mapped[bool] = mapped_column(Boolean, server_default="false")
    model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(10))
    is_current: Mapped[bool] = mapped_column(Boolean, server_default="true")
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)


class ClassificationCorrection(Base):
    __tablename__ = "classification_corrections"
    __table_args__ = (CheckConstraint(_in("corrected_category", CATEGORIES)),)
    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id"))
    classification_id: Mapped[int | None] = mapped_column(ForeignKey("classifications.id"))
    corrected_category: Mapped[str] = mapped_column(String(30))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)


class ProposedAction(Base):
    __tablename__ = "proposed_actions"
    __table_args__ = (
        CheckConstraint(_in("status", ["proposed", "approved", "rejected", "applied", "failed", "expired"])),
        Index("ix_proposed_actions_status", "status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int | None] = mapped_column(ForeignKey("emails.id"))
    action_type: Mapped[str] = mapped_column(String(40))  # must be an ActionType value
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_OBJ)
    status: Mapped[str] = mapped_column(String(10), server_default="proposed")
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    decided_at: Mapped[datetime | None] = mapped_column(TZ)
    applied_at: Mapped[datetime | None] = mapped_column(TZ)
    error: Mapped[str | None] = mapped_column(Text)


class EventProposal(Base):
    __tablename__ = "event_proposals"
    __table_args__ = (
        CheckConstraint(_in("kind", ["invitation", "proposed_date", "mention"])),
        CheckConstraint(_in("status", ["proposed", "approved", "rejected", "created", "failed", "superseded"])),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id"), index=True)
    attachment_id: Mapped[int | None] = mapped_column(ForeignKey("attachments.id"))
    kind: Mapped[str] = mapped_column(String(15))
    ics_uid: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    start_at: Mapped[datetime | None] = mapped_column(TZ)
    end_at: Mapped[datetime | None] = mapped_column(TZ)
    all_day: Mapped[bool] = mapped_column(Boolean, server_default="false")
    tz_name: Mapped[str | None] = mapped_column(Text)
    tz_original: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    organiser: Mapped[str | None] = mapped_column(Text)
    attendees: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    meeting_url: Mapped[str | None] = mapped_column(Text)
    conflicts: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    evidence_quote: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12), server_default="proposed")
    google_event_id: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(Text, unique=True)
    notes: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    decided_at: Mapped[datetime | None] = mapped_column(TZ)
    # Asynchronous approval (Phase 6): the dashboard requests, the worker creates and writes the outcome.
    approval_requested_at: Mapped[datetime | None] = mapped_column(TZ)
    approval_result: Mapped[str | None] = mapped_column(Text)
    # Calendar choice (work = default calendar, shared = personal shared calendar): suggested at scan time,
    # set by the user when approving; created_calendar_id records where the event was actually created.
    target_calendar: Mapped[str | None] = mapped_column(String(10))
    created_calendar_id: Mapped[str | None] = mapped_column(Text)


class CalendarCache(Base):
    __tablename__ = "calendar_cache"
    __table_args__ = (UniqueConstraint("google_calendar_id", "google_event_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    google_calendar_id: Mapped[str] = mapped_column(Text)
    google_event_id: Mapped[str] = mapped_column(Text)
    ical_uid: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    start_at: Mapped[datetime | None] = mapped_column(TZ, index=True)
    end_at: Mapped[datetime | None] = mapped_column(TZ)
    all_day: Mapped[bool] = mapped_column(Boolean, server_default="false")
    location: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    transparency: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_OBJ)
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)


class Followup(Base):
    __tablename__ = "followups"
    __table_args__ = (
        CheckConstraint(_in("direction", ["I_OWE", "THEY_OWE"])),
        CheckConstraint(_in("status", ["proposed", "open", "waiting", "completed", "cancelled"])),
        CheckConstraint(_in("remind_basis", ["explicit", "estimated", "manual"]), name="ck_followups_remind_basis"),
        Index("ix_followups_status_remind", "status", "remind_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id"))
    thread_id: Mapped[int | None] = mapped_column(ForeignKey("threads.id"), index=True)
    direction: Mapped[str] = mapped_column(String(10))
    counterparty: Mapped[str | None] = mapped_column(Text)
    people: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=EMPTY_ARR)
    action_text: Mapped[str] = mapped_column(Text)
    evidence_quote: Mapped[str | None] = mapped_column(Text)
    deadline_at: Mapped[datetime | None] = mapped_column(TZ)  # explicit deadline only
    remind_at: Mapped[datetime | None] = mapped_column(TZ)
    remind_basis: Mapped[str | None] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(10), server_default="proposed")
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    explanation: Mapped[str | None] = mapped_column(Text)
    user_edited: Mapped[bool] = mapped_column(Boolean, server_default="false")
    resolved_by_email_id: Mapped[int | None] = mapped_column(ForeignKey("emails.id"))
    # Proposed closure (Phase 5): set by resolution detection, only the user turns it into 'completed'.
    closure_email_id: Mapped[int | None] = mapped_column(ForeignKey("emails.id"))
    closure_quote: Mapped[str | None] = mapped_column(Text)
    closure_proposed_at: Mapped[datetime | None] = mapped_column(TZ)
    notes: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    notified_at: Mapped[datetime | None] = mapped_column(TZ)  # pushed to Home Assistant (Phase 6)
    dedupe_key: Mapped[str | None] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW, onupdate=NOW)


class FollowupEvent(Base):
    __tablename__ = "followup_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    followup_id: Mapped[int] = mapped_column(ForeignKey("followups.id"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(10))
    to_status: Mapped[str] = mapped_column(String(10))
    actor: Mapped[str] = mapped_column(String(20))
    email_id: Mapped[int | None] = mapped_column(ForeignKey("emails.id"))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(_in("status", ["queued", "running", "done", "dead"])),
        Index("ix_jobs_ready", "status", "priority", "run_after"),
        Index("uq_jobs_dedupe_active", "dedupe_key", unique=True,
              postgresql_where=text("dedupe_key IS NOT NULL AND status IN ('queued','running')")),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    type: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_OBJ)
    status: Mapped[str] = mapped_column(String(10), server_default="queued")
    priority: Mapped[int] = mapped_column(Integer, server_default="100")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, server_default="5")
    run_after: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    dedupe_key: Mapped[str | None] = mapped_column(Text)
    locked_by: Mapped[str | None] = mapped_column(Text)
    locked_at: Mapped[datetime | None] = mapped_column(TZ)
    defer_reason: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    finished_at: Mapped[datetime | None] = mapped_column(TZ)


class LlmCall(Base):
    """Prompts/responses for local debugging. Purged after LLM_LOG_RETENTION_DAYS."""
    __tablename__ = "llm_calls"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_id: Mapped[int | None] = mapped_column(BigInteger)
    task: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    prompt: Mapped[str | None] = mapped_column(Text)
    response: Mapped[str | None] = mapped_column(Text)
    parsed_ok: Mapped[bool | None] = mapped_column(Boolean)
    validation_error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW, index=True)


class Briefing(Base):
    __tablename__ = "briefings"
    id: Mapped[int] = mapped_column(primary_key=True)
    briefing_date: Mapped[date] = mapped_column(Date, unique=True)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW, onupdate=NOW)


class OAuthToken(Base):
    __tablename__ = "oauth_tokens"
    __table_args__ = (UniqueConstraint("provider", "label"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(30))
    label: Mapped[str] = mapped_column(String(100))
    encrypted_refresh_token: Mapped[bytes] = mapped_column(LargeBinary)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=EMPTY_ARR)
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW, onupdate=NOW)


class AuditLog(Base):
    """Append-only (enforced by DB triggers in migration 0002)."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[datetime] = mapped_column(TZ, server_default=NOW, index=True)
    actor: Mapped[str] = mapped_column(String(20))
    action: Mapped[str] = mapped_column(String(60))
    entity_type: Mapped[str | None] = mapped_column(String(40))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    before: Mapped[Any | None] = mapped_column(JSONB)
    after: Mapped[Any | None] = mapped_column(JSONB)
    detail: Mapped[str | None] = mapped_column(Text)


class WebSession(Base):
    """Dashboard login sessions (Phase 6). Only a hash of the cookie token is stored."""
    __tablename__ = "web_sessions"
    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    last_seen_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    expires_at: Mapped[datetime] = mapped_column(TZ, index=True)
    user_agent: Mapped[str | None] = mapped_column(String(200))


class ServiceStatus(Base):
    """Health of the desktop side, written by the worker so the dashboard needs only the database."""
    __tablename__ = "service_status"
    component: Mapped[str] = mapped_column(String(30), primary_key=True)  # worker/ollama/google/bridge/ha
    ok: Mapped[bool] = mapped_column(Boolean)
    detail: Mapped[str | None] = mapped_column(Text)                      # never mail content
    checked_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)


class Capture(Base):
    """Something the user added themself (dashboard, voice, mail to a +agenda/+todo address). The text lives in
    an Email row (real for forwarded mail, synthetic otherwise) so proposals can reference it like any mail."""
    __tablename__ = "captures"
    __table_args__ = (
        CheckConstraint(_in("source", ["mail", "dashboard", "assist"])),
        CheckConstraint(_in("kind", ["event", "followup", "auto"])),
        CheckConstraint(_in("status", ["queued", "done", "nothing", "failed"])),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id"), unique=True)
    source: Mapped[str] = mapped_column(String(10))
    kind: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(10), server_default="queued")
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_OBJ)  # what was proposed / why not
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    processed_at: Mapped[datetime | None] = mapped_column(TZ)
