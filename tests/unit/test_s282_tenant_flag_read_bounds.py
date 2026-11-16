"""S282 Sentry triage (AA-CIS-API-1B) — the tenant-pool flag read no longer cuts the Redis client's own retry short.

The first tenant request on a fresh task opened a new TLS connection during a burst; the outer 0.5 s bound on the
flag GET equalled one socket timeout, so it fired as the client's designed retry began, and logged an ERROR (a Sentry
event and an alarm e-mail after most deploys). Now: the client is warmed with one PING at start-up, the outer bound
sits above the client's worst case, and a timeout (whose fail-closed outcome is right) logs a warning under the same
event name.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from api.core import aa544_tenant_pool as tp
from api.core import redis_client as rcl


def test_outer_bound_is_above_the_clients_worst_case():
    worst = (rcl.REDIS_RETRIES + 1) * (rcl.REDIS_SOCKET_CONNECT_TIMEOUT_S + rcl.REDIS_SOCKET_TIMEOUT_S)
    assert tp.FLAG_READ_TIMEOUT_S > worst
    assert tp.FLAG_READ_TIMEOUT_S < 5      # still far inside the 30 s API Gateway timeout


@pytest.mark.asyncio
async def test_warm_pings_once():
    redis = AsyncMock()
    assert await rcl.warm_api_redis(redis) is True
    redis.ping.assert_awaited_once()


@pytest.mark.asyncio
async def test_warm_failure_is_a_warning_and_never_raises():
    redis = AsyncMock()
    redis.ping = AsyncMock(side_effect=ConnectionError("no route"))
    with capture_logs() as logs:
        assert await rcl.warm_api_redis(redis) is False
    assert [e["log_level"] for e in logs if e["event"] == "api_redis_warm_failed"] == ["warning"]


@pytest.mark.asyncio
async def test_warm_without_a_client_does_nothing():
    assert await rcl.warm_api_redis(None) is False


def test_lifespan_warms_the_client():
    import inspect

    from api import main

    src = inspect.getsource(main.lifespan)
    assert src.index("app.state.redis = redis") < src.index("await warm_api_redis(redis)")
