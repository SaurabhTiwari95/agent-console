"""Ask, history, knowledge, health and cost pages, end to end through the app with a scripted Claude."""

from __future__ import annotations

import asyncio
import json
import logging
import re

import pytest

from tests.conftest import ALICE, DEV, ROOT, make_settings
from tests.fakes import response, sign_in, text, tool_use, usage

PROTECTED = ["/ask", "/history", "/knowledge", "/knowledge/search?q=fee", "/knowledge/file?path=faq.md", "/health",
             "/requests/" + "a" * 32]  # fmt: skip


def answer_script():
    return [
        response([tool_use("search_index", {"query": "late fee", "limit": 5}, "a")], "tool_use"),
        response(
            [tool_use("read_file", {"path": "handbook/payment-terms.md", "start_line": 1, "end_line": 27}, "b")],
            "tool_use",
            use=usage(2_000, 80, cache_read=1_500),
        ),
        response([text("1.5% a month after 30 days overdue (handbook/payment-terms.md:17-21; see also `faq.md:12`).")]),
    ]


def rid_of(r) -> str:
    assert r.status_code == 303, r.text
    return r.headers["location"].rsplit("/", 1)[1]


@pytest.mark.parametrize("path", PROTECTED)
async def test_pages_need_sign_in(app_h, path):
    r = await app_h.client.get(path)
    assert r.status_code == 303 and r.headers["location"] == "/login"


async def test_post_routes_need_sign_in(app_h):
    r = await app_h.client.post("/ask", data={"question": "hello there"})
    assert r.headers["location"] == "/login"


async def test_home_redirects_to_ask(local_h):
    r = await local_h.client.get("/")
    assert r.headers["location"] == "/ask"


async def test_ask_runs_the_agent_in_the_background_and_shows_the_answer(local_h, fake_claude):
    fake_claude.script = answer_script()
    r = await local_h.ask("When is a late fee charged?", "0.10")
    rid = rid_of(r)
    await local_h.console.runs.wait_idle()

    page = await local_h.client.get(f"/requests/{rid}")
    assert page.status_code == 200
    html = page.text
    assert "1.5% a month after 30 days overdue" in html
    assert 'class="status done"' in html
    assert "/knowledge/file?path=handbook/payment-terms.md&amp;from=7&amp;hl=17-21#L17" in html
    assert "faq.md:12-12" in html
    assert "search_index" in html and "read_file" in html
    assert "of a $0.1000 cap" in html
    assert 'http-equiv="refresh"' not in html  # finished: no more polling

    # The request really went to Claude through cost-capped-agent, capped at what the user chose.
    endpoint, req = fake_claude.calls[0]
    assert endpoint == "beta.messages" and req["model"] == "claude-opus-5-5"
    row = local_h.console.db.get(rid, DEV)
    assert row.status == "done" and row.stopped == "done" and row.steps == 3 and row.calls == 3
    assert row.cap_usd == "0.10" and float(row.usd) > 0 and row.run_id


async def test_status_page_polls_while_running_and_one_question_at_a_time(local_h, fake_claude):
    gate = asyncio.Event()

    async def slow(kwargs):
        await gate.wait()
        return response([text("done now")])

    fake_claude.script = [slow]
    rid = rid_of(await local_h.ask("First question here"))
    for _ in range(50):
        if local_h.console.db.get(rid, DEV).status == "running":
            break
        await asyncio.sleep(0.01)
    page = await local_h.client.get(f"/requests/{rid}")
    assert 'http-equiv="refresh" content="2"' in page.text and 'class="status running"' in page.text

    second = await local_h.ask("Second question here")
    assert second.status_code == 409 and "already have a question running" in second.text
    ask_page = await local_h.client.get("/ask")
    assert f"/requests/{rid}" in ask_page.text and "disabled" in ask_page.text

    gate.set()
    await local_h.console.runs.wait_idle()
    assert local_h.console.db.get(rid, DEV).status == "done"
    fake_claude.script = [response([text("ok")])]
    assert (await local_h.ask("Now the next one")).status_code == 303


async def test_runs_queue_beyond_the_concurrency_limit(make_harness, tmp_path, agent_settings, fake_claude):
    s = make_settings(tmp_path, environment="development", base_url="http://127.0.0.1:8000",
                      session_cookie_secure=False, dev_login_enabled=True, max_concurrent_runs=1)  # fmt: skip
    gate = asyncio.Event()

    async def slow(kwargs):
        await gate.wait()
        return response([text("ok")])

    fake_claude.script = [slow, slow]
    h1 = await make_harness(s, agent_settings, fake_claude)
    await h1.dev_login()
    a = rid_of(await h1.ask("question one"))
    rm = h1.console.runs
    b = await rm.submit("alice@example.com", "question two", agent_settings.run_cap_usd)
    await asyncio.sleep(0.05)
    assert h1.console.db.get(a, DEV).status == "running"
    assert h1.console.db.get(b, "alice@example.com").status == "queued"
    gate.set()
    await rm.wait_idle()
    assert h1.console.db.get(b, "alice@example.com").status == "done"


async def test_ask_needs_a_valid_csrf_token(local_h):
    r = await local_h.ask("A good question", csrf="forged")
    assert r.status_code == 403
    r = await local_h.client.post("/ask", data={"question": "no token at all"})
    assert r.status_code == 403


@pytest.mark.parametrize(
    "question, cap, message",
    [
        ("hi", "0.10", "at least 3 characters"),
        ("   ", "0.10", "at least 3 characters"),
        ("A fine question", "abc", "a cap in dollars"),
        ("A fine question", "NaN", "a cap in dollars"),
        ("A fine question", "Infinity", "a cap in dollars"),
        ("A fine question", "0", "between $0.01 and $0.25"),
        ("A fine question", "0.26", "between $0.01 and $0.25"),
        ("A fine question", "-1", "between $0.01 and $0.25"),
        ("x" * 2_001, "0.10", "limited to 2000 characters"),
    ],
)
async def test_ask_validates_input(local_h, fake_claude, question, cap, message):
    r = await local_h.ask(question, cap)
    assert r.status_code == 422 and message in r.text
    assert fake_claude.calls == [] and local_h.console.runs.in_flight == 0


async def test_cap_defaults_to_the_run_cap(local_h, fake_claude):
    fake_claude.script = [response([text("ok")])]
    token = await local_h.page_csrf()
    rid = rid_of(await local_h.client.post("/ask", data={"question": "Default cap please", "csrf_token": token}))
    await local_h.console.runs.wait_idle()
    assert local_h.console.db.get(rid, DEV).cap_usd == "0.25"


async def test_a_tiny_cap_stops_cleanly_with_the_budget_reason(local_h, fake_claude):
    rid = rid_of(await local_h.ask("Anything at all", "0.01"))
    await local_h.console.runs.wait_idle()
    row = local_h.console.db.get(rid, DEV)
    assert row.status == "stopped" and row.stopped == "budget" and fake_claude.calls == []
    page = (await local_h.client.get(f"/requests/{rid}")).text
    assert "spend cap was reached" in page and 'class="status stopped"' in page


async def test_client_errors_and_crashes_are_failed_runs_without_leaks(local_h, fake_claude):
    fake_claude.script = [TypeError("Could not resolve authentication method")]
    rid = rid_of(await local_h.ask("Question one"))
    await local_h.console.runs.wait_idle()
    row = local_h.console.db.get(rid, DEV)
    assert row.status == "failed" and row.stopped == "client_error"

    fake_claude.script = [RuntimeError("secret internals at /srv/app")]
    rid = rid_of(await local_h.ask("Question two"))
    await local_h.console.runs.wait_idle()
    row = local_h.console.db.get(rid, DEV)
    assert row.status == "failed" and row.detail == "internal error"
    assert "secret internals" not in (await local_h.client.get(f"/requests/{rid}")).text


async def test_no_claude_client_fails_the_run_with_a_hint(make_harness, local_settings, agent_settings, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import agentconsole.main as main_mod

    monkeypatch.setattr(main_mod, "default_client", lambda s, a: None)
    h = await make_harness(local_settings, agent_settings, None)
    await h.dev_login()
    rid = rid_of(await h.ask("Is anyone there?"))
    await h.console.runs.wait_idle()
    assert "isn&#39;t configured" in (await h.client.get(f"/requests/{rid}")).text


async def test_requests_are_private_to_their_owner(make_harness, settings, agent_settings, provider, fake_claude):
    fake_claude.script = [response([text("Alice's answer")])]
    alice = await make_harness(settings, agent_settings, fake_claude, oidc=provider)
    await sign_in(alice.client, provider)
    rid = rid_of(await alice.ask("Alice's private question"))
    await alice.console.runs.wait_idle()

    bob = await make_harness(settings, agent_settings, fake_claude, oidc=provider)
    provider.claims = {"email": "bob@example.com"}
    await sign_in(bob.client, provider)
    assert (await bob.client.get(f"/requests/{rid}")).status_code == 404
    assert "Alice&#39;s private question" not in (await bob.client.get("/history")).text
    assert (await bob.client.get("/requests/not-a-valid-id")).status_code == 404


async def test_history_is_paginated_newest_first(local_h):
    db = local_h.console.db
    for i in range(23):
        rid = db.create_if_idle(DEV, f"question number {i}", "0.10")
        db.finish(rid, "done", answer="a", usd="0.001")
    first = (await local_h.client.get("/history")).text
    assert "question number 22" in first and "question number 2<" not in first
    assert "/history?page=2" in first
    second = (await local_h.client.get("/history?page=2")).text
    assert "question number 0" in second and "/history?page=3" not in second and "/history?page=1" in second
    assert (await local_h.client.get("/history?page=0")).status_code == 422
    empty = (await local_h.client.get("/history?page=9")).text
    assert "No questions on this page" in empty


async def test_knowledge_lists_searches_and_shows_files(local_h):
    listing = (await local_h.client.get("/knowledge")).text
    for path in ("faq.md", "handbook/payment-terms.md", "catalogue/products.md"):
        assert path in listing
    only = (await local_h.client.get("/knowledge?prefix=handbook/")).text
    assert "handbook/returns.md" in only and "faq.md" not in only

    hits = (await local_h.client.get("/knowledge/search?q=late fee")).text
    assert "<mark>" in hits and "handbook/payment-terms.md:" in hits

    view = (await local_h.client.get("/knowledge/file?path=handbook/payment-terms.md&hl=3-4")).text
    assert 'id="L1"' in view and '<span id="L3" class="line hl">' in view and '<span id="L5" class="line">' in view
    assert (await local_h.client.get("/knowledge/file?path=nope.md")).status_code == 404
    assert (await local_h.client.get("/knowledge/file?path=faq.md&hl=1;drop")).status_code == 422
    assert (await local_h.client.get("/knowledge/search?q=")).status_code == 200


async def test_knowledge_pages_are_paginated(make_harness, tmp_path, local_settings, agent_settings):
    corpus = tmp_path / "big"
    corpus.mkdir()
    for i in range(120):
        (corpus / f"doc{i:03}.md").write_text(
            "\n".join(f"line {n} of doc {i}" for n in range(1, 251)), encoding="utf-8"
        )
    s = local_settings.model_copy(update={"index_on_start": str(corpus)})
    h = await make_harness(s, agent_settings)
    await h.dev_login()
    p1 = (await h.client.get("/knowledge")).text
    assert "doc000.md" in p1 and "doc050.md" not in p1 and "page=2" in p1
    p3 = (await h.client.get("/knowledge?page=3")).text
    assert "doc119.md" in p3 and "page=4" not in p3
    view = (await h.client.get("/knowledge/file?path=doc001.md")).text
    assert 'id="L200"' in view and 'id="L201"' not in view and "from=201" in view
    tail = (await h.client.get("/knowledge/file?path=doc001.md&from=201")).text
    assert 'id="L250"' in tail and "from=1" in tail


async def test_indexed_content_is_escaped_everywhere(make_harness, tmp_path, local_settings, agent_settings):
    corpus = tmp_path / "evil"
    corpus.mkdir()
    (corpus / "x.md").write_text("<script>alert('pwned')</script> invoice [markers]\n", encoding="utf-8")
    h = await make_harness(local_settings.model_copy(update={"index_on_start": str(corpus)}), agent_settings)
    await h.dev_login()
    for path in ("/knowledge/search?q=invoice", "/knowledge/file?path=x.md", "/knowledge"):
        html = (await h.client.get(path)).text
        assert "<script>" not in html, path
    assert "&lt;script&gt;" in (await h.client.get("/knowledge/file?path=x.md")).text


async def test_health_page_shows_spend_caps_cache_and_last_runs(local_h, fake_claude):
    fake_claude.script = answer_script()
    rid_of(await local_h.ask("When is a late fee charged?"))
    await local_h.console.runs.wait_idle()
    local_h.console.costs.clear()
    html = (await local_h.client.get("/health")).text
    assert "claude-opus-5-5" in html and "answer" in html
    assert "<progress" in html and "of $2.0000" in html
    assert "Prompt cache hit rate" in html
    assert "console db" in html and "agent db" in html and "index" in html
    assert 'class="status done">ok' in html
    assert "Indexed files: 6" in html
    assert "When is a late fee charged?" not in html  # no questions on the shared page
    assert (await local_h.client.get("/health?days=30")).status_code == 200
    assert (await local_h.client.get("/health?days=0")).status_code == 422


async def test_healthz_and_readyz(make_harness, tmp_path, local_settings, agent_settings):
    h = await make_harness(local_settings, agent_settings)
    assert (await h.client.get("/healthz")).json() == {"status": "ok"}
    ready = await h.client.get("/readyz")
    assert ready.status_code == 200 and ready.json() == {
        "status": "ready",
        "console_db": True,
        "agent_db": True,
        "index": True,
    }

    empty = await make_harness(
        make_settings(tmp_path / "e", index_on_start=""),
        agent_settings.model_copy(update={"db_path": str(tmp_path / "e.db")}),
    )
    r = await empty.client.get("/readyz")
    assert r.status_code == 503 and r.json()["index"] is False


async def test_missing_index_folder_is_a_warning_not_a_crash(make_harness, tmp_path, agent_settings, caplog):
    h = await make_harness(make_settings(tmp_path, index_on_start=str(tmp_path / "missing")), agent_settings)
    assert (await h.client.get("/healthz")).status_code == 200
    assert "INDEX_ON_START folder not found" in caplog.text


async def test_security_headers_and_no_inline_code(local_h):
    r = await local_h.client.get("/ask")
    h = r.headers
    csp = h["content-security-policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
    assert "script-src" not in csp  # no scripts at all
    assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
    assert h["cache-control"] == "no-store" and len(h["x-request-id"]) == 32
    for path in ("/ask", "/history", "/knowledge", "/health", "/login"):
        html = (await local_h.client.get(path)).text
        assert "<script" not in html and "style=" not in html and " on" + "click" not in html
    css = await local_h.client.get("/static/app.css")
    assert css.status_code == 200 and css.headers["cache-control"] == "public, max-age=3600"


async def test_hsts_is_sent_when_cookies_are_secure(app_h):
    r = await app_h.client.get("/login")
    assert r.headers["strict-transport-security"].startswith("max-age=")


async def test_request_size_limits(local_h):
    token = await local_h.page_csrf()
    big = await local_h.client.post("/ask", data={"question": "x" * 20_000, "csrf_token": token})
    assert big.status_code == 413

    async def body():
        yield b"question=x"

    chunked = await local_h.client.post("/ask", content=body())
    assert chunked.status_code == 411


async def test_login_rate_limit(app_h):
    codes = [(await app_h.client.get("/auth/google")).status_code for _ in range(11)]
    assert codes[:10] == [302] * 10 and codes[10] == 429


async def test_ask_rate_limit(make_harness, local_settings, agent_settings, fake_claude):
    s = local_settings.model_copy(update={"rate_limit_ask_per_minute": 2})
    h = await make_harness(s, agent_settings, fake_claude)
    await h.dev_login()
    statuses = [(await h.ask("hi")).status_code for _ in range(3)]
    assert statuses == [422, 422, 429]
    assert "Retry-After" in (await h.ask("hi")).headers


async def test_global_rate_limit_spares_health_checks(make_harness, local_settings, agent_settings):
    s = local_settings.model_copy(update={"rate_limit_global_per_minute": 10})
    h = await make_harness(s, agent_settings)
    codes = [(await h.client.get("/login")).status_code for _ in range(12)]
    assert codes[-1] == 429
    assert (await h.client.get("/healthz")).status_code == 200


async def test_unexpected_errors_show_a_generic_page(local_h, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("database password is hunter2")

    monkeypatch.setattr(local_h.console.db, "history", boom)
    r = await local_h.client.get("/history")
    assert r.status_code == 500 and "hunter2" not in r.text and "Traceback" not in r.text
    assert r.json()["request_id"] == r.headers["x-request-id"]


async def test_not_found_and_bad_method_pages(local_h):
    r = await local_h.client.get("/no-such-page")
    assert r.status_code == 404 and "Not found." in r.text and r.headers["x-request-id"] in r.text
    assert (await local_h.client.delete("/ask")).status_code == 405


async def test_logs_hold_no_question_text_email_or_query_string(local_h, fake_claude, caplog):
    caplog.set_level(logging.INFO)
    fake_claude.script = answer_script()
    rid_of(await local_h.ask("What is my secret salary question?"))
    await local_h.console.runs.wait_idle()
    await local_h.client.get("/knowledge/search?q=confidential-term")
    everything = caplog.text + json.dumps([r.__dict__ for r in caplog.records], default=str)
    assert "salary" not in everything and "confidential-term" not in everything
    assert DEV not in everything and ALICE not in everything
    assert any(r.getMessage().startswith("run finished") for r in caplog.records)


async def test_restart_marks_interrupted_runs_failed(make_harness, local_settings, agent_settings):
    from agentconsole.db import ConsoleDB

    db = ConsoleDB(local_settings.console_db_path)
    rid = db.create_if_idle(DEV, "cut off by a restart", "0.10")
    db.close()
    h = await make_harness(local_settings, agent_settings)
    await h.dev_login()
    row = h.console.db.get(rid, DEV)
    assert row.status == "failed" and row.detail == "interrupted by a restart"


async def test_demo_client_answers_offline_end_to_end(make_harness, local_settings, agent_settings):
    s = local_settings.model_copy(update={"claude_client": "demo"})
    h = await make_harness(s, agent_settings)
    h.console.runs.client.delay = 0
    await h.dev_login()
    page = (await h.client.get("/ask")).text
    assert "Demo mode" in page
    rid = rid_of(await h.ask("When is a late fee charged?"))
    await h.console.runs.wait_idle()
    row = h.console.db.get(rid, DEV)
    assert row.status == "done" and row.citations and row.steps == 3
    assert re.search(r"late fee", row.answer, re.I)
    rid = rid_of(await h.ask("zzqx qqzz"))
    await h.console.runs.wait_idle()
    assert "couldn't find" in h.console.db.get(rid, DEV).answer


def test_sample_corpus_is_present():
    assert (ROOT / "sample_corpus" / "faq.md").is_file()
