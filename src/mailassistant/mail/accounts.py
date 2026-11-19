"""Mail accounts: the Proton account from .env plus extra IMAP accounts stored in the database.

Extra accounts are managed in the WORKER container (it has the Fernet key; the dashboard does not):

    docker compose run --rm --no-deps worker python -m mailassistant.mail.accounts add \\
        --label work --address you@company.com --auth microsoft_oauth
    docker compose run --rm --no-deps worker python -m mailassistant.mail.accounts authorize work [--browser]
    docker compose run --rm --no-deps worker python -m mailassistant.mail.accounts test work
    ... list | disable LABEL | enable LABEL | folders LABEL

Microsoft 365 / Outlook.com: OAuth2 device-code sign-in (docs/MICROSOFT_SETUP.md). Other IMAP servers:
--auth password (asked for interactively, stored Fernet-encrypted, never in .env).
"""
from __future__ import annotations

import argparse
import getpass
import re
import sys
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from mailassistant.config import Settings
from mailassistant.db.models import MailAccount, OAuthToken
from mailassistant.mail.imap_bridge import ImapBridgeSource
from mailassistant.mail.source import MailSourceError
from mailassistant.security import crypto

MS_HOST, MS_PORT = "outlook.office365.com", 993
PROVIDER_MS = "microsoft"
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,29}$")

# Folders never synchronised for extra accounts (besides Trash/Spam/Drafts/All, recognised by their flags):
# Exchange's non-mail folders, in English and Dutch.
IMAP_DEFAULT_EXCLUDE = [
    "Calendar", "Calendar/*", "Contacts", "Contacts/*", "Tasks", "Tasks/*", "Notes", "Journal", "Outbox",
    "Conversation History", "RSS Feeds", "RSS Feeds/*", "Sync Issues", "Sync Issues/*", "Junk Email",
    "Deleted Items", "Drafts", "Agenda", "Agenda/*", "Contactpersonen", "Contactpersonen/*", "Taken", "Taken/*",
    "Notities", "Logboek", "Postvak UIT", "Gespreksgeschiedenis", "RSS-feeds", "RSS-feeds/*",
    "Synchronisatieproblemen", "Synchronisatieproblemen/*", "Ongewenste e-mail", "Verwijderde items",
    "Concepten",
]
SKIP_ROLES = {"trash", "spam", "drafts", "all", "starred", "label"}


class AccountError(Exception):
    pass


# ------------------------------------------------------------------ queries
def proton_account(session: Session, settings: Settings) -> MailAccount:
    """The account configured in .env (created on first use)."""
    address = (settings.imap_username or (settings.my_addresses or [""])[0]).lower()
    acct = session.scalar(select(MailAccount).where(MailAccount.address == address))
    if acct is None:
        acct = MailAccount(address=address, kind="proton_bridge")
        session.add(acct)
    acct.aliases = [a for a in settings.my_addresses if a.lower() != address]
    session.flush()
    return acct


def extra_accounts(session: Session, *, enabled_only: bool = True) -> list[MailAccount]:
    q = select(MailAccount).where(MailAccount.kind == "imap")
    if enabled_only:
        q = q.where(MailAccount.enabled.is_(True))
    return list(session.scalars(q.order_by(MailAccount.id)))


def own_addresses(session: Session, settings: Settings) -> list[str]:
    """Every address that is the user's own: MY_ADDRESSES + all accounts and their aliases (lower case)."""
    out: list[str] = []
    for a in [*settings.my_addresses, *(x for acct in session.scalars(select(MailAccount))
                                        for x in [acct.address, *(acct.aliases or [])])]:
        a = (a or "").strip().lower()
        if a and a not in out:
            out.append(a)
    return out


def status_component(account: MailAccount) -> str:
    return "bridge" if account.kind == "proton_bridge" else f"mail:{account.label}"


def backfill_since(account: MailAccount, settings: Settings) -> date | None:
    """Extra accounts only fetch mail from MAIL_BACKFILL_DAYS before the account was added (a fixed date, so
    later full syncs do not suddenly pull years of old mail). The Proton account has no limit."""
    if account.kind != "imap":
        return None
    added = account.created_at.date() if account.created_at else date.today()
    return added - timedelta(days=max(1, settings.mail_backfill_days))


def sync_patterns(account: MailAccount, settings: Settings) -> tuple[list[str], list[str]]:
    if account.kind == "proton_bridge":
        return list(settings.sync_include), list(settings.sync_exclude)
    return (list(account.sync_include or []) or ["*"],
            list(account.sync_exclude or []) or IMAP_DEFAULT_EXCLUDE)


# ------------------------------------------------------------------ connections
def _key(settings: Settings) -> str:
    if not settings.fernet_key:
        raise AccountError("secrets/fernet_key is missing (needed to store account credentials encrypted)")
    return settings.fernet_key.get_secret_value().strip()


def save_ms_refresh_token(session: Session, settings: Settings, label: str, refresh_token: str,
                          scopes: list[str]) -> None:
    enc = crypto.encrypt(_key(settings), refresh_token)
    row = session.scalar(select(OAuthToken).where(OAuthToken.provider == PROVIDER_MS, OAuthToken.label == label))
    if row is None:
        session.add(OAuthToken(provider=PROVIDER_MS, label=label, encrypted_refresh_token=enc, scopes=scopes))
    else:
        row.encrypted_refresh_token, row.scopes = enc, scopes


def load_ms_refresh_token(session: Session, settings: Settings, label: str) -> str | None:
    row = session.scalar(select(OAuthToken).where(OAuthToken.provider == PROVIDER_MS, OAuthToken.label == label))
    return crypto.decrypt(_key(settings), bytes(row.encrypted_refresh_token)) if row else None


def source_for(session: Session, settings: Settings, account: MailAccount, cls=ImapBridgeSource):
    """An (unconnected) IMAP client for this account; cls=MailWriter for the Move-to-Trash job."""
    if account.kind == "proton_bridge":
        if not (settings.imap_username and settings.imap_password and settings.bridge_cert_sha256):
            raise AccountError("not configured (IMAP_USERNAME, imap_password, BRIDGE_CERT_SHA256)")
        return cls(settings.imap_host, settings.imap_port, settings.imap_username,
                   settings.imap_password.get_secret_value(), settings.bridge_cert_sha256)
    user = account.username or account.address
    if account.auth == "microsoft_oauth":
        from mailassistant.mail import msauth

        refresh = load_ms_refresh_token(session, settings, account.label)
        if not refresh:
            raise AccountError(f"not authorised yet: run `python -m mailassistant.mail.accounts authorize "
                               f"{account.label}`")
        tok = msauth.refresh(settings, refresh)   # raises msauth.MicrosoftAuthError / MicrosoftUnavailable
        if tok.get("refresh_token") and tok["refresh_token"] != refresh:   # Microsoft rotates refresh tokens
            save_ms_refresh_token(session, settings, account.label, tok["refresh_token"],
                                  tok.get("scope", "").split())
            session.commit()
        access = tok["access_token"]
        return cls(account.host or MS_HOST, account.port or MS_PORT, user, None, None, tls="imaps",
                   token_provider=lambda: access)
    if not account.encrypted_password:
        raise AccountError("no password stored for this account")
    password = crypto.decrypt(_key(settings), bytes(account.encrypted_password))
    return cls(account.host, account.port or 993, user, password, None, tls="imaps")


# ------------------------------------------------------------------ CLI
def _get(session: Session, label: str) -> MailAccount:
    acct = session.scalar(select(MailAccount).where(MailAccount.label == label))
    if acct is None:
        raise AccountError(f"no account with label {label!r} (see `list`)")
    return acct


def cmd_add(session: Session, settings: Settings, a) -> str:
    if not LABEL_RE.match(a.label):
        raise AccountError("label: lower-case letters, digits, - and _ (max 30), e.g. 'work'")
    if a.label == "bridge":
        raise AccountError("'bridge' is reserved")
    address = a.address.strip().lower()
    if "@" not in address:
        raise AccountError("--address must be an e-mail address")
    if session.scalar(select(MailAccount).where((MailAccount.label == a.label) | (MailAccount.address == address))):
        raise AccountError("an account with this label or address already exists")
    acct = MailAccount(kind="imap", label=a.label, address=address, auth=a.auth, username=a.username or address,
                       aliases=[x.strip().lower() for x in (a.aliases or "").split(",") if x.strip()],
                       sync_include=[x for x in (a.include or "").split(",") if x],
                       sync_exclude=[x for x in (a.exclude or "").split(",") if x], enabled=True)
    if a.auth == "microsoft_oauth":
        acct.host, acct.port = a.host or MS_HOST, a.port or MS_PORT
        if not settings.microsoft_client_id:
            raise AccountError("MICROSOFT_CLIENT_ID is not set in .env (docs/MICROSOFT_SETUP.md)")
    else:
        if not a.host:
            raise AccountError("--host is required for password accounts (e.g. imap.example.com)")
        acct.host, acct.port = a.host, a.port or 993
        pw = getpass.getpass(f"IMAP password (or app password) for {acct.username}: ")
        if not pw:
            raise AccountError("no password entered")
        acct.encrypted_password = crypto.encrypt(_key(settings), pw)
    session.add(acct)
    session.flush()
    nxt = (f"authorize {a.label}" if a.auth == "microsoft_oauth" else f"test {a.label}")
    return f"Account {a.label!r} added. Next: python -m mailassistant.mail.accounts {nxt}"


def cmd_authorize(session: Session, settings: Settings, label: str, browser: bool = False) -> str:
    from mailassistant.mail import msauth

    acct = _get(session, label)
    if acct.auth != "microsoft_oauth":
        raise AccountError("only Microsoft accounts are authorised this way")
    if browser:
        tok = msauth.browser_login(settings, print, input, login_hint=acct.username or acct.address)
    else:
        tok = msauth.device_login(settings, print)
    save_ms_refresh_token(session, settings, label, tok["refresh_token"], tok.get("scope", "").split())
    return f"Authorisation for {label!r} stored (encrypted). Next: python -m mailassistant.mail.accounts test {label}"


def cmd_test(session: Session, settings: Settings, label: str) -> str:
    from mailassistant.mail.mailboxes import mailbox_role, select_mailboxes

    acct = _get(session, label)
    src = source_for(session, settings, acct)
    try:
        with src:
            infos = src.list_mailboxes()
    except MailSourceError as e:
        hint = ""
        if acct.auth == "microsoft_oauth" and "AUTHENTICATE" in str(e).upper():
            hint = ("\nMicrosoft refused the login. Common causes: IMAP is disabled for this mailbox (admin: "
                    "Exchange admin center > mailbox > Email apps > IMAP), or the organisation does not allow "
                    "this app (admin consent needed). See docs/MICROSOFT_SETUP.md.")
        raise AccountError(f"connection failed: {type(e).__name__}: {e}{hint}") from e
    inc, exc = sync_patterns(acct, settings)
    chosen = {i.name for i in select_mailboxes(infos, inc, exc) if mailbox_role(i.name, i.flags) not in SKIP_ROLES}
    lines = [f"Connected to {acct.host} as {acct.username}. Folders (* = synchronised):"]
    lines += [f"  {'*' if i.name in chosen else ' '} {i.name}  [{mailbox_role(i.name, i.flags)}]" for i in infos]
    return "\n".join(lines)


def cmd_list(session: Session, settings: Settings) -> str:
    rows = [f"{'label':12} {'address':35} {'auth':16} {'enabled':7} host"]
    p = proton_account(session, settings) if settings.imap_username else None
    if p:
        rows.append(f"{'bridge':12} {p.address:35} {'bridge (.env)':16} {'yes':7} {settings.imap_host}")
    for a in extra_accounts(session, enabled_only=False):
        rows.append(f"{a.label:12} {a.address:35} {a.auth:16} {'yes' if a.enabled else 'no':7} {a.host}")
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    from mailassistant.config import get_settings
    from mailassistant.db import audit
    from mailassistant.db.session import session_scope

    ap = argparse.ArgumentParser(prog="python -m mailassistant.mail.accounts")
    sub = ap.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add", help="add an extra IMAP account")
    add.add_argument("--label", required=True)
    add.add_argument("--address", required=True)
    add.add_argument("--auth", choices=["microsoft_oauth", "password"], default="microsoft_oauth")
    add.add_argument("--host")
    add.add_argument("--port", type=int)
    add.add_argument("--username", help="login name if different from the address")
    add.add_argument("--aliases", help="other addresses of this account, comma-separated")
    add.add_argument("--include", help="folders to sync (fnmatch patterns, comma-separated; default: all)")
    add.add_argument("--exclude", help="folders to skip (default: Exchange's non-mail folders)")
    sub.add_parser("list")
    auth = sub.add_parser("authorize", help="sign in (Microsoft accounts)")
    auth.add_argument("label")
    auth.add_argument("--browser", action="store_true",
                      help="sign in with a link and paste the address back (when sign-in with a code is blocked)")
    for name in ("test", "disable", "enable"):
        sub.add_parser(name).add_argument("label")
    a = ap.parse_args(argv)
    s = get_settings()
    try:
        with session_scope() as session:
            if a.cmd == "add":
                out = cmd_add(session, s, a)
                audit.record(session, actor="user", action="mail_account_added", detail=a.label)
            elif a.cmd == "authorize":
                out = cmd_authorize(session, s, a.label, browser=a.browser)
                audit.record(session, actor="user", action="mail_account_authorized", detail=a.label)
            elif a.cmd == "test":
                out = cmd_test(session, s, a.label)
            elif a.cmd in ("disable", "enable"):
                _get(session, a.label).enabled = a.cmd == "enable"
                audit.record(session, actor="user", action=f"mail_account_{a.cmd}d", detail=a.label)
                out = f"{a.label}: {a.cmd}d"
            else:
                out = cmd_list(session, s)
    except AccountError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - e.g. Microsoft errors; show the class and message, no mail content
        print(f"Error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
