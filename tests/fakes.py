"""Test doubles. No network anywhere.

- FakeClient: the scripted stand-in for anthropic.AsyncAnthropic, copied from cost-capped-agent's tests.
- FakeOIDCProvider: a fake Google. Real RSA keys, a real JWKS and discovery document served through
  respx, real signed ID tokens. Authlib's checks (signature, iss, aud, exp, nonce, state, PKCE) run
  for real against it.
"""

from __future__ import annotations

import base64
import hashlib
import time
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
from joserfc import jwt
from joserfc.jwk import RSAKey

# --- Claude (copied from cost-capped-agent/tests/fakes.py) ------------------------------------------


def usage(inp=1_000, out=200, cache_write=0, cache_read=0):
    return SimpleNamespace(
        input_tokens=inp, output_tokens=out, cache_creation_input_tokens=cache_write, cache_read_input_tokens=cache_read
    )


def text(t):
    return SimpleNamespace(type="text", text=t)


def thinking():
    return SimpleNamespace(type="thinking", thinking="", signature="sig")


def tool_use(name, args, id_="tu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=args, id=id_)


def response(content, stop_reason="end_turn", model="claude-opus-5-5", use=None, stop_details=None):
    return SimpleNamespace(
        content=content, stop_reason=stop_reason, model=model, usage=use or usage(), stop_details=stop_details
    )


class _Endpoint:
    def __init__(self, owner, name):
        self.owner, self.name = owner, name

    async def create(self, **kwargs):
        self.owner.calls.append((self.name, kwargs))
        if not self.owner.script:
            raise AssertionError(f"unexpected extra call to {self.name}")
        nxt = self.owner.script.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        if callable(nxt):
            out = nxt(kwargs)
            if hasattr(out, "__await__"):
                out = await out
            return out
        return nxt


class FakeClient:
    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls: list[tuple[str, dict]] = []
        self.messages = _Endpoint(self, "messages")
        self.beta = SimpleNamespace(messages=_Endpoint(self, "beta.messages"))


# --- Google (OpenID Connect) -----------------------------------------------------------------------

ISSUER = "https://idp.example.com"
DISCOVERY_URL = ISSUER + "/.well-known/openid-configuration"
CLIENT_ID = "console-test.apps.example.com"
CLIENT_SECRET = "test-client-secret"


def _b64url_sha256(value: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(value.encode()).digest()).rstrip(b"=").decode()


_KEYS: list[RSAKey] = []


def _test_keys() -> tuple[RSAKey, RSAKey]:
    """Two RSA keys with the same kid, generated once per test session (key generation is slow)."""
    if not _KEYS:
        _KEYS.extend(RSAKey.generate_key(2048, parameters={"kid": "k1"}) for _ in range(2))
    return _KEYS[0], _KEYS[1]


class FakeOIDCProvider:
    """Serves discovery, JWKS and the token endpoint through an httpx2 MockTransport (what Authlib uses)."""

    def __init__(self):
        self.key, self.other_key = _test_keys()  # other_key: same kid, different private key
        self.nonce = ""
        self.claims: dict[str, Any] = {}
        self.sign_with = self.key
        self.header: dict[str, Any] = {"alg": "RS256", "kid": "k1"}
        self.token_status = 200
        self.discovery_status = 200
        self.include_id_token = True
        self.token_requests: list[dict[str, list[str]]] = []
        self.discovery_hits = 0
        self.transport = httpx2.MockTransport(self.handle)

    def handle(self, request):
        url = str(request.url)
        if request.method == "GET" and url == DISCOVERY_URL:
            return self._discovery(request)
        if request.method == "GET" and url == ISSUER + "/jwks":
            return httpx2.Response(200, json={"keys": [self.key.as_dict(private=False)]})
        if request.method == "POST" and url == ISSUER + "/token":
            return self._token(request)
        raise AssertionError(f"unexpected request to the identity provider: {request.method} {url}")

    def _discovery(self, request):
        self.discovery_hits += 1
        if self.discovery_status != 200:
            return httpx2.Response(self.discovery_status)
        return httpx2.Response(
            200,
            json={
                "issuer": ISSUER,
                "authorization_endpoint": ISSUER + "/authorize",
                "token_endpoint": ISSUER + "/token",
                "jwks_uri": ISSUER + "/jwks",
                "id_token_signing_alg_values_supported": ["RS256"],
            },
        )

    def default_claims(self) -> dict[str, Any]:
        now = int(time.time())
        return {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": "1234567890",
            "iat": now,
            "exp": now + 3600,
            "nonce": self.nonce,
            "email": "alice@example.com",
            "email_verified": True,
        }

    def _token(self, request):
        self.token_requests.append(parse_qs(request.content.decode()))
        if self.token_status != 200:
            return httpx2.Response(self.token_status, json={"error": "invalid_grant"})
        body: dict[str, Any] = {"access_token": "at-123", "token_type": "Bearer", "expires_in": 3600}
        if self.include_id_token:
            claims = {**self.default_claims(), **self.claims}
            claims = {k: v for k, v in claims.items() if v is not None}  # None removes a claim
            body["id_token"] = jwt.encode(self.header, claims, self.sign_with)
        return httpx2.Response(200, json=body)


def query_of(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


async def start_google(client) -> dict[str, str]:
    """GET /auth/google and return the authorization request's parameters."""
    r = await client.get("/auth/google")
    assert r.status_code == 302, r.text
    return query_of(r.headers["location"])


async def sign_in(client, provider: FakeOIDCProvider, state: str | None = None, code: str = "code-1"):
    params = await start_google(client)
    provider.nonce = params["nonce"]
    return await client.get("/auth/callback", params={"code": code, "state": state or params["state"]}), params


def pkce_matches(verifier: str, challenge: str) -> bool:
    return _b64url_sha256(verifier) == challenge
