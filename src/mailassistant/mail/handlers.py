import logging
from datetime import timedelta

from mailassistant.config import get_settings
from mailassistant.db import audit
from mailassistant.db.session import session_scope
from mailassistant.db.status import set_status
from mailassistant.jobs.registry import Deferred, handler
from mailassistant.mail.imap_bridge import ImapBridgeSource
from mailassistant.mail.source import (BridgeCertMismatch, MailSourceAuthError, MailSourceError,
                                       MailSourceUnavailable)
from mailassistant.mail.sync import sync_account

log = logging.getLogger(__name__)


def _bridge_status(ok: bool, detail: str) -> None:
    with session_scope() as session:
        set_status(session, "bridge", ok, detail)


@handler("mail.sync")
def mail_sync(payload: dict) -> None:
    """payload: {"full": bool}. Regular runs are cheap; the hourly full run also detects removals."""
    s = get_settings()
    if not (s.imap_username and s.imap_password and s.bridge_cert_sha256):
        _bridge_status(False, "not configured")
        raise Deferred("mail sync not configured (IMAP_USERNAME, imap_password, BRIDGE_CERT_SHA256)",
                       timedelta(minutes=10))
    try:
        with ImapBridgeSource.from_settings(s) as source, session_scope() as session:
            stats = sync_account(session, source, s, full=bool(payload.get("full")),
                                 budget_seconds=120)
    except MailSourceUnavailable as e:  # Bridge not running yet / desktop just booted
        _bridge_status(False, "bridge unavailable")
        raise Deferred(f"bridge unavailable: {e}", timedelta(minutes=2)) from e
    except (BridgeCertMismatch, MailSourceAuthError) as e:
        log.error("mail sync blocked: %s", e)
        _bridge_status(False, f"blocked: {type(e).__name__}")
        with session_scope() as session:
            audit.record(session, actor="system", action="mail_sync_blocked", detail=str(e)[:500])
        raise Deferred(str(e), timedelta(minutes=30)) from e
    except MailSourceError:
        raise
    _bridge_status(stats.errors == 0, f"last sync: new={stats.new_messages} errors={stats.errors} "
                                      f"remaining={stats.remaining}")
    if stats.new_messages or stats.linked_existing or stats.removed or stats.errors or stats.remaining:
        log.info("mail sync: new=%d linked=%d removed=%d errors=%d remaining=%d",
                 stats.new_messages, stats.linked_existing, stats.removed, stats.errors,
                 stats.remaining)
