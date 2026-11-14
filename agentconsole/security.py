"""Security headers, client address, CSRF tokens and an in-memory rate limiter."""

from __future__ import annotations

import hmac
import secrets
import time
from collections import OrderedDict, deque

from starlette.requests import Request

CSP = (
    "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)


def security_headers(secure: bool) -> dict[str, str]:
    headers = {
        "Content-Security-Policy": CSP,
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "same-origin",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    }
    if secure:
        headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return headers


def client_ip(request: Request, trust_proxy_headers: bool) -> str:
    if trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # The right-most entry is the one our own proxy added; anything left of it is client-supplied.
            return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else ""


def is_loopback(host: str) -> bool:
    return host in {"127.0.0.1", "::1", "localhost"} or host.startswith("127.")


# --- CSRF: a random token per session, sent back in every form ------------------------------------

CSRF_SESSION_KEY = "csrf"


def csrf_token(request: Request) -> str:
    token = request.session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    return token


def csrf_ok(request: Request, submitted: object) -> bool:
    expected = request.session.get(CSRF_SESSION_KEY)
    return bool(expected) and isinstance(submitted, str) and hmac.compare_digest(expected, submitted)


# --- rate limiting --------------------------------------------------------------------------------


class RateLimiter:
    """Sliding window per key, in process memory.

    Fine for one instance (the free host runs one). Several instances need a shared store such as
    Redis; see README, "How it scales". Memory is bounded: the least recently used keys are dropped.
    """

    def __init__(self, max_keys: int = 10_000, clock=time.monotonic):
        self.max_keys, self.clock = max_keys, clock
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    def hit(self, key: str, limit: int, window: float = 60.0) -> bool:
        now = self.clock()
        q = self._hits.get(key)
        if q is None:
            q = self._hits[key] = deque()
            if len(self._hits) > self.max_keys:
                self._hits.popitem(last=False)
        else:
            self._hits.move_to_end(key)
        while q and q[0] <= now - window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True
