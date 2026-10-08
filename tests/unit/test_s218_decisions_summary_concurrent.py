"""AA-660 follow-up (S218) — /admin/decisions/summary runs its reads side by side, not one after another.

At 7 days the ledger window held ~700k rows and the five reads in sequence crossed the 29 s
API Gateway limit (504). Each read now gets its own pooled connection via asyncio.gather."""
import asyncio
import time
from datetime import datetime, date, timezone
from types import SimpleNamespace

import pytest

from api.routers import admin_decisions as m

_NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)


class _SlowPool:
    def __init__(self, delay):
        self.delay, self.in_flight, self.peak = delay, 0, 0

    async def fetch(self, sql, *args):
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        await asyncio.sleep(self.delay)
        self.in_flight -= 1
        if sql is m._SUMMARY_SQL:
            return [{"question_key": "q", "criteria": '["a"]', "updated_at": _NOW,
                     "last_used_at": _NOW, "first_used_at": _NOW}]
        if sql is m._CALLS_SQL:
            return [{"stage": "s", "calls": 3, "cost_usd": 0.5, "tokens_in": 10}]
        if sql is m._DAILY_SQL:
            return [{"day": date(2026, 10, 8), "verdicts": 3}]
        if sql in (m._ORPHAN_SQL, m._STAGE_SQL):
            return [{"stage": "s", "last_used_at": _NOW}]
        return [{"tenant_id": "t", "slug": "x", "name": "X", "reason": "r"}]


@pytest.mark.asyncio
async def test_summary_reads_run_concurrently(monkeypatch):
    monkeypatch.setattr(m, "verify_admin_secret", lambda _s: None)
    pool = _SlowPool(0.2)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))
    t0 = time.monotonic()
    out = await m.summary(req, days=7, x_admin_secret="x")
    elapsed = time.monotonic() - t0
    assert pool.peak == 6
    assert elapsed < 0.6  # six 0.2 s reads in sequence would take 1.2 s
    assert out["questions"][0]["criteria"] == ["a"]
    assert out["questions"][0]["last_used_at"] == _NOW.isoformat()
    assert out["daily"][0]["day"] == "2026-10-08"
    assert out["total_calls"] == 3 and out["total_cost_usd"] == 0.5
    assert out["tenant_allowlist"][0]["slug"] == "x"
