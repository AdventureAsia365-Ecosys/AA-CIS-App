"""AA-859 W2 — PATCH /v1/content-writing/pieces/{id} on the aa_app_user tenant pool behind PIECE_EDIT_FLAG.

The same fail-closed matrix as tests/unit/test_aa817_fail_closed_routes.py (helpers imported from it, kept
in a separate file so the sibling AA-859 route PRs do not conflict on its ROUTES list), plus what is
specific to this route: flag off is the pre-AA-859 shape exactly (admin pool, audit on the same
connection, an audit failure propagates), flag on writes the audit row on MAIN after the tenant
transaction committed and a failing audit does not fail the edit.
"""
import asyncio
from unittest.mock import AsyncMock

import asyncpg
import pytest
from fastapi import HTTPException
from structlog.testing import capture_logs

from api.core import aa544_tenant_pool as tp
from api.routers import v1_content_writing as router_mod
from services.acp_content_writing import service
from tests.unit.test_aa817_fail_closed_routes import (
    PIECE_ID, TENANT_ID, _conn, _flag_only, _piece_row, _request,
)

FLAG = tp.PIECE_EDIT_FLAG
EVENT = "aa859_piece_edit"
TEXT = "Edited by tenant"


async def _call(request, tenant_id):
    return await service.update_piece_content_text(tenant_id, PIECE_ID, TEXT, request.app.state.pool,
                                                   request=request)


def _audit_calls(request):
    return [c for c in request.app.state.pool.execute.call_args_list if "acp_shared.audit_log" in c[0][0]]


# ── fail-closed matrix (same cases as test_aa817_fail_closed_routes) ─────────────

@pytest.mark.asyncio
async def test_flag_on_runs_on_tenant_pool_and_logs_it():
    tenant = _conn(fetchrow=_piece_row())
    request = _request(flag=True, tenant_conn=tenant)
    request.app.state.redis.get = _flag_only(FLAG)
    with capture_logs() as logs:
        result = await _call(request, TENANT_ID)
    assert result["ready_state"] == "ready" and "status" not in result, "tenant-safe shape"
    request.app.state.pool.acquire.assert_not_called()
    assert request.app.state.tenant_pool.acquire.call_args.kwargs == {"timeout": tp.TENANT_POOL_ACQUIRE_TIMEOUT_S}
    sql, value = tenant.fetchval.call_args[0]
    assert "set_config('app.tenant_id', $1, true)" in sql and value == str(TENANT_ID)
    update_sql, *params = tenant.fetchrow.call_args[0]
    assert update_sql is service._PIECE_EDIT_SQL and params == [PIECE_ID, TENANT_ID, TEXT]
    tenant.execute.assert_not_awaited()  # no audit on the tenant connection (no grant on audit_log)
    lines = [entry for entry in logs if entry["event"] == EVENT]
    assert len(lines) == 1 and lines[0]["used_tenant_pool"] is True and lines[0]["updated"] is True


@pytest.mark.asyncio
async def test_flag_off_is_the_old_shape_on_the_admin_pool():
    main = _conn(fetchrow=_piece_row(), execute="INSERT 0 1")
    request = _request(flag=False, main_conn=main)
    with capture_logs() as logs:
        await _call(request, TENANT_ID)
    request.app.state.tenant_pool.acquire.assert_not_called()
    main.transaction.assert_not_called()  # autocommit UPDATE, as before
    audit_sql, *params = main.execute.call_args[0]
    assert "INSERT INTO acp_shared.audit_log" in audit_sql, "audit on the same admin connection, as before"
    assert params[2] == "content_piece.edited" and params[4] == str(PIECE_ID)
    assert not _audit_calls(request), "no separate MAIN audit when the flag is off"
    lines = [entry for entry in logs if entry["event"] == EVENT]
    assert len(lines) == 1 and lines[0]["used_tenant_pool"] is False


@pytest.mark.asyncio
async def test_flag_off_audit_failure_still_propagates():
    """Flag off = exact old behaviour: write_audit_log is not best-effort there."""
    main = _conn(fetchrow=_piece_row())
    main.execute = AsyncMock(side_effect=asyncpg.PostgresError("audit insert failed"))
    request = _request(flag=False, main_conn=main)
    with pytest.raises(asyncpg.PostgresError):
        await _call(request, TENANT_ID)


@pytest.mark.asyncio
async def test_flag_on_without_tenant_pool_is_503():
    request = _request(flag=True, tenant_pool=False)
    with pytest.raises(HTTPException) as exc:
        await _call(request, TENANT_ID)
    assert exc.value.status_code == 503
    request.app.state.pool.acquire.assert_not_called()
    assert not _audit_calls(request)


@pytest.mark.asyncio
async def test_flag_on_without_tenant_id_is_503():
    request = _request(flag=True)
    with pytest.raises(HTTPException) as exc:
        await _call(request, None)
    assert exc.value.status_code == 503
    request.app.state.tenant_pool.acquire.assert_not_called()
    request.app.state.pool.acquire.assert_not_called()


@pytest.mark.parametrize("bad_id", ["not-a-uuid", "' OR true --", "urn:uuid:12345678-1234-5678-1234-567812345678"])
@pytest.mark.asyncio
async def test_flag_on_malformed_tenant_id_is_503_before_any_acquire(bad_id):
    request = _request(flag=True)
    with pytest.raises(HTTPException) as exc:
        await _call(request, bad_id)
    assert exc.value.status_code == 503
    request.app.state.tenant_pool.acquire.assert_not_called()
    request.app.state.pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_flag_on_guc_not_applied_is_503():
    tenant = _conn(guc_echo="", fetchrow=_piece_row())
    request = _request(flag=True, tenant_conn=tenant)
    with pytest.raises(HTTPException) as exc:
        await _call(request, TENANT_ID)
    assert exc.value.status_code == 503
    tenant.fetchrow.assert_not_awaited()
    request.app.state.pool.acquire.assert_not_called()
    assert not _audit_calls(request)


@pytest.mark.parametrize("error,reason", [
    (asyncpg.TooManyConnectionsError('too many connections for role "aa_app_user"'), "too_many_connections"),
    (asyncio.TimeoutError(), "acquire_timeout"),
    (ConnectionRefusedError(111, "refused"), "connect_error"),
    (asyncpg.InterfaceError("pool is closed"), "pool_closed"),
])
@pytest.mark.asyncio
async def test_flag_on_no_tenant_connection_is_503_with_metric_event(error, reason):
    request = _request(flag=True, acquire_error=error)
    with capture_logs() as logs, pytest.raises(HTTPException) as exc:
        await _call(request, TENANT_ID)
    assert exc.value.status_code == 503
    request.app.state.pool.acquire.assert_not_called()
    assert not _audit_calls(request)
    events = [entry for entry in logs if entry["event"] == "aa817_tenant_pool_unavailable"]
    assert len(events) == 1 and events[0]["reason"] == reason and events[0]["flag_key"] == FLAG


@pytest.mark.asyncio
async def test_redis_error_keeps_tenant_pool_loudly():
    """S241 D-12: an unreadable flag is ON -- the tenant pool, never the admin pool."""
    tenant = _conn(fetchrow=_piece_row())
    request = _request(flag=True, tenant_conn=tenant)
    request.app.state.redis.get = AsyncMock(side_effect=ConnectionError("redis down"))
    with capture_logs() as logs:
        await _call(request, TENANT_ID)
    request.app.state.pool.acquire.assert_not_called()
    failed = [entry for entry in logs if entry["event"] == "aa817_tenant_flag_read_failed"]
    assert failed and failed[0]["flag_key"] == FLAG and failed[0]["fallback"] == "tenant_pool"
    lines = [entry for entry in logs if entry["event"] == EVENT]
    assert lines and lines[0]["used_tenant_pool"] is True


@pytest.mark.asyncio
async def test_query_error_is_not_reported_as_pool_unavailable_and_writes_no_audit():
    tenant = _conn(fetchrow=_piece_row())
    tenant.fetchrow = AsyncMock(side_effect=asyncpg.InsufficientPrivilegeError("permission denied"))
    request = _request(flag=True, tenant_conn=tenant)
    with capture_logs() as logs, pytest.raises(asyncpg.InsufficientPrivilegeError):
        await _call(request, TENANT_ID)
    assert not [entry for entry in logs if entry["event"] == "aa817_tenant_pool_unavailable"]
    assert tenant.tx.__aexit__.call_args[0][0] is asyncpg.InsufficientPrivilegeError
    assert not _audit_calls(request)


@pytest.mark.asyncio
async def test_tenant_pool_logs_one_timing_line():
    request = _request(flag=True, tenant_conn=_conn(fetchrow=_piece_row()))
    with capture_logs() as logs:
        await _call(request, TENANT_ID)
    timing = [entry for entry in logs if entry["event"] == "aa859_tenant_pool_timing"]
    assert len(timing) == 1 == request.app.state.tenant_pool.acquire.call_count
    assert timing[0]["flag_key"] == FLAG


# ── route-specific ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_flag_on_audit_runs_on_main_after_the_tenant_transaction_committed():
    order = []
    tenant = _conn(fetchrow=_piece_row())
    tenant.tx.__aexit__ = AsyncMock(side_effect=lambda *a: order.append("commit") or False)
    request = _request(flag=True, tenant_conn=tenant)
    ctx = request.app.state.tenant_pool.acquire.return_value
    ctx.__aexit__ = AsyncMock(side_effect=lambda *a: order.append("release") or False)
    request.app.state.pool.execute = AsyncMock(side_effect=lambda *a: order.append("audit") or "INSERT 0 1")
    await _call(request, TENANT_ID)
    assert order == ["commit", "release", "audit"], "audit on MAIN only after commit, no tenant conn held"
    audit_sql, *params = request.app.state.pool.execute.call_args[0]
    assert "INSERT INTO acp_shared.audit_log" in audit_sql
    assert params[0] == str(TENANT_ID) and params[2] == "content_piece.edited" and params[4] == str(PIECE_ID)


@pytest.mark.asyncio
async def test_flag_on_audit_failure_does_not_fail_the_edit():
    request = _request(flag=True, tenant_conn=_conn(fetchrow=_piece_row()))
    request.app.state.pool.execute = AsyncMock(side_effect=asyncpg.PostgresError("audit insert failed"))
    with capture_logs() as logs:
        result = await _call(request, TENANT_ID)
    assert result["content_text"] == "Body"
    failed = [entry for entry in logs if entry["event"] == "aa859_piece_edit_audit_failed"]
    assert len(failed) == 1 and failed[0]["log_level"] == "warning"


@pytest.mark.parametrize("flag", [True, False], ids=["tenant_pool", "admin_pool"])
@pytest.mark.asyncio
async def test_not_found_or_not_editable_raises_and_writes_no_audit(flag):
    conn = _conn(fetchrow=None)
    request = _request(flag=flag, tenant_conn=conn, main_conn=conn)
    with capture_logs() as logs, pytest.raises(service.ContentWritingError):
        await _call(request, TENANT_ID)
    conn.execute.assert_not_awaited()
    assert not _audit_calls(request)
    lines = [entry for entry in logs if entry["event"] == EVENT]
    assert len(lines) == 1 and lines[0]["updated"] is False and lines[0]["used_tenant_pool"] is flag


@pytest.mark.asyncio
async def test_router_passes_the_request_and_maps_not_found_to_404():
    request = _request(flag=True, tenant_conn=_conn(fetchrow=None))
    body = router_mod.UpdateContentBody(content_text="x")
    with pytest.raises(HTTPException) as exc:
        await router_mod.update_piece(PIECE_ID, body, request, tenant={"sub": str(TENANT_ID)})
    assert exc.value.status_code == 404
    assert request.app.state.tenant_pool.acquire.call_count == 1, "the router reached the tenant pool"


def test_piece_edit_sql_keeps_owner_and_status_filters():
    sql = service._PIECE_EDIT_SQL
    assert "piece_id = $1 AND tenant_id = $2 AND status IN ('approved', 'held')" in sql
    assert "SET content_text = $3" in sql and "RETURNING" in sql
