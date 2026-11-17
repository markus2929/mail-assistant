"""Minimal Ollama client (standard library only). All inference stays on the local Ollama server.

Errors are split by what the caller should do:
  LLMUnavailable  - server down / timeout / 5xx: defer the job and retry later (no attempt used)
  LLMModelMissing - model not pulled: defer for a long time and tell the operator
  LLMBadResponse  - the HTTP reply itself is garbage: treated as a failed attempt
"""
from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


class LLMError(Exception):
    pass


class LLMUnavailable(LLMError):
    pass


class LLMModelMissing(LLMError):
    pass


class LLMBadResponse(LLMError):
    pass


@dataclass(frozen=True)
class LLMReply:
    content: str
    duration_ms: int
    model: str
    prompt_tokens: int | None = None  # as reported by Ollama (prompt_eval_count)


_THINK_RE = re.compile(r"(?is)<think>.*?</think>")


def strip_thinking(text: str) -> str:
    return _THINK_RE.sub("", text).strip()


class OllamaClient:
    def __init__(self, base_url: str, model: str, *, timeout: float = 180.0,
                 num_ctx: int | None = None, num_predict: int = 400):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.num_ctx = num_ctx
        self.num_predict = num_predict

    @classmethod
    def from_settings(cls, s) -> OllamaClient:
        return cls(s.ollama_base_url, s.ollama_model, timeout=s.ollama_timeout_seconds,
                   num_ctx=s.ollama_num_ctx)

    def healthy(self) -> bool:
        try:
            with urllib.request.urlopen(self.base_url + "/api/tags", timeout=3) as r:
                return r.status == 200
        except (OSError, ValueError):
            return False

    def _post(self, payload: dict) -> dict:
        req = urllib.request.Request(
            self.base_url + "/api/chat", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.load(r)

    def chat(self, messages: list[dict], schema: dict | None = None, *,
             temperature: float = 0.0) -> LLMReply:
        options: dict = {"temperature": temperature, "num_predict": self.num_predict}
        if self.num_ctx:
            options["num_ctx"] = self.num_ctx
        payload: dict = {"model": self.model, "messages": messages, "stream": False,
                         "think": False, "options": options}
        if schema is not None:
            payload["format"] = schema  # constrained decoding to the JSON schema
        started = time.monotonic()
        try:
            try:
                body = self._post(payload)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                if e.code == 400 and "think" in detail.lower():
                    payload.pop("think")  # model/server without "thinking" support
                    body = self._post(payload)
                elif e.code == 404:
                    raise LLMModelMissing(f"model {self.model!r} not found on Ollama: {detail}") from e
                elif e.code >= 500:
                    raise LLMUnavailable(f"Ollama HTTP {e.code}: {detail}") from e
                else:
                    raise LLMBadResponse(f"Ollama HTTP {e.code}: {detail}") from e
        except urllib.error.HTTPError as e:  # the think-less retry failed too
            raise LLMBadResponse(f"Ollama HTTP {e.code}") from e
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            raise LLMUnavailable(f"cannot reach Ollama at {self.base_url}: {e}") from e
        except ValueError as e:  # invalid JSON envelope
            raise LLMBadResponse(f"unreadable Ollama response: {e}") from e
        content = (body.get("message") or {}).get("content") if isinstance(body, dict) else None
        if not isinstance(content, str):
            raise LLMBadResponse("Ollama response has no message content")
        pt = body.get("prompt_eval_count")
        return LLMReply(strip_thinking(content), int((time.monotonic() - started) * 1000), self.model,
                        pt if isinstance(pt, int) else None)
