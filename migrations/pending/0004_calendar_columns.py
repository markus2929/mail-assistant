"""Phase 4: emails.events_scanned_at and event_proposals.notes

Revision ID: 0004
Revises: 0003

Idempotent: fresh installs generate 0001 from the models, which already contain these columns.
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE emails ADD COLUMN IF NOT EXISTS events_scanned_at TIMESTAMPTZ")
    op.execute("ALTER TABLE event_proposals ADD COLUMN IF NOT EXISTS notes JSONB NOT NULL DEFAULT '[]'::jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE event_proposals DROP COLUMN IF EXISTS notes")
    op.execute("ALTER TABLE emails DROP COLUMN IF EXISTS events_scanned_at")
