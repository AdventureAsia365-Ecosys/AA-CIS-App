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
            patch.dict("os.environ", {"DATABASE_URL": "postgresql://x:y@localhost/z"}):
        app = SimpleNamespace(state=SimpleNamespace())
        async with main.lifespan(app):
            running = gauge_tasks()
            assert len(running) == 1 and not running[0].done()
        assert running[0].cancelled()
    assert order == ["gauge.stop", "pool.close"]
    assert not gauge_tasks()
