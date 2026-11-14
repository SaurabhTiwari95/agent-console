"""Signed-in pages: ask, request status, history, knowledge, health and cost."""

from __future__ import annotations

import asyncio
import re
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, ValidationError, field_validator

from agentconsole import costs
from agentconsole.auth import current_user
from agentconsole.security import csrf_ok

router = APIRouter()
User = Annotated[str, Depends(current_user)]

REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")
HISTORY_PAGE_SIZE = 20
FILES_PAGE_SIZE = 50
MAX_FILES_PAGES = 100
FILE_WINDOW_LINES = 200


class AskForm(BaseModel):
    question: str = Field(min_length=3)
    max_usd: Decimal = Field(allow_inf_nan=False, max_digits=12)

    @field_validator("question", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v


@router.get("/")
async def home():
    return RedirectResponse("/ask", status_code=303)


# --- ask ------------------------------------------------------------------------------------------


@router.get("/ask")
async def ask_page(request: Request, user: User):
    c = request.app.state.console
    active = await asyncio.to_thread(c.db.active_for, user)
    recent, _ = await asyncio.to_thread(c.db.history, user, 1, 5)
    return c.render(request, "ask.html", {"active": active, "recent": recent, "error": "", "question": ""})


@router.post("/ask")
async def ask_submit(request: Request, user: User):
    c = request.app.state.console
    form = await request.form()
    if not csrf_ok(request, form.get("csrf_token")):
        raise HTTPException(403)
    if not c.limiter.hit("ask:" + user, c.settings.rate_limit_ask_per_minute):
        raise HTTPException(429, headers={"Retry-After": "60"})

    question = str(form.get("question", ""))
    error = ""
    try:
        data = AskForm(question=question, max_usd=str(form.get("max_usd") or c.agent_settings.run_cap_usd))
    except (ValidationError, InvalidOperation):
        data, error = None, "Write a question of at least 3 characters, and a cap in dollars."
    if data is not None:
        if len(data.question) > c.settings.max_question_chars:
            error = f"Questions are limited to {c.settings.max_question_chars} characters."
        elif not (Decimal("0.01") <= data.max_usd <= c.agent_settings.run_cap_usd):
            error = f"The cap must be between $0.01 and ${c.agent_settings.run_cap_usd}."
    if error:
        return await _ask_again(request, user, error, question, 422)

    rid = await c.runs.submit(user, data.question, data.max_usd)
    if rid is None:
        return await _ask_again(
            request, user, "You already have a question running. Wait for it to finish.", question, 409
        )
    return RedirectResponse(f"/requests/{rid}", status_code=303)


async def _ask_again(request: Request, user: str, error: str, question: str, status: int):
    c = request.app.state.console
    active = await asyncio.to_thread(c.db.active_for, user)
    recent, _ = await asyncio.to_thread(c.db.history, user, 1, 5)
    ctx = {"active": active, "recent": recent, "error": error, "question": question[: c.settings.max_question_chars]}
    return c.render(request, "ask.html", ctx, status)


@router.get("/requests/{rid}")
async def request_page(request: Request, rid: str, user: User):
    c = request.app.state.console
    row = await asyncio.to_thread(c.db.get, rid, user) if REQUEST_ID.match(rid) else None
    if row is None:  # unknown, or someone else's: the same answer either way
        raise HTTPException(404)
    return c.render(request, "request.html", {"r": row})


@router.get("/history")
async def history_page(request: Request, user: User, page: Annotated[int, Query(ge=1, le=10_000)] = 1):
    c = request.app.state.console
    rows, more = await asyncio.to_thread(c.db.history, user, page, HISTORY_PAGE_SIZE)
    return c.render(request, "history.html", {"rows": rows, "page": page, "more": more})


# --- knowledge ------------------------------------------------------------------------------------


@router.get("/knowledge")
async def knowledge_page(
    request: Request,
    user: User,
    prefix: Annotated[str, Query(max_length=300)] = "",
    page: Annotated[int, Query(ge=1, le=MAX_FILES_PAGES)] = 1,
):
    c = request.app.state.console
    files = await asyncio.to_thread(c.index.list_files, prefix, page * FILES_PAGE_SIZE + 1)
    window = files[(page - 1) * FILES_PAGE_SIZE : page * FILES_PAGE_SIZE]
    more = len(files) > page * FILES_PAGE_SIZE and page < MAX_FILES_PAGES
    return c.render(request, "knowledge.html", {"files": window, "prefix": prefix, "page": page, "more": more})


@router.get("/knowledge/search")
async def knowledge_search(request: Request, user: User, q: Annotated[str, Query(max_length=200)] = ""):
    c = request.app.state.console
    hits = await asyncio.to_thread(c.index.search, q, 20) if q.strip() else []
    return c.render(request, "search.html", {"q": q, "hits": hits})


@router.get("/knowledge/file")
async def knowledge_file(
    request: Request,
    user: User,
    path: Annotated[str, Query(min_length=1, max_length=300)],
    start: Annotated[int, Query(alias="from", ge=1, le=10_000_000)] = 1,
    hl: Annotated[str, Query(max_length=30, pattern=r"^(\d{1,7}(-\d{1,7})?)?$")] = "",
):
    c = request.app.state.console
    text = await asyncio.to_thread(c.index.file_text, path)
    if text is None:
        raise HTTPException(404)
    meta = next((f for f in await asyncio.to_thread(c.index.list_files, path, 5) if f["path"] == path), None)
    lines = text.splitlines()
    start = min(start, max(len(lines), 1))
    end = min(len(lines), start + FILE_WINDOW_LINES - 1)
    hl_start, _, hl_end = hl.partition("-")
    hl_range = (int(hl_start), int(hl_end or hl_start)) if hl_start else (0, -1)
    shown = [(n, lines[n - 1]) for n in range(start, end + 1)]
    ctx = {
        "path": path,
        "summary": (meta or {}).get("summary"),
        "total": len(lines),
        "shown": shown,
        "start": start,
        "end": end,
        "hl": hl_range,
        "window": FILE_WINDOW_LINES,
    }
    return c.render(request, "file.html", ctx)


# --- health and cost ------------------------------------------------------------------------------


@router.get("/health")
async def health_page(request: Request, user: User, days: Annotated[int, Query(ge=1, le=90)] = 7):
    c = request.app.state.console
    s = c.agent_settings

    def gather() -> dict:
        # One trip to the worker thread for all five reads (profiled: each hop costs ~0.4 ms on Windows).
        def compute() -> costs.CostSummary:
            return costs.summarise(c.store, days, s.day_cap_usd, s.run_cap_usd)

        return {
            "summary": c.costs.get(compute) if days == 7 else compute(),
            "checks": c.check_ready(),
            "recent": c.db.recent_runs(10),
            "counts": c.db.status_counts(),
            "files": c.indexed_file_count(),
        }

    data = await asyncio.to_thread(gather)
    ctx = {
        **data,
        "in_flight": c.runs.in_flight,
        "days": days,
        "models": {"answer": s.answer_model, "summary": s.summary_model},
        "client_kind": c.settings.claude_client,
    }
    return c.render(request, "health.html", ctx)
