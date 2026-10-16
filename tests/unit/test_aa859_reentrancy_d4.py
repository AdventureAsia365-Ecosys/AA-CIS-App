"""AA-859 W4 — reentrancy entry for DELETE /v1/publish-log/{publish_id} (unpublish).

Uses the guard pool of test_aa859_tenant_pool_reentrancy.py: three sequential tenant-pool blocks, never a
nested acquire, every connection released, and no tenant connection held while Secrets Manager or the
channel's HTTP delete runs.
"""
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from api.routers import v1_publish as pub_mod
from tests.unit.test_aa859_tenant_pool_reentrancy import (
    TENANT_ID, _conn, _ReentrancyGuardPool, _request, test_handler_never_nests_tenant_pool_acquires as _check,
)

PUBLISH_ID = uuid4()
_ROW = {"publish_id": str(PUBLISH_ID), "piece_id": str(uuid4()), "channel": "blog", "external_id": "42",
        "external_url": "https://b.com/p/42/", "secret_key": "acp/cms/x", "status": "unpublished",
        "unpublished_at": None}
_CREDS = {"wp_url": "https://b.com", "username": "u", "app_password": "p"}


async def _unpublish(request, held=None):
    pool = request.app.state.tenant_pool

    def get_secret(key):
        if held is not None:
            held.append(("secret", len(pool.holders)))
        return dict(_CREDS)

    async def delete_remote(channel, creds, external_id):
        if held is not None:
            held.append(("remote", len(pool.holders)))
        return "trashed"

    with patch.object(pub_mod, "_get_secret", side_effect=get_secret), \
            patch.object(pub_mod, "_delete_remote", AsyncMock(side_effect=delete_remote)):
        return await pub_mod.unpublish(PUBLISH_ID, request, tenant={"sub": TENANT_ID})


HANDLERS = [pytest.param(_unpublish, _ROW, 3, id="DELETE /v1/publish-log/{id}")]


@pytest.mark.parametrize("handler,row,expected_acquires", HANDLERS)
@pytest.mark.asyncio
async def test_handler_never_nests_tenant_pool_acquires(handler, row, expected_acquires):
    await _check(handler, row, expected_acquires)


@pytest.mark.asyncio
async def test_unpublish_holds_no_tenant_connection_across_secrets_manager_or_http():
    pool = _ReentrancyGuardPool(_conn(_ROW))
    held = []
    await _unpublish(_request(pool), held=held)
    assert held == [("secret", 0), ("remote", 0)]
