"""AA-859 W4 — DELETE /v1/publish-log/{publish_id} (tenant self-unpublish) on the tenant pool behind UNPUBLISH_FLAG.

- The AA-817 fail-closed matrix (test_aa817_fail_closed_routes.py), run for this route: flag on = tenant pool,
  503 on every "no tenant context / connection" case, never the admin pool; flag off = admin pool; the
  per-request `aa859_unpublish` line carries used_tenant_pool; one timing line per tenant-pool block.
- Three short blocks (publish_log SELECT, tenant_integrations SELECT, status-guarded UPDATE); the Secrets
  Manager read runs in a worker thread and, like the remote delete, with no tenant connection held.
- Audit row on MAIN after the UPDATE committed, best-effort.
- Flag off: the same three statements on the admin pool with no transaction wrapper (the pre-AA-859 shape).
"""
import threading
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException
from structlog.testing import capture_logs

from api.core import aa544_tenant_pool as tp
from api.routers import v1_publish as pub_mod
from tests.unit import test_aa817_fail_closed_routes as matrix

TENANT_ID = str(uuid4())
PUBLISH_ID = uuid4()
PIECE_ID = str(uuid4())
NOW = datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc)
WP_CREDS = {"wp_url": "https://b.com", "username": "u", "app_password": "p"}

PUBLISHED = {"publish_id": str(PUBLISH_ID), "piece_id": PIECE_ID, "channel": "blog", "external_id": "42",
             "external_url": "https://www.b.com/p/42/"}
INTEG = {"secret_key": "acp/cms/x"}
UPDATED = {"publish_id": str(PUBLISH_ID), "channel": "blog", "status": "unpublished", "unpublished_at": NOW}


def _row():
    """One row that answers all three statements (the matrix fakes return the same row for every fetchrow)."""
    return {**PUBLISHED, **INTEG, **UPDATED}


async def _call_unpublish(request, tenant_id):
    with patch.object(pub_mod, "_get_secret", return_value=dict(WP_CREDS)), \
            patch.object(pub_mod, "_delete_remote", AsyncMock(return_value="trashed")):
        return await pub_mod.unpublish(PUBLISH_ID, request, tenant={"sub": str(tenant_id) if tenant_id else ""})


# ── the AA-817 fail-closed matrix, for this route ─────────────────────────────

ROUTE = [pytest.param(tp.UNPUBLISH_FLAG, _call_unpublish, _row, "aa859_unpublish", id="unpublish")]
_route = pytest.mark.parametrize("flag_key,call,row,event", ROUTE)


def _params_of(fn, argnames):
    """The extra parametrize list a matrix test declares (kept in one place: the matrix file)."""
    return next(m.args[1] for m in fn.pytestmark if m.name == "parametrize" and m.args[0] == argnames)


@_route
@pytest.mark.asyncio
async def test_flag_on_runs_on_tenant_pool_and_logs_it(flag_key, call, row, event):
    await matrix.test_flag_on_runs_on_tenant_pool_and_logs_it(flag_key, call, row, event)


@_route
@pytest.mark.asyncio
async def test_flag_off_stays_on_admin_pool_and_logs_it(flag_key, call, row, event):
    await matrix.test_flag_off_stays_on_admin_pool_and_logs_it(flag_key, call, row, event)


@_route
@pytest.mark.asyncio
async def test_flag_on_without_tenant_pool_is_503(flag_key, call, row, event):
    await matrix.test_flag_on_without_tenant_pool_is_503(flag_key, call, row, event)


@_route
@pytest.mark.asyncio
async def test_flag_on_without_tenant_id_is_503(flag_key, call, row, event):
    await matrix.test_flag_on_without_tenant_id_is_503(flag_key, call, row, event)


@_route
@pytest.mark.asyncio
async def test_flag_on_guc_not_applied_is_503(flag_key, call, row, event):
    await matrix.test_flag_on_guc_not_applied_is_503(flag_key, call, row, event)


@pytest.mark.parametrize(
    "error,reason", _params_of(matrix.test_flag_on_no_tenant_connection_is_503_with_metric_event, "error,reason"))
@_route
@pytest.mark.asyncio
async def test_flag_on_no_tenant_connection_is_503_with_metric_event(flag_key, call, row, event, error, reason):
    await matrix.test_flag_on_no_tenant_connection_is_503_with_metric_event(flag_key, call, row, event, error, reason)


@_route
@pytest.mark.asyncio
async def test_redis_error_keeps_tenant_pool_loudly(flag_key, call, row, event):
    await matrix.test_redis_error_keeps_tenant_pool_loudly(flag_key, call, row, event)


@_route
@pytest.mark.asyncio
async def test_missing_flag_key_uses_tenant_pool(flag_key, call, row, event):
    await matrix.test_missing_flag_key_uses_tenant_pool(flag_key, call, row, event)


@_route
@pytest.mark.asyncio
async def test_query_error_is_not_reported_as_pool_unavailable(flag_key, call, row, event):
    await matrix.test_query_error_is_not_reported_as_pool_unavailable(flag_key, call, row, event)


@pytest.mark.parametrize("bad_id", _params_of(matrix.test_flag_on_malformed_tenant_id_is_503_before_any_acquire,
                                              "bad_id"))
@_route
@pytest.mark.asyncio
async def test_flag_on_malformed_tenant_id_is_503_before_any_acquire(flag_key, call, row, event, bad_id):
    await matrix.test_flag_on_malformed_tenant_id_is_503_before_any_acquire(flag_key, call, row, event, bad_id)


@_route
@pytest.mark.asyncio
async def test_tenant_pool_logs_wait_and_hold_per_block(flag_key, call, row, event):
    await matrix.test_tenant_pool_logs_wait_and_hold_per_block(flag_key, call, row, event)


@_route
@pytest.mark.asyncio
async def test_no_timing_line_when_the_tenant_pool_is_not_used(flag_key, call, row, event):
    await matrix.test_no_timing_line_when_the_tenant_pool_is_not_used(flag_key, call, row, event)


# ── three short blocks, nothing held across Secrets Manager / HTTP ────────────

def _ordered_request(order, rows, flag=True):
    """Request whose pool (tenant pool when `flag`, else admin) appends acquire / statement / release to `order`."""
    conn = matrix._conn()
    queue = list(rows)

    async def fetchrow(sql, *args):
        order.append("update" if sql.lstrip().startswith("UPDATE") else
                     "select_integration" if "tenant_integrations" in sql else "select_log")
        return queue.pop(0)
    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    request = matrix._request(flag=flag, tenant_conn=conn, main_conn=conn)
    pool = request.app.state.tenant_pool if flag else request.app.state.pool
    ctx = pool.acquire.return_value
    ctx.__aenter__ = AsyncMock(side_effect=lambda *a: order.append("acquire") or conn)
    ctx.__aexit__ = AsyncMock(side_effect=lambda *a: order.append("release") or False)
    return request, conn


@pytest.mark.asyncio
async def test_secret_read_and_remote_delete_run_between_blocks_with_no_connection_held():
    order, threads = [], []

    def get_secret(key):
        order.append("secret")
        threads.append(threading.get_ident())
        return dict(WP_CREDS)

    async def delete_remote(channel, creds, external_id):
        order.append("remote")
        return "trashed"

    request, _ = _ordered_request(order, [PUBLISHED, INTEG, UPDATED])
    with patch.object(pub_mod, "_get_secret", side_effect=get_secret), \
            patch.object(pub_mod, "_delete_remote", side_effect=delete_remote):
        out = await pub_mod.unpublish(PUBLISH_ID, request, tenant={"sub": TENANT_ID})

    assert out["status"] == "unpublished" and out["remote"] == "trashed"
    assert order == ["acquire", "select_log", "release", "acquire", "select_integration", "release",
                     "secret", "remote", "acquire", "update", "release"]
    assert threads and threads[0] != threading.get_ident(), "boto3 runs in a worker thread (asyncio.to_thread)"
    request.app.state.pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_flag_off_runs_the_same_three_statements_on_the_admin_pool_without_transactions():
    order = []
    request, conn = _ordered_request(order, [PUBLISHED, INTEG, UPDATED], flag=False)
    with patch.object(pub_mod, "_get_secret", return_value=dict(WP_CREDS)), \
            patch.object(pub_mod, "_delete_remote", AsyncMock(return_value="trashed")):
        out = await pub_mod.unpublish(PUBLISH_ID, request, tenant={"sub": TENANT_ID})
    assert order == ["acquire", "select_log", "release", "acquire", "select_integration", "release",
                     "acquire", "update", "release"]
    conn.transaction.assert_not_called()
    conn.fetchval.assert_not_awaited()  # no set_config
    request.app.state.tenant_pool.acquire.assert_not_called()
    assert out == {"publish_id": str(PUBLISH_ID), "channel": "blog", "status": "unpublished",
                   "unpublished_at": NOW.isoformat(), "unpublished_by": f"tenant:{TENANT_ID}", "remote": "trashed"}
    update_args = conn.fetchrow.await_args_list[2][0]
    assert update_args[1:] == (PUBLISH_ID, TENANT_ID, f"tenant:{TENANT_ID}")


def _tenant_request(*rows):
    conn = matrix._conn(fetchrow=list(rows))
    request = matrix._request(flag=True, tenant_conn=conn)
    request.app.state.redis.get = matrix._flag_only(tp.UNPUBLISH_FLAG)
    return request, conn


@pytest.mark.asyncio
async def test_audit_row_is_written_on_main_after_the_update():
    request, conn = _tenant_request(PUBLISHED, INTEG, UPDATED)
    await _call_unpublish(request, TENANT_ID)
    sql, *args = request.app.state.pool.execute.await_args[0]
    assert "INSERT INTO acp_shared.audit_log" in sql
    assert args[2] == "content_piece.unpublished" and args[4] == PIECE_ID
    assert not [c for c in conn.execute.await_args_list if "audit_log" in c[0][0]], "audit never on the tenant pool"
    request.app.state.pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_audit_failure_does_not_fail_the_unpublish():
    request, _ = _tenant_request(PUBLISHED, INTEG, UPDATED)
    request.app.state.pool.execute = AsyncMock(side_effect=RuntimeError("main pool down"))
    with capture_logs() as logs:
        out = await _call_unpublish(request, TENANT_ID)
    assert out["status"] == "unpublished"
    assert [e for e in logs if e["event"] == "unpublish_audit_log_failed"]


@pytest.mark.asyncio
async def test_not_found_is_404_after_one_block():
    request, conn = _tenant_request(None)
    with pytest.raises(HTTPException) as exc:
        await _call_unpublish(request, TENANT_ID)
    assert exc.value.status_code == 404
    assert request.app.state.tenant_pool.acquire.call_count == 1


@pytest.mark.parametrize("published", [
    {**PUBLISHED, "channel": "tiktok"}, {**PUBLISHED, "external_id": None},
], ids=["unsupported_channel", "no_external_id"])
@pytest.mark.asyncio
async def test_unremovable_row_is_422_before_the_integration_read(published):
    request, _ = _tenant_request(published)
    with pytest.raises(HTTPException) as exc:
        await _call_unpublish(request, TENANT_ID)
    assert exc.value.status_code == 422
    assert request.app.state.tenant_pool.acquire.call_count == 1


@pytest.mark.parametrize("integ", [None, {"secret_key": None}], ids=["no_row", "no_secret_key"])
@pytest.mark.asyncio
async def test_not_connected_is_422_with_no_secret_read(integ):
    request, _ = _tenant_request(PUBLISHED, integ)
    with patch.object(pub_mod, "_get_secret", side_effect=AssertionError("no Secrets Manager call")):
        with pytest.raises(HTTPException) as exc:
            await pub_mod.unpublish(PUBLISH_ID, request, tenant={"sub": TENANT_ID})
    assert exc.value.status_code == 422 and "Connect WordPress to unpublish" in exc.value.detail
    assert request.app.state.tenant_pool.acquire.call_count == 2


@pytest.mark.asyncio
async def test_secret_read_failure_is_502_and_the_row_stays():
    request, conn = _tenant_request(PUBLISHED, INTEG)
    err = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "GetSecretValue")
    with patch.object(pub_mod, "_get_secret", side_effect=err):
        with pytest.raises(HTTPException) as exc:
            await pub_mod.unpublish(PUBLISH_ID, request, tenant={"sub": TENANT_ID})
    assert exc.value.status_code == 502
    assert conn.fetchrow.await_count == 2, "no UPDATE"


@pytest.mark.asyncio
async def test_tombstoned_credentials_are_422_reconnect_on_the_tenant_pool():
    request, conn = _tenant_request(PUBLISHED, INTEG)
    with patch.object(pub_mod, "_get_secret", return_value={"disconnected": True}), \
            patch.object(pub_mod, "_delete_remote", AsyncMock(side_effect=AssertionError("no remote call"))):
        with pytest.raises(HTTPException) as exc:
            await pub_mod.unpublish(PUBLISH_ID, request, tenant={"sub": TENANT_ID})
    assert exc.value.status_code == 422 and "reconnect" in exc.value.detail
    assert conn.fetchrow.await_count == 2 and request.app.state.tenant_pool.acquire.call_count == 2


@pytest.mark.asyncio
async def test_remote_failure_is_502_with_no_update_and_no_audit():
    request, conn = _tenant_request(PUBLISHED, INTEG)
    with patch.object(pub_mod, "_get_secret", return_value=dict(WP_CREDS)), \
            patch.object(pub_mod, "_delete_remote", AsyncMock(side_effect=RuntimeError("WP 500"))):
        with pytest.raises(HTTPException) as exc:
            await pub_mod.unpublish(PUBLISH_ID, request, tenant={"sub": TENANT_ID})
    assert exc.value.status_code == 502
    assert request.app.state.tenant_pool.acquire.call_count == 2
    request.app.state.pool.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_unpublish_makes_the_guarded_update_0_rows_and_404():
    """Double click: both requests read 'published'; the second UPDATE matches nothing (status guard)."""
    request, _ = _tenant_request(PUBLISHED, INTEG, None)
    with pytest.raises(HTTPException) as exc:
        await _call_unpublish(request, TENANT_ID)
    assert exc.value.status_code == 404
    assert request.app.state.tenant_pool.acquire.call_count == 3
    request.app.state.pool.execute.assert_not_awaited()


def test_sql_constants_filter_on_the_caller_and_guard_the_status():
    for sql in (pub_mod._UNPUBLISH_SELECT_SQL, pub_mod._UNPUBLISH_UPDATE_SQL):
        assert "publish_id = $1 AND tenant_id = $2::uuid AND status = 'published'" in sql
    assert "tenant_id = $1::uuid AND integration_type = $2" in pub_mod._UNPUBLISH_INTEGRATION_SQL


def test_get_secret_uses_its_own_session_per_call():
    """boto3.client() on the shared default Session is not thread-safe; the read now runs in to_thread."""
    with patch.object(pub_mod.boto3.session, "Session") as session:
        session.return_value.client.return_value.get_secret_value.return_value = {"SecretString": '{"a": 1}'}
        assert pub_mod._get_secret("acp/x") == {"a": 1}
        assert pub_mod._get_secret("acp/y") == {"a": 1}
    assert session.call_count == 2


@pytest.mark.asyncio
async def test_publish_reads_the_secret_in_a_worker_thread_too():
    """PR-D4 review: _get_secret builds a fresh Session (~50 ms), so publish() must not call it on the event loop."""
    from tests.unit import test_aa458_publish as t458

    threads = []

    def get_secret(key):
        threads.append(threading.get_ident())
        raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}}, "GetSecretValue")

    pool, _ = t458._sequential_pool(t458._piece_row(), t458._integ_row())
    with patch.object(pub_mod, "_get_secret", side_effect=get_secret):
        with pytest.raises(HTTPException) as exc_info:
            await pub_mod.publish(t458.PIECE_ID, t458._make_request(pool), t458.TENANT)
    assert exc_info.value.status_code == 502, "ClientError from the worker thread still maps to 502"
    assert threads and threads[0] != threading.get_ident()
