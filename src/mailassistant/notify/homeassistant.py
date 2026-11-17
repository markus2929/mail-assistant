"""Minimal Home Assistant client (urllib): POST /api/services/notify/<service>. Only ever the user's own HA."""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request


class HANotConfigured(Exception):
    pass


class HAUnavailable(Exception):
    """Network problem or HA down: retry later."""


class HARefused(Exception):
    """HA answered with an error (wrong token, unknown service): needs the user."""


def send(base_url: str, service: str, token: str, title: str, message: str, *, url: str = "",
         ca_file: str = "", timeout: float = 15.0) -> None:
    if not (base_url and service and token):
        raise HANotConfigured("set HA_URL, HA_NOTIFY_SERVICE and secrets/ha_token")
    service = service.removeprefix("notify.")
    data: dict = {"title": title, "message": message}
    if url:
        data["data"] = {"url": url, "clickAction": url}  # opens the dashboard from the phone notification
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/services/notify/{service}", data=json.dumps(data).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    ctx = ssl.create_default_context(cafile=ca_file) if ca_file else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            r.read()
    except urllib.error.HTTPError as e:
        if e.code >= 500:
            raise HAUnavailable(f"HTTP {e.code}") from e
        raise HARefused(f"HTTP {e.code} (check the token and HA_NOTIFY_SERVICE)") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise HAUnavailable(type(e).__name__) from e
