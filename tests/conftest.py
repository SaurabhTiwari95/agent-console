from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from costagent.config import Settings as AgentSettings

from agentconsole.config import Settings
from agentconsole.main import create_app
from tests.fakes import CLIENT_ID, CLIENT_SECRET, DISCOVERY_URL, ISSUER, FakeClient, FakeOIDCProvider

ROOT = Path(__file__).resolve().parent.parent
SECRET = "test-session-secret-" + "s" * 40
ALICE = "alice@example.com"
BOB = "bob@example.com"
DEV = "dev@example.com"


def make_settings(tmp_path, **overrides) -> Settings:
    values = dict(
        _env_file=None,
        environment="test",
        base_url="https://console.example.com",
        session_secret=SECRET,
        google_client_id=CLIENT_ID,
        google_client_secret=CLIENT_SECRET,
        oidc_discovery_url=DISCOVERY_URL,
        oidc_allowed_issuers=ISSUER,
        allowed_emails=f"{ALICE},{BOB},{DEV}",
        console_db_path=str(tmp_path / "console.db"),
        index_on_start=str(ROOT / "sample_corpus"),
    )
    values.update(overrides)
    return Settings(**values)


def make_agent_settings(tmp_path, **overrides) -> AgentSettings:
    values = dict(
        _env_file=None, db_path=str(tmp_path / "agent.db"), run_cap_usd=Decimal("0.25"), day_cap_usd=Decimal("2.00")
    )
    values.update(overrides)
    return AgentSettings(**values)


@pytest.fixture
def provider() -> FakeOIDCProvider:
    return FakeOIDCProvider()


@pytest.fixture
def fake_claude() -> FakeClient:
    return FakeClient()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def agent_settings(tmp_path) -> AgentSettings:
    return make_agent_settings(tmp_path)


class Harness:
    """A running app plus an HTTP client that keeps cookies, like a browser."""

    def __init__(self, app, client: httpx.AsyncClient):
        self.app, self.client = app, client

    @property
    def console(self):
        return self.app.state.console

    async def dev_login(self) -> httpx.Response:
        page = await self.client.get("/login")
        token = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        return await self.client.post("/auth/dev-login", data={"csrf_token": token})

    async def page_csrf(self, path: str = "/ask") -> str:
        page = await self.client.get(path)
        return page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]

    async def ask(self, question: str, max_usd: str = "0.10", csrf: str | None = None) -> httpx.Response:
        token = csrf if csrf is not None else await self.page_csrf()
        return await self.client.post("/ask", data={"question": question, "max_usd": max_usd, "csrf_token": token})


async def start(settings, agent_settings, claude=None, client_addr=("127.0.0.1", 1234), base_url=None, oidc=None):
    app = create_app(settings, agent_settings, client=claude, oidc_transport=oidc.transport if oidc else None)
    ctx = app.router.lifespan_context(app)
    await ctx.__aenter__()
    transport = httpx.ASGITransport(app=app, client=client_addr)
    client = httpx.AsyncClient(transport=transport, base_url=base_url or settings.base_url)
    return Harness(app, client), ctx


@pytest.fixture
async def make_harness():
    opened = []

    async def factory(settings, agent_settings, claude=None, **kw) -> Harness:
        h, ctx = await start(settings, agent_settings, claude, **kw)
        opened.append((h, ctx))
        return h

    yield factory
    for h, ctx in reversed(opened):
        await h.console.runs.wait_idle()
        await h.client.aclose()
        await ctx.__aexit__(None, None, None)


@pytest.fixture
def local_settings(tmp_path) -> Settings:
    """Local development: localhost, dev login on, non-Secure cookie allowed."""
    return make_settings(
        tmp_path,
        environment="development",
        base_url="http://127.0.0.1:8000",
        session_cookie_secure=False,
        dev_login_enabled=True,
        dev_login_email=DEV,
    )


@pytest.fixture
async def app_h(make_harness, settings, agent_settings, provider, fake_claude) -> Harness:
    """The production-shaped app (Google sign-in) with a scripted Claude client."""
    return await make_harness(settings, agent_settings, fake_claude, oidc=provider)


@pytest.fixture
async def local_h(make_harness, local_settings, agent_settings, fake_claude) -> Harness:
    """Local dev app, signed in with dev login."""
    h = await make_harness(local_settings, agent_settings, fake_claude)
    r = await h.dev_login()
    assert r.status_code == 303 and r.headers["location"] == "/ask"
    return h
