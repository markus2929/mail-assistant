"""Ollama on another machine behind the HTTPS proxy: certificate pin + bearer token."""
import hashlib
import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from mailassistant.llm.client import LLMCertMismatch, LLMUnavailable, OllamaClient
from mailassistant.web.makecert import build


@pytest.fixture
def proxy(tmp_path):
    cert_pem, key_pem, _ = build(["desktop"], ["127.0.0.1"])
    (tmp_path / "c.pem").write_bytes(cert_pem)
    (tmp_path / "k.pem").write_bytes(key_pem)
    der = x509.load_pem_x509_certificate(cert_pem).public_bytes(serialization.Encoding.DER)
    seen: list[dict] = []

    class H(BaseHTTPRequestHandler):
        def _reply(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            seen.append({"path": self.path, "auth": self.headers.get("Authorization")})
            ok = self.headers.get("Authorization") == "Bearer s3cret"
            self._reply(200 if ok else 403, {"models": []})

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            seen.append({"path": self.path, "auth": self.headers.get("Authorization")})
            if self.headers.get("Authorization") != "Bearer s3cret":
                return self._reply(401, {"error": "no"})
            self._reply(200, {"message": {"content": "{\"ok\": true}"}, "prompt_eval_count": 7})

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"https://127.0.0.1:{srv.server_address[1]}", hashlib.sha256(der).hexdigest(), seen
    srv.shutdown()


def test_pinned_https_with_token(proxy):
    url, pin, seen = proxy
    c = OllamaClient(url, "m", token="s3cret", cert_sha256=":".join(pin[i:i + 2] for i in range(0, 64, 2)).upper())
    r = c.chat([{"role": "user", "content": "x"}])
    assert r.content == '{"ok": true}' and r.prompt_tokens == 7
    assert c.healthy() and seen[0] == {"path": "/api/chat", "auth": "Bearer s3cret"}


def test_wrong_pin_sends_nothing(proxy):
    url, _, seen = proxy
    c = OllamaClient(url, "m", token="s3cret", cert_sha256="00" * 32)
    with pytest.raises(LLMCertMismatch):
        c.chat([{"role": "user", "content": "x"}])
    assert not c.healthy() and seen == []                     # the token never left the client


def test_wrong_token_is_reported_and_deferred(proxy):
    url, pin, _ = proxy
    with pytest.raises(LLMUnavailable, match="OLLAMA_TOKEN"):
        OllamaClient(url, "m", token="wrong", cert_sha256=pin).chat([{"role": "user", "content": "x"}])


def test_token_never_over_plain_http_to_another_host():
    with pytest.raises(LLMUnavailable, match="plain HTTP"):
        OllamaClient("http://192.168.1.20:11434", "m", token="s3cret").chat([{"role": "user", "content": "x"}])
    assert OllamaClient("http://192.168.1.20:11434", "m", token="s3cret").healthy() is False
