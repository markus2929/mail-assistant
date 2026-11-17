"""Google OAuth2 token refresh and a minimal Calendar v3 REST client (standard library only).

Scopes: calendar.events (read and write events) and calendar.calendarlist.readonly (see which calendars
exist, so .env may name a calendar instead of giving its ID). Nothing else.
The client never sends invitations: events are inserted with sendUpdates=none and without attendees.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime

SCOPE = "https://www.googleapis.com/auth/calendar.events"
SCOPE_CALENDAR_LIST = "https://www.googleapis.com/auth/calendar.calendarlist.readonly"
SCOPES = [SCOPE, SCOPE_CALENDAR_LIST]
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://www.googleapis.com/calendar/v3"


class GoogleError(Exception):
    pass


class GoogleNotConfigured(GoogleError):
    """No client secret file / no stored authorisation yet."""


class GoogleAuthRevoked(GoogleError):
    """invalid_grant: the refresh token expired or was revoked. Run the authorize command again."""


class GoogleUnavailable(GoogleError):
    """Network trouble, rate limit or 5xx: retry later."""


class GoogleApiError(GoogleError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Google API {status}: {message}")
        self.status = status


class GoogleScopeMissing(GoogleApiError):
    """HTTP 403 insufficient scope: the stored authorisation predates a permission this call needs."""


class GoogleConflict(GoogleApiError):
    """HTTP 409: the event id already exists."""


def rfc3339(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _http(req: urllib.request.Request, timeout: float):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {"error": {"message": body[:300]}}
        return e.code, payload
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        raise GoogleUnavailable(f"cannot reach Google: {e}") from e
    except ValueError as e:
        raise GoogleUnavailable(f"unreadable Google response: {e}") from e


class GoogleAuth:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str, *,
                 token_url: str = TOKEN_URL, timeout: float = 30.0):
        self.client_id, self.client_secret, self.refresh_token = client_id, client_secret, refresh_token
        self.token_url, self.timeout = token_url, timeout
        self._token: str | None = None
        self._expires = 0.0

    def invalidate(self) -> None:
        self._token, self._expires = None, 0.0

    def access_token(self) -> str:
        if self._token and time.monotonic() < self._expires - 60:
            return self._token
        data = urllib.parse.urlencode({
            "client_id": self.client_id, "client_secret": self.client_secret,
            "refresh_token": self.refresh_token, "grant_type": "refresh_token"}).encode()
        status, body = _http(urllib.request.Request(self.token_url, data=data, method="POST"), self.timeout)
        if status != 200:
            err = body.get("error")
            if err == "invalid_grant":
                raise GoogleAuthRevoked("Google refused the refresh token (expired or revoked). "
                                        "Run: python -m mailassistant.gcal.authorize")
            if status in (429, 500, 502, 503, 504):
                raise GoogleUnavailable(f"token endpoint {status}")
            raise GoogleApiError(status, f"token endpoint error: {err}")
        self._token = body["access_token"]
        self._expires = time.monotonic() + int(body.get("expires_in", 3600))
        return self._token


class GoogleCalendarClient:
    def __init__(self, auth: GoogleAuth, *, base_url: str = API_BASE, timeout: float = 30.0):
        self.auth, self.base_url, self.timeout = auth, base_url.rstrip("/"), timeout

    def _request(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None,
                 _retry_auth: bool = True) -> dict:
        url = self.base_url + path + (("?" + urllib.parse.urlencode(params)) if params else "")
        req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.auth.access_token()}",
                                              "Content-Type": "application/json"})
        status, payload = _http(req, self.timeout)
        if status == 401 and _retry_auth:
            self.auth.invalidate()
            return self._request(method, path, params=params, body=body, _retry_auth=False)
        if 200 <= status < 300:
            return payload
        err = payload.get("error", {}) if isinstance(payload, dict) else {}
        msg = err.get("message", "") if isinstance(err, dict) else str(err)
        reasons = " ".join(e.get("reason", "") for e in (err.get("errors", []) if isinstance(err, dict) else []))
        if status == 409:
            raise GoogleConflict(status, msg)
        if status == 403 and ("insufficient" in reasons.lower() or "insufficient" in msg.lower()
                              or "scope" in msg.lower()):
            raise GoogleScopeMissing(status, msg)
        if status in (429, 500, 502, 503, 504) or "ratelimit" in reasons.lower():
            raise GoogleUnavailable(f"{status} {msg}")
        raise GoogleApiError(status, msg)

    @staticmethod
    def _cal(calendar_id: str) -> str:
        return urllib.parse.quote(calendar_id, safe="")

    def list_events(self, calendar_id: str, time_min: datetime, time_max: datetime) -> list[dict]:
        events, token = [], None
        while True:
            params = {"timeMin": rfc3339(time_min), "timeMax": rfc3339(time_max), "singleEvents": "true",
                      "orderBy": "startTime", "maxResults": "250", "showDeleted": "false"}
            if token:
                params["pageToken"] = token
            page = self._request("GET", f"/calendars/{self._cal(calendar_id)}/events", params=params)
            events += page.get("items", [])
            token = page.get("nextPageToken")
            if not token:
                return events

    def get_event(self, calendar_id: str, event_id: str) -> dict:
        return self._request("GET", f"/calendars/{self._cal(calendar_id)}/events/{urllib.parse.quote(event_id, safe='')}")

    def insert_event(self, calendar_id: str, body: dict) -> dict:
        # sendUpdates=none: Google must never email anyone because of this call
        return self._request("POST", f"/calendars/{self._cal(calendar_id)}/events",
                             params={"sendUpdates": "none"}, body=body)

    def list_calendars(self) -> list[dict]:
        """All entries of the user's calendar list (needs SCOPE_CALENDAR_LIST)."""
        items, token = [], None
        while True:
            params = {"maxResults": "250", "showDeleted": "false", "showHidden": "true"}
            if token:
                params["pageToken"] = token
            page = self._request("GET", "/users/me/calendarList", params=params)
            items += page.get("items", [])
            token = page.get("nextPageToken")
            if not token:
                return items
