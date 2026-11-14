"""An offline stand-in for the Claude client, for local demos only (CLAUDE_CLIENT=demo).

No network, no API key, no cost. It plays the agent's tool loop the way a model would: search the
index for the question, read the best passage, answer from it with a citation. Token usage is
estimated from the request size so the cost pages have plausible numbers to show; the "spend" it
records is simulated. Refused at startup when ENVIRONMENT=production.
"""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace
from typing import Any

DEMO_DELAY_SECONDS = 0.4  # long enough to see "running" in the browser
_WORD = re.compile(r"[A-Za-z0-9]{3,}")
_STOP = {"the", "and", "for", "are", "can", "how", "what", "when", "who", "why", "does", "with", "our", "you", "your"}


def _usage(kwargs: dict[str, Any], output_tokens: int, cached: bool) -> SimpleNamespace:
    prompt = len(json.dumps([kwargs.get("system"), kwargs.get("messages")], default=str)) // 4
    prefix = len(json.dumps(kwargs.get("system"), default=str)) // 4
    cache_read = prefix if cached else 0
    return SimpleNamespace(
        input_tokens=max(prompt - cache_read, 1),
        output_tokens=output_tokens,
        cache_creation_input_tokens=0 if cached else prefix,
        cache_read_input_tokens=cache_read,
    )


def _response(kwargs, content, stop_reason, output_tokens, cached):
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        model=kwargs.get("model", "demo"),
        usage=_usage(kwargs, output_tokens, cached),
        stop_details=None,
    )


def _tool_use(name: str, args: dict[str, Any], n: int) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input=args, id=f"demo_{n}")


def _text(t: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=t)


def _is_heading(body: str) -> bool:
    return body.startswith("#") or body.endswith("?") or (body.startswith("**") and body.endswith("**"))


def best_passage(numbered: str, question: str) -> tuple[int, int, str] | None:
    """Pick the line sharing most words with the question (headings and questions skipped), then
    continue to the end of its paragraph (at most 3 lines). Lines look like '   12  text'."""
    lines: list[tuple[int, str]] = []
    for line in numbered.splitlines():
        number, _, body = line.strip().partition("  ")
        if number.isdigit():
            lines.append((int(number), body.strip()))
    words = {w for w in _WORD.findall(question.lower()) if w not in _STOP}
    best, best_score = None, -1
    for i, (_, body) in enumerate(lines):
        score = len(words & set(_WORD.findall(body.lower())))
        if body and not _is_heading(body) and score > best_score:
            best, best_score = i, score
    if best is None:
        return None
    chosen = [lines[best]]
    for n, body in lines[best + 1 : best + 3]:
        if not body or _is_heading(body) or body.startswith("- "):
            break
        chosen.append((n, body))
    return chosen[0][0], chosen[-1][0], " ".join(body for _, body in chosen)


class _Messages:
    def __init__(self, owner: DemoClaudeClient):
        self.owner = owner

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        await asyncio.sleep(self.owner.delay)
        return self.owner.reply(kwargs)


class DemoClaudeClient:
    def __init__(self, delay: float = DEMO_DELAY_SECONDS):
        self.delay = delay
        self.messages = _Messages(self)
        self.beta = SimpleNamespace(messages=_Messages(self))

    def reply(self, kwargs: dict[str, Any]) -> SimpleNamespace:
        messages = kwargs["messages"]
        question = messages[0]["content"] if isinstance(messages[0]["content"], str) else ""
        last = messages[-1]["content"]
        step = len(messages)
        if isinstance(last, str):  # first turn: the question itself
            if "tools" not in kwargs:  # a summary request
                return _response(
                    kwargs, [_text("A short document from the sample knowledge base.")], "end_turn", 12, False
                )
            words = [w for w in _WORD.findall(question.lower()) if w not in _STOP][:8]
            query = " ".join(words) or question[:100]
            return _response(
                kwargs, [_tool_use("search_index", {"query": query, "limit": 3}, step)], "tool_use", 40, False
            )

        result = last[0]
        previous_call = next(b for b in messages[-2]["content"] if getattr(b, "type", "") == "tool_use")
        if previous_call.name == "search_index" and not result.get("is_error"):
            try:
                hits = json.loads(result["content"])
            except ValueError:
                hits = []
            if hits:
                top = hits[0]
                args = {"path": top["path"], "start_line": top["start_line"], "end_line": top["end_line"]}
                return _response(kwargs, [_tool_use("read_file", args, step)], "tool_use", 45, True)
            text = "I couldn't find anything about that in the indexed files."
            return _response(kwargs, [_text(text)], "end_turn", 20, True)

        if previous_call.name == "read_file" and not result.get("is_error"):
            passage = best_passage(result["content"], question)
            if passage:
                first, last, body = passage
                path = previous_call.input["path"]
                source = f"Source: `{path}:{first}-{last}`. (Demo mode: an offline stand-in for Claude wrote this.)"
                text = f"{body}\n\n{source}"
                return _response(kwargs, [_text(text)], "end_turn", 60, True)
        return _response(kwargs, [_text("I couldn't read the passage I found.")], "end_turn", 15, True)
