"""Microsoft 365 / Outlook.com sign-in for IMAP (OAuth 2.0, public client, no secret).

Two ways (same result):
- device code: the CLI prints a short code and https://microsoft.com/devicelogin; you sign in on any device.
  Many organisations block this flow (Conditional Access, error 53003).
- browser (authorization code + PKCE): the CLI prints a sign-in link; after signing in, the browser goes to
  http://localhost/?code=... (nothing needs to listen there) and you paste that address into the CLI. Works on a
  headless server; the redirect never leaves your browser.
Only the refresh token is stored (Fernet-encrypted, `oauth_tokens`).
Scope: IMAP access to your own mailbox + offline_access (refresh token). Nothing else (no Graph, no send).
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

SCOPES = ["https://outlook.office.com/IMAP.AccessAsUser.All", "offline_access"]
AUTHORITY = "https://login.microsoftonline.com"
REDIRECT_URI = "http://localhost"   # register exactly this under "Mobile and desktop applications"


class MicrosoftAuthError(Exception):
    """Sign-in refused / expired / not configured. Not transient."""


class MicrosoftUnavailable(Exception):
    """Network problem talking to Microsoft. Transient."""


def _endpoint(settings, kind: str) -> str:
    tenant = (settings.microsoft_tenant or "common").strip()
    return f"{AUTHORITY}/{urllib.parse.quote(tenant, safe='')}/oauth2/v2.0/{kind}"


def _client_id(settings) -> str:
    cid = (settings.microsoft_client_id or "").strip()
    if not cid:
        raise MicrosoftAuthError("MICROSOFT_CLIENT_ID is not set in .env (docs/MICROSOFT_SETUP.md)")
    return cid


def _post(url: str, data: dict, opener=urllib.request.urlopen) -> tuple[int, dict]:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with opener(req, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:   # 400 with a JSON error body is normal while polling
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")
        except ValueError:
            return e.code, {"error": f"http_{e.code}"}
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise MicrosoftUnavailable(f"cannot reach Microsoft: {e}") from e


def device_login(settings, say: Callable[[str], None], *, opener=urllib.request.urlopen,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> dict:
    """Run the device-code flow. Returns the token response (with refresh_token)."""
    cid = _client_id(settings)
    status, dc = _post(_endpoint(settings, "devicecode"), {"client_id": cid, "scope": " ".join(SCOPES)}, opener)
    if status != 200 or "device_code" not in dc:
        raise MicrosoftAuthError(f"device code request refused: {dc.get('error')}: "
                                 f"{(dc.get('error_description') or '')[:300]}")
    say(dc.get("message") or f"Open {dc.get('verification_uri')} and enter the code {dc.get('user_code')}")
    interval = float(dc.get("interval") or 5)
    deadline = clock() + float(dc.get("expires_in") or 900)
    while clock() < deadline:
        sleep(interval)
        status, tok = _post(_endpoint(settings, "token"), {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code", "client_id": cid,
            "device_code": dc["device_code"]}, opener)
        if status == 200 and tok.get("access_token"):
            if not tok.get("refresh_token"):
                raise MicrosoftAuthError("Microsoft returned no refresh token (offline_access not granted)")
            return tok
        err = tok.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        raise MicrosoftAuthError(f"sign-in failed: {err}: {(tok.get('error_description') or '')[:300]}")
    raise MicrosoftAuthError("the code expired before sign-in was completed; run authorize again")


# ------------------------------------------------------------------ browser sign-in (authorization code + PKCE)
def make_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_auth_url(settings, state: str, challenge: str, login_hint: str | None = None) -> str:
    q = {"client_id": _client_id(settings), "response_type": "code", "redirect_uri": REDIRECT_URI,
         "response_mode": "query", "scope": " ".join(SCOPES), "state": state, "code_challenge": challenge,
         "code_challenge_method": "S256", "prompt": "select_account"}
    if login_hint:
        q["login_hint"] = login_hint
    return _endpoint(settings, "authorize") + "?" + urllib.parse.urlencode(q)


def code_from_redirect(pasted: str, state: str) -> str:
    """Take the address the browser ended up on (http://localhost/?code=...&state=...) and return the code."""
    pasted = (pasted or "").strip()
    query = urllib.parse.urlparse(pasted).query if "://" in pasted else pasted.lstrip("?")
    q = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
    if q.get("error"):
        raise MicrosoftAuthError(f"sign-in failed: {q['error']}: {(q.get('error_description') or '')[:300]}")
    if not q.get("code"):
        raise MicrosoftAuthError("no code in that address: paste the full address from the browser's address bar "
                                 "(it starts with http://localhost/?code=)")
    if q.get("state") != state:
        raise MicrosoftAuthError("state mismatch: this address does not belong to this sign-in; start again")
    return q["code"]


def exchange_code(settings, code: str, verifier: str, *, opener=urllib.request.urlopen) -> dict:
    status, tok = _post(_endpoint(settings, "token"), {
        "grant_type": "authorization_code", "client_id": _client_id(settings), "code": code,
        "redirect_uri": REDIRECT_URI, "code_verifier": verifier, "scope": " ".join(SCOPES)}, opener)
    if status == 200 and tok.get("access_token"):
        if not tok.get("refresh_token"):
            raise MicrosoftAuthError("Microsoft returned no refresh token (offline_access not granted)")
        return tok
    raise MicrosoftAuthError(f"code exchange refused: {tok.get('error')}: {(tok.get('error_description') or '')[:300]}")


def browser_login(settings, say: Callable[[str], None], ask: Callable[[str], str], login_hint: str | None = None,
                  *, opener=urllib.request.urlopen) -> dict:
    verifier, challenge = make_pkce()
    state = secrets.token_urlsafe(24)
    say("\n1. Open this link in your browser and sign in with the account:\n\n"
        + build_auth_url(settings, state, challenge, login_hint)
        + "\n\n2. Afterwards the browser shows an error page ('unable to connect' / 'site can't be reached') -"
          " that is expected. Copy the FULL address from the address bar (it starts with http://localhost/?code=).\n")
    code = code_from_redirect(ask("3. Paste the address here: "), state)
    return exchange_code(settings, code, verifier, opener=opener)


def refresh(settings, refresh_token: str, *, opener=urllib.request.urlopen) -> dict:
    """Exchange the stored refresh token for an access token (and possibly a new refresh token)."""
    status, tok = _post(_endpoint(settings, "token"), {
        "grant_type": "refresh_token", "client_id": _client_id(settings), "refresh_token": refresh_token,
        "scope": " ".join(SCOPES)}, opener)
    if status == 200 and tok.get("access_token"):
        return tok
    if status >= 500:
        raise MicrosoftUnavailable(f"Microsoft token endpoint answered {status}")
    raise MicrosoftAuthError(f"token refresh refused: {tok.get('error')} - run `accounts authorize` again "
                             f"({(tok.get('error_description') or '')[:200]})")
