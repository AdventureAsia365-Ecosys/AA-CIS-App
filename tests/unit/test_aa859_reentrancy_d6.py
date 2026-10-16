"""AA-859 PR-D6 — reentrancy stub entries for PATCH /v1/tours/versions/{id} (see
tests/unit/test_aa859_tenant_pool_reentrancy.py for the guard pool and why nesting is a self-deadlock).

Kept in its own file so the AA-859 sibling PRs do not conflict on HANDLERS. One tenant-pool acquire per
request for every action (edit: own-row SELECT, lock, MAX + 1, INSERT in the same transaction).
"""
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from api.routers import v1_tours
from tests.unit.test_aa859_tenant_pool_reentrancy import TENANT_ID, _conn, _ReentrancyGuardPool, _request

VERSION_ID = str(uuid4())
_ROW = {"id": VERSION_ID, "published_tour_id": uuid4(), "version_number": 1, "rewritten_content": "{}"}


def _version_conn():
    conn = _conn(_ROW)

    async def fetchval(sql, *args):
        if "set_config('app.tenant_id'" in sql:
            return args[0]
        return 2 if sql is v1_tours._VERSION_NEXT_NUMBER_SQL else uuid4()

    conn.fetchval = AsyncMock(side_effect=fetchval)
    return conn


@pytest.mark.parametrize("action,content", [("edit", {"name": "x"}), ("approve", None), ("reject", None)])
@pytest.mark.asyncio
async def test_version_patch_never_nests_tenant_pool_acquires(action, content):
    pool = _ReentrancyGuardPool(_version_conn())
    body = v1_tours.VersionActionRequest(action=action, edited_content=content)
    await v1_tours.update_version(VERSION_ID, body, _request(pool), tenant={"sub": TENANT_ID})
    assert pool.violations == [], "nested tenant-pool acquire (self-deadlock at max_size 1)"
    assert pool.acquires == 1
    assert pool.holders == set(), "every tenant connection was released"
