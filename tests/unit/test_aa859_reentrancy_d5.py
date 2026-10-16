"""AA-859 PR-D5a — reentrancy stub entries for the W5 T2 version reads (see test_aa859_tenant_pool_reentrancy.py).

These handlers also read shared.job on MAIN (plan D2), so the admin pool here is allowed, but only once the
request holds no tenant connection: a MAIN acquire inside the tenant block would hold the tenant connection
across it.
"""
import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.routers import v1_tours
from tests.unit.test_aa859_tenant_pool_reentrancy import TENANT_ID, _ReentrancyGuardPool

JOB_ID = uuid.uuid4()
NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)


def _version_row():
    return {"id": uuid.uuid4(), "tenant_id": uuid.UUID(TENANT_ID), "published_tour_id": uuid.uuid4(),
            "version_number": 1, "status": "ai_generated", "edit_source": "ai_generated", "created_at": NOW,
            "job_id": JOB_ID, "_job_id": JOB_ID, "job_status": None}


def _conn(row):
    conn = MagicMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    conn.fetchval = AsyncMock(side_effect=lambda sql, *a: a[0] if "set_config" in sql else 1)
    conn.fetchrow = AsyncMock(return_value=row)
    conn.fetch = AsyncMock(return_value=[row] if row else [])
    return conn


class _MainPool:
    """Admin pool that fails if the same task still holds a tenant connection."""

    def __init__(self, tenant_pool):
        self.tenant_pool = tenant_pool
        self.acquires = 0
        self.conn = MagicMock()
        self.conn.fetch = AsyncMock(return_value=[{"id": JOB_ID, "status": "succeeded"}])

    def acquire(self):
        @asynccontextmanager
        async def _acquire():
            assert asyncio.current_task() not in self.tenant_pool.holders, "MAIN acquired inside the tenant block"
            self.acquires += 1
            yield self.conn
        return _acquire()


def _request(tenant_pool):
    request = MagicMock()
    request.app.state.redis.get = AsyncMock(return_value="true")
    request.app.state.tenant_pool = tenant_pool
    request.app.state.pool = _MainPool(tenant_pool)
    return request


async def _pool(request):
    return await v1_tours.browse_pool(request, {"sub": TENANT_ID}, page=1, page_size=20, country=None,
                                      search=None, duration_min=None, duration_max=None, sort="newest")


async def _my_versions(request):
    return await v1_tours.list_my_versions(request, {"sub": TENANT_ID}, page=1, page_size=20, status=None,
                                           country=None)


async def _version(request):
    return await v1_tours.get_version(str(uuid.uuid4()), request, {"sub": TENANT_ID})


# (handler, row its queries return, tenant-pool acquires, MAIN acquires (job status))
HANDLERS = [
    pytest.param(_pool, None, 1, 0, id="GET /v1/tours/pool"),
    pytest.param(_my_versions, _version_row(), 1, 1, id="GET /v1/tours/my-versions"),
    pytest.param(_version, _version_row(), 1, 1, id="GET /v1/tours/versions/{id}"),
]


@pytest.mark.parametrize("handler,row,tenant_acquires,main_acquires", HANDLERS)
@pytest.mark.asyncio
async def test_handler_never_nests_tenant_pool_acquires(handler, row, tenant_acquires, main_acquires):
    pool = _ReentrancyGuardPool(_conn(row))
    request = _request(pool)
    await handler(request)
    assert pool.violations == [], "nested tenant-pool acquire (self-deadlock at max_size 1)"
    assert pool.acquires == tenant_acquires
    assert pool.holders == set(), "every tenant connection was released"
    assert request.app.state.pool.acquires == main_acquires
