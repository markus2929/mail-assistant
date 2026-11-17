import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mailassistant.llm.client import (LLMBadResponse, LLMModelMissing, LLMUnavailable, OllamaClient,
                                      strip_thinking)


class FakeOllama:
    """Local HTTP server that replays a script of (status, body, delay_seconds) answers."""

    def __init__(self, script):
        outer = self
        self.script, self.seen = list(script), []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.seen.append(payload)
                status, body, delay = outer.script.pop(0)
                time.sleep(delay)
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        class Quiet(ThreadingHTTPServer):
            def handle_error(self, request, client_address):  # client hung up (timeout test)
                pass

        self.server = Quiet(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def ok(content):
    return (200, {"message": {"role": "assistant", "content": content}, "prompt_eval_count": 321}, 0)


def run(script, **kw):
    srv = FakeOllama(script)
    try:
        client = OllamaClient(srv.url, "test-model", **kw)
        return srv, client
    except Exception:
        srv.close()
        raise


def test_request_is_local_schema_constrained_and_deterministic():
    srv, c = run([ok('<think>hmm</think>{"a": 1}')], num_ctx=8192)
    try:
        reply = c.chat([{"role": "user", "content": "hi"}], {"type": "object"})
        p = srv.seen[0]
        assert reply.content == '{"a": 1}' and reply.model == "test-model" and reply.prompt_tokens == 321
        assert p["model"] == "test-model" and p["stream"] is False and p["think"] is False
        assert p["format"] == {"type": "object"}
        assert p["options"]["temperature"] == 0.0 and p["options"]["num_ctx"] == 8192
        assert c.healthy()
    finally:
        srv.close()


def expect(exc, script, **kw):
    srv, c = run(script, **kw)
    try:
        c.chat([{"role": "user", "content": "x"}])
    except exc:
        return srv
    finally:
        srv.close()
    raise AssertionError(f"expected {exc.__name__}")


def test_error_mapping():
    expect(LLMModelMissing, [(404, {"error": "model not found"}, 0)])
    expect(LLMUnavailable, [(503, {"error": "busy"}, 0)])
    expect(LLMBadResponse, [(200, b"not json at all", 0)])
    expect(LLMBadResponse, [(200, {"message": {}}, 0)])
    expect(LLMBadResponse, [(400, {"error": "bad request"}, 0)])


def test_timeout_and_connection_refused_mean_unavailable():
    expect(LLMUnavailable, [ok("{}")[:2] + (0.5,)], timeout=0.1)
    try:
        OllamaClient("http://127.0.0.1:1", "m", timeout=1).chat([{"role": "user", "content": "x"}])
    except LLMUnavailable:
        assert not OllamaClient("http://127.0.0.1:1", "m").healthy()
        return
    raise AssertionError("expected LLMUnavailable")


def test_models_without_think_support_are_retried_without_it():
    srv, c = run([(400, {"error": "model does not support think"}, 0), ok("{}")])
    try:
        assert c.chat([{"role": "user", "content": "x"}]).content == "{}"
        assert "think" in srv.seen[0] and "think" not in srv.seen[1]
    finally:
        srv.close()


def test_strip_thinking():
    assert strip_thinking("<think>a\nb</think>  answer") == "answer"
