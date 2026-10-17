"""S241 (AA-859 review) — the api's Redis client is time-bounded (api/core/redis_client.py).

Without socket timeouts a Redis that accepts no answer never raises, so the rate-limit middleware (which
awaits Redis before the route runs) held every tenant request until the API GW 30 s timeout. These tests
use a real redis-py client against a local TCP server that accepts connections and never answers.
"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from starlette.requests import Request

from api.core import aa544_tenant_pool as tp
from api.core import redis_client


def test_client_has_connect_and_read_timeouts_and_bounded_retry():
    client = redis_client.build_api_redis("redis://localhost:6379")
    kw = client.connection_pool.connection_kwargs
    assert kw["socket_connect_timeout"] == redis_client.REDIS_SOCKET_CONNECT_TIMEOUT_S == 0.5
    assert kw["socket_timeout"] == redis_client.REDIS_SOCKET_TIMEOUT_S == 0.5
    assert not kw.get("health_check_interval")  # hangs with retry on redis-py 5.2 (see the module)
    assert kw["decode_responses"] is True
    assert kw["retry"]._retries == redis_client.REDIS_RETRIES == 1


@pytest.mark.asyncio
async def test_api_lifespan_builds_the_bounded_client():
    from api import main

    pool = MagicMock()
    pool.close = AsyncMock()
    with patch.object(main.asyncpg, "create_pool", AsyncMock(return_value=pool)), \
            patch("api.core.aa544_tenant_pool.create_tenant_pool", AsyncMock(return_value=None)), \
            patch("shared.jobs.worker.in_api_enabled", return_value=False), \
            patch("api.core.aa859_main_pool_gauge.start_main_pool_gauge", return_value=None), \
            patch("api.core.aa859_main_pool_gauge.stop_main_pool_gauge", AsyncMock()), \
            patch.dict("os.environ", {"DATABASE_URL": "postgresql://x:y@localhost/z", "ENVIRONMENT": "test"}):
        app = SimpleNamespace(state=SimpleNamespace())
        async with main.lifespan(app):
            kw = app.state.redis.connection_pool.connection_kwargs
            assert kw["socket_timeout"] == 0.5 and kw["socket_connect_timeout"] == 0.5


@pytest_asyncio.fixture
async def silent_redis():
    """A TCP server that accepts and never replies (a hung / blackholed Redis)."""
    done = asyncio.Event()
    writers = []

    async def handle(reader, writer):
        writers.append(writer)
        await done.wait()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield f"redis://127.0.0.1:{port}"
    done.set()
    server.close()
    await server.wait_closed()
    await asyncio.sleep(0)


def _jwt_payload():
    return {"sub": "9fb0a3db-59aa-468a-a082-ded01ac50bee", "plan_tier": "starter", "role": "tenant"}


@pytest.mark.asyncio
async def test_rate_limit_middleware_fails_open_quickly_on_a_hung_redis(silent_redis):
    from api.middleware import rate_limit

    redis = redis_client.build_api_redis(silent_redis, socket_connect_timeout=0.1, socket_timeout=0.1)
    request = MagicMock()
    request.url.path = "/v1/tours/pool"
    request.headers = {"Authorization": "Bearer fake.jwt.token"}
    request.app.state.redis = redis
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"rate_limit_rpm": 60, "is_active": True})
    request.app.state.pool.acquire.return_value.__aenter__.return_value = conn
    call_next = AsyncMock(return_value=MagicMock(status_code=200))

    started = time.monotonic()
    with patch.object(rate_limit, "verify_jwt", return_value=_jwt_payload()), \
            patch.object(rate_limit, "track_api_call", AsyncMock()):
        resp = await asyncio.wait_for(rate_limit.rate_limit_middleware(request, call_next), timeout=10)
    elapsed = time.monotonic() - started
    await redis.aclose()

    assert resp.status_code == 200
    call_next.assert_awaited_once()  # failed open: the route ran
    # 3 Redis commands (GET meta, SET meta, INCR) x (1 try + 1 retry) x 0.1 s, plus connect overhead.
    assert elapsed < 3, f"middleware held the request {elapsed:.2f}s on a hung Redis"


@pytest.mark.asyncio
async def test_flag_read_on_a_hung_redis_is_tenant_pool_and_bounded(silent_redis):
    redis = redis_client.build_api_redis(silent_redis, socket_connect_timeout=0.1, socket_timeout=0.1)
    request = MagicMock()
    request.app.state.redis = redis
    started = time.monotonic()
    assert await tp._flag_enabled(request, tp.UNPUBLISH_FLAG) is True
    assert time.monotonic() - started < 2
    await redis.aclose()


@pytest.mark.asyncio
async def test_flag_cache_lives_on_a_real_starlette_request_state():
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [],
                       "app": SimpleNamespace(state=SimpleNamespace())})
    request.app.state.redis = MagicMock()
    request.app.state.redis.get = AsyncMock(return_value="false")
    assert await tp._flag_enabled(request, tp.UNPUBLISH_FLAG) is False
    assert await tp._flag_enabled(request, tp.UNPUBLISH_FLAG) is False
    request.app.state.redis.get.assert_awaited_once()
    assert request.state._tenant_flag_cache == {tp.UNPUBLISH_FLAG: False}
