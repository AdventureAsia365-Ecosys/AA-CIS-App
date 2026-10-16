"""AA-859 W9 (PR-I) — GET / POST /v1/integrations/{wordpress,facebook} and POST .../test on the tenant pool
behind INTEGRATION_CONNECT_FLAG (aa859:integration_connect_pool).

  - Each route passes the AA-817 fail-closed matrix (test_aa817_fail_closed_routes.py's checks, run here
    with these routes so that file is not edited by parallel PRs): own flag, 503 on every "no tenant
    context" case, never the admin pool once the flag is on, one `used_tenant_pool=` line per request,
    the pool timing line per block.
  - The connect UPSERT and the verify UPDATEs write only the columns migration 241 grants to aa_app_user
    (parsed from the migration file, so a drift on either side fails here, not as a 42501 after a flip).
  - Connect writes the secret before it takes a connection, and a failed secret write takes none.
  - Flag off is the old path: the main pool, one statement per acquire, no transaction wrapper.
  - Test-connection: a row gone by block 2 is a 404 (was a TypeError on a None row).
"""
import asyncio
import re
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

from api.core import aa544_tenant_pool as tp
from api.routers import v1_integrations as integ_mod
from tests.integration._rls_fixture_helpers import migration_path
from tests.unit import test_aa817_fail_closed_routes as matrix

TENANT_ID = matrix.TENANT_ID
WP_CREDS = {"wp_url": "https://blog.example.com", "username": "u", "app_password": "p"}
FB_CREDS = {"page_id": "123", "page_access_token": "t"}


def _row():
    return {"config": {"site_url": "https://blog.example.com", "page_id": "123"}, "secret_key": "acp/x",
            "connected_at": None, "last_verified_at": None, "last_verify_error": None}


def _http(status=200, body='{"id": "1"}'):
    resp = MagicMock()
    resp.status = status
    resp.headers = {"content-type": "application/json"}
    resp.text = AsyncMock(return_value=body)
    resp_ctx = MagicMock()
    resp_ctx.__aenter__ = AsyncMock(return_value=resp)
    resp_ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=resp_ctx)
    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session)
    session_ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=session_ctx)


def _tenant(tenant_id):
    return {"sub": str(tenant_id) if tenant_id else ""}


async def _status_wp(request, tenant_id):
    return await integ_mod.get_wordpress_status(request, tenant=_tenant(tenant_id))


async def _status_fb(request, tenant_id):
    return await integ_mod.get_facebook_status(request, tenant=_tenant(tenant_id))


async def _connect_wp(request, tenant_id):
    body = integ_mod.SaveWordPressRequest(wp_url="https://blog.example.com", username="u", app_password="p")
    with patch.object(integ_mod, "_put_secret"):
        return await integ_mod.save_wordpress(body, request, tenant=_tenant(tenant_id))


async def _connect_fb(request, tenant_id):
    body = integ_mod.SaveFacebookRequest(page_id="123", page_access_token="t")
    with patch.object(integ_mod, "_put_secret"):
        return await integ_mod.save_facebook(body, request, tenant=_tenant(tenant_id))


async def _test_wp(request, tenant_id):
    with patch.object(integ_mod, "_get_secret", return_value=dict(WP_CREDS)), \
            patch.object(integ_mod.aiohttp, "ClientSession", _http()):
        return await integ_mod.test_wordpress(request, tenant=_tenant(tenant_id))


async def _test_fb(request, tenant_id):
    with patch.object(integ_mod, "_get_secret", return_value=dict(FB_CREDS)), \
            patch.object(integ_mod.aiohttp, "ClientSession", _http()):
        return await integ_mod.test_facebook(request, tenant=_tenant(tenant_id))


FLAG = tp.INTEGRATION_CONNECT_FLAG
# (flag, call(request, tenant_id), fresh row, log event) — the shape of matrix.ROUTES
ROUTES = [
    pytest.param(FLAG, _status_wp, _row, "aa859_integration_status", id="status_wordpress"),
    pytest.param(FLAG, _status_fb, _row, "aa859_integration_status", id="status_facebook"),
    pytest.param(FLAG, _connect_wp, _row, "aa859_integration_connect", id="connect_wordpress"),
    pytest.param(FLAG, _connect_fb, _row, "aa859_integration_connect", id="connect_facebook"),
    pytest.param(FLAG, _test_wp, _row, "aa859_integration_test", id="test_wordpress"),
    pytest.param(FLAG, _test_fb, _row, "aa859_integration_test", id="test_facebook"),
]

MATRIX_CHECKS = [
    "test_flag_on_runs_on_tenant_pool_and_logs_it",
    "test_flag_off_stays_on_admin_pool_and_logs_it",
    "test_flag_on_without_tenant_pool_is_503",
    "test_flag_on_without_tenant_id_is_503",
    "test_flag_on_guc_not_applied_is_503",
    "test_redis_error_falls_back_to_admin_pool_loudly",
    "test_query_error_is_not_reported_as_pool_unavailable",
    "test_tenant_pool_logs_wait_and_hold_per_block",
    "test_no_timing_line_when_the_tenant_pool_is_not_used",
]


def test_flag_constant_is_the_one_the_plan_names():
    assert FLAG == "aa859:integration_connect_pool"


# ── the AA-817 fail-closed matrix ─────────────────────────────────────────────

@pytest.mark.parametrize("check", MATRIX_CHECKS)
@pytest.mark.parametrize("flag_key,call,row,event", ROUTES)
@pytest.mark.asyncio
async def test_route_passes_the_fail_closed_matrix(check, flag_key, call, row, event):
    await getattr(matrix, check)(flag_key, call, row, event)


@pytest.mark.parametrize("error,reason", [
    (asyncpg.TooManyConnectionsError('too many connections for role "aa_app_user"'), "too_many_connections"),
    (asyncio.TimeoutError(), "acquire_timeout"),
    (ConnectionRefusedError(111, "refused"), "connect_error"),
    (asyncpg.InterfaceError("pool is closed"), "pool_closed"),
])
@pytest.mark.parametrize("flag_key,call,row,event", ROUTES)
@pytest.mark.asyncio
async def test_no_tenant_connection_is_503(flag_key, call, row, event, error, reason):
    await matrix.test_flag_on_no_tenant_connection_is_503_with_metric_event(flag_key, call, row, event, error,
                                                                            reason)


@pytest.mark.parametrize("bad_id", ["not-a-uuid", "' OR true --", "{12345678-1234-5678-1234-567812345678}"])
@pytest.mark.parametrize("flag_key,call,row,event", ROUTES)
@pytest.mark.asyncio
async def test_malformed_tenant_id_is_503_before_any_acquire(flag_key, call, row, event, bad_id):
    await matrix.test_flag_on_malformed_tenant_id_is_503_before_any_acquire(flag_key, call, row, event, bad_id)


@pytest.mark.parametrize("flag_key,call,row,event", ROUTES)
@pytest.mark.asyncio
async def test_other_flags_do_not_move_the_route(flag_key, call, row, event):
    """Only aa859:integration_connect_pool moves these routes (disconnect's own flag does not)."""
    request = matrix._request(flag=False, main_conn=matrix._conn(fetchrow=row()))
    request.app.state.redis.get = matrix._flag_only(tp.INTEGRATION_DISCONNECT_FLAG)
    await call(request, TENANT_ID)
    request.app.state.tenant_pool.acquire.assert_not_called()


# ── 241 column grants ─────────────────────────────────────────────────────────

def _grant_columns(privilege):
    with open(migration_path(241)) as f:
        sql = f.read()
    m = re.search(rf"^GRANT {privilege} \(([^)]*)\)\s+ON shared\.tenant_integrations TO aa_app_user;", sql, re.M)
    assert m, f"migration 241 no longer grants column {privilege} on shared.tenant_integrations"
    return {c.strip() for c in m.group(1).split(",")}


def _set_columns(sql):
    """Columns assigned in the SET list of an UPDATE / ON CONFLICT DO UPDATE (up to WHERE / RETURNING)."""
    set_list = re.search(r"\bSET\b(.*?)(\bWHERE\b|\bRETURNING\b)", sql, re.S).group(1)
    return set(re.findall(r"(\w+)\s*=", set_list))


def _insert_columns(sql):
    return {c.strip() for c in re.search(r"INSERT INTO shared\.tenant_integrations\s*\(([^)]*)\)", sql).group(1)
            .split(",")}


def test_connect_upsert_writes_only_the_241_granted_columns():
    assert _insert_columns(integ_mod._CONNECT_UPSERT_SQL) <= _grant_columns("INSERT")
    assert _set_columns(integ_mod._CONNECT_UPSERT_SQL) == {"config", "secret_key", "updated_at"}
    assert _set_columns(integ_mod._CONNECT_UPSERT_SQL) <= _grant_columns("UPDATE")
    assert "ON CONFLICT (tenant_id, integration_type) DO UPDATE SET" in integ_mod._CONNECT_UPSERT_SQL


@pytest.mark.parametrize("name", ["_VERIFY_OK_SQL", "_VERIFY_FAILED_SQL"])
def test_verify_updates_write_only_the_241_granted_columns(name):
    assert _set_columns(getattr(integ_mod, name)) <= _grant_columns("UPDATE")


def test_grant_parser_sees_a_column_outside_the_grant():
    """Mutation check for the two tests above."""
    mutated = integ_mod._CONNECT_UPSERT_SQL.replace("updated_at = now()", "updated_at = now(), connected_at = now()")
    assert not _set_columns(mutated) <= _grant_columns("UPDATE")
    assert "connected_at" not in _grant_columns("UPDATE") and "tenant_id" not in _grant_columns("UPDATE")


@pytest.mark.parametrize("name", ["_STATUS_SQL", "_TEST_SELECT_SQL", "_VERIFY_OK_SQL", "_VERIFY_FAILED_SQL"])
def test_every_statement_filters_on_the_caller_tenant_and_type(name):
    sql = getattr(integ_mod, name)
    assert "WHERE tenant_id = $1::uuid AND integration_type = $2" in sql


# ── connect: secret first, then one block ─────────────────────────────────────

@pytest.mark.parametrize("call", [_connect_wp, _connect_fb], ids=["wordpress", "facebook"])
@pytest.mark.asyncio
async def test_connect_writes_the_secret_before_taking_a_connection(call):
    request = matrix._request(flag=True, tenant_conn=matrix._conn(fetchrow=_row()))
    order = []
    request.app.state.tenant_pool.acquire.side_effect = lambda **kw: order.append("acquire") or \
        matrix._ctx(matrix._conn(fetchrow=_row()))
    with patch.object(integ_mod, "_put_secret", side_effect=lambda *a: order.append("secret")):
        body = (integ_mod.SaveWordPressRequest(wp_url="https://blog.example.com", username="u", app_password="p")
                if call is _connect_wp else integ_mod.SaveFacebookRequest(page_id="1", page_access_token="t"))
        save = integ_mod.save_wordpress if call is _connect_wp else integ_mod.save_facebook
        await save(body, request, tenant=_tenant(TENANT_ID))
    assert order == ["secret", "acquire"]


@pytest.mark.parametrize("flag", [True, False], ids=["tenant_pool", "admin_pool"])
@pytest.mark.asyncio
async def test_connect_secret_failure_is_502_and_takes_no_connection(flag):
    request = matrix._request(flag=flag)
    err = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "CreateSecret")
    with patch.object(integ_mod, "_put_secret", side_effect=err), pytest.raises(HTTPException) as exc:
        await integ_mod.save_wordpress(
            integ_mod.SaveWordPressRequest(wp_url="https://blog.example.com", username="u", app_password="p"),
            request, tenant=_tenant(TENANT_ID))
    assert exc.value.status_code == 502
    request.app.state.tenant_pool.acquire.assert_not_called()
    request.app.state.pool.acquire.assert_not_called()


# ── flag off = the old path ───────────────────────────────────────────────────

@pytest.mark.parametrize("call,statements", [
    (_status_wp, 1), (_status_fb, 1), (_connect_wp, 1), (_connect_fb, 1), (_test_wp, 2), (_test_fb, 2),
], ids=["status_wp", "status_fb", "connect_wp", "connect_fb", "test_wp", "test_fb"])
@pytest.mark.asyncio
async def test_flag_off_is_one_statement_per_main_acquire_without_transaction(call, statements):
    main = matrix._conn(fetchrow=_row())
    request = matrix._request(flag=False, main_conn=main)
    await call(request, TENANT_ID)
    assert request.app.state.pool.acquire.call_count == statements
    assert main.fetchrow.await_count == statements
    main.transaction.assert_not_called()
    main.fetchval.assert_not_awaited()   # no set_config on the main pool
    for args in main.fetchrow.await_args_list:
        assert args[0][1] == str(TENANT_ID)


# ── test-connection ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("flag", [True, False], ids=["tenant_pool", "admin_pool"])
@pytest.mark.parametrize("call,name", [(_test_wp, "WordPress"), (_test_fb, "Facebook")], ids=["wp", "fb"])
@pytest.mark.asyncio
async def test_connection_test_row_gone_in_block_two_is_404(call, name, flag):
    conn = matrix._conn(fetchrow=[_row(), None])
    request = matrix._request(flag=flag, tenant_conn=conn, main_conn=conn)
    with pytest.raises(HTTPException) as exc:
        await call(request, TENANT_ID)
    assert exc.value.status_code == 404 and exc.value.detail == f"{name} is not connected yet"


@pytest.mark.parametrize("call,kind", [(_test_wp, "wordpress"), (_test_fb, "facebook")], ids=["wp", "fb"])
@pytest.mark.parametrize("status,sql_name,params", [
    (200, "_VERIFY_OK_SQL", 2), (401, "_VERIFY_FAILED_SQL", 3),
], ids=["success", "failure"])
@pytest.mark.asyncio
async def test_connection_test_block_two_is_the_verify_update(call, kind, status, sql_name, params):
    tenant = matrix._conn(fetchrow=_row())
    request = matrix._request(flag=True, tenant_conn=tenant)
    with patch.object(integ_mod, "_get_secret", return_value=dict(WP_CREDS if kind == "wordpress" else FB_CREDS)), \
            patch.object(integ_mod.aiohttp, "ClientSession",
                         _http(status, '{"id": "1"}' if status == 200 else '{"error": {"code": 190}}')):
        test = integ_mod.test_wordpress if kind == "wordpress" else integ_mod.test_facebook
        out = await test(request, tenant=_tenant(TENANT_ID))
    assert out["success"] is (status == 200)
    first, second = tenant.fetchrow.await_args_list
    assert first[0][:3] == (integ_mod._TEST_SELECT_SQL, str(TENANT_ID), kind)
    assert second[0][0] == getattr(integ_mod, sql_name) and second[0][1:3] == (str(TENANT_ID), kind)
    assert len(second[0]) == params + 1
    assert request.app.state.tenant_pool.acquire.call_count == 2
