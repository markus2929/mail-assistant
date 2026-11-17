"""Minimal Ollama client (standard library only). All inference stays on your own Ollama server.

Ollama may run on another machine (e.g. the desktop with the GPU) behind an HTTPS proxy: then the proxy's
certificate is pinned (OLLAMA_CERT_SHA256) and a bearer token (OLLAMA_TOKEN) is sent - only after the pin
matched, and never over plain HTTP to another host.

Errors are split by what the caller should do:
  LLMUnavailable  - server down / timeout / 5xx: defer the job and retry later (no attempt used)
  LLMModelMissing - model not pulled: defer for a long time and tell the operator
  LLMBadResponse  - the HTTP reply itself is garbage: treated as a failed attempt
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
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


class LLMCertMismatch(LLMUnavailable):
    """The HTTPS endpoint presented another certificate than OLLAMA_CERT_SHA256: nothing was sent."""


def _pin(value: str | None) -> str:
    return (value or "").replace(":", "").strip().lower()


class _PinnedConnection(http.client.HTTPSConnection):
    expected = ""

    def connect(self) -> None:
        super().connect()
        fp = hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest()
        if not hmac.compare_digest(fp, self.expected):
            self.sock.close()
            raise LLMCertMismatch(f"Ollama endpoint certificate {fp[:16]}... does not match OLLAMA_CERT_SHA256; "
                                  "nothing was sent")


class _PinnedHandler(urllib.request.HTTPSHandler):
    """HTTPS where trust comes from the pinned fingerprint (self-signed proxy certificate)."""

    def __init__(self, pin: str):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE        # replaced by the fingerprint check in _PinnedConnection
        super().__init__(context=ctx)
        self._pin = pin

    def https_open(self, req):
        pin = self._pin

        def factory(host, **kw):
            c = _PinnedConnection(host, **kw)
            c.expected = pin
            return c
        return self.do_open(factory, req, context=self._context)


def _is_loopback(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


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
                 num_ctx: int | None = None, num_predict: int = 400, token: str | None = None,
                 cert_sha256: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.num_ctx = num_ctx
        self.num_predict = num_predict
        self.token = (token or "").strip() or None
        pin = _pin(cert_sha256)
        self._opener = urllib.request.build_opener(_PinnedHandler(pin)) if pin else urllib.request.build_opener()

    @classmethod
    def from_settings(cls, s) -> OllamaClient:
        return cls(s.ollama_base_url, s.ollama_model, timeout=s.ollama_timeout_seconds,
                   num_ctx=s.ollama_num_ctx,
                   token=s.ollama_token.get_secret_value() if getattr(s, "ollama_token", None) else None,
                   cert_sha256=getattr(s, "ollama_cert_sha256", None))

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.token:
            if not self.base_url.startswith("https://") and not _is_loopback(self.base_url):
                raise LLMUnavailable("refusing to send OLLAMA_TOKEN over plain HTTP; use the https:// proxy address")
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def healthy(self) -> bool:
        try:
            req = urllib.request.Request(self.base_url + "/api/tags", headers=self._headers())
            with self._opener.open(req, timeout=5) as r:
                return r.status == 200
        except (OSError, ValueError, LLMError):
            return False

    def _post(self, payload: dict) -> dict:
        req = urllib.request.Request(
            self.base_url + "/api/chat", data=json.dumps(payload).encode(),
            headers=self._headers(), method="POST")
        with self._opener.open(req, timeout=self.timeout) as r:
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
                elif e.code in (401, 403):
                    raise LLMUnavailable(f"Ollama proxy refused the request (HTTP {e.code}): check OLLAMA_TOKEN") from e
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
