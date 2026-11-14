"""Sign-in: Google (OpenID Connect, authorization code + PKCE) and, for local development only, a dev login.

The OIDC flow is Authlib's, not hand-rolled:
  /auth/google    -> state, nonce and a PKCE S256 challenge are generated and kept in the signed
                     session cookie; the browser goes to Google
  /auth/callback  -> state must match the session; the code is exchanged (with the PKCE verifier);
                     the ID token's signature is checked against Google's JWKS, and its issuer,
                     audience, expiry and nonce are validated

Authlib checks the audience only when it is told to, so it is passed explicitly here, and the key
claims are checked a second time below (defence in depth). Then: `email_verified` must be true and
the email must be on ALLOWED_EMAILS.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from agentconsole.config import Settings
from agentconsole.runs import user_ref
from agentconsole.security import client_ip, csrf_ok, csrf_token, is_loopback

log = logging.getLogger("agentconsole.auth")
router = APIRouter()

ALLOWED_ID_TOKEN_ALGS = {"RS256"}
LOGIN_ERRORS = {
    "failed": "Sign-in didn't complete. Please try again.",
    "denied": "That account isn't allowed to use this console.",
    "expired": "Your session ended. Please sign in again.",
}


class NotSignedIn(Exception):
    """Raised by `current_user`; the app turns it into a redirect to /login."""


def build_oidc_client(settings: Settings, transport=None):
    """Authlib's Google client. `transport` (an httpx2 transport) lets tests serve a fake provider."""
    if not settings.google_configured:
        return None
    client_kwargs: dict[str, Any] = {"scope": "openid email", "code_challenge_method": "S256", "timeout": 10.0}
    if transport is not None:
        client_kwargs["transport"] = transport
    oauth = OAuth()
    oauth.register(
        name="google",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        server_metadata_url=settings.oidc_discovery_url,
        client_kwargs=client_kwargs,
    )
    return oauth.google


def claims_options(settings: Settings) -> dict[str, Any]:
    return {
        "iss": {"essential": True, "values": settings.issuer_list},
        "aud": {"essential": True, "value": settings.google_client_id},
        "sub": {"essential": True},
        "exp": {"essential": True},
        "iat": {"essential": True},
    }


def _header_alg(id_token: str) -> str:
    segment = id_token.split(".", 1)[0]
    header = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    return str(header.get("alg", ""))


def check_identity(claims: dict[str, Any], settings: Settings, now: float | None = None) -> tuple[bool, str]:
    """Second, independent check of what matters, after Authlib's validation. Returns (ok, reason)."""
    now = time.time() if now is None else now
    aud = claims.get("aud")
    audiences = aud if isinstance(aud, list) else [aud]
    if claims.get("iss") not in settings.issuer_list:
        return False, "issuer"
    if settings.google_client_id not in audiences:
        return False, "audience"
    if len(audiences) > 1 and claims.get("azp") != settings.google_client_id:
        return False, "azp"
    if not isinstance(claims.get("exp"), int | float) or claims["exp"] < now - 60:
        return False, "expired"
    if claims.get("email_verified") is not True:
        return False, "email_unverified"
    email = str(claims.get("email", "")).lower()
    if not email or email not in settings.allowed_email_set:
        return False, "not_allowed"
    return True, email


def start_session(request: Request, email: str) -> None:
    request.session.clear()  # nothing from before sign-in carries over
    request.session.update({"user": email, "login_at": int(time.time())})
    csrf_token(request)  # a fresh CSRF token for the new session


def current_user(request: Request) -> str:
    """FastAPI dependency: the signed-in, still-allowed user's email, or NotSignedIn."""
    settings: Settings = request.app.state.console.settings
    user = request.session.get("user")
    login_at = request.session.get("login_at", 0)
    fresh = isinstance(login_at, int) and time.time() - login_at < settings.session_max_age_seconds
    if not user or not fresh or user not in settings.allowed_email_set:
        expired = bool(user)
        request.session.clear()
        raise NotSignedIn("expired" if expired else "")
    return user


def _limit_login(request: Request) -> None:
    c = request.app.state.console
    if not c.limiter.hit(
        "login:" + client_ip(request, c.settings.trust_proxy_headers), c.settings.rate_limit_login_per_minute
    ):
        raise HTTPException(429, headers={"Retry-After": "60"})


def _refuse(reason: str, error: str = "failed") -> Response:
    log.warning("sign-in refused reason=%s", reason)
    return RedirectResponse(f"/login?error={error}", status_code=303)


# --- routes ---------------------------------------------------------------------------------------


@router.get("/login")
async def login_page(request: Request):
    c = request.app.state.console
    if request.session.get("user"):
        return RedirectResponse("/ask", status_code=303)
    error = LOGIN_ERRORS.get(request.query_params.get("error", ""), "")
    return c.render(
        request,
        "login.html",
        {
            "error": error,
            "google": c.oidc is not None,
            "dev_login": c.settings.dev_login_enabled,
            "dev_email": c.settings.dev_login_email,
        },
    )


@router.get("/auth/google")
async def google_start(request: Request):
    c = request.app.state.console
    if c.oidc is None:
        raise HTTPException(404)
    _limit_login(request)
    try:
        return await c.oidc.authorize_redirect(request, c.settings.redirect_uri, prompt="select_account")
    except Exception as exc:  # discovery document unreachable, for example
        return _refuse(f"start:{type(exc).__name__}")


@router.get("/auth/callback")
async def google_callback(request: Request):
    c = request.app.state.console
    if c.oidc is None:
        raise HTTPException(404)
    _limit_login(request)
    try:
        token = await c.oidc.authorize_access_token(request, claims_options=claims_options(c.settings), leeway=60)
    except Exception as exc:  # state mismatch, provider error, bad signature, wrong iss/aud, expired, nonce...
        return _refuse(type(exc).__name__)
    claims = token.get("userinfo")
    id_token = token.get("id_token")
    if not claims or not id_token:
        return _refuse("no_id_token")
    try:
        alg = _header_alg(id_token)
    except ValueError:
        return _refuse("bad_header")
    if alg not in ALLOWED_ID_TOKEN_ALGS:
        return _refuse("alg")
    ok, detail = check_identity(dict(claims), c.settings)
    if not ok:
        return _refuse(detail, "denied" if detail in ("email_unverified", "not_allowed") else "failed")
    start_session(request, detail)
    log.info("signed in user=%s via=google", user_ref(detail))
    return RedirectResponse("/ask", status_code=303)


@router.post("/auth/dev-login")
async def dev_login(request: Request):
    c = request.app.state.console
    s = c.settings
    # Dev login exists only when enabled, outside production, on a localhost BASE_URL (checked at
    # startup) and for a request that really comes from this machine (checked here).
    if (
        not s.dev_login_enabled
        or s.environment == "production"
        or not is_loopback(request.client.host if request.client else "")
    ):
        raise HTTPException(404)
    _limit_login(request)
    form = await request.form()
    if not csrf_ok(request, form.get("csrf_token")):
        raise HTTPException(403)
    start_session(request, s.dev_login_email.lower())
    log.info("signed in user=%s via=dev-login", user_ref(s.dev_login_email.lower()))
    return RedirectResponse("/ask", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    form = await request.form()
    if not csrf_ok(request, form.get("csrf_token")):
        raise HTTPException(403)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
