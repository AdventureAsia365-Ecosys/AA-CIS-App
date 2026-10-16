"""AA-817 PR-A — every route on the aa_app_user tenant pool fails closed and logs `used_tenant_pool`.

Routes (flag in api/core/aa544_tenant_pool.py):
  - GET /v1/content-writing/pieces/{id} (+ /export)  service.fetch_piece              STAGE5_GET_PIECE_FLAG
  - GET /v1/publish-log/pending                       v1_publish.list_pending          STAGE1_PUBLISH_PENDING_FLAG
  - GET /v1/publish-log                               v1_publish.list_publish_log      PUBLISH_LOG_LIST_FLAG
  - GET /v1/content-writing/requests/{id}/latest-piece service.fetch_latest_piece...   CONTENT_PIECE_READS_FLAG
  - DELETE /v1/integrations/{wordpress,facebook}      v1_integrations._disconnect      INTEGRATION_DISCONNECT_FLAG

For each: flag on + no tenant pool / no tenant id / GUC not applied / no tenant connection (CONNECTION LIMIT,
acquire timeout) is a 503 and the admin pool is never touched; the per-request log line carries
used_tenant_pool. Plus the pool sizing (critique C4), the loud Redis fallback (C5) and the disconnect
row-count check (C3).
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg
import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException
from structlog.testing import capture_logs

from api.core import aa544_tenant_pool as tp
from api.routers import v1_integrations as integ_mod
from api.routers import v1_publish as pub_mod
from services.acp_content_writing import service

TENANT_ID = uuid4()
PIECE_ID = uuid4()
REQUEST_ID = uuid4()


# ── fakes ─────────────────────────────────────────────────────────────────────

def _ctx(value=None, enter_error=None):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=value, side_effect=enter_error)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _pool(conn=None, acquire_error=None):
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_ctx(conn, enter_error=acquire_error))
    pool.execute = AsyncMock(return_value="INSERT 0 1")  # write_audit_log(pool, ...)
    return pool


def _piece_row():
    return {
        "piece_id": PIECE_ID, "tenant_id": TENANT_ID, "angle_gate_request_id": REQUEST_ID,
        "attempt_number": 1, "content_text": "Body", "status": "approved", "held_reason": None,
        "gate_ledger": [], "repair_log": [], "created_at": datetime(2026, 10, 16, tzinfo=timezone.utc),
        "seo_title": None, "meta_description": None, "slug": None, "channel": "blog",
        "route_hub_name": None, "route_segment_count": None, "flags": [],
    }


def _conn(guc_echo=None, fetchrow=None, execute="DELETE 1"):
    conn = MagicMock()
    conn.tx = _ctx()
    conn.transaction = MagicMock(return_value=conn.tx)
    conn.fetchval = AsyncMock(side_effect=lambda sql, value: value if guc_echo is None else guc_echo)
    conn.fetchrow = AsyncMock(side_effect=fetchrow) if isinstance(fetchrow, list) else \
        AsyncMock(return_value=fetchrow)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value=execute)
    return conn


def _request(flag, tenant_conn=None, main_conn=None, tenant_pool=True, acquire_error=None):
    request = MagicMock()
    request.app.state.redis.get = AsyncMock(return_value="true" if flag else None)
    request.app.state.pool = _pool(main_conn or _conn())
    request.app.state.tenant_pool = (_pool(tenant_conn or _conn(), acquire_error=acquire_error)
                                     if tenant_pool else None)
    return request


# One entry per route: (name, flag, fresh conn rows, call(request, tenant_id), log event).
async def _call_fetch_piece(request, tenant_id):
    return await service.fetch_piece(tenant_id, PIECE_ID, request.app.state.pool, request=request)


async def _call_pending(request, tenant_id):
    return await pub_mod.list_pending(request, {"sub": str(tenant_id) if tenant_id else ""})


async def _call_publish_log(request, tenant_id):
    return await pub_mod.list_publish_log(request, tenant={"sub": str(tenant_id) if tenant_id else ""}, limit=50)


async def _call_latest_piece(request, tenant_id):
    return await service.fetch_latest_piece_for_request(tenant_id, REQUEST_ID, request.app.state.pool,
                                                        request=request)


async def _call_disconnect(request, tenant_id):
    with patch.object(integ_mod, "_sm_client", return_value=MagicMock()):
        return await integ_mod._disconnect(request, str(tenant_id) if tenant_id else "", "wordpress")


ROUTES = [
    pytest.param(tp.STAGE5_GET_PIECE_FLAG, _call_fetch_piece, lambda: _piece_row(),
                 "aa544_stage5_fetch_piece", id="get_piece"),
    pytest.param(tp.STAGE1_PUBLISH_PENDING_FLAG, _call_pending, lambda: None,
                 "aa544_stage1_list_pending", id="publish_pending"),
    pytest.param(tp.PUBLISH_LOG_LIST_FLAG, _call_publish_log, lambda: None,
                 "publish_log_listed", id="publish_log_list"),
    pytest.param(tp.CONTENT_PIECE_READS_FLAG, _call_latest_piece, lambda: None,
                 "aa817_latest_piece", id="latest_piece"),
    pytest.param(tp.INTEGRATION_DISCONNECT_FLAG, _call_disconnect, lambda: {"secret_key": "acp/cms/x"},
                 "aa817_integration_disconnect", id="disconnect"),
]


def _flag_only(flag_key):
    """A Redis whose only 'true' key is `flag_key` — proves each route reads its OWN flag."""
    async def get(key):
        return "true" if key == flag_key else None
    return AsyncMock(side_effect=get)


# ── per-route fail closed ─────────────────────────────────────────────────────

@pytest.mark.parametrize("flag_key,call,row,event", ROUTES)
@pytest.mark.asyncio
async def test_flag_on_runs_on_tenant_pool_and_logs_it(flag_key, call, row, event):
    tenant = _conn(fetchrow=row())
    request = _request(flag=True, tenant_conn=tenant)
    request.app.state.redis.get = _flag_only(flag_key)
    with capture_logs() as logs:
        await call(request, TENANT_ID)
    request.app.state.pool.acquire.assert_not_called()
    assert request.app.state.tenant_pool.acquire.call_args.kwargs == {"timeout": tp.TENANT_POOL_ACQUIRE_TIMEOUT_S}
    sql, value = tenant.fetchval.call_args[0]
    assert "set_config('app.tenant_id', $1, true)" in sql and value == str(TENANT_ID)
    lines = [entry for entry in logs if entry["event"] == event]
    assert lines and all(entry["used_tenant_pool"] is True for entry in lines)


@pytest.mark.parametrize("flag_key,call,row,event", ROUTES)
@pytest.mark.asyncio
async def test_flag_off_stays_on_admin_pool_and_logs_it(flag_key, call, row, event):
    main = _conn(fetchrow=row())
    request = _request(flag=False, main_conn=main)
    with capture_logs() as logs:
        await call(request, TENANT_ID)
    request.app.state.tenant_pool.acquire.assert_not_called()
    lines = [entry for entry in logs if entry["event"] == event]
    assert lines and all(entry["used_tenant_pool"] is False for entry in lines)


@pytest.mark.parametrize("flag_key,call,row,event", ROUTES)
@pytest.mark.asyncio
async def test_flag_on_without_tenant_pool_is_503(flag_key, call, row, event):
    request = _request(flag=True, tenant_pool=False)
    with pytest.raises(HTTPException) as exc:
        await call(request, TENANT_ID)
    assert exc.value.status_code == 503
    request.app.state.pool.acquire.assert_not_called()


@pytest.mark.parametrize("flag_key,call,row,event", ROUTES)
@pytest.mark.asyncio
async def test_flag_on_without_tenant_id_is_503(flag_key, call, row, event):
    request = _request(flag=True)
    with pytest.raises(HTTPException) as exc:
        await call(request, None)
    assert exc.value.status_code == 503
    request.app.state.tenant_pool.acquire.assert_not_called()
    request.app.state.pool.acquire.assert_not_called()


@pytest.mark.parametrize("flag_key,call,row,event", ROUTES)
@pytest.mark.asyncio
async def test_flag_on_guc_not_applied_is_503(flag_key, call, row, event):
    tenant = _conn(guc_echo="", fetchrow=row())
    request = _request(flag=True, tenant_conn=tenant)
    with pytest.raises(HTTPException) as exc:
        await call(request, TENANT_ID)
    assert exc.value.status_code == 503
    tenant.fetchrow.assert_not_awaited()
    tenant.fetch.assert_not_awaited()
    request.app.state.pool.acquire.assert_not_called()


@pytest.mark.parametrize("error,reason", [
    (asyncpg.TooManyConnectionsError('too many connections for role "aa_app_user"'), "too_many_connections"),
    (asyncio.TimeoutError(), "acquire_timeout"),
    (ConnectionRefusedError(111, "refused"), "connect_error"),
    (asyncpg.InterfaceError("pool is closed"), "pool_closed"),
])
@pytest.mark.parametrize("flag_key,call,row,event", ROUTES)
@pytest.mark.asyncio
async def test_flag_on_no_tenant_connection_is_503_with_metric_event(flag_key, call, row, event, error, reason):
    """Critique C4: the 11th connection of aa_app_user (CONNECTION LIMIT 15) or an exhausted pool is a
    503 for that request, logged as one event a metric filter can count; never the admin pool."""
    request = _request(flag=True, acquire_error=error)
    with capture_logs() as logs, pytest.raises(HTTPException) as exc:
        await call(request, TENANT_ID)
    assert exc.value.status_code == 503
    request.app.state.pool.acquire.assert_not_called()
    events = [entry for entry in logs if entry["event"] == "aa817_tenant_pool_unavailable"]
    assert len(events) == 1
    assert events[0]["reason"] == reason and events[0]["flag_key"] == flag_key
    assert events[0]["log_level"] == "error"


@pytest.mark.parametrize("flag_key,call,row,event", ROUTES)
@pytest.mark.asyncio
async def test_redis_error_falls_back_to_admin_pool_loudly(flag_key, call, row, event):
    """Critique C5: kept behaviour (a Redis outage is not a 503 on every tenant read), but logged at
    ERROR, and the per-request line says used_tenant_pool=False, which is what the evidence reads."""
    main = _conn(fetchrow=row())
    request = _request(flag=True, main_conn=main)
    request.app.state.redis.get = AsyncMock(side_effect=ConnectionError("redis down"))
    with capture_logs() as logs:
        await call(request, TENANT_ID)
    request.app.state.tenant_pool.acquire.assert_not_called()
    failed = [entry for entry in logs if entry["event"] == "aa817_tenant_flag_read_failed"]
    assert failed and failed[0]["log_level"] == "error" and failed[0]["flag_key"] == flag_key
    assert failed[0]["fallback"] == "admin_pool"
    lines = [entry for entry in logs if entry["event"] == event]
    assert lines and all(entry["used_tenant_pool"] is False for entry in lines)


@pytest.mark.parametrize("flag_key,call,row,event", ROUTES)
@pytest.mark.asyncio
async def test_query_error_is_not_reported_as_pool_unavailable(flag_key, call, row, event):
    """Only acquire failures are 'unavailable'; a failing query on a tenant connection propagates as is
    (e.g. a missing grant must show up as the Postgres error, not as a 503 that hides it)."""
    tenant = _conn(fetchrow=row())
    err = asyncpg.InsufficientPrivilegeError("permission denied")
    tenant.fetchrow = AsyncMock(side_effect=err)
    tenant.fetch = AsyncMock(side_effect=err)
    request = _request(flag=True, tenant_conn=tenant)
    with capture_logs() as logs, pytest.raises(asyncpg.InsufficientPrivilegeError):
        await call(request, TENANT_ID)
    assert not [entry for entry in logs if entry["event"] == "aa817_tenant_pool_unavailable"]
    exc_type = tenant.tx.__aexit__.call_args[0][0]
    assert exc_type is asyncpg.InsufficientPrivilegeError, "the tenant transaction exits with the error"


# ── disconnect: one transaction, DELETE count checked before the tombstone (critique C3) ─────────

@pytest.mark.parametrize("flag", [True, False], ids=["tenant_pool", "admin_pool"])
@pytest.mark.asyncio
async def test_disconnect_zero_row_delete_is_503_and_leaves_the_secret(flag):
    conn = _conn(fetchrow={"secret_key": "acp/cms/x"}, execute="DELETE 0")
    request = _request(flag=flag, tenant_conn=conn, main_conn=conn)
    sm = MagicMock()
    with capture_logs() as logs, patch.object(integ_mod, "_sm_client", return_value=sm):
        with pytest.raises(HTTPException) as exc:
            await integ_mod.disconnect_wordpress(request, tenant={"sub": str(TENANT_ID)})
    assert exc.value.status_code == 503
    assert not sm.put_secret_value.called, "no tombstone unless exactly the one row was deleted"
    assert not request.app.state.pool.execute.called, "no audit row"
    mismatch = [entry for entry in logs if entry["event"] == "aa817_integration_disconnect_row_mismatch"]
    assert mismatch and mismatch[0]["rows_deleted"] == 0 and mismatch[0]["used_tenant_pool"] is flag


@pytest.mark.asyncio
async def test_disconnect_deletes_before_tombstone_inside_one_transaction():
    order = []
    conn = _conn(fetchrow={"secret_key": "acp/cms/x"})
    conn.execute = AsyncMock(side_effect=lambda *a: order.append("delete") or "DELETE 1")
    sm = MagicMock()
    sm.put_secret_value.side_effect = lambda **kw: order.append("tombstone")
    conn.tx.__aexit__ = AsyncMock(side_effect=lambda *a: order.append("commit") or False)
    request = _request(flag=True, tenant_conn=conn)
    with patch.object(integ_mod, "_sm_client", return_value=sm):
        result = await integ_mod.disconnect_wordpress(request, tenant={"sub": str(TENANT_ID)})
    assert result["disconnected"] is True
    assert order == ["delete", "tombstone", "commit"]
    # tenant pool: the transaction is acquire_scoped_conn's own (GUC LOCAL), no second one opened
    conn.transaction.assert_called_once()
    assert request.app.state.pool.execute.call_args[0][3] == "integration.disconnected"


@pytest.mark.asyncio
async def test_disconnect_flag_off_opens_its_own_transaction():
    conn = _conn(fetchrow={"secret_key": "acp/cms/x"})
    request = _request(flag=False, main_conn=conn)
    with patch.object(integ_mod, "_sm_client", return_value=MagicMock()):
        result = await integ_mod.disconnect_facebook(request, tenant={"sub": str(TENANT_ID)})
    assert result["disconnected"] is True
    conn.transaction.assert_called_once()
    request.app.state.tenant_pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_disconnect_tombstone_timeout_is_502_and_rolls_back():
    from botocore.exceptions import ReadTimeoutError
    conn = _conn(fetchrow={"secret_key": "acp/cms/x"})
    request = _request(flag=True, tenant_conn=conn)
    sm = MagicMock()
    sm.put_secret_value.side_effect = ReadTimeoutError(endpoint_url="https://secretsmanager")
    with patch.object(integ_mod, "_sm_client", return_value=sm):
        with pytest.raises(HTTPException) as exc:
            await integ_mod.disconnect_wordpress(request, tenant={"sub": str(TENANT_ID)})
    assert exc.value.status_code == 502
    assert conn.tx.__aexit__.call_args[0][0] is HTTPException  # rollback of the DELETE


@pytest.mark.asyncio
async def test_disconnect_tombstone_uses_bounded_client():
    sm = MagicMock()
    with patch.object(integ_mod, "_sm_client", return_value=sm) as factory:
        integ_mod._tombstone_secret("acp/cms/x")
    config = factory.call_args[0][0]
    assert config.connect_timeout <= 5 and config.read_timeout <= 5
    assert config.retries == {"mode": "standard", "total_max_attempts": 2}


@pytest.mark.asyncio
async def test_disconnect_commit_failure_after_tombstone_is_logged():
    conn = _conn(fetchrow={"secret_key": "acp/cms/x"})
    conn.tx.__aexit__ = AsyncMock(side_effect=asyncpg.ConnectionDoesNotExistError("gone"))
    request = _request(flag=True, tenant_conn=conn)
    with capture_logs() as logs, patch.object(integ_mod, "_sm_client", return_value=MagicMock()):
        with pytest.raises(asyncpg.ConnectionDoesNotExistError):
            await integ_mod.disconnect_wordpress(request, tenant={"sub": str(TENANT_ID)})
    assert [entry for entry in logs if entry["event"] == "aa817_integration_disconnect_commit_failed"]


@pytest.mark.asyncio
async def test_disconnect_missing_secret_still_deletes():
    conn = _conn(fetchrow={"secret_key": "acp/cms/x"})
    sm = MagicMock()
    sm.put_secret_value.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}}, "PutSecretValue")
    request = _request(flag=True, tenant_conn=conn)
    with patch.object(integ_mod, "_sm_client", return_value=sm):
        result = await integ_mod.disconnect_wordpress(request, tenant={"sub": str(TENANT_ID)})
    assert result["disconnected"] is True


# ── pool sizing (critique C4) ─────────────────────────────────────────────────

def test_pool_fits_the_role_connection_limit():
    """aa_app_user CONNECTION LIMIT 15 (migration 234) >= max_size x api max_capacity 10 (Infra
    modules/ecs/main.tf). Change both together (data standards C2.3c)."""
    assert tp.TENANT_POOL_MIN_SIZE == 0
    assert tp.TENANT_POOL_MAX_SIZE * 10 <= 15
    assert tp.TENANT_POOL_ACQUIRE_TIMEOUT_S <= 5


@pytest.mark.asyncio
async def test_create_tenant_pool_is_lazy_and_bounded():
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=1)
    sm = MagicMock()
    sm.get_secret_value.return_value = {"SecretString": "postgresql://u:p@h/db"}
    with patch("boto3.client", return_value=sm), \
            patch.object(tp.asyncpg, "create_pool", AsyncMock(return_value=pool)) as create:
        assert await tp.create_tenant_pool() is pool
    kw = create.call_args.kwargs
    assert kw["min_size"] == 0 and kw["max_size"] == tp.TENANT_POOL_MAX_SIZE
    assert kw["max_inactive_connection_lifetime"] == tp.TENANT_POOL_IDLE_LIFETIME_S
    assert kw["server_settings"]["application_name"] == "cis-tenant"
    assert kw["server_settings"]["statement_timeout"] == "15000"


@pytest.mark.asyncio
async def test_create_tenant_pool_keeps_the_pool_when_the_boot_probe_fails():
    """A task that boots while the role is at its limit keeps a pool and recovers on its own, instead
    of running its whole life without one (every fail_closed route 503)."""
    pool = MagicMock()
    pool.fetchval = AsyncMock(side_effect=asyncpg.TooManyConnectionsError("too many connections"))
    sm = MagicMock()
    sm.get_secret_value.return_value = {"SecretString": "postgresql://u:p@h/db"}
    with capture_logs() as logs, patch("boto3.client", return_value=sm), \
            patch.object(tp.asyncpg, "create_pool", AsyncMock(return_value=pool)):
        assert await tp.create_tenant_pool() is pool
    events = [entry for entry in logs if entry["event"] == "aa817_tenant_pool_unavailable"]
    assert events and events[0]["stage"] == "boot_probe" and events[0]["reason"] == "too_many_connections"


@pytest.mark.asyncio
async def test_create_tenant_pool_without_secret_is_none():
    with patch("boto3.client", side_effect=RuntimeError("no creds")):
        assert await tp.create_tenant_pool() is None
