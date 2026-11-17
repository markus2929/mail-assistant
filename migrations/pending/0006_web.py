"""Phase 6: dashboard sessions, worker heartbeat, asynchronous approval, Home Assistant push marker

Revision ID: 0006
Revises: 0005

Idempotent: fresh installs generate 0001 from the models, which already contain these objects.
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE IF NOT EXISTS web_sessions (
        id_hash VARCHAR(64) PRIMARY KEY, csrf_token VARCHAR(64) NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at TIMESTAMPTZ NOT NULL, user_agent VARCHAR(200))""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_web_sessions_expires_at ON web_sessions (expires_at)")
    op.execute("""CREATE TABLE IF NOT EXISTS service_status (
        component VARCHAR(30) PRIMARY KEY, ok BOOLEAN NOT NULL, detail TEXT,
        checked_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    op.execute("ALTER TABLE event_proposals ADD COLUMN IF NOT EXISTS approval_requested_at TIMESTAMPTZ")
    op.execute("ALTER TABLE event_proposals ADD COLUMN IF NOT EXISTS approval_result TEXT")
    op.execute("ALTER TABLE followups ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ")


def downgrade() -> None:
    op.execute("ALTER TABLE followups DROP COLUMN IF EXISTS notified_at")
    op.execute("ALTER TABLE event_proposals DROP COLUMN IF EXISTS approval_result")
    op.execute("ALTER TABLE event_proposals DROP COLUMN IF EXISTS approval_requested_at")
    op.execute("DROP TABLE IF EXISTS service_status")
    op.execute("DROP TABLE IF EXISTS web_sessions")
