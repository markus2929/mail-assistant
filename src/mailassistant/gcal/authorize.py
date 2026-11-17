"""One-time Google authorisation (OAuth 2.0 authorization-code flow with PKCE and a loopback redirect).

    docker compose run --rm --no-deps worker python -m mailassistant.gcal.authorize

Open the printed URL in a browser on this machine. The container uses host networking, so the
redirect to http://127.0.0.1:PORT reaches it. The refresh token is stored encrypted in the database.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import urllib.parse
import urllib.request

from mailassistant.gcal.google import SCOPE, SCOPE_CALENDAR_LIST, SCOPES, TOKEN_URL

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


def make_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_auth_url(client_id: str, redirect_uri: str, state: str, challenge: str, auth_url: str = AUTH_URL) -> str:
    return auth_url + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": " ".join(SCOPES),
        "access_type": "offline", "prompt": "consent",   # consent => Google returns a refresh token
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})


def exchange_code(client_id: str, client_secret: str, code: str, verifier: str, redirect_uri: str,
                  token_url: str = TOKEN_URL) -> dict:
    data = urllib.parse.urlencode({
        "client_id": client_id, "client_secret": client_secret, "code": code, "code_verifier": verifier,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code"}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(token_url, data=data, method="POST"), timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"token exchange failed: {e.read().decode('utf-8', 'replace')[:300]}") from e


def load_client_file(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    c = raw.get("installed") or raw.get("web") or raw
    if not c.get("client_id") or not c.get("client_secret"):
        raise ValueError("client file has no client_id/client_secret (download the Desktop app JSON)")
    return c


def wait_for_callback(port: int, timeout: float = 300.0) -> dict:
    """Run a one-shot loopback server and return the query parameters Google redirects with."""
    result: dict = {}
    done = threading.Event()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Authorisation received. You can close this tab and return to the terminal.")
            done.set()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        if not done.wait(timeout):
            raise TimeoutError("no authorisation received within 5 minutes")
    finally:
        srv.shutdown()
        srv.server_close()
    return result


def main() -> int:
    from mailassistant.config import get_settings
    from mailassistant.db.session import session_scope
    from mailassistant.gcal.tokens import save_refresh_token

    s = get_settings()
    try:
        client = load_client_file(s.google_client_file)
    except FileNotFoundError:
        print(f"{s.google_client_file} is not available here. Google commands run in the worker container (the "
              "dashboard has no Google secrets):\n  docker compose run --rm --no-deps worker python -m "
              "mailassistant.gcal.authorize")
        return 1
    except ValueError as e:
        print(f"{s.google_client_file} is not a usable Desktop-app client file ({e}); see docs/GOOGLE_SETUP.md step 3")
        return 1
    port = int(os.environ.get("GOOGLE_AUTH_PORT", "8765"))
    redirect = f"http://127.0.0.1:{port}/"
    verifier, challenge = make_pkce()
    state = secrets.token_urlsafe(24)
    print("\nOpen this URL in your browser (on this computer) and approve access:\n")
    print(build_auth_url(client["client_id"], redirect, state, challenge))
    print(f"\nWaiting for Google to redirect to {redirect} ...")
    cb = wait_for_callback(port)
    if cb.get("error"):
        print(f"Google reported an error: {cb['error']}")
        return 1
    if cb.get("state") != state:
        print("State mismatch: aborting (possible forged request).")
        return 1
    tok = exchange_code(client["client_id"], client["client_secret"], cb["code"], verifier, redirect,
                        client.get("token_uri", TOKEN_URL))
    if not tok.get("refresh_token"):
        print("Google returned no refresh token. Remove the app's access at "
              "https://myaccount.google.com/permissions and run this again.")
        return 1
    granted = tok.get("scope", "").split()
    if SCOPE not in granted:
        print(f"Warning: granted scopes are {tok.get('scope')!r}; calendar.events is required.")
        return 1
    if SCOPE_CALENDAR_LIST not in granted:
        print("Note: the calendar-list permission was not granted. Calendars must then be given by ID in .env,\n"
              "and conflict checks cover only the configured calendars (see docs/GOOGLE_SETUP.md).")
    with session_scope() as session:
        save_refresh_token(session, s, tok["refresh_token"], tok["scope"].split())
    print("Authorisation stored (encrypted). Next: python -m mailassistant.gcal.cli sync")
    return 0


if __name__ == "__main__":
    sys.exit(main())
