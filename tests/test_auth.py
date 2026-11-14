"""Google sign-in through a fake OpenID provider: Authlib's real checks, plus ours."""

from __future__ import annotations

import time

import pytest
from joserfc.jwk import OctKey

from agentconsole import auth
from agentconsole.auth import check_identity
from tests.conftest import ALICE, make_settings
from tests.fakes import CLIENT_ID, ISSUER, pkce_matches, query_of, sign_in, start_google


async def test_authorization_request_has_state_nonce_pkce_and_exact_redirect(app_h):
    params = await start_google(app_h.client)
    assert params["response_type"] == "code"
    assert params["client_id"] == CLIENT_ID
    assert params["redirect_uri"] == "https://console.example.com/auth/callback"
    assert set(params["scope"].split()) == {"openid", "email"}
    assert params["code_challenge_method"] == "S256"
    assert len(params["state"]) >= 20 and len(params["nonce"]) >= 20 and len(params["code_challenge"]) >= 43
    assert params["prompt"] == "select_account"


async def test_sign_in_succeeds_and_sends_the_pkce_verifier(app_h, provider):
    r, params = await sign_in(app_h.client, provider)
    assert r.status_code == 303 and r.headers["location"] == "/ask"
    form = provider.token_requests[0]
    assert form["code"] == ["code-1"] and form["grant_type"] == ["authorization_code"]
    assert pkce_matches(form["code_verifier"][0], params["code_challenge"])
    assert form["redirect_uri"] == ["https://console.example.com/auth/callback"]
    page = await app_h.client.get("/ask")
    assert page.status_code == 200 and ALICE in page.text


async def test_session_cookie_is_httponly_secure_samesite_lax_host_prefixed(app_h, provider):
    r, _ = await sign_in(app_h.client, provider)
    cookie = r.headers["set-cookie"].lower()
    assert cookie.startswith("__host-console=")
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie and "path=/" in cookie
    assert "domain=" not in cookie


@pytest.mark.parametrize(
    "claims, reason",
    [
        ({"aud": "someone-else.apps.example.com"}, "wrong audience"),
        ({"aud": ["someone-else.apps.example.com"]}, "wrong audience (list)"),
        ({"iss": "https://evil.example.com"}, "wrong issuer"),
        ({"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200}, "expired"),
        ({"nonce": "not-the-nonce-we-sent"}, "nonce mismatch"),
        ({"nonce": None}, "nonce missing"),
        ({"aud": None}, "audience missing"),
        ({"exp": None}, "expiry missing"),
        ({"email_verified": False}, "email not verified"),
        ({"email_verified": "true"}, "email_verified not a real boolean"),
        ({"email_verified": None}, "email_verified missing"),
        ({"email": "mallory@example.com"}, "not on the allow-list"),
        ({"email": None}, "no email"),
        ({"aud": [CLIENT_ID, "other.apps.example.com"]}, "several audiences without azp"),
        ({"azp": "other.apps.example.com"}, "wrong authorized party"),
    ],
)
async def test_bad_id_tokens_are_refused(app_h, provider, claims, reason):
    provider.claims = claims
    r, _ = await sign_in(app_h.client, provider)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?error="), reason
    assert (await app_h.client.get("/ask")).headers["location"] == "/login", reason


async def test_bad_signature_is_refused(app_h, provider):
    provider.sign_with = provider.other_key  # same kid, different private key
    r, _ = await sign_in(app_h.client, provider)
    assert r.headers["location"] == "/login?error=failed"


async def test_hmac_signed_token_is_refused(app_h, provider):
    provider.header = {"alg": "HS256", "kid": "k1"}
    provider.sign_with = OctKey.import_key("a-shared-secret-anyone-could-guess-0123456789")
    r, _ = await sign_in(app_h.client, provider)
    assert r.headers["location"] == "/login?error=failed"


async def test_unverified_or_unlisted_email_shows_denied(app_h, provider):
    provider.claims = {"email": "mallory@example.com"}
    r, _ = await sign_in(app_h.client, provider)
    assert r.headers["location"] == "/login?error=denied"
    page = await app_h.client.get(r.headers["location"])
    assert "isn&#39;t allowed" in page.text


async def test_state_mismatch_is_refused(app_h, provider):
    r, _ = await sign_in(app_h.client, provider, state="forged-state")
    assert r.headers["location"] == "/login?error=failed"
    assert provider.token_requests == []  # the code is never exchanged


async def test_callback_without_a_started_flow_is_refused(app_h, provider):
    r = await app_h.client.get("/auth/callback", params={"code": "c", "state": "s"})
    assert r.headers["location"] == "/login?error=failed"


async def test_state_is_single_use(app_h, provider):
    r, params = await sign_in(app_h.client, provider)
    assert r.headers["location"] == "/ask"
    replay = await app_h.client.get("/auth/callback", params={"code": "code-1", "state": params["state"]})
    assert replay.headers["location"] == "/login?error=failed"


async def test_provider_error_and_token_endpoint_failure_are_refused(app_h, provider):
    params = await start_google(app_h.client)
    r = await app_h.client.get("/auth/callback", params={"error": "access_denied", "state": params["state"]})
    assert r.headers["location"] == "/login?error=failed"
    provider.token_status = 400
    r, _ = await sign_in(app_h.client, provider)
    assert r.headers["location"] == "/login?error=failed"


async def test_token_response_without_id_token_is_refused(app_h, provider):
    provider.include_id_token = False
    r, _ = await sign_in(app_h.client, provider)
    assert r.headers["location"] == "/login?error=failed"


async def test_discovery_failure_is_a_clean_refusal(app_h, provider):
    provider.discovery_status = 503
    r = await app_h.client.get("/auth/google")
    assert r.status_code == 303 and r.headers["location"] == "/login?error=failed"


async def test_google_routes_404_when_not_configured(make_harness, tmp_path, agent_settings):
    s = make_settings(tmp_path, google_client_id="", google_client_secret="")
    h = await make_harness(s, agent_settings)
    assert (await h.client.get("/auth/google")).status_code == 404
    assert (await h.client.get("/auth/callback")).status_code == 404
    assert "configured on this server" in (await h.client.get("/login")).text


async def test_logout_needs_csrf_and_clears_the_session(app_h, provider):
    await sign_in(app_h.client, provider)
    assert (await app_h.client.post("/logout", data={"csrf_token": "wrong"})).status_code == 403
    token = await app_h.page_csrf()
    r = await app_h.client.post("/logout", data={"csrf_token": token})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert (await app_h.client.get("/ask")).headers["location"] == "/login"


async def test_session_expires_after_max_age_even_if_the_cookie_is_kept(app_h, provider):
    await sign_in(app_h.client, provider)
    app_h.console.settings.session_max_age_seconds = 0  # the absolute limit, checked from login_at
    r = await app_h.client.get("/ask")
    assert r.headers["location"] == "/login?error=expired"


async def test_removing_an_email_from_the_allow_list_signs_it_out(app_h, provider):
    await sign_in(app_h.client, provider)
    app_h.console.settings.allowed_emails = "bob@example.com"
    assert (await app_h.client.get("/ask")).headers["location"].startswith("/login")


async def test_tampered_session_cookie_is_ignored(app_h, provider):
    await sign_in(app_h.client, provider)
    jar = app_h.client.cookies
    value = jar.get("__Host-console")
    jar.clear()
    jar.set("__Host-console", value[:-4] + "AAAA", domain="console.example.com")
    assert (await app_h.client.get("/ask")).headers["location"] == "/login"


async def test_signed_in_user_visiting_login_goes_to_ask(app_h, provider):
    await sign_in(app_h.client, provider)
    r = await app_h.client.get("/login")
    assert r.headers["location"] == "/ask"


async def test_login_errors_come_from_a_fixed_list(app_h):
    r = await app_h.client.get("/login", params={"error": "<script>alert(1)</script>"})
    assert "<script>" not in r.text and "alert(1)" not in r.text


def test_check_identity_accepts_a_good_token_and_names_the_failure(tmp_path):
    s = make_settings(tmp_path)
    good = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "exp": time.time() + 60,
        "email": "Alice@Example.com",
        "email_verified": True,
    }
    assert check_identity(good, s) == (True, ALICE)  # case-insensitive allow-list
    assert check_identity({**good, "iss": "x"}, s) == (False, "issuer")
    assert check_identity({**good, "aud": "x"}, s) == (False, "audience")
    assert check_identity({**good, "aud": [CLIENT_ID, "x"]}, s) == (False, "azp")
    assert check_identity({**good, "aud": [CLIENT_ID, "x"], "azp": CLIENT_ID}, s)[0] is True
    assert check_identity({**good, "exp": time.time() - 3600}, s) == (False, "expired")
    assert check_identity({**good, "exp": "soon"}, s) == (False, "expired")
    assert check_identity({**good, "email_verified": 1}, s) == (False, "email_unverified")
    assert check_identity({**good, "email": "x@example.com"}, s) == (False, "not_allowed")


def test_claims_options_require_issuer_audience_and_expiry(tmp_path):
    opts = auth.claims_options(make_settings(tmp_path))
    assert opts["aud"] == {"essential": True, "value": CLIENT_ID}
    assert opts["iss"]["values"] == [ISSUER] and opts["exp"]["essential"]


def test_header_alg_reads_the_unverified_header():
    assert auth._header_alg("eyJhbGciOiJSUzI1NiJ9.e30.sig") == "RS256"
    with pytest.raises(ValueError):
        auth._header_alg("not-base64!.x.y")


async def test_callback_rejects_a_token_whose_header_alg_is_not_rs256(app_h, provider, monkeypatch):
    monkeypatch.setattr(auth, "ALLOWED_ID_TOKEN_ALGS", {"ES256"})
    r, _ = await sign_in(app_h.client, provider)
    assert r.headers["location"] == "/login?error=failed"


def test_query_helper():
    assert query_of("https://x.example.com/a?b=1&c=2") == {"b": "1", "c": "2"}
