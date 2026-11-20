"""Primary Ollama (desktop GPU) first, fallback Ollama (e.g. small CPU model on the home server) when the primary
is not reachable. Results of the fallback are final; `served_by` tells the caller which one answered.

Same interface as OllamaClient (`chat`, `healthy`, `model`), so run_structured and the services do not change.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable

from mailassistant.llm.client import LLMModelMissing, LLMReply, LLMUnavailable, OllamaClient

log = logging.getLogger(__name__)

DOWN_SECONDS = 60.0     # after a failure, the primary is skipped this long
TRUST_SECONDS = 60.0    # an answer this recent means: no health check needed before the next call

# Shared per worker process (all tasks), keyed by base URL: url -> (down_until, last_ok)
_state: dict[str, tuple[float, float]] = {}

TASKS = ("classify", "event", "followup", "voice")


def reset_state() -> None:
    _state.clear()


class FallbackClient:
    def __init__(self, primary: OllamaClient, fallback: OllamaClient, *,
                 clock: Callable[[], float] = time.monotonic):
        self.primary = primary
        self.fallback = fallback
        self._clock = clock
        self.model = primary.model          # the model that answered last (set after every chat)
        self.served_by: str | None = None   # "desktop" | "server" after a chat

    def _primary_usable(self) -> bool:
        now = self._clock()
        down_until, last_ok = _state.get(self.primary.base_url, (0.0, 0.0))
        if now < down_until:
            return False
        if now - last_ok <= TRUST_SECONDS:
            return True
        if self.primary.healthy():          # 5 s: a switched-off desktop costs seconds, not the chat timeout
            return True
        self._mark_down("health check failed")
        return False

    def _mark_down(self, why: str) -> None:
        now = self._clock()
        _, last_ok = _state.get(self.primary.base_url, (0.0, 0.0))
        _state[self.primary.base_url] = (now + DOWN_SECONDS, last_ok)
        log.info("primary Ollama not usable (%s); using the fallback", why)

    def chat(self, messages: list[dict], schema: dict | None = None, *, temperature: float = 0.0) -> LLMReply:
        if self._primary_usable():
            try:
                reply = self.primary.chat(messages, schema, temperature=temperature)
            except (LLMUnavailable, LLMModelMissing) as e:
                self._mark_down(type(e).__name__)
            else:
                _state[self.primary.base_url] = (0.0, self._clock())
                self.model, self.served_by = self.primary.model, "desktop"
                return reply
        reply = self.fallback.chat(messages, schema, temperature=temperature)   # errors propagate: both down
        self.model, self.served_by = self.fallback.model, "server"
        return reply

    def healthy(self) -> bool:
        return self.primary.healthy() or self.fallback.healthy()


def _desktop_model(s, task: str) -> str:
    if task not in TASKS:
        raise ValueError(f"unknown LLM task {task!r}")
    if task == "followup":
        return s.followup_ollama_model or s.ollama_model
    if task == "voice":
        return s.capture_voice_followup_model or s.followup_ollama_model or s.ollama_model
    return s.ollama_model


def fallback_client(s) -> OllamaClient | None:
    if not (s.ollama_fallback_url or "").strip() or not (s.ollama_fallback_model or "").strip():
        return None
    return OllamaClient(s.ollama_fallback_url.strip(), s.ollama_fallback_model.strip(),
                        timeout=s.ollama_fallback_timeout_seconds, num_ctx=s.ollama_num_ctx)


def llm_client(s, task: str) -> OllamaClient | FallbackClient:
    """The client for one task: the primary Ollama with the task's model, plus the fallback when configured."""
    primary = OllamaClient.from_settings(s)
    primary.model = _desktop_model(s, task)
    fb = fallback_client(s)
    return FallbackClient(primary, fb) if fb is not None else primary
