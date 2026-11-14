"""Unit tests: citations, costs, rate limiter, client address, logs, console DB, run manager, demo client, scripts."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import anthropic  # noqa: F401 - imported up front: the first import is slow (see README, Performance)
import pytest
from costagent.budget import RunBudget
from costagent.indexer import Index
from costagent.pricing import Usage
from costagent.store import Store

from agentconsole import citations, costs, logs
from agentconsole.db import ConsoleDB
from agentconsole.demo_client import DemoClaudeClient
from agentconsole.main import highlight, usd
from agentconsole.runs import RunManager, status_for, user_ref
from agentconsole.security import RateLimiter, client_ip, is_loopback, security_headers
from tests.conftest import ROOT, make_agent_settings
from tests.fakes import FakeClient, response, text

sys.path.insert(0, str(ROOT / "scripts"))
import make_env  # noqa: E402

# --- citations --------------------------------------------------------------------------------------


def test_citations_keep_only_indexed_paths_and_normalise_ranges():
    indexed = {"faq.md", "handbook/returns.md"}
    found = citations.extract(
        "See faq.md:12-14, `handbook/returns.md:30-20`, faq.md:12-14 again, ghost.md:1-2 and v1.2:3.",
        indexed.__contains__,
    )
    assert found == [
        {"path": "faq.md", "start": 12, "end": 14},
        {"path": "handbook/returns.md", "start": 20, "end": 30},
    ]
    assert citations.extract("faq.md:7", indexed.__contains__) == [{"path": "faq.md", "start": 7, "end": 7}]
    assert citations.extract("", indexed.__contains__) == []


def test_citations_are_capped():
    answer = " ".join(f"faq.md:{n}" for n in range(1, 60))
    assert len(citations.extract(answer, lambda p: True)) == citations.MAX_CITATIONS


# --- costs ------------------------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "agent.db")
    yield s
    s.close()


def _spend(store, day: dt.datetime, model: str, usd_: str, purpose="answer", **use):
    rid = store.reserve("run", purpose, model, Decimal("0.05"), Decimal("100"), day)
    store.settle(rid, model, Decimal(usd_), Usage(**use))


def test_cost_summary_per_day_and_model_with_cache_rate_and_today(store):
    now = dt.datetime(2026, 3, 5, 12, tzinfo=dt.UTC)
    _spend(store, now, "claude-opus-5-5", "0.10", input_tokens=500, cache_read_tokens=1500, output_tokens=100)
    _spend(store, now, "claude-opus-5-5", "0.05", input_tokens=500, output_tokens=50)
    _spend(store, now, "claude-haiku-5-5", "0.01", purpose="summary", input_tokens=1000)
    _spend(store, now - dt.timedelta(days=1), "claude-opus-5-5", "0.20", input_tokens=100)
    _spend(store, now - dt.timedelta(days=30), "claude-opus-5-5", "9.00", input_tokens=100)  # outside the window
    open_reservation = store.reserve("run2", "answer", "claude-opus-5-5", Decimal("0.07"), Decimal("100"), now)
    assert open_reservation

    s = costs.summarise(store, 7, Decimal("2.00"), Decimal("0.25"), now)
    assert s.total_usd == Decimal("0.36")
    assert s.per_day == [("2026-03-05", Decimal("0.16")), ("2026-03-04", Decimal("0.20"))]
    assert s.today_usd == Decimal("0.23")  # settled 0.16 + the open 0.07 reservation, as the cap sees it
    assert s.today_left_usd == Decimal("1.77") and round(s.today_pct, 1) == 11.5
    opus_today = next(r for r in s.rows if r.day == "2026-03-05" and r.model == "claude-opus-5-5")
    assert opus_today.calls == 2 and opus_today.usd == Decimal("0.15") and opus_today.cache_hit_pct == 60.0
    assert round(s.cache_hit_pct, 1) == round(100 * 1500 / 3600, 1)  # all prompt tokens in the window


def test_cost_summary_when_empty_and_over_the_cap(store):
    now = dt.datetime(2026, 3, 5, tzinfo=dt.UTC)
    empty = costs.summarise(store, 7, Decimal("1"), Decimal("0.25"), now)
    assert empty.rows == [] and empty.cache_hit_pct == 0 and empty.today_pct == 0
    assert costs.ModelDay("d", "m", "p", 0, 0, 0, 0, 0, Decimal(0)).cache_hit_pct == 0
    _spend(store, now, "claude-opus-5-5", "3.00")
    over = costs.summarise(store, 1, Decimal("1"), Decimal("0.25"), now)
    assert over.today_pct == 100.0 and over.today_left_usd == 0
    assert costs.summarise(store, 1, Decimal("0"), Decimal("0.25"), now).today_pct == 0.0


def test_cost_cache_expires():
    calls = []

    def compute():
        calls.append(1)
        return costs.CostSummary(1, [], [], Decimal(0), Decimal(0), Decimal(1), Decimal(1), 0.0)

    cache = costs.CostCache(ttl=60)
    cache.get(compute)
    cache.get(compute)
    assert len(calls) == 1
    cache.ttl = -1
    cache.get(compute)
    assert len(calls) == 2
    cache.clear()
    cache.ttl = 60
    cache.get(compute)
    assert len(calls) == 3


# --- security helpers -------------------------------------------------------------------------------


def test_rate_limiter_window_and_memory_bound():
    now = [0.0]
    rl = RateLimiter(max_keys=2, clock=lambda: now[0])
    assert [rl.hit("a", 2) for _ in range(3)] == [True, True, False]
    now[0] = 61
    assert rl.hit("a", 2)
    rl.hit("b", 1)
    rl.hit("c", 1)  # evicts the least recently used key
    assert len(rl._hits) == 2 and "a" not in rl._hits


def _req(headers=None, host="198.51.100.7"):
    return SimpleNamespace(headers=headers or {}, client=SimpleNamespace(host=host) if host else None)


def test_client_ip_trusts_only_the_rightmost_forwarded_entry_when_told_to():
    r = _req({"x-forwarded-for": "203.0.113.66, 198.51.100.20"})
    assert client_ip(r, True) == "198.51.100.20"
    assert client_ip(r, False) == "198.51.100.7"
    assert client_ip(_req(host=None), False) == ""
    assert client_ip(_req(), True) == "198.51.100.7"


def test_loopback_and_headers():
    assert is_loopback("127.0.0.1") and is_loopback("::1") and is_loopback("127.5.5.5")
    assert not is_loopback("198.51.100.7") and not is_loopback("testclient")
    assert "Strict-Transport-Security" in security_headers(True)
    assert "Strict-Transport-Security" not in security_headers(False)


def test_template_filters():
    assert str(highlight("<b>[fee]</b>")) == "&lt;b&gt;<mark>fee</mark>&lt;/b&gt;"
    assert usd("0.1") == "$0.1000" and usd(None) == "-" and usd("x") == "-"


# --- logs -------------------------------------------------------------------------------------------


def test_json_log_lines(capsys):
    record = logging.LogRecord("agentconsole", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    record.rid = "abc"
    line = json.loads(logs.JsonFormatter().format(record))
    assert line["msg"] == "hello world" and line["rid"] == "abc" and line["level"] == "INFO"
    try:
        raise ValueError("boom")
    except ValueError:
        err = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    assert "ValueError: boom" in json.loads(logs.JsonFormatter().format(err))["exc"]


def test_configure_is_idempotent():
    logs.configure("INFO")
    before = len(logging.getLogger().handlers)
    logs.configure("WARNING")
    assert len(logging.getLogger().handlers) == before
    assert logging.getLogger().level == logging.WARNING
    assert logging.getLogger("httpx2").level == logging.WARNING
    logs.configure("INFO")


def test_user_ref_is_a_stable_pseudonym():
    assert user_ref("a@example.com") == user_ref("a@example.com") != user_ref("b@example.com")
    assert "@" not in user_ref("a@example.com") and len(user_ref("x")) == 12


def test_status_mapping():
    assert status_for("done") == "done"
    assert {status_for(s) for s in ("budget", "max_steps", "output_limit", "refused")} == {"stopped"}
    assert status_for("api_error") == status_for("client_error") == "failed"


# --- console DB -------------------------------------------------------------------------------------


def test_db_migrates_once_and_enforces_one_active_request(tmp_path):
    path = tmp_path / "c.db"
    db = ConsoleDB(path)
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 1
    a = db.create_if_idle("u@example.com", "q1", "0.10")
    assert a and db.create_if_idle("u@example.com", "q2", "0.10") is None
    assert db.create_if_idle("v@example.com", "q3", "0.10")
    db.mark_running(a, "run-1")
    db.finish(a, "done", answer="x", usd="0.01", tool_calls=[{"tool": "t"}], citations=[{"path": "p"}])
    row = db.get(a, "u@example.com")
    assert row.status == "done" and row.run_id == "run-1" and row.tool_calls == [{"tool": "t"}] and not row.active
    assert db.get(a, "v@example.com") is None
    assert db.create_if_idle("u@example.com", "q4", "0.10")
    assert db.status_counts() == {"done": 1, "queued": 2}
    assert [r["status"] for r in db.recent_runs()] == ["done"]
    db.close()
    again = ConsoleDB(path)  # re-opening runs no migration twice
    assert again.fail_interrupted() == 2 and again.active_for("u@example.com") is None
    assert again.ping()
    again.close()
    assert not again.ping()


# --- run manager ------------------------------------------------------------------------------------


@pytest.fixture
def run_parts(tmp_path):
    agent_settings = make_agent_settings(tmp_path)
    store = Store(agent_settings.db_path)
    index = Index(store, agent_settings.chunk_lines)
    index.build(ROOT / "sample_corpus", agent_settings.max_file_bytes)
    db = ConsoleDB(tmp_path / "c.db")
    yield db, store, index, agent_settings
    db.close()
    store.close()


async def test_a_run_that_takes_too_long_is_cancelled_and_its_reservation_counted(run_parts):
    db, store, index, agent_settings = run_parts

    in_call = asyncio.Event()

    async def hang(kwargs):
        in_call.set()
        await asyncio.sleep(10)

    client = FakeClient([hang])
    rm = RunManager(db, store, index, agent_settings, client, max_concurrent=1, timeout_seconds=0.5)
    rid = await rm.submit("u@example.com", "will time out", Decimal("0.10"))
    await rm.wait_idle()
    assert in_call.is_set()
    row = db.get(rid, "u@example.com")
    assert row.status == "failed" and row.stopped == "timeout" and "no answer within" in row.detail
    assert Decimal(row.usd) > 0  # the worst case stays counted: the call may have been billed
    today = dt.datetime.now(dt.UTC).date().isoformat()
    assert store.day_committed(today) == Decimal(row.usd)


async def test_shutdown_cancels_runs_and_marks_them(run_parts):
    db, store, index, agent_settings = run_parts

    async def hang(kwargs):
        await asyncio.sleep(10)

    rm = RunManager(db, store, index, agent_settings, FakeClient([hang]), max_concurrent=1, timeout_seconds=60)
    rid = await rm.submit("u@example.com", "cut short", Decimal("0.10"))
    await asyncio.sleep(0.05)
    await rm.shutdown(grace_seconds=0.01)
    row = db.get(rid, "u@example.com")
    assert row.status == "failed" and "shut down" in row.detail
    await RunManager(db, store, index, agent_settings, None, 1, 1).shutdown()  # nothing to do


async def test_the_daily_cap_is_shared_across_runs(run_parts):
    db, store, index, agent_settings = run_parts
    agent_settings = agent_settings.model_copy(update={"day_cap_usd": Decimal("0.01")})
    rm = RunManager(db, store, index, agent_settings, FakeClient([response([text("ok")])]), 2, 60)
    rid = await rm.submit("u@example.com", "first", Decimal("0.25"))
    await rm.wait_idle()
    row = db.get(rid, "u@example.com")
    assert row.stopped == "budget"  # the per-question cap is fine, the day cap isn't


# --- demo client ------------------------------------------------------------------------------------


async def test_demo_client_plays_search_read_answer(run_parts):
    db, store, index, agent_settings = run_parts
    from costagent.agent import Agent

    budget = RunBudget(store, Decimal("0.25"), Decimal("2"), agent_settings.min_output_tokens)
    answer = await Agent(agent_settings, index, DemoClaudeClient(delay=0), budget).ask("What are the payment terms?")
    assert answer.stopped == "done" and [t["tool"] for t in answer.tool_calls] == ["search_index", "read_file"]
    assert "Source: `" in answer.text and answer.usd > 0
    assert answer.usage_by_model["claude-opus-5-5"]["cache_read_tokens"] > 0


async def test_demo_client_summary_and_odd_tool_results():
    demo = DemoClaudeClient(delay=0)
    summary = await demo.messages.create(model="claude-haiku-5-5", messages=[{"role": "user", "content": "<file/>"}])
    assert summary.content[0].text.startswith("A short document")
    first = SimpleNamespace(type="tool_use", name="search_index", input={}, id="1")
    bad = demo.reply({"messages": [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [first]},
        {"role": "user", "content": [{"type": "tool_result", "content": "not json"}]},
    ]})  # fmt: skip
    assert "couldn't find" in bad.content[0].text
    read = SimpleNamespace(type="tool_use", name="read_file", input={"path": "p"}, id="2")
    err = demo.reply({"messages": [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [read]},
        {"role": "user", "content": [{"type": "tool_result", "content": "x", "is_error": True}]},
    ]})  # fmt: skip
    assert "couldn't read" in err.content[0].text


# --- scripts ----------------------------------------------------------------------------------------


def test_make_env_writes_a_fresh_secret_and_never_overwrites(tmp_path, capsys):
    (tmp_path / ".env.example").write_text(
        "# comment\nSESSION_SECRET=replace-with-a-long-random-secret\nOTHER=1\n", encoding="utf-8"
    )
    assert make_env.main([], root=tmp_path) == 0
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    secret = next(line.split("=", 1)[1] for line in env.splitlines() if line.startswith("SESSION_SECRET="))
    assert len(secret) >= 48 and "replace-with" not in secret and "OTHER=1" in env
    assert make_env.main([], root=tmp_path) == 1
    assert make_env.main(["--force"], root=tmp_path) == 0
    assert (tmp_path / ".env").read_text(encoding="utf-8") != env


def test_env_example_is_a_working_local_config(tmp_path, monkeypatch):
    """The documented quick start: make_env.py output must pass the startup checks."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.example").write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
    make_env.main([], root=tmp_path)
    from agentconsole.config import Settings

    s = Settings(_env_file=str(tmp_path / ".env"))
    assert s.dev_login_enabled and s.claude_client == "demo" and s.is_local and not s.session_cookie_secure


def test_project_files_exist():
    for name in ("README.md", "SECURITY.md", "CHANGELOG.md", "LICENSE", "Dockerfile", "render.yaml", ".gitignore",
                 "docs/deploy.md", "docs/CHECKLIST.md", ".github/workflows/ci.yml", ".github/dependabot.yml"):  # fmt: skip
        assert (ROOT / name).is_file(), name
    assert not Path(ROOT / "blocked-words.txt").exists()


def test_demo_best_passage_skips_headings_and_reads_to_the_paragraph_end():
    from agentconsole.demo_client import best_passage

    numbered = "   12  **I was charged a late fee. Can it be removed?**\n   13  A late fee can be waived once\n   14  if paid within 7 days.\n   15  \n   16  Unrelated."
    assert best_passage(numbered, "Can a late fee be waived?") == (
        13,
        14,
        "A late fee can be waived once if paid within 7 days.",
    )
    assert best_passage("    1  # Title\n    2  **Bold?**", "anything") is None
    assert best_passage("not numbered", "x") is None
