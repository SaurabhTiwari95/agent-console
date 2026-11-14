"""Startup refuses unsafe settings; dev login only on localhost, outside production, from this machine."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentconsole.__main__ import check_bind, main
from tests.conftest import DEV, make_settings

LOCAL = dict(environment="development", base_url="http://127.0.0.1:8000", session_cookie_secure=False)


@pytest.mark.parametrize(
    "overrides, message",
    [
        (
            dict(dev_login_enabled=True, environment="production"),
            "DEV_LOGIN_ENABLED is refused when ENVIRONMENT=production",
        ),
        (dict(dev_login_enabled=True, environment="development"), "DEV_LOGIN_ENABLED is allowed only for a localhost"),
        (dict(dev_login_enabled=True, environment="test", base_url="https://console.example.com"), "localhost"),
        ({**LOCAL, "dev_login_enabled": True, "environment": "production"}, "refused when ENVIRONMENT=production"),
        (
            {**LOCAL, "dev_login_enabled": True, "dev_login_email": "eve@example.com"},
            "DEV_LOGIN_EMAIL must also be in ALLOWED_EMAILS",
        ),
        (dict(claude_client="demo", environment="production"), "CLAUDE_CLIENT=demo is refused"),
        (dict(session_cookie_secure=False), "SESSION_COOKIE_SECURE=false is allowed only"),
        (dict(session_secret="short"), "SESSION_SECRET must be"),
        (dict(session_secret="replace-with-" + "x" * 40), "SESSION_SECRET must be"),
        (dict(environment="production", google_client_id=""), "production needs GOOGLE_CLIENT_ID"),
        (dict(environment="production", allowed_emails=""), "production needs"),
        (dict(base_url="http://console.example.com"), "must use https unless it is localhost"),
        (dict(base_url="ftp://console.example.com"), "must be an origin"),
        (dict(base_url="https://console.example.com/app"), "must be an origin"),
        (dict(allowed_emails="alice@example.com,not-an-email"), "comma-separated list of email addresses"),
    ],
)
def test_unsafe_settings_refuse_to_start(tmp_path, overrides, message):
    with pytest.raises(ValidationError, match=message):
        make_settings(tmp_path, **overrides)


@pytest.mark.parametrize("host", ["http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000"])
def test_dev_login_is_allowed_on_localhost_outside_production(tmp_path, host):
    s = make_settings(tmp_path, **{**LOCAL, "base_url": host}, dev_login_enabled=True, dev_login_email=DEV)
    assert s.dev_login_enabled and s.is_local


def test_production_with_google_and_an_allow_list_starts(tmp_path):
    s = make_settings(tmp_path, environment="production")
    assert s.environment == "production" and s.session_cookie_secure
    assert s.redirect_uri == "https://console.example.com/auth/callback"


def test_defaults_are_the_safe_ones(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env here
    for key in ("ENVIRONMENT", "DEV_LOGIN_ENABLED", "CLAUDE_CLIENT", "SESSION_COOKIE_SECURE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SESSION_SECRET", "s" * 40)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("ALLOWED_EMAILS", "alice@example.com, Bob@Example.com ")
    from agentconsole.config import Settings

    s = Settings()
    assert s.environment == "production" and s.session_cookie_secure and not s.dev_login_enabled
    assert s.claude_client == "anthropic"
    assert s.allowed_email_set == {"alice@example.com", "bob@example.com"}


def test_the_runner_refuses_a_public_bind_with_dev_login(tmp_path):
    s = make_settings(tmp_path, **LOCAL, dev_login_enabled=True, dev_login_email=DEV)
    assert "refusing to bind to 0.0.0.0" in check_bind(s, "0.0.0.0")
    assert check_bind(s, "127.0.0.1") is None
    s2 = make_settings(tmp_path)
    assert check_bind(s2, "0.0.0.0") is None  # without dev login any bind is fine


def test_runner_main_checks_config_and_bind(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SESSION_SECRET", "short")
    assert main([]) == 2
    assert "configuration error" in capsys.readouterr().err

    for k, v in {
        "SESSION_SECRET": "s" * 40, "ENVIRONMENT": "development", "BASE_URL": "http://127.0.0.1:8000",
        "SESSION_COOKIE_SECURE": "false", "DEV_LOGIN_ENABLED": "true", "ALLOWED_EMAILS": DEV,
    }.items():  # fmt: skip
        monkeypatch.setenv(k, v)
    assert main(["--host", "0.0.0.0"]) == 2
    calls = []
    assert main(["--host", "127.0.0.1", "--port", "8123"], run=lambda *a, **kw: calls.append(kw)) == 0
    assert calls[0]["host"] == "127.0.0.1" and calls[0]["port"] == 8123
    assert calls[0]["access_log"] is False and calls[0]["factory"] is True


async def test_dev_login_route_is_404_when_disabled(app_h):
    r = await app_h.client.post("/auth/dev-login", data={"csrf_token": "x"})
    assert r.status_code == 404


async def test_dev_login_is_404_for_a_request_from_another_machine(make_harness, local_settings, agent_settings):
    h = await make_harness(local_settings, agent_settings, client_addr=("203.0.113.9", 4000))
    page = await h.client.get("/login")
    token = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
    r = await h.client.post("/auth/dev-login", data={"csrf_token": token})
    assert r.status_code == 404


async def test_dev_login_needs_the_csrf_token(make_harness, local_settings, agent_settings):
    h = await make_harness(local_settings, agent_settings)
    await h.client.get("/login")
    assert (await h.client.post("/auth/dev-login", data={"csrf_token": "forged"})).status_code == 403
    assert (await h.client.post("/auth/dev-login", data={})).status_code == 403


async def test_dev_login_signs_in_with_a_non_secure_local_cookie(make_harness, local_settings, agent_settings):
    h = await make_harness(local_settings, agent_settings)
    r = await h.dev_login()
    cookie = r.headers["set-cookie"].lower()
    assert cookie.startswith("console_session=") and "httponly" in cookie and "samesite=lax" in cookie
    assert "secure" not in cookie
    assert "Strict-Transport-Security" not in r.headers  # no HSTS on plain-http localhost
    assert DEV in (await h.client.get("/ask")).text


def test_a_refused_config_never_echoes_secrets(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SESSION_SECRET", "s" * 40)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "very-secret-google-value")
    monkeypatch.setenv("ENVIRONMENT", "production")  # no client id or allow-list: refused
    assert main([]) == 2
    err = capsys.readouterr().err
    assert "production needs" in err
    assert "very-secret-google-value" not in err and "s" * 40 not in err and "input_value" not in err
