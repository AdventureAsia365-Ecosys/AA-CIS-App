"""AA-859 W2 — PATCH /v1/content-writing/pieces/{id} never nests tenant-pool acquires (reentrancy stub entry).

Kept out of test_aa859_tenant_pool_reentrancy.py's HANDLERS list so the parallel AA-859 route PRs do not
conflict on it; the stub pool and fakes are imported from there.
"""
import pytest

from services.acp_content_writing import service
from tests.unit.test_aa859_tenant_pool_reentrancy import (
    PIECE_ID, TENANT_ID, _ReentrancyGuardPool, _conn, _piece_row, _request,
)


async def _edit_piece(request):
    return await service.update_piece_content_text(TENANT_ID, PIECE_ID, "Edited", request.app.state.pool,
                                                   request=request)


@pytest.mark.asyncio
async def test_piece_edit_never_nests_tenant_pool_acquires():
    pool = _ReentrancyGuardPool(_conn(_piece_row()))
    request = _request(pool)
    await _edit_piece(request)
    assert pool.violations == [], "nested tenant-pool acquire (self-deadlock at max_size 1)"
    assert pool.acquires == 1
    assert pool.holders == set(), "every tenant connection was released"
    request.app.state.pool.execute.assert_awaited_once()  # the audit row, on MAIN after the release
