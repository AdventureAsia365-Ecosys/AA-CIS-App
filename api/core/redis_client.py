"""The api's shared Redis client (app.state.redis), with client-level time bounds (S241).

Every Redis call on the api's request path -- the rate-limit middleware (tenant meta cache + RPM counter),
the tenant-pool flag read (api/core/aa544_tenant_pool.py) and the progress reads -- already fails open on
a Redis error. Without socket timeouts, a Redis that accepts no packets (security group change, node
replacement, blackholed IP) never raises: each await waits until the API GW 30 s timeout, before the
route even runs. These bounds turn that into a fast error, which each caller already handles.

Only short single-key commands run on this client (GET / SET / INCR / EXPIRE / INFO); nothing blocking
(no BLPOP, pub/sub or long scans), so 0.5 s per socket read is ample within the VPC. One retry with no
backoff keeps a stale pooled connection (after a Redis restart) from failing the first command, while a
dead Redis costs at most two timeouts per command. redis-py 6+ otherwise retries 3 times with backoff by
default; passing `retry` explicitly keeps the bound the same on every version requirements.txt allows.

No health_check_interval, on purpose: measured against a Redis that accepts and never answers, redis-py
5.2.1 with health_check_interval + retry HANGS (no timeout fires at all), and 8.1 doubles the wait (the
health-check PING times out first). The single retry already covers a stale pooled connection.
(tests/unit/test_s241_redis_client_bounds.py runs that scenario.)
"""
from __future__ import annotations

import redis.asyncio as aioredis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

REDIS_SOCKET_CONNECT_TIMEOUT_S = 0.5
REDIS_SOCKET_TIMEOUT_S = 0.5
REDIS_RETRIES = 1


def build_api_redis(url: str, *, socket_connect_timeout: float = REDIS_SOCKET_CONNECT_TIMEOUT_S,
                    socket_timeout: float = REDIS_SOCKET_TIMEOUT_S) -> aioredis.Redis:
    """Lazy: no I/O here. The keyword overrides exist for tests."""
    return aioredis.from_url(
        url, encoding="utf-8", decode_responses=True,
        socket_connect_timeout=socket_connect_timeout, socket_timeout=socket_timeout,
        retry=Retry(NoBackoff(), REDIS_RETRIES), retry_on_timeout=True,
    )
