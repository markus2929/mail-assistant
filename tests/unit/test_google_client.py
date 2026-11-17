import json
import threading
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mailassistant.gcal.authorize import (build_auth_url, exchange_code, load_client_file, make_pkce,
                                          wait_for_callback)
from mailassistant.gcal.google import (GoogleApiError, GoogleAuth, GoogleAuthRevoked, GoogleCalendarClient,
                                       GoogleConflict, GoogleUnavailable, rfc3339)


class FakeGoogle:
    """Replays a script of (status, body) answers and records every request."""

    def __init__(self, script):
        self.script, self.seen = list(script), []
        outer = self

        class H(BaseHTTPRequestHandler):
            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.seen.append({"method": self.command, "path": self.path, "auth": self.headers.get("Authorization"),
                                   "body": self.rfile.read(n).decode()})
                status, body = outer.script.pop(0)
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _do

            def log_message(self, *a):
                pass

        class Quiet(ThreadingHTTPServer):
            def handle_error(self, *a):
                pass

        self.server = Quiet(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


TOKEN_OK = (200, {"access_token": "AT1", "expires_in": 3600})
T0, T1 = datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC)


def make(script):
    g = FakeGoogle(script)
    auth = GoogleAuth("cid", "secret", "refresh-me", token_url=g.url + "/token")
    return g, GoogleCalendarClient(auth, base_url=g.url + "/calendar/v3")


def test_token_refresh_is_cached_and_used_as_bearer():
    g, c = make([TOKEN_OK, (200, {"items": [{"id": "a"}]}), (200, {"items": []})])
    try:
        assert c.list_events("primary", T0, T1) == [{"id": "a"}]
        c.list_events("me@example.com", T0, T1)
        assert [s["path"].split("?")[0] for s in g.seen] == ["/token", "/calendar/v3/calendars/primary/events",
                                                              "/calendar/v3/calendars/me%40example.com/events"]
        assert "grant_type=refresh_token" in g.seen[0]["body"] and "refresh_token=refresh-me" in g.seen[0]["body"]
        assert g.seen[1]["auth"] == "Bearer AT1" and g.seen[2]["auth"] == "Bearer AT1"   # token reused
        q = urllib.parse.parse_qs(urllib.parse.urlparse(g.seen[1]["path"]).query)
        assert q["singleEvents"] == ["true"] and q["timeMin"] == ["2026-11-01T00:00:00Z"]
    finally:
        g.close()


def test_pagination_and_401_refresh_retry():
    g, c = make([TOKEN_OK, (401, {"error": {"message": "expired"}}), TOKEN_OK,
                 (200, {"items": [{"id": "1"}], "nextPageToken": "P2"}), (200, {"items": [{"id": "2"}]})])
    try:
        assert [e["id"] for e in c.list_events("primary", T0, T1)] == ["1", "2"]
        assert "pageToken=P2" in g.seen[-1]["path"]
    finally:
        g.close()


def expect(exc, script, call):
    g, c = make(script)
    try:
        call(c)
    except exc as e:
        return e
    finally:
        g.close()
    raise AssertionError(f"expected {exc.__name__}")


def test_error_mapping():
    expect(GoogleAuthRevoked, [(400, {"error": "invalid_grant"})], lambda c: c.list_events("primary", T0, T1))
    expect(GoogleUnavailable, [TOKEN_OK, (503, {"error": {"message": "busy"}})], lambda c: c.list_events("primary", T0, T1))
    expect(GoogleUnavailable, [TOKEN_OK, (403, {"error": {"message": "x", "errors": [{"reason": "rateLimitExceeded"}]}})],
           lambda c: c.list_events("primary", T0, T1))
    e = expect(GoogleApiError, [TOKEN_OK, (404, {"error": {"message": "Not Found"}})], lambda c: c.get_event("primary", "x"))
    assert e.status == 404
    expect(GoogleConflict, [TOKEN_OK, (409, {"error": {"message": "identifier already exists"}})],
           lambda c: c.insert_event("primary", {"id": "abc"}))


def test_insert_never_sends_updates():
    g, c = make([TOKEN_OK, (200, {"id": "abc"})])
    try:
        assert c.insert_event("primary", {"id": "abc", "summary": "x"}) == {"id": "abc"}
        assert "sendUpdates=none" in g.seen[1]["path"] and json.loads(g.seen[1]["body"])["summary"] == "x"
    finally:
        g.close()


def test_unreachable_google_is_unavailable():
    c = GoogleCalendarClient(GoogleAuth("a", "b", "c", token_url="http://127.0.0.1:1/token", timeout=1),
                             base_url="http://127.0.0.1:1")
    try:
        c.list_events("primary", T0, T1)
    except GoogleUnavailable:
        return
    raise AssertionError("expected GoogleUnavailable")


def test_pkce_and_auth_url():
    v, ch = make_pkce()
    assert 43 <= len(v) <= 128 and ch and "=" not in ch
    q = urllib.parse.parse_qs(urllib.parse.urlparse(build_auth_url("cid", "http://127.0.0.1:8765/", "st", ch)).query)
    assert q["scope"] == ["https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.calendarlist.readonly"]
    assert q["code_challenge_method"] == ["S256"] and q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert q["state"] == ["st"] and q["redirect_uri"] == ["http://127.0.0.1:8765/"]


def test_code_exchange_and_callback_server(tmp_path=None):
    g = FakeGoogle([(200, {"access_token": "a", "refresh_token": "r", "scope": "x"})])
    try:
        tok = exchange_code("cid", "sec", "CODE", "VERIFIER", "http://127.0.0.1:8765/", g.url + "/token")
        assert tok["refresh_token"] == "r"
        body = urllib.parse.parse_qs(g.seen[0]["body"])
        assert body["code"] == ["CODE"] and body["code_verifier"] == ["VERIFIER"] and body["grant_type"] == ["authorization_code"]
    finally:
        g.close()
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    threading.Timer(0.3, lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/?code=C&state=S").read()).start()
    assert wait_for_callback(port, timeout=5) == {"code": "C", "state": "S"}


def test_client_file_formats(tmp_path=None):
    import os
    import tempfile
    d = tempfile.mkdtemp()
    p = os.path.join(d, "c.json")
    open(p, "w").write(json.dumps({"installed": {"client_id": "i", "client_secret": "s"}}))
    assert load_client_file(p)["client_id"] == "i"
    open(p, "w").write("{}")
    try:
        load_client_file(p)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_rfc3339_is_utc():
    assert rfc3339(datetime(2026, 11, 1, 1, 0, tzinfo=UTC)) == "2026-11-01T01:00:00Z"
