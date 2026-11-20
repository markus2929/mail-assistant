"""Which Ollama answered (desktop or the server fallback) for classifications, follow-ups and event proposals

Revision ID: 0011
Revises: 0010

Idempotent: fresh installs generate 0001 from the models, which already contain these columns.
"""
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

TABLES = ("classifications", "followups", "event_proposals")


def upgrade() -> None:
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS served_by TEXT")


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS served_by")
