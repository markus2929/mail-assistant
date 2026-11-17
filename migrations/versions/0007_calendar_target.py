"""Calendar choice per event proposal (work vs shared personal calendar)

Revision ID: 0007
Revises: 0006

Idempotent: fresh installs generate 0001 from the models, which already contain these columns.
"""
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE event_proposals ADD COLUMN IF NOT EXISTS target_calendar VARCHAR(10)")
    op.execute("ALTER TABLE event_proposals ADD COLUMN IF NOT EXISTS created_calendar_id TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE event_proposals DROP COLUMN IF EXISTS created_calendar_id")
    op.execute("ALTER TABLE event_proposals DROP COLUMN IF EXISTS target_calendar")
