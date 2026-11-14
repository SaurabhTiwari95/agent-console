"""Turn `path:12-30` citations in an answer into links, keeping only paths that are really indexed."""

from __future__ import annotations

import re
from collections.abc import Callable

CITATION = re.compile(r"(?<![\w/.-])([\w][\w./-]{0,250}\.[A-Za-z0-9]{1,8}):(\d{1,6})(?:-(\d{1,6}))?")
MAX_CITATIONS = 20


def extract(answer: str, is_indexed: Callable[[str], bool]) -> list[dict[str, int | str]]:
    found: list[dict[str, int | str]] = []
    seen: set[tuple[str, int, int]] = set()
    for m in CITATION.finditer(answer or ""):
        path, start = m.group(1), int(m.group(2))
        end = int(m.group(3) or start)
        if end < start:
            start, end = end, start
        key = (path, start, end)
        if key in seen or not is_indexed(path):
            continue
        seen.add(key)
        found.append({"path": path, "start": start, "end": end})
        if len(found) >= MAX_CITATIONS:
            break
    return found
