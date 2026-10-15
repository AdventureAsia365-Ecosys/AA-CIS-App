"""AA-817 — tenant content-piece reads on the aa_app_user pool: acquire_scoped_conn(fail_closed=True)
and service.fetch_latest_piece_for_request(request=...).

Flag off: the admin pool, exactly as before. Flag on: the tenant pool inside one transaction with the
GUC set LOCAL; no tenant pool, no tenant id, or a GUC that did not take is a 503, never a fallback to
the admin pool (which RLS does not filter). Callers without fail_closed keep the old fallback.
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.core import aa544_tenant_pool as tp
from services.acp_content_writing import service

TENANT_ID = uuid4()
REQUEST_ID = uuid4()
OPTION_ID = uuid4()


def _ctx(value):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=value)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _pool(conn):
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_ctx(conn))
    return pool


def _conn(guc_echo=None):
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_ctx(None))
    conn.fetchval = AsyncMock(side_effect=lambda sql, value: value if guc_echo is None else guc_echo)
    return conn


def _request(flag, tenant_conn=None, main_conn=None, tenant_pool=True):
    request = MagicMock()
    request.app.state.redis.get = AsyncMock(return_value="true" if flag else None)
    request.app.state.pool = _pool(main_conn or _conn())
    request.app.state.tenant_pool = _pool(tenant_conn or _conn()) if tenant_pool else None
    return request


async def _enter(request, tenant_id, **kw):
    async with tp.acquire_scoped_conn(request, tenant_id, tp.CONTENT_PIECE_READS_FLAG, **kw) as (conn, used):
        return conn, used


@pytest.mark.asyncio
async def test_flag_off_uses_admin_pool():
    main = _conn()
    request = _request(flag=False, main_conn=main)
    conn, used = await _enter(request, str(TENANT_ID), fail_closed=True)
    assert conn is main and used is False
    request.app.state.tenant_pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_flag_on_uses_tenant_pool_with_local_guc():
    tenant = _conn()
    request = _request(flag=True, tenant_conn=tenant)
    conn, used = await _enter(request, str(TENANT_ID), fail_closed=True)
    assert conn is tenant and used is True
    tenant.transaction.assert_called_once()
    sql, value = tenant.fetchval.call_args[0]
    assert "set_config('app.tenant_id', $1, true)" in sql and value == str(TENANT_ID)
    request.app.state.pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_flag_on_without_tenant_pool_is_503_not_admin_pool():
    request = _request(flag=True, tenant_pool=False)
    with pytest.raises(HTTPException) as exc:
        await _enter(request, str(TENANT_ID), fail_closed=True)
    assert exc.value.status_code == 503
    request.app.state.pool.acquire.assert_not_called()


@pytest.mark.parametrize("tenant_id", ["", None])
@pytest.mark.asyncio
async def test_flag_on_without_tenant_id_is_503(tenant_id):
    request = _request(flag=True)
    with pytest.raises(HTTPException) as exc:
        await _enter(request, tenant_id, fail_closed=True)
    assert exc.value.status_code == 503
    request.app.state.tenant_pool.acquire.assert_not_called()
    request.app.state.pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_guc_not_applied_is_503():
    request = _request(flag=True, tenant_conn=_conn(guc_echo=""))
    with pytest.raises(HTTPException) as exc:
        await _enter(request, str(TENANT_ID), fail_closed=True)
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_redis_error_counts_as_flag_off():
    request = _request(flag=True)
    request.app.state.redis.get = AsyncMock(side_effect=ConnectionError("redis down"))
    conn, used = await _enter(request, str(TENANT_ID), fail_closed=True)
    assert used is False


@pytest.mark.asyncio
async def test_existing_callers_keep_admin_pool_fallback():
    """Without fail_closed (AA-544 stage 1/5, AA-815 routes) a missing tenant pool still falls back."""
    main = _conn()
    request = _request(flag=True, main_conn=main, tenant_pool=False)
    conn, used = await _enter(request, str(TENANT_ID))
    assert conn is main and used is False


def _piece_row():
    return {
        "piece_id": uuid4(), "tenant_id": TENANT_ID, "angle_gate_request_id": REQUEST_ID,
        "attempt_number": 1, "content_text": "Body", "status": "approved", "held_reason": None,
        "gate_ledger": [], "repair_log": [], "created_at": datetime(2026, 10, 15, tzinfo=timezone.utc),
        "seo_title": None,
        "meta_description": None, "slug": None, "route_hub_name": None, "route_segment_count": None,
        "flags": [],
    }


@pytest.mark.asyncio
async def test_latest_piece_runs_on_tenant_pool_when_flag_on():
    tenant = _conn()
    tenant.fetchrow = AsyncMock(side_effect=[{"option_id": OPTION_ID}, _piece_row()])
    request = _request(flag=True, tenant_conn=tenant)
    result = await service.fetch_latest_piece_for_request(TENANT_ID, REQUEST_ID, request.app.state.pool,
                                                          request=request)
    assert result["ready_state"] == "ready"
    assert [c[0][1:] for c in tenant.fetchrow.call_args_list] == [
        (REQUEST_ID, TENANT_ID), (REQUEST_ID, TENANT_ID, OPTION_ID)]
    request.app.state.pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_latest_piece_same_shape_on_both_pools():
    rows = [{"option_id": OPTION_ID}, _piece_row()]
    tenant, main = _conn(), _conn()
    tenant.fetchrow = AsyncMock(side_effect=list(rows))
    main.fetchrow = AsyncMock(side_effect=list(rows))
    on = await service.fetch_latest_piece_for_request(
        TENANT_ID, REQUEST_ID, None, request=_request(flag=True, tenant_conn=tenant))
    off = await service.fetch_latest_piece_for_request(
        TENANT_ID, REQUEST_ID, None, request=_request(flag=False, main_conn=main))
    assert on == off


@pytest.mark.asyncio
async def test_latest_piece_flag_on_without_tenant_pool_is_503():
    request = _request(flag=True, tenant_pool=False)
    with pytest.raises(HTTPException) as exc:
        await service.fetch_latest_piece_for_request(TENANT_ID, REQUEST_ID, request.app.state.pool,
                                                     request=request)
    assert exc.value.status_code == 503
