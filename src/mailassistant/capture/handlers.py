import copy
import logging
import time
from datetime import timedelta

from mailassistant.capture.service import process_capture, register_mail_captures
from mailassistant.config import get_settings
from mailassistant.db.session import session_scope
from mailassistant.jobs.registry import Deferred, handler
from mailassistant.llm.client import LLMModelMissing, LLMUnavailable, OllamaClient

log = logging.getLogger(__name__)
_down_until = 0.0


@handler("capture.scan_mail")
def capture_scan_mail(payload: dict) -> None:
    s = get_settings()
    with session_scope() as session:
        made = register_mail_captures(session, s)
    if made:
        log.info("captures from mail: %d", len(made))


@handler("capture.process")
def capture_process(payload: dict) -> None:
    global _down_until
    if time.monotonic() < _down_until:
        raise Deferred("ollama unavailable (waiting)", timedelta(minutes=2))
    s = get_settings()
    event_client = OllamaClient.from_settings(s)
    followup_client = copy.copy(event_client)
    if s.followup_ollama_model:
        followup_client.model = s.followup_ollama_model
    voice_client = None
    if s.capture_voice_followup_model:
        voice_client = copy.copy(event_client)
        voice_client.model = s.capture_voice_followup_model
    try:
        with session_scope() as session:
            cap = process_capture(session, int(payload["capture_id"]), event_client, followup_client, s,
                                  voice_followup_client=voice_client)
        if cap is not None:
            log.info("capture %s: %s", cap.id, cap.status)
    except LLMUnavailable as e:
        _down_until = time.monotonic() + 60
        raise Deferred(f"ollama unavailable: {e}", timedelta(minutes=2)) from e
    except LLMModelMissing as e:
        _down_until = time.monotonic() + 600
        raise Deferred(str(e), timedelta(minutes=30)) from e
