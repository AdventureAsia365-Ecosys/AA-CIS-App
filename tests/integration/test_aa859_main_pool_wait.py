"""AA-859 wave 4 (S247) — the main-pool acquire-wait signal on a real asyncpg pool and Postgres: a queued
acquire logs `aa859_main_pool_wait` with the wait, the gauge line carries the interval peak, and an acquire
with no timeout of its own gives up after MAIN_POOL_ACQUIRE_TIMEOUT_S with `aa859_main_pool_acquire_timeout`.
No schema needed (SELECT 1 only)."""
import asyncio

import asyncpg
import pytest
from structlog.testing import capture_logs

from api.core import aa859_main_pool_gauge as gauge
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


@pytest.fixture(autouse=True)
def _fresh_stats():
    gauge._STATS.reset()
    yield
    gauge._STATS.reset()


async def _pool(max_size=2):
    return gauge.instrument_main_pool(await asyncpg.create_pool(DSN, min_size=max_size, max_size=max_size))


@pytest.mark.asyncio
async def test_a_queued_acquire_is_logged_with_its_wait(monkeypatch):
    monkeypatch.setattr(gauge, "MAIN_POOL_WAIT_LOG_MS", 50.0)
    pool = await _pool()
    try:
        assert await pool.fetchval("SELECT 1") == 1          # warm, fast: no line
        a, b = await pool.acquire(), await pool.acquire()      # pool full

        async def free_one_later():
            await asyncio.sleep(0.15)
            await pool.release(a)

        with capture_logs() as logs:
            releaser = asyncio.create_task(free_one_later())
            assert await pool.fetchval("SELECT 2") == 2         # waits ~150 ms for `a`
            await releaser
        await pool.release(b)
        lines = [e for e in logs if e["event"] == "aa859_main_pool_wait"]
        assert len(lines) == 1
        assert lines[0]["wait_ms"] >= 100 and lines[0]["max"] == 2 and lines[0]["in_use"] >= 1

        with capture_logs() as logs:
            gauge.log_main_pool_gauge(pool)
        line = next(e for e in logs if e["event"] == "aa859_main_pool_gauge")
        assert line["acquires"] == 4 and line["peak_in_use"] == 2 and line["waited"] == 1
        assert line["max_wait_ms"] >= 100 and line["in_use"] == 0 and line["timeouts"] == 0
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_an_acquire_without_its_own_timeout_gives_up(monkeypatch):
    monkeypatch.setattr(gauge, "MAIN_POOL_ACQUIRE_TIMEOUT_S", 0.2)
    pool = await _pool(max_size=1)
    try:
        held = await pool.acquire()
        with capture_logs() as logs, pytest.raises(asyncio.TimeoutError):
            await pool.fetchval("SELECT 1")
        err = [e for e in logs if e["event"] == "aa859_main_pool_acquire_timeout"]
        assert len(err) == 1 and err[0]["wait_ms"] >= 150 and err[0]["timeout_s"] == 0.2
        await pool.release(held)
        assert await pool.fetchval("SELECT 3") == 3              # the pool is fine afterwards
    finally:
        await pool.close()
