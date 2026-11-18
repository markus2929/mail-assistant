"""Move-to-Trash requests from the dashboard (mail_actions) and extra IMAP accounts (mail_accounts columns)

Revision ID: 0010
Revises: 0009

Idempotent: fresh installs generate 0001 from the models, which already contain all of this.
"""
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE IF NOT EXISTS mail_actions (
        id SERIAL PRIMARY KEY,
        email_id INTEGER NOT NULL REFERENCES emails(id),
        action VARCHAR(10) NOT NULL DEFAULT 'trash',
        status VARCHAR(10) NOT NULL DEFAULT 'requested',
        requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        done_at TIMESTAMPTZ,
        detail TEXT,
        CONSTRAINT ck_mail_actions_action CHECK (action IN ('trash')),
        CONSTRAINT ck_mail_actions_status CHECK (status IN ('requested', 'done', 'skipped', 'failed')))""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_mail_actions_status ON mail_actions (status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_mail_actions_email_id ON mail_actions (email_id)")
    for col in ("kind VARCHAR(20) NOT NULL DEFAULT 'proton_bridge'", "label VARCHAR(40)", "host TEXT",
                "port INTEGER", "username TEXT", "auth VARCHAR(20) NOT NULL DEFAULT 'password'",
                "encrypted_password BYTEA", "sync_include TEXT[] NOT NULL DEFAULT '{}'",
                "sync_exclude TEXT[] NOT NULL DEFAULT '{}'", "enabled BOOLEAN NOT NULL DEFAULT true"):
        op.execute(f"ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS {col}")
    op.execute("""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'mail_accounts_label_key') THEN
            ALTER TABLE mail_accounts ADD CONSTRAINT mail_accounts_label_key UNIQUE (label); END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_mail_accounts_kind') THEN
            ALTER TABLE mail_accounts ADD CONSTRAINT ck_mail_accounts_kind
                CHECK (kind IN ('proton_bridge', 'imap')); END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_mail_accounts_auth') THEN
            ALTER TABLE mail_accounts ADD CONSTRAINT ck_mail_accounts_auth
                CHECK (auth IN ('password', 'microsoft_oauth')); END IF;
    END $$""")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS mail_actions")
    for col in ("kind", "label", "host", "port", "username", "auth", "encrypted_password", "sync_include",
                "sync_exclude", "enabled"):
        op.execute(f"ALTER TABLE mail_accounts DROP COLUMN IF EXISTS {col}")
