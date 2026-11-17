"""attachments.ics_text: raw iCalendar text of .ics attachments (parsed in Phase 4)

Revision ID: 0003
Revises: 0002

Idempotent on purpose: fresh installs generate 0001 from the models, which already contain
the column.
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE attachments ADD COLUMN IF NOT EXISTS ics_text TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE attachments DROP COLUMN IF EXISTS ics_text")
