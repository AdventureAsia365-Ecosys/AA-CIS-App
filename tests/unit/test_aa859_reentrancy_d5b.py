"""AA-859 D5b — reentrancy entries for the T2 detail reads (see test_aa859_tenant_pool_reentrancy.py).

One tenant-pool block per request, never nested, every connection released. export-docx writes its audit row on
MAIN (plan D2), which is allowed only once the request holds no tenant connection.
"""
import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.routers import v1_tours
from tests.unit.test_aa859_tenant_pool_reentrancy import TENANT_ID, _ReentrancyGuardPool

RAW_TOUR_ID = uuid.uuid4()
_ROW = {"id": uuid.uuid4(), "tour_id": RAW_TOUR_ID, "aa_name": "Ha Long", "aa_subtitle": "Bay", "aa_summary": "S",
        "aa_highlights": "[]", "aa_itineraries": "Day 1", "aa_description": "D", "seo_title": "T", "seo_meta": "M",
        "version_number": 1, "rewritten_content": {}, "rewrite_language": "en-US", "country": "Vietnam",
        "duration": "3 days", "inclusions": None, "exclusions": None}


def _conn(row):
    conn = MagicMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    conn.fetchval = AsyncMock(side_effect=lambda sql, *a: a[0] if "set_config" in sql else 1)
    conn.fetchrow = AsyncMock(return_value=row)
    return conn


class _MainPool:
    """Admin pool whose audit write fails if the same task still holds a tenant connection."""

    def __init__(self, tenant_pool):
        self.tenant_pool = tenant_pool
        self.writes = 0

    def acquire(self):
        raise AssertionError("flag on: no admin-pool connection for these reads")

    async def execute(self, sql, *args):
        assert asyncio.current_task() not in self.tenant_pool.holders, "MAIN written inside the tenant block"
        self.writes += 1
        return "INSERT 0 1"


def _request(tenant_pool):
    request = MagicMock()
    request.app.state.redis.get = AsyncMock(return_value="true")
    request.app.state.tenant_pool = tenant_pool
    request.app.state.pool = _MainPool(tenant_pool)
    return request


async def _full(request, monkeypatch):
    monkeypatch.setattr(v1_tours, "verify_tenant_jwt", lambda token: {"sub": TENANT_ID})
    return await v1_tours.get_tour_full(str(uuid.uuid4()), request, x_admin_secret=None,
                                        credentials=SimpleNamespace(credentials="t"))


async def _tour(request, monkeypatch):
    return await v1_tours.get_tour(str(uuid.uuid4()), request, {"sub": TENANT_ID})


async def _export(request, monkeypatch):
    return await v1_tours.export_version_docx(str(uuid.uuid4()), request, {"sub": TENANT_ID})


# (handler, tenant-pool acquires, MAIN audit writes)
HANDLERS = [
    pytest.param(_full, 1, 0, id="GET /v1/tours/{id}/full (tenant)"),
    pytest.param(_tour, 1, 0, id="GET /v1/tours/{id}"),
    pytest.param(_export, 1, 1, id="GET /v1/tours/versions/{id}/export-docx"),
]


@pytest.mark.parametrize("handler,tenant_acquires,main_writes", HANDLERS)
@pytest.mark.asyncio
async def test_handler_never_nests_tenant_pool_acquires(handler, tenant_acquires, main_writes, monkeypatch):
    pool = _ReentrancyGuardPool(_conn(dict(_ROW)))
    request = _request(pool)
    await handler(request, monkeypatch)
    assert pool.violations == [], "nested tenant-pool acquire (self-deadlock at max_size 1)"
    assert pool.acquires == tenant_acquires
    assert pool.holders == set(), "every tenant connection was released"
    assert request.app.state.pool.writes == main_writes
