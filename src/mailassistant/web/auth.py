"""Password hashing, session tokens and login throttling for the dashboard. Standard library only."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

_N, _R, _P = 2**15, 8, 1  # scrypt cost: ~0.1 s and 32 MiB per check


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=64 * 1024 * 1024, dklen=32)
    b = lambda x: base64.b64encode(x).decode()  # noqa: E731
    return f"scrypt${_N}${_R}${_P}${b(salt)}${b(dk)}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, n, r, p, salt, dk = stored.strip().split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             maxmem=64 * 1024 * 1024, dklen=len(base64.b64decode(dk)))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, base64.b64decode(dk))


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """Only this hash is stored, so a database leak does not hand out live sessions."""
    return hashlib.sha256(token.encode()).hexdigest()


def same(a: str | None, b: str | None) -> bool:
    return bool(a and b) and hmac.compare_digest(a.encode(), b.encode())


class LoginThrottle:
    """At most `limit` failed logins per `window` seconds per client address (in memory, per process)."""

    def __init__(self, limit: int = 5, window: float = 900.0):
        self.limit, self.window = limit, window
        self._fails: dict[str, deque[float]] = defaultdict(deque)

    def _trim(self, key: str, now: float) -> deque[float]:
        q = self._fails[key]
        while q and q[0] < now - self.window:
            q.popleft()
        return q

    def blocked(self, key: str, now: float | None = None) -> bool:
        return len(self._trim(key, now or time.monotonic())) >= self.limit

    def failed(self, key: str, now: float | None = None) -> None:
        self._trim(key, now or time.monotonic()).append(now or time.monotonic())

    def succeeded(self, key: str) -> None:
        self._fails.pop(key, None)
