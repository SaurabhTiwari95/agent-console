"""Spend figures for the health and cost page, read from cost-capped-agent's ledger.

Uses `Store.report()` (spend per day, model and purpose) and `Store.day_committed()` (today's
settled spend plus open reservations, the number the daily cap is checked against). The summary is
cached in-process for a few seconds: the page is cheap to refresh and the ledger is append-mostly.
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field
from decimal import Decimal

from costagent.store import Store, utc_now

CACHE_SECONDS = 5.0


@dataclass
class ModelDay:
    day: str
    model: str
    purpose: str
    calls: int
    input_tokens: int
    output_tokens: int
    cache_write: int
    cache_read: int
    usd: Decimal

    @property
    def cache_hit_pct(self) -> float:
        prompt = self.input_tokens + self.cache_write + self.cache_read
        return 100.0 * self.cache_read / prompt if prompt else 0.0


@dataclass
class CostSummary:
    days: int
    rows: list[ModelDay]
    per_day: list[tuple[str, Decimal]]
    total_usd: Decimal
    today_usd: Decimal
    day_cap_usd: Decimal
    run_cap_usd: Decimal
    cache_hit_pct: float
    generated_at: float = field(default_factory=time.monotonic)

    @property
    def today_pct(self) -> float:
        return float(min(100 * self.today_usd / self.day_cap_usd, Decimal(100))) if self.day_cap_usd else 0.0

    @property
    def today_left_usd(self) -> Decimal:
        return max(self.day_cap_usd - self.today_usd, Decimal(0))


def summarise(
    store: Store, days: int, day_cap: Decimal, run_cap: Decimal, now: dt.datetime | None = None
) -> CostSummary:
    now = now or utc_now()
    since = (now - dt.timedelta(days=days - 1)).date().isoformat()
    rows: list[ModelDay] = []
    for r in store.report(since):
        usd = sum((Decimal(x) for x in (r["usd_list"] or "0").split(",")), Decimal(0))
        rows.append(
            ModelDay(
                r["day"], r["model"], r["purpose"], r["calls"], r["input_tokens"] or 0, r["output_tokens"] or 0,
                r["cache_write"] or 0, r["cache_read"] or 0, usd,
            )
        )  # fmt: skip
    per_day: dict[str, Decimal] = {}
    for row in rows:
        per_day[row.day] = per_day.get(row.day, Decimal(0)) + row.usd
    prompt = sum(r.input_tokens + r.cache_write + r.cache_read for r in rows)
    cache_read = sum(r.cache_read for r in rows)
    return CostSummary(
        days=days,
        rows=rows,
        per_day=sorted(per_day.items(), reverse=True),
        total_usd=sum((r.usd for r in rows), Decimal(0)),
        today_usd=store.day_committed(now.date().isoformat()),
        day_cap_usd=day_cap,
        run_cap_usd=run_cap,
        cache_hit_pct=100.0 * cache_read / prompt if prompt else 0.0,
    )


class CostCache:
    def __init__(self, ttl: float = CACHE_SECONDS):
        self.ttl = ttl
        self._value: CostSummary | None = None

    def get(self, compute) -> CostSummary:
        if self._value is None or time.monotonic() - self._value.generated_at > self.ttl:
            self._value = compute()
        return self._value

    def clear(self) -> None:
        self._value = None
