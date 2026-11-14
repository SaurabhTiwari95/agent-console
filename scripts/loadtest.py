"""A small load test against a running console (local dev login, demo client). No external calls.

    python scripts/loadtest.py --base-url http://127.0.0.1:8000 --requests 400 --concurrency 10

Start the server with a higher global limit first, or the rate limiter (correctly) answers 429:
    RATE_LIMIT_GLOBAL_PER_MINUTE=100000 python -m agentconsole

It signs in with dev login, then times GET requests to the main pages and reports p50/p95 and
requests per second per page.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time

import httpx

PAGES = ["/ask", "/history", "/knowledge", "/knowledge/search?q=late+fee", "/knowledge/file?path=faq.md", "/health"]


def _csrf(html: str) -> str:
    return html.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]


async def _worker(client: httpx.AsyncClient, path: str, n: int, timings: list[float], statuses: list[int]) -> None:
    for _ in range(n):
        t0 = time.perf_counter()
        resp = await client.get(path)
        timings.append((time.perf_counter() - t0) * 1000)
        statuses.append(resp.status_code)


async def run(base_url: str, total: int, concurrency: int) -> dict[str, dict[str, float]]:
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
        login = await client.get("/login")
        r = await client.post("/auth/dev-login", data={"csrf_token": _csrf(login.text)})
        if r.status_code != 303:
            raise SystemExit(f"dev login failed ({r.status_code}): is DEV_LOGIN_ENABLED on?")
        results: dict[str, dict[str, float]] = {}
        for page in PAGES:
            timings: list[float] = []
            statuses: list[int] = []
            started = time.perf_counter()
            await asyncio.gather(
                *(_worker(client, page, total // concurrency, timings, statuses) for _ in range(concurrency))
            )
            elapsed = time.perf_counter() - started
            q = statistics.quantiles(timings, n=20)
            results[page] = {
                "p50": statistics.median(timings),
                "p95": q[18],
                "rps": len(timings) / elapsed,
                "errors": sum(s != 200 for s in statuses),
            }
        return results


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    p.add_argument("--requests", type=int, default=400)
    p.add_argument("--concurrency", type=int, default=10)
    args = p.parse_args()
    results = asyncio.run(run(args.base_url, args.requests, args.concurrency))
    print(f"{'page':<30} {'p50 ms':>8} {'p95 ms':>8} {'req/s':>7} {'errors':>6}")
    for page, r in results.items():
        print(f"{page:<30} {r['p50']:>8.1f} {r['p95']:>8.1f} {r['rps']:>7.0f} {r['errors']:>6.0f}")


if __name__ == "__main__":
    main()
