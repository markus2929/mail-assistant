"""Captures: appointments and follow-ups added by the user (dashboard, Home Assistant Assist, +agenda mail)

Revision ID: 0009
Revises: 0008

Idempotent: fresh installs generate 0001 from the models, which already contain this table.
"""
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE IF NOT EXISTS captures (
        id SERIAL PRIMARY KEY,
        email_id INTEGER NOT NULL UNIQUE REFERENCES emails(id),
        source VARCHAR(10) NOT NULL CHECK (source IN ('mail', 'dashboard', 'assist')),
        kind VARCHAR(10) NOT NULL CHECK (kind IN ('event', 'followup', 'auto')),
        status VARCHAR(10) NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'done', 'nothing', 'failed')),
        result JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        processed_at TIMESTAMPTZ)""")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS captures")
