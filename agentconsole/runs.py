"""Background runs: each question is answered by cost-capped-agent's `Agent.ask()` off the request path.

- The request handler only inserts a `queued` row and starts a task; the page polls by refreshing.
- One active question per user (a partial unique index in the console DB enforces it).
- At most MAX_CONCURRENT_RUNS answers are in flight; the rest wait as `queued`.
- Each run gets its own `RunBudget` with the cap the user chose (never above AGENT_RUN_CAP_USD);
  the daily cap is shared through the agent's ledger.
- A run that exceeds RUN_TIMEOUT_SECONDS is cancelled. `Agent.ask()` settles any open reservation
  at its worst case when cancelled, so the spend cap stays safe.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from decimal import Decimal

from costagent.agent import Agent
from costagent.budget import RunBudget
from costagent.config import Settings as AgentSettings
from costagent.indexer import Index
from costagent.store import Store

from agentconsole import citations
from agentconsole.db import ConsoleDB

log = logging.getLogger("agentconsole.runs")

STOPPED_EARLY = {"budget", "max_steps", "output_limit", "refused"}


def user_ref(user_key: str) -> str:
    """A short, stable pseudonym for logs: never log the email itself."""
    return hashlib.sha256(user_key.encode()).hexdigest()[:12]


def status_for(stopped: str) -> str:
    if stopped == "done":
        return "done"
    return "stopped" if stopped in STOPPED_EARLY else "failed"


class RunManager:
    def __init__(
        self,
        db: ConsoleDB,
        store: Store,
        index: Index,
        agent_settings: AgentSettings,
        client,
        max_concurrent: int,
        timeout_seconds: float,
    ):
        self.db, self.store, self.index, self.agent_settings = db, store, index, agent_settings
        self.client = client
        self.timeout = timeout_seconds
        self._slots = asyncio.Semaphore(max_concurrent)
        self._tasks: set[asyncio.Task] = set()

    @property
    def in_flight(self) -> int:
        return len(self._tasks)

    async def submit(self, user_key: str, question: str, cap: Decimal) -> str | None:
        """Queue a question. Returns the request id, or None if this user already has one active."""
        rid = await asyncio.to_thread(self.db.create_if_idle, user_key, question, str(cap))
        if rid is None:
            return None
        task = asyncio.create_task(self._run(rid, user_key, question, cap), name=f"run-{rid}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        log.info("run queued rid=%s user=%s chars=%d cap=%s", rid, user_ref(user_key), len(question), cap)
        return rid

    async def _run(self, rid: str, user_key: str, question: str, cap: Decimal) -> None:
        async with self._slots:
            s = self.agent_settings
            budget = RunBudget(self.store, cap, s.day_cap_usd, s.min_output_tokens)
            await asyncio.to_thread(self.db.mark_running, rid, budget.run_id)
            try:
                if self.client is None:
                    raise _NoClient
                agent = Agent(s, self.index, self.client, budget)
                answer = await asyncio.wait_for(agent.ask(question), self.timeout)
            except _NoClient:
                await self._finish(rid, user_key, "failed", detail="the Claude client isn't configured (see README)")
                return
            except TimeoutError:
                await self._finish(
                    rid, user_key, "failed", stopped="timeout", detail=f"no answer within {self.timeout:.0f} s",
                    usd=str(budget.spent.usd), calls=budget.spent.calls,
                )  # fmt: skip
                return
            except asyncio.CancelledError:
                await asyncio.shield(
                    self._finish(
                        rid, user_key, "failed", detail="cancelled: the server shut down", usd=str(budget.spent.usd)
                    )
                )
                raise
            except Exception:
                log.exception("run crashed rid=%s", rid)
                await self._finish(rid, user_key, "failed", detail="internal error", usd=str(budget.spent.usd))
                return

            cited = await asyncio.to_thread(citations.extract, answer.text, self._is_indexed)
            await self._finish(
                rid,
                user_key,
                status_for(answer.stopped),
                answer=answer.text,
                stopped=answer.stopped,
                detail=answer.detail,
                steps=answer.steps,
                calls=answer.calls,
                usd=str(answer.usd),
                tool_calls=answer.tool_calls,
                citations=cited,
            )

    def _is_indexed(self, path: str) -> bool:
        return any(f["path"] == path for f in self.index.list_files(path, 5))

    async def _finish(self, rid: str, user_key: str, status: str, **fields) -> None:
        await asyncio.to_thread(self.db.finish, rid, status, **fields)
        log.info(
            "run finished rid=%s user=%s status=%s stopped=%s usd=%s",
            rid, user_ref(user_key), status, fields.get("stopped", ""), fields.get("usd", "0"),
        )  # fmt: skip

    async def wait_idle(self) -> None:
        """Wait for every run in flight (tests and graceful shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def shutdown(self, grace_seconds: float = 5.0) -> None:
        if not self._tasks:
            return
        _, pending = await asyncio.wait(list(self._tasks), timeout=grace_seconds)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


class _NoClient(Exception):
    pass
