"""The web app. Run with:  uvicorn agentconsole.main:create_app --factory --no-access-log

Public:     GET /healthz, GET /readyz, GET /login, GET /auth/google, GET /auth/callback,
            POST /auth/dev-login (local development only), /static/*
Signed in:  /ask, /requests/{id}, /history, /knowledge, /knowledge/search, /knowledge/file,
            /health, POST /logout
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from costagent.config import Settings as AgentSettings
from costagent.indexer import Index
from costagent.store import Store
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from agentconsole import __version__, auth, logs, pages
from agentconsole.auth import NotSignedIn
from agentconsole.config import Settings
from agentconsole.costs import CostCache
from agentconsole.db import ConsoleDB
from agentconsole.runs import RunManager
from agentconsole.security import RateLimiter, client_ip, csrf_token, security_headers

log = logging.getLogger("agentconsole")
HERE = Path(__file__).resolve().parent
UNMETERED = {"/healthz", "/readyz"}
ERROR_TEXT = {
    400: "That request wasn't valid.",
    403: "That action isn't allowed. If you were filling in a form, reload the page and try again.",
    404: "Not found.",
    405: "That method isn't allowed here.",
    411: "Send a Content-Length header.",
    413: "That request is too large.",
    422: "Some of the input wasn't valid.",
    429: "Too many requests. Wait a minute and try again.",
    500: "Something went wrong on our side. It has been logged.",
}


def highlight(snippet: str) -> Markup:
    """FTS snippets mark matches with [ and ]: escape first, then turn the markers into <mark>."""
    safe = str(escape(snippet))
    return Markup(safe.replace("[", "<mark>").replace("]", "</mark>"))  # noqa: S704 - escaped above


def usd(value: Any) -> str:
    try:
        return f"${float(value):.4f}"
    except (TypeError, ValueError):
        return "-"


@dataclass
class Console:
    settings: Settings
    agent_settings: AgentSettings
    templates: Jinja2Templates
    limiter: RateLimiter
    oidc: Any
    db: ConsoleDB | None = None
    store: Store | None = None
    index: Index | None = None
    runs: RunManager | None = None
    costs: CostCache = field(default_factory=CostCache)

    def render(self, request: Request, name: str, ctx: dict[str, Any] | None = None, status: int = 200) -> Response:
        context = {
            "user": request.session.get("user"),
            "csrf": csrf_token(request),
            "version": __version__,
            "demo": self.settings.claude_client == "demo",
            "max_chars": self.settings.max_question_chars,
            "run_cap": self.agent_settings.run_cap_usd,
            "day_cap": self.agent_settings.day_cap_usd,
            **(ctx or {}),
        }
        return self.templates.TemplateResponse(request, name, context, status_code=status)

    def indexed_file_count(self) -> int:
        return int(self.store.query("SELECT count(*) FROM files")[0][0])

    def check_ready(self) -> dict[str, bool]:
        out = {"console_db": self.db.ping(), "agent_db": False, "index": False}
        try:
            out["agent_db"] = self.store.query("SELECT 1")[0][0] == 1
            out["index"] = bool(self.index.list_files("", 1))
        except Exception:
            log.warning("readiness check failed", exc_info=True)
        return out

    async def readiness(self) -> dict[str, bool]:
        return await asyncio.to_thread(self.check_ready)


def index_folder(index: Index, folder: str, agent_settings: AgentSettings):
    """Offline, free indexing at startup (unchanged files are skipped). None if the folder is missing."""
    root = Path(folder)
    if not root.is_dir():
        return None
    return index.build(root.resolve(), agent_settings.max_file_bytes)


def default_client(settings: Settings, agent_settings: AgentSettings):
    if settings.claude_client == "demo":
        from agentconsole.demo_client import DemoClaudeClient

        return DemoClaudeClient()
    from costagent.cli import default_client_factory

    try:
        return default_client_factory(agent_settings)  # reads ANTHROPIC_API_KEY from the environment
    except Exception as exc:
        log.warning(
            "Claude client not created (%s); questions will fail until ANTHROPIC_API_KEY is set", type(exc).__name__
        )
        return None


def create_app(
    settings: Settings | None = None,
    agent_settings: AgentSettings | None = None,
    client: Any = None,
    oidc_transport: Any = None,
) -> FastAPI:
    """`client` replaces the Claude client and `oidc_transport` the HTTP transport to Google (tests)."""
    settings = settings or Settings()
    agent_settings = agent_settings or AgentSettings()
    logs.configure(settings.log_level)

    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.auto_reload = settings.environment == "development"  # no per-render file stat elsewhere
    templates.env.filters["usd"] = usd
    templates.env.filters["highlight"] = highlight
    console = Console(
        settings=settings,
        agent_settings=agent_settings,
        templates=templates,
        limiter=RateLimiter(),
        oidc=auth.build_oidc_client(settings, oidc_transport),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        console.db = ConsoleDB(settings.console_db_path)
        console.store = Store(agent_settings.db_path)
        console.index = Index(console.store, agent_settings.chunk_lines)
        interrupted = await asyncio.to_thread(console.db.fail_interrupted)
        if interrupted:
            log.warning("marked %d interrupted run(s) as failed", interrupted)
        if settings.index_on_start:
            report = await asyncio.to_thread(index_folder, console.index, settings.index_on_start, agent_settings)
            if report is None:
                log.warning("INDEX_ON_START folder not found; the index is left as it is")
            else:
                log.info(
                    "index ready indexed=%d unchanged=%d removed=%d", report.indexed, report.unchanged, report.removed
                )
        # Import the Anthropic SDK now, not on the first question: Agent.ask() imports it for its error
        # types, and that first import took about 4 s on a Windows laptop (measured; see README, Performance).
        await asyncio.to_thread(importlib.import_module, "anthropic")
        runs_client = client if client is not None else default_client(settings, agent_settings)
        console.runs = RunManager(
            console.db, console.store, console.index, agent_settings, runs_client,
            settings.max_concurrent_runs, settings.run_timeout_seconds,
        )  # fmt: skip
        if console.oidc is None:
            log.warning("Google sign-in is not configured (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET)")
        log.info(
            "agent-console started env=%s claude=%s dev_login=%s",
            settings.environment, settings.claude_client, settings.dev_login_enabled,
        )  # fmt: skip
        try:
            yield
        finally:
            await console.runs.shutdown()
            console.db.close()
            console.store.close()

    app = FastAPI(
        title="agent-console", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.console = console

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="__Host-console" if settings.session_cookie_secure else "console_session",
        max_age=settings.session_max_age_seconds,
        same_site="lax",
        https_only=settings.session_cookie_secure,
    )

    headers = security_headers(settings.session_cookie_secure)

    def error_page(request: Request, status: int, extra_headers: dict[str, str] | None = None) -> Response:
        rid = getattr(request.state, "request_id", "")
        message = ERROR_TEXT.get(status, ERROR_TEXT[400] if status < 500 else ERROR_TEXT[500])
        if request.url.path in UNMETERED or "session" not in request.scope:
            return JSONResponse({"error": status, "request_id": rid}, status_code=status, headers=extra_headers)
        response = console.templates.TemplateResponse(
            request, "error.html", {"status": status, "message": message, "rid": rid, "user": None, "csrf": ""},
            status_code=status,
        )  # fmt: skip
        response.headers.update(extra_headers or {})
        return response

    @app.middleware("http")
    async def edge(request: Request, call_next):
        """Request id, body-size and global rate limits, security headers, one log line per request."""
        rid = uuid.uuid4().hex  # never trust a client-supplied id
        request.state.request_id = rid
        started = time.perf_counter()
        ip = client_ip(request, settings.trust_proxy_headers)
        if request.method == "POST":
            length = request.headers.get("content-length")
            if length is None or not length.isdigit():
                response: Response = JSONResponse({"error": 411, "request_id": rid}, status_code=411)
            elif int(length) > settings.max_body_bytes:
                response = JSONResponse({"error": 413, "request_id": rid}, status_code=413)
            else:
                response = None  # type: ignore[assignment]
        else:
            response = None  # type: ignore[assignment]
        if response is None and request.url.path not in UNMETERED:
            if not console.limiter.hit("ip:" + ip, settings.rate_limit_global_per_minute):
                response = JSONResponse(
                    {"error": 429, "request_id": rid}, status_code=429, headers={"Retry-After": "60"}
                )
        if response is None:
            try:
                response = await call_next(request)
            except Exception:
                log.exception("unhandled error rid=%s", rid)
                response = JSONResponse({"error": 500, "request_id": rid}, status_code=500)
        response.headers.update(headers)
        response.headers["X-Request-Id"] = rid
        static = request.url.path.startswith("/static/") and response.status_code == 200
        response.headers["Cache-Control"] = "public, max-age=3600" if static else "no-store"
        log.info(
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,  # never the query string
                "status": response.status_code,
                "ms": round((time.perf_counter() - started) * 1000, 1),
                "rid": rid,
            },
        )
        return response

    @app.exception_handler(NotSignedIn)
    async def not_signed_in(request: Request, exc: NotSignedIn):
        return RedirectResponse("/login?error=expired" if str(exc) == "expired" else "/login", status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if exc.status_code in (403, 429):
            log.warning("refused status=%d path=%s rid=%s", exc.status_code, request.url.path, request.state.request_id)
        return error_page(request, exc.status_code, getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError):
        return error_page(request, 422)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():
        checks = await console.readiness()
        ready = all(checks.values())
        return JSONResponse({"status": "ready" if ready else "not_ready", **checks}, status_code=200 if ready else 503)

    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    app.include_router(auth.router)
    app.include_router(pages.router)
    return app
