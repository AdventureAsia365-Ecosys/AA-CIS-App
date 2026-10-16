"""AA-859 — no handler on the tenant pool acquires a second tenant connection while it still holds one.

The tenant pool has 1-2 connections per api task (aa_app_user CONNECTION LIMIT 15 shared by every task). A
handler that nests a second acquire_scoped_conn (or a helper that does) inside the first waits for a
connection only it can release: with max_size 1 that is a self-deadlock (with 2, two such requests at once)
until the 5 s acquire timeout, then a 503. The stub pool below raises on a nested acquire from the same
task; sequential blocks (disconnect's two short transactions) are fine.

Every handler moved to the tenant pool gets an entry in HANDLERS (later AA-859 PRs add theirs).
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from api.core import aa544_tenant_pool as tp
from api.routers import v1_integrations as integ_mod
from api.routers import v1_publish as pub_mod
from services.acp_content_writing import service

TENANT_ID = str(uuid4())
PIECE_ID = uuid4()
REQUEST_ID = uuid4()


class _ReentrancyGuardPool:
    """pool.acquire() that records (and raises on) a second acquire by a task that already holds one."""

    def __init__(self, conn):
        self.conn = conn
        self.holders = set()
        self.acquires = 0
        self.violations = []

    def acquire(self, timeout=None):
        @asynccontextmanager
        async def _acquire():
            task = asyncio.current_task()
            if task in self.holders:
                self.violations.append(task.get_name())
                raise AssertionError("nested tenant-pool acquire while this task holds a tenant connection")
            self.holders.add(task)
            self.acquires += 1
            try:
                yield self.conn
            finally:
                self.holders.discard(task)
        return _acquire()


def _conn(row):
    conn = MagicMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    conn.fetchval = AsyncMock(side_effect=lambda sql, value: value)  # set_config echoes the tenant id
    conn.fetchrow = AsyncMock(return_value=row)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value="SELECT 1")
    return conn


def _request(tenant_pool):
    main_ctx = MagicMock()
    main_ctx.__aenter__ = AsyncMock(side_effect=AssertionError("flag on: the admin pool must not be used"))
    main_ctx.__aexit__ = AsyncMock(return_value=False)
    request = MagicMock()
    request.app.state.redis.get = AsyncMock(return_value="true")
    request.app.state.tenant_pool = tenant_pool
    request.app.state.pool.acquire = MagicMock(return_value=main_ctx)
    request.app.state.pool.execute = AsyncMock(return_value="INSERT 0 1")  # write_audit_log(pool, ...)
    return request


def _piece_row():
    return {
        "piece_id": PIECE_ID, "tenant_id": TENANT_ID, "angle_gate_request_id": REQUEST_ID,
        "attempt_number": 1, "content_text": "Body", "status": "approved", "held_reason": None,
        "gate_ledger": [], "repair_log": [], "created_at": datetime(2026, 10, 16, tzinfo=timezone.utc),
        "seo_title": None, "meta_description": None, "slug": None, "channel": "blog",
        "route_hub_name": None, "route_segment_count": None, "flags": [],
    }


async def _fetch_piece(request):
    return await service.fetch_piece(TENANT_ID, PIECE_ID, request.app.state.pool, request=request)


async def _latest_piece(request):
    return await service.fetch_latest_piece_for_request(TENANT_ID, REQUEST_ID, request.app.state.pool,
                                                        request=request)


async def _pending(request):
    return await pub_mod.list_pending(request, {"sub": TENANT_ID})


async def _publish_log(request):
    return await pub_mod.list_publish_log(request, tenant={"sub": TENANT_ID}, limit=50)


async def _disconnect(request):
    with patch.object(integ_mod, "_sm_client", return_value=MagicMock()):
        return await integ_mod.disconnect_wordpress(request, tenant={"sub": TENANT_ID})


_INTEG_ROW = {"secret_key": "acp/cms/x", "updated_at": datetime(2026, 10, 16, tzinfo=timezone.utc)}

# (handler, row its queries return, tenant-pool acquires one request makes)
HANDLERS = [
    pytest.param(_fetch_piece, _piece_row(), 1, id="GET /v1/content-writing/pieces/{id}"),
    pytest.param(_latest_piece, None, 1, id="GET /v1/content-writing/requests/{id}/latest-piece"),
    pytest.param(_pending, None, 1, id="GET /v1/publish-log/pending"),
    pytest.param(_publish_log, None, 1, id="GET /v1/publish-log"),
    pytest.param(_disconnect, _INTEG_ROW, 2, id="DELETE /v1/integrations/wordpress"),
]


@pytest.mark.parametrize("handler,row,expected_acquires", HANDLERS)
@pytest.mark.asyncio
async def test_handler_never_nests_tenant_pool_acquires(handler, row, expected_acquires):
    pool = _ReentrancyGuardPool(_conn(row))
    await handler(_request(pool))
    assert pool.violations == [], "nested tenant-pool acquire (self-deadlock at max_size 1)"
    assert pool.acquires == expected_acquires
    assert pool.holders == set(), "every tenant connection was released"


@pytest.mark.asyncio
async def test_guard_pool_catches_a_nested_acquire():
    """Mutation check: the stub really fails a handler that nests acquire_scoped_conn."""
    pool = _ReentrancyGuardPool(_conn(None))
    request = _request(pool)

    async def nested(req):
        async with tp.acquire_scoped_conn(req, TENANT_ID, tp.PUBLISH_LOG_LIST_FLAG, fail_closed=True):
            async with tp.acquire_scoped_conn(req, TENANT_ID, tp.PUBLISH_LOG_LIST_FLAG, fail_closed=True):
                pass

    with pytest.raises(AssertionError):
        await nested(request)
    assert len(pool.violations) == 1
