"""AA-859 W3 (PR-D3) — reentrancy entries for POST /v1/angle-gate/requests/{id}/choose and /reopen.

Same guard as test_aa859_tenant_pool_reentrancy.py (its stub pool raises on a second tenant acquire by a task
that already holds one): each decision takes one tenant connection and releases it. AA-859 wave 4: with
ANGLE_GATE_READ_FLAG ON too (like every flag here) the response re-read runs on that same connection, so the
handler takes exactly one tenant connection, never a second (fetch_request_on stubbed; the job-status step a
pass-through, the shared `_request` makes any main-pool acquire fail).
"""
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from api.routers import v1_angle_gate as ag
from services.acp_angle_gate import service
from tests.unit.test_aa859_tenant_pool_reentrancy import TENANT_ID, _conn, _ReentrancyGuardPool, _request

REQUEST_ID = uuid4()


def _decide_conn(status):
    """The shared stub connection, with fetchval answering the decision SQL (set_config echoes the tenant)."""
    conn = _conn(None)

    async def fetchval(sql, *args):
        if "set_config('app.tenant_id'" in sql:
            return args[0]
        if sql == service._CHOOSE_LOCK_SQL or sql == service._REQUEST_STATUS_SQL:
            return status
        if sql == service._OPTION_EXISTS_SQL:
            return 1
        if sql == service._REOPEN_SQL:
            return args[0]
        raise AssertionError(f"unexpected fetchval: {sql}")
    conn.fetchval = AsyncMock(side_effect=fetchval)
    conn.execute = AsyncMock(return_value="UPDATE 1")
    return conn


async def _choose(request):
    return await ag.choose(REQUEST_ID, ag.ChooseBody(idx=1), request, tenant={"sub": TENANT_ID})


async def _reopen(request):
    return await ag.reopen(REQUEST_ID, request, tenant={"sub": TENANT_ID})


HANDLERS = [
    pytest.param(_choose, "pending_choice", 1, id="POST /v1/angle-gate/requests/{id}/choose"),
    pytest.param(_reopen, "approved", 1, id="POST /v1/angle-gate/requests/{id}/reopen"),
]


@pytest.mark.parametrize("handler,status,expected_acquires", HANDLERS)
@pytest.mark.asyncio
async def test_handler_never_nests_tenant_pool_acquires(handler, status, expected_acquires):
    pool = _ReentrancyGuardPool(_decide_conn(status))
    held_during_reread = []

    async def fetch_request_on(conn, tenant_id, request_id):
        held_during_reread.append(len(pool.holders))
        return {"status": "re-read", "goal": "g"}

    async def passthrough(result, main_pool):
        return result

    with patch.object(service, "fetch_request_on", new=AsyncMock(side_effect=fetch_request_on)), \
            patch.object(service, "with_generation_status", new=AsyncMock(side_effect=passthrough)):
        assert (await handler(_request(pool)))["status"] == "re-read"
    assert pool.violations == [], "nested tenant-pool acquire (self-deadlock at max_size 1)"
    assert pool.acquires == expected_acquires
    assert pool.holders == set(), "every tenant connection was released"
    assert held_during_reread == [1], "the re-read holds only its own connection (the decide one is back)"
