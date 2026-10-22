"""AA-859 wave 4 — reentrancy entries for the moved handlers (test_aa859_tenant_pool_reentrancy.py's guard: its stub
pool raises on a second tenant acquire by a task that already holds one). Each handler takes one tenant connection per
block and never nests a second; the trip-reallocation confirm with accept=true takes two, one after the other (reads,
then save + approve). The choose / reopen re-read is in test_aa859_reentrancy_d3.py.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.unit.test_aa859_tenant_pool_reentrancy import _ReentrancyGuardPool
from tests.unit.test_aa859_wave4_pools import (
    _angle_gate_get, _confirm, _confirm_reject, _Conn, _cut, _marketplace, _pick, _Pool, _review,
    _reviews, _slate_get, _suggest,
)


def _request():
    events = []
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()), state=SimpleNamespace())
    request.app.state.pool = _Pool("main", events)
    guard = _ReentrancyGuardPool(_Conn("tenant", events))
    request.app.state.tenant_pool = guard
    request.app.state.redis = SimpleNamespace(get=AsyncMock(return_value="true"))   # every flag ON
    return request, events, guard


async def _confirm_accept(request, events):
    return await _confirm(request, events, accept=True)


HANDLERS = [
    pytest.param(_angle_gate_get, 1, id="GET /v1/angle-gate/requests/{id}"),
    pytest.param(_slate_get, 1, id="GET /v1/slate"),
    pytest.param(_pick, 1, id="POST /v1/subjects/{id}/pick"),
    pytest.param(_cut, 1, id="POST /v1/subjects/{id}/cut"),
    pytest.param(_suggest, 1, id="GET /v1/planning/trip-reallocation/suggest"),
    pytest.param(_confirm_reject, 1, id="POST /v1/planning/trip-reallocation/confirm accept=false"),
    pytest.param(_confirm_accept, 2, id="POST /v1/planning/trip-reallocation/confirm accept=true"),
    pytest.param(_marketplace, 1, id="GET /v1/marketplace"),
    pytest.param(_reviews, 1, id="GET /v1/content-writing/reviews"),
    pytest.param(_review, 1, id="GET /v1/content-writing/requests/{id}/review"),
]


@pytest.mark.parametrize("handler,expected_acquires", HANDLERS)
@pytest.mark.asyncio
async def test_handler_never_nests_tenant_pool_acquires(handler, expected_acquires):
    request, events, guard = _request()
    await handler(request, events)
    assert guard.violations == [], "nested tenant-pool acquire (self-deadlock at max_size 1)"
    assert guard.acquires == expected_acquires
    assert guard.holders == set(), "every tenant connection was released"
