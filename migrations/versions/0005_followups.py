"""Phase 5: emails.followups_scanned_at and proposed-closure columns on followups

Revision ID: 0005
Revises: 0004

Idempotent: fresh installs generate 0001 from the models, which already contain these columns.
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE emails ADD COLUMN IF NOT EXISTS followups_scanned_at TIMESTAMPTZ")
    op.execute("ALTER TABLE followups ADD COLUMN IF NOT EXISTS closure_email_id INTEGER REFERENCES emails(id)")
    op.execute("ALTER TABLE followups ADD COLUMN IF NOT EXISTS closure_quote TEXT")
    op.execute("ALTER TABLE followups ADD COLUMN IF NOT EXISTS closure_proposed_at TIMESTAMPTZ")
    op.execute("ALTER TABLE followups ADD COLUMN IF NOT EXISTS notes JSONB NOT NULL DEFAULT '[]'::jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE followups DROP COLUMN IF EXISTS notes")
    op.execute("ALTER TABLE followups DROP COLUMN IF EXISTS closure_proposed_at")
    op.execute("ALTER TABLE followups DROP COLUMN IF EXISTS closure_quote")
    op.execute("ALTER TABLE followups DROP COLUMN IF EXISTS closure_email_id")
    op.execute("ALTER TABLE emails DROP COLUMN IF EXISTS followups_scanned_at")
