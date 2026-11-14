"""The console's own SQLite file: one row per question asked, with its status and result.

Kept separate from cost-capped-agent's database (index + spend ledger) on purpose: that schema
belongs to the agent library, and the console only reads it through `Index` and `Store`. Both files
live in the same data folder (one volume).

Schema changes are numbered migrations applied at startup (PRAGMA user_version).
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ACTIVE = ("queued", "running")

MIGRATIONS: list[str] = [
    """
    CREATE TABLE requests (
        id           TEXT PRIMARY KEY,
        user_key     TEXT NOT NULL,          -- lower-cased email of the signed-in user
        question     TEXT NOT NULL,
        status       TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'stopped', 'failed')),
        cap_usd      TEXT NOT NULL,
        run_id       TEXT,                   -- links to the agent's spend ledger
        answer       TEXT,
        stopped      TEXT,                   -- the agent's stop reason
        detail       TEXT,
        steps        INTEGER,
        calls        INTEGER,
        usd          TEXT,
        tool_calls   TEXT,                   -- JSON
        citations    TEXT,                   -- JSON
        created_at   TEXT NOT NULL,
        started_at   TEXT,
        finished_at  TEXT
    );
    CREATE INDEX requests_user_created ON requests(user_key, created_at DESC);
    CREATE INDEX requests_created ON requests(created_at DESC);
    -- One active question per user, enforced by the database itself.
    CREATE UNIQUE INDEX requests_one_active ON requests(user_key) WHERE status IN ('queued', 'running');
    """,
]


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="microseconds")  # fixed width: sorts as text


@dataclass
class RequestRow:
    id: str
    user_key: str
    question: str
    status: str
    cap_usd: str
    run_id: str | None
    answer: str | None
    stopped: str | None
    detail: str | None
    steps: int | None
    calls: int | None
    usd: str | None
    tool_calls: list[dict[str, Any]]
    citations: list[dict[str, Any]]
    created_at: str
    started_at: str | None
    finished_at: str | None

    @property
    def active(self) -> bool:
        return self.status in ACTIVE

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> RequestRow:
        d = dict(r)
        d["tool_calls"] = json.loads(d["tool_calls"] or "[]")
        d["citations"] = json.loads(d["citations"] or "[]")
        return cls(**d)


class ConsoleDB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()  # one connection shared by worker threads
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=5000")
            self._migrate()

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for number, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in filter(str.strip, sql.split(";")):
                    self.conn.execute(statement)
                self.conn.execute(f"PRAGMA user_version = {int(number)}")
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def ping(self) -> bool:
        try:
            with self.lock:
                return self.conn.execute("SELECT 1").fetchone()[0] == 1
        except sqlite3.Error:
            return False

    # --- writes -------------------------------------------------------------------------------------

    def create_if_idle(self, user_key: str, question: str, cap_usd: str) -> str | None:
        """Insert a queued request unless this user already has one queued or running."""
        rid = uuid.uuid4().hex
        with self.lock:
            try:
                cur = self.conn.execute(
                    "INSERT INTO requests (id, user_key, question, status, cap_usd, created_at) "
                    "VALUES (?, ?, ?, 'queued', ?, ?)",
                    (rid, user_key, question, cap_usd, utc_now()),
                )
            except sqlite3.IntegrityError:  # the partial unique index: one active request per user
                return None
        return rid if cur.rowcount == 1 else None

    def mark_running(self, rid: str, run_id: str) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE requests SET status = 'running', run_id = ?, started_at = ? WHERE id = ?",
                (run_id, utc_now(), rid),
            )

    def finish(
        self,
        rid: str,
        status: str,
        *,
        answer: str = "",
        stopped: str = "",
        detail: str = "",
        steps: int = 0,
        calls: int = 0,
        usd: str = "0",
        tool_calls: list[dict[str, Any]] | None = None,
        citations: list[dict[str, Any]] | None = None,
    ) -> None:
        with self.lock:
            self.conn.execute(
                """UPDATE requests SET status = ?, answer = ?, stopped = ?, detail = ?, steps = ?, calls = ?,
                   usd = ?, tool_calls = ?, citations = ?, finished_at = ? WHERE id = ?""",
                (
                    status,
                    answer,
                    stopped,
                    detail,
                    steps,
                    calls,
                    usd,
                    json.dumps(tool_calls or []),
                    json.dumps(citations or []),
                    utc_now(),
                    rid,
                ),
            )

    def fail_interrupted(self) -> int:
        """At startup: anything still queued or running was cut off by a restart."""
        with self.lock:
            cur = self.conn.execute(
                "UPDATE requests SET status = 'failed', detail = 'interrupted by a restart', finished_at = ? "
                "WHERE status IN ('queued', 'running')",
                (utc_now(),),
            )
        return cur.rowcount

    # --- reads --------------------------------------------------------------------------------------

    def get(self, rid: str, user_key: str) -> RequestRow | None:
        """A request, only if it belongs to this user."""
        with self.lock:
            row = self.conn.execute("SELECT * FROM requests WHERE id = ? AND user_key = ?", (rid, user_key)).fetchone()
        return RequestRow.from_row(row) if row else None

    def active_for(self, user_key: str) -> RequestRow | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM requests WHERE user_key = ? AND status IN ('queued', 'running')", (user_key,)
            ).fetchone()
        return RequestRow.from_row(row) if row else None

    def history(self, user_key: str, page: int, page_size: int) -> tuple[list[RequestRow], bool]:
        """One page of a user's requests, newest first, and whether there is another page."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM requests WHERE user_key = ? ORDER BY created_at DESC, id LIMIT ? OFFSET ?",
                (user_key, page_size + 1, (page - 1) * page_size),
            ).fetchall()
        return [RequestRow.from_row(r) for r in rows[:page_size]], len(rows) > page_size

    def recent_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        """Last finished runs across users, without questions or answers (for the health page)."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT status, stopped, detail, steps, calls, usd, created_at, finished_at FROM requests "
                "WHERE status NOT IN ('queued', 'running') ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def status_counts(self) -> dict[str, int]:
        with self.lock:
            rows = self.conn.execute("SELECT status, count(*) AS n FROM requests GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}
