"""Dashboard: "keep me signed in on this device" sessions (no idle timeout)

Revision ID: 0012
Revises: 0011

Idempotent: fresh installs generate 0001 from the models, which already contain this column.
"""
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE web_sessions ADD COLUMN IF NOT EXISTS remember BOOLEAN NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE web_sessions DROP COLUMN IF EXISTS remember")
