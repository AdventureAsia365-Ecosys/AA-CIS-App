"""AA-859 PR-S — the 60 s main-pool gauge (api/core/aa859_main_pool_gauge.py) and its wiring in the api
lifespan: one `aa859_main_pool_gauge` line per tick, never raises, cancelled before the main pool closes."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from api.core import aa859_main_pool_gauge as gauge


def _fake_pool(size=4, idle=3, max_size=10):
    pool = MagicMock()
    pool.get_size.return_value = size
    pool.get_idle_size.return_value = idle
    pool.get_max_size.return_value = max_size
    return pool


async def _wait_for(logs, event, count=1, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while sum(1 for entry in logs if entry["event"] == event) < count:
        assert loop.time() < deadline, f"no {count} x {event} within {timeout} s"
        await asyncio.sleep(0.005)


def test_default_interval_is_60_s():
    assert gauge.MAIN_POOL_GAUGE_INTERVAL_S == 60.0


@pytest.mark.asyncio
async def test_start_tick_cancel():
    app = SimpleNamespace(state=SimpleNamespace(pool=_fake_pool()))
    with capture_logs() as logs:
        task = gauge.start_main_pool_gauge(app, interval_s=0.01)
        await _wait_for(logs, "aa859_main_pool_gauge")
        await gauge.stop_main_pool_gauge(task)
    assert task.done() and task.cancelled()
    line = next(entry for entry in logs if entry["event"] == "aa859_main_pool_gauge")
    assert line["log_level"] == "info"
    assert (line["size"], line["idle"], line["in_use"], line["max"]) == (4, 3, 1, 10)


@pytest.mark.asyncio
async def test_first_line_waits_one_interval():
    app = SimpleNamespace(state=SimpleNamespace(pool=_fake_pool()))
    with capture_logs() as logs:
        task = gauge.start_main_pool_gauge(app, interval_s=60)
        await asyncio.sleep(0.02)
        await gauge.stop_main_pool_gauge(task)
    assert not [entry for entry in logs if entry["event"] == "aa859_main_pool_gauge"]


@pytest.mark.asyncio
async def test_a_failing_read_is_logged_and_the_loop_goes_on():
    pool = _fake_pool()
    pool.get_size.side_effect = [RuntimeError("pool closing"), 5]
    app = SimpleNamespace(state=SimpleNamespace(pool=pool))
    with capture_logs() as logs:
        task = gauge.start_main_pool_gauge(app, interval_s=0.01)
        await _wait_for(logs, "aa859_main_pool_gauge")
        await gauge.stop_main_pool_gauge(task)
    failed = [entry for entry in logs if entry["event"] == "aa859_main_pool_gauge_failed"]
    assert len(failed) == 1 and "pool closing" in failed[0]["error"]
    assert next(entry for entry in logs if entry["event"] == "aa859_main_pool_gauge")["size"] == 5


@pytest.mark.asyncio
async def test_no_pool_logs_nothing():
    app = SimpleNamespace(state=SimpleNamespace())
    with capture_logs() as logs:
        task = gauge.start_main_pool_gauge(app, interval_s=0.01)
        await asyncio.sleep(0.05)
        await gauge.stop_main_pool_gauge(task)
    assert not logs


@pytest.mark.asyncio
async def test_stop_none_is_a_no_op():
    await gauge.stop_main_pool_gauge(None)


@pytest.mark.asyncio
async def test_api_lifespan_runs_the_gauge_and_cancels_it_before_the_pool_closes():
    from api import main

    pool = _fake_pool()
    order = []
    pool.close = AsyncMock(side_effect=lambda: order.append("pool.close"))
    redis = MagicMock()
    redis.aclose = AsyncMock()
    real_stop = gauge.stop_main_pool_gauge

    async def stop(task):
        order.append("gauge.stop")
        await real_stop(task)

    def gauge_tasks():
        return [t for t in asyncio.all_tasks() if t.get_name() == "aa859-main-pool-gauge"]

    with patch.object(main.asyncpg, "create_pool", AsyncMock(return_value=pool)), \
            patch.object(main.aioredis, "from_url", return_value=redis), \
            patch("api.core.aa544_tenant_pool.create_tenant_pool", AsyncMock(return_value=None)), \
            patch("shared.jobs.worker.in_api_enabled", return_value=False), \
            patch.object(gauge, "stop_main_pool_gauge", stop), \
            patch("api.core.identity_config.start_cis_staff_jwks_prefetch", return_value=None), \
            patch.dict("os.environ", {"DATABASE_URL": "postgresql://x:y@localhost/z"}):
        app = SimpleNamespace(state=SimpleNamespace())
        async with main.lifespan(app):
            running = gauge_tasks()
            assert len(running) == 1 and not running[0].done()
        assert running[0].cancelled()
    assert order == ["gauge.stop", "pool.close"]
    assert not gauge_tasks()


# ── AA-859 wave 4 (S247): main-pool acquire-wait signal ──────────────────────────────────────────────────────

import asyncpg  # noqa: E402


def _unopened_pool(max_size=5):
    """A real asyncpg.Pool object that never connects (not awaited, so no _initialize)."""
    return asyncpg.create_pool("postgresql://x:y@127.0.0.1:1/z", min_size=0, max_size=max_size)


@pytest.fixture(autouse=True)
def _fresh_stats():
    gauge._STATS.reset()
    yield
    gauge._STATS.reset()


@pytest.fixture
def timed_pool():
    """An instrumented pool whose base `_acquire` is faked: `delay_s` to wait, `in_use` reported after."""
    pool = gauge.instrument_main_pool(_unopened_pool())
    ctl = SimpleNamespace(delay_s=0.0, in_use=1, calls=[], raise_exc=None)

    async def fake_base_acquire(self, timeout):
        ctl.calls.append(timeout)
        if ctl.raise_exc is not None:
            raise ctl.raise_exc
        await asyncio.sleep(ctl.delay_s)
        return "conn"

    with patch.object(asyncpg.Pool, "_acquire", fake_base_acquire), \
            patch.object(asyncpg.Pool, "release", AsyncMock()), \
            patch.object(gauge._InstrumentedMainPool, "get_size", side_effect=lambda: max(ctl.in_use, 2)), \
            patch.object(gauge._InstrumentedMainPool, "get_idle_size",
                         side_effect=lambda: max(ctl.in_use, 2) - ctl.in_use):
        yield pool, ctl


def test_instrument_keeps_the_same_object_and_skips_test_doubles():
    pool = _unopened_pool()
    assert gauge.instrument_main_pool(pool) is pool
    assert isinstance(pool, asyncpg.Pool) and type(pool) is gauge._InstrumentedMainPool
    assert pool.get_max_size() == 5
    double = _fake_pool()
    assert gauge.instrument_main_pool(double) is double and not isinstance(double, gauge._InstrumentedMainPool)


def test_default_acquire_timeout_meets_c2_3g():
    assert 0 < gauge.MAIN_POOL_ACQUIRE_TIMEOUT_S <= 5   # data standards C2.3g: fail fast, <= 5 s


def test_the_hook_is_asyncpgs_single_acquire_path():
    """Pins the asyncpg internals the signal relies on (asyncpg 0.29): an upgrade that renames `_acquire` or
    stops routing acquire()/fetch*() through it must fail here, not silently drop the wait signal."""
    import inspect
    assert inspect.iscoroutinefunction(asyncpg.Pool._acquire)
    assert list(inspect.signature(asyncpg.Pool._acquire).parameters) == ["self", "timeout"]
    assert "pool._acquire(" in inspect.getsource(asyncpg.pool.PoolAcquireContext)
    for shortcut in ("fetch", "fetchval", "fetchrow", "execute"):
        assert "self.acquire()" in inspect.getsource(getattr(asyncpg.Pool, shortcut))
    assert gauge._InstrumentedMainPool.__slots__ == ()


@pytest.mark.asyncio
async def test_every_acquire_path_is_timed_and_gets_the_default_timeout(timed_pool):
    pool, ctl = timed_pool
    assert await pool.acquire() == "conn"
    async with pool.acquire() as conn:
        assert conn == "conn"
    assert await pool.acquire(timeout=2.5) == "conn"
    assert ctl.calls == [gauge.MAIN_POOL_ACQUIRE_TIMEOUT_S, gauge.MAIN_POOL_ACQUIRE_TIMEOUT_S, 2.5]
    assert gauge._STATS.acquires == 3


@pytest.mark.asyncio
async def test_pool_shortcuts_go_through_the_timed_acquire(timed_pool):
    pool, ctl = timed_pool

    class _Conn:
        async def fetchval(self, *a, **k):
            return 7

    async def fake_base_acquire(self, timeout):
        ctl.calls.append(timeout)
        return _Conn()

    with patch.object(asyncpg.Pool, "_acquire", fake_base_acquire):
        assert await pool.fetchval("SELECT 7") == 7
    assert ctl.calls == [gauge.MAIN_POOL_ACQUIRE_TIMEOUT_S] and gauge._STATS.acquires == 1


@pytest.mark.asyncio
async def test_a_fast_acquire_logs_no_wait_line(timed_pool):
    pool, ctl = timed_pool
    with capture_logs() as logs:
        await pool.acquire()
    assert not [e for e in logs if e["event"] == "aa859_main_pool_wait"]
    assert gauge._STATS.waited == 0 and gauge._STATS.acquires == 1


@pytest.mark.asyncio
async def test_a_slow_acquire_logs_one_wait_line(timed_pool, monkeypatch):
    pool, ctl = timed_pool
    monkeypatch.setattr(gauge, "MAIN_POOL_WAIT_LOG_MS", 20.0)
    ctl.delay_s, ctl.in_use = 0.03, 4
    with capture_logs() as logs:
        await pool.acquire()
    lines = [e for e in logs if e["event"] == "aa859_main_pool_wait"]
    assert len(lines) == 1 and lines[0]["log_level"] == "info"
    assert lines[0]["wait_ms"] >= 20.0 and (lines[0]["in_use"], lines[0]["size"], lines[0]["max"]) == (4, 4, 5)
    assert gauge._STATS.waited == 1 and gauge._STATS.max_wait_ms == lines[0]["wait_ms"]


@pytest.mark.asyncio
async def test_wait_lines_are_capped_per_interval_but_still_counted(timed_pool, monkeypatch):
    pool, ctl = timed_pool
    monkeypatch.setattr(gauge, "MAIN_POOL_WAIT_LOG_MS", 0.0)
    monkeypatch.setattr(gauge, "MAIN_POOL_WAIT_LOG_CAP", 3)
    with capture_logs() as logs:
        for _ in range(5):
            await pool.acquire()
    assert len([e for e in logs if e["event"] == "aa859_main_pool_wait"]) == 3
    assert gauge._STATS.waited == 5
    gauge._STATS.take()   # the next interval logs again
    with capture_logs() as logs:
        await pool.acquire()
    assert len([e for e in logs if e["event"] == "aa859_main_pool_wait"]) == 1


@pytest.mark.asyncio
async def test_an_acquire_timeout_is_logged_counted_and_raised(timed_pool):
    pool, ctl = timed_pool
    ctl.raise_exc = asyncio.TimeoutError()
    with capture_logs() as logs:
        with pytest.raises(asyncio.TimeoutError):
            await pool.acquire()
    err = [e for e in logs if e["event"] == "aa859_main_pool_acquire_timeout"]
    assert len(err) == 1 and err[0]["log_level"] == "error"
    assert err[0]["timeout_s"] == gauge.MAIN_POOL_ACQUIRE_TIMEOUT_S and err[0]["max"] == 5
    assert gauge._STATS.timeouts == 1 and gauge._STATS.acquires == 0


@pytest.mark.asyncio
async def test_other_acquire_errors_pass_through_uncounted(timed_pool):
    pool, ctl = timed_pool
    ctl.raise_exc = asyncpg.InterfaceError("pool is closing")
    with capture_logs() as logs, pytest.raises(asyncpg.InterfaceError):
        await pool.acquire()
    assert not logs and gauge._STATS.timeouts == 0


@pytest.mark.asyncio
async def test_gauge_line_carries_the_interval_peak_and_resets(timed_pool, monkeypatch):
    pool, ctl = timed_pool
    monkeypatch.setattr(gauge, "MAIN_POOL_WAIT_LOG_MS", 20.0)
    for in_use, delay in ((2, 0.0), (5, 0.03), (3, 0.0)):
        ctl.in_use, ctl.delay_s = in_use, delay
        await pool.acquire()
    ctl.in_use = 1
    with capture_logs() as logs:
        gauge.log_main_pool_gauge(pool)
        gauge.log_main_pool_gauge(pool)
    first, second = [e for e in logs if e["event"] == "aa859_main_pool_gauge"]
    assert (first["acquires"], first["peak_in_use"], first["waited"], first["timeouts"]) == (3, 5, 1, 0)
    assert first["max_wait_ms"] >= 20.0 and (first["in_use"], first["max"]) == (1, 5)
    assert (second["acquires"], second["peak_in_use"], second["waited"], second["max_wait_ms"]) == (0, 0, 0, 0.0)
