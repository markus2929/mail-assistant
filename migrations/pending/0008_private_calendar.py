"""Rename the calendar choice value 'shared' to 'private' (settings are now GOOGLE_WORK/PRIVATE_CALENDAR)

Revision ID: 0008
Revises: 0007

Data only and idempotent; no schema change.
"""
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE event_proposals SET target_calendar = 'private' WHERE target_calendar = 'shared'")


def downgrade() -> None:
    op.execute("UPDATE event_proposals SET target_calendar = 'shared' WHERE target_calendar = 'private'")
