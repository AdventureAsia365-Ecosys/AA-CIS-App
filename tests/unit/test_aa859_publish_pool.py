"""AA-859 wave 3 — POST /v1/publish-log/{piece_id}/publish on the tenant pool behind PUBLISH_FLAG.

- The fail-closed matrix: flag on = tenant pool for the three blocks (piece + connection, claim, finish), 503 on
  every "no tenant context / connection" case before any secret read or claim, never the admin pool for those
  blocks; explicit OFF = admin pool. One `aa859_publish` line per request (block 1) and `channel_publish` carries
  used_tenant_pool (block 3).
- MAIN exceptions (plan D2): the daily-limit override (shared.tenants) is read on the admin pool before the claim,
  the audit row is written on it after finish(); neither while a tenant connection is held.
- claim() and finish() get the tenant connection; claim's own transaction is nested (a savepoint) in the scoped one.
- The connection is never held across Secrets Manager or the channel's HTTP call (also in
  test_aa859_reentrancy_wave3.py).
"""
import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
from fastapi import HTTPException
from structlog.testing import capture_logs

from api.core import aa544_tenant_pool as tp
from api.routers import v1_publish as pub_mod
from services.acp_publish.idempotency import Claim

TENANT_ID = str(uuid.uuid4())
PIECE_ID = uuid.uuid4()
PUBLISH_ID = str(uuid.uuid4())
WP_CREDS = {"wp_url": "https://b.com", "username": "u", "app_password": "p"}
PIECE = {"piece_id": PIECE_ID, "content_text": "## Hello\nReal content.", "status": "approved", "slug": None,
         "seo_title": "T", "meta_description": "M", "channel": "blog", "angle_name": "A", "superseded": False,
         "secret_key": "acp/cms/x"}   # one row answers the piece and the connection reads
FINISHED = {"publish_id": PUBLISH_ID, "status": "published", "external_id": "42", "external_url": "https://b.com/42",
            "published_at": None, "last_error": None, "idempotency_key": "k"}


def _ctx(value=None, enter_error=None):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=value, side_effect=enter_error)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _conn(row=PIECE, guc_echo=None):
    conn = MagicMock()
    conn.tx = _ctx()
    conn.transaction = MagicMock(return_value=conn.tx)
    conn.fetchval = AsyncMock(side_effect=lambda sql, *a: (a[0] if guc_echo is None else guc_echo)
                              if "set_config" in sql else 20)
    conn.fetchrow = AsyncMock(return_value=dict(row) if row else None)
    conn.execute = AsyncMock(return_value="OK")
    return conn


def _request(flag, tenant_conn=None, main_conn=None, tenant_pool=True, acquire_error=None):
    request = MagicMock()
    request.app.state.redis.get = AsyncMock(
        side_effect=lambda key: "true" if flag and key == tp.PUBLISH_FLAG else "false")
    main = MagicMock()
    main.acquire = MagicMock(return_value=_ctx(main_conn or _conn()))
    main.execute = AsyncMock(return_value="INSERT 0 1")     # write_audit_log(pool, ...)
    request.app.state.pool = main
    if tenant_pool:
        tenant = MagicMock()
        tenant.acquire = MagicMock(return_value=_ctx(tenant_conn or _conn(), enter_error=acquire_error))
        request.app.state.tenant_pool = tenant
    else:
        request.app.state.tenant_pool = None
    return request


class _Calls:
    """Patched collaborators: secret read, adapter, limit, claim and finish, recording the connection used."""

    def __init__(self, claim_state="claimed"):
        self.claim = AsyncMock(return_value=Claim(claim_state, publish_id=PUBLISH_ID,
                                                  row=dict(FINISHED) if claim_state == "published" else None))
        self.finish = AsyncMock(return_value=dict(FINISHED))
        self.limit = AsyncMock(return_value=20)
        self.secret = MagicMock(return_value=dict(WP_CREDS))
        self.adapter = AsyncMock(return_value=("42", "https://b.com/42"))

    def patches(self):
        return (patch.object(pub_mod, "claim", self.claim), patch.object(pub_mod, "finish", self.finish),
                patch.object(pub_mod.daily_limit, "effective_limit", self.limit),
                patch.object(pub_mod, "_get_secret", self.secret),
                patch.object(pub_mod, "publish_to_channel", self.adapter))


async def _publish(request, tenant_id=TENANT_ID, calls=None):
    calls = calls or _Calls()
    p = calls.patches()
    with p[0], p[1], p[2], p[3], p[4]:
        return await pub_mod.publish(PIECE_ID, request, tenant={"sub": tenant_id})


# ── flag on / off ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_flag_on_runs_three_tenant_blocks_and_main_only_for_limit_and_audit():
    tenant = _conn()
    request = _request(flag=True, tenant_conn=tenant)
    calls = _Calls()
    with capture_logs() as logs:
        out = await _publish(request, calls=calls)
    assert out["success"] is True and out["status"] == "published"
    assert request.app.state.tenant_pool.acquire.call_count == 3, "piece + connection, claim, finish"
    assert all(c.kwargs == {"timeout": tp.TENANT_POOL_ACQUIRE_TIMEOUT_S}
               for c in request.app.state.tenant_pool.acquire.call_args_list)
    assert request.app.state.pool.acquire.call_count == 1, "MAIN: the daily-limit read only"
    assert calls.limit.await_args.args[0] is not tenant, "the override is read on MAIN, not the tenant conn"
    assert calls.claim.await_args.args[0] is tenant and calls.claim.await_args.kwargs["daily_limit"] == 20
    assert calls.finish.await_args.args[0] is tenant
    request.app.state.pool.execute.assert_awaited()            # audit row on MAIN
    assert [e["used_tenant_pool"] for e in logs if e["event"] == "aa859_publish"] == [True]
    assert [e["used_tenant_pool"] for e in logs if e["event"] == "channel_publish"] == [True]
    timing = [e for e in logs if e["event"] == "aa859_tenant_pool_timing"]
    assert len(timing) == 3 and {e["flag_key"] for e in timing} == {tp.PUBLISH_FLAG}


@pytest.mark.asyncio
async def test_flag_on_piece_read_uses_the_tenant_condition_and_the_connection_read():
    tenant = _conn()
    await _publish(_request(flag=True, tenant_conn=tenant))
    piece_sql, pid, tid = tenant.fetchrow.call_args_list[0].args
    assert pub_mod._PUBLISH_PIECE_TENANT_COND in piece_sql and pid == PIECE_ID and tid == TENANT_ID
    assert tenant.fetchrow.call_args_list[1].args == (pub_mod._PUBLISH_INTEGRATION_SQL, TENANT_ID, "wordpress")


@pytest.mark.asyncio
async def test_flag_off_stays_on_admin_pool():
    main = _conn()
    request = _request(flag=False, main_conn=main)
    calls = _Calls()
    with capture_logs() as logs:
        await _publish(request, calls=calls)
    request.app.state.tenant_pool.acquire.assert_not_called()
    assert calls.claim.await_args.args[0] is main and calls.finish.await_args.args[0] is main
    assert [e["used_tenant_pool"] for e in logs if e["event"] == "aa859_publish"] == [False]
    main.transaction.assert_not_called()   # flag off: no wrapper transaction (claim opens its own)


# ── fail closed ───────────────────────────────────────────────────────────────

def _assert_nothing_done(request, calls):
    request.app.state.pool.acquire.assert_not_called()
    calls.secret.assert_not_called()
    calls.claim.assert_not_awaited()
    calls.adapter.assert_not_awaited()


@pytest.mark.asyncio
async def test_flag_on_without_tenant_pool_is_503_before_anything():
    request, calls = _request(flag=True, tenant_pool=False), _Calls()
    with pytest.raises(HTTPException) as exc:
        await _publish(request, calls=calls)
    assert exc.value.status_code == 503
    _assert_nothing_done(request, calls)


@pytest.mark.parametrize("bad_id", ["not-a-uuid", "", "{12345678-1234-5678-1234-567812345678}"])
@pytest.mark.asyncio
async def test_flag_on_malformed_tenant_id_is_503_before_anything(bad_id):
    request, calls = _request(flag=True), _Calls()
    with pytest.raises(HTTPException) as exc:
        await _publish(request, tenant_id=bad_id, calls=calls)
    assert exc.value.status_code == 503
    request.app.state.tenant_pool.acquire.assert_not_called()
    _assert_nothing_done(request, calls)


@pytest.mark.asyncio
async def test_flag_on_guc_not_applied_is_503_before_anything():
    tenant = _conn(guc_echo="")
    request, calls = _request(flag=True, tenant_conn=tenant), _Calls()
    with pytest.raises(HTTPException) as exc:
        await _publish(request, calls=calls)
    assert exc.value.status_code == 503
    tenant.fetchrow.assert_not_awaited()
    _assert_nothing_done(request, calls)


@pytest.mark.parametrize("error,reason", [
    (asyncpg.TooManyConnectionsError('too many connections for role "aa_app_user"'), "too_many_connections"),
    (asyncio.TimeoutError(), "acquire_timeout"),
])
@pytest.mark.asyncio
async def test_flag_on_no_tenant_connection_is_503_with_metric_event(error, reason):
    request, calls = _request(flag=True, acquire_error=error), _Calls()
    with capture_logs() as logs, pytest.raises(HTTPException) as exc:
        await _publish(request, calls=calls)
    assert exc.value.status_code == 503
    _assert_nothing_done(request, calls)
    events = [e for e in logs if e["event"] == "aa817_tenant_pool_unavailable"]
    assert len(events) == 1 and events[0]["reason"] == reason and events[0]["flag_key"] == tp.PUBLISH_FLAG


@pytest.mark.asyncio
async def test_redis_error_or_missing_key_keeps_tenant_pool():
    for get in (AsyncMock(side_effect=ConnectionError("redis down")), AsyncMock(return_value=None)):
        request = _request(flag=True)
        request.app.state.redis.get = get
        with capture_logs() as logs:
            await _publish(request)
        assert request.app.state.tenant_pool.acquire.call_count == 3
        assert [e["used_tenant_pool"] for e in logs if e["event"] == "aa859_publish"] == [True]
        assert len([e for e in logs if e["event"] == "aa817_tenant_flag_read_failed"]) <= 1, "read once per request"


@pytest.mark.asyncio
async def test_piece_not_yours_is_404_after_one_block_and_no_secret_read():
    tenant = _conn(row=None)
    request, calls = _request(flag=True, tenant_conn=tenant), _Calls()
    with pytest.raises(HTTPException) as exc:
        await _publish(request, calls=calls)
    assert exc.value.status_code == 404
    assert request.app.state.tenant_pool.acquire.call_count == 1
    assert tenant.fetchrow.await_count == 1, "no connection read for a piece that is not the caller's"
    _assert_nothing_done(request, calls)


@pytest.mark.parametrize("row,status", [
    ({**PIECE, "status": "processing"}, 422),
    ({**PIECE, "superseded": True}, 409),
    ({**PIECE, "channel": "tiktok"}, 404),
])
@pytest.mark.asyncio
async def test_publishable_checks_are_unchanged_on_the_tenant_pool(row, status):
    request, calls = _request(flag=True, tenant_conn=_conn(row=row)), _Calls()
    with pytest.raises(HTTPException) as exc:
        await _publish(request, calls=calls)
    assert exc.value.status_code == status
    _assert_nothing_done(request, calls)


# ── MAIN reads / writes never overlap a tenant connection ─────────────────────

@pytest.mark.asyncio
async def test_limit_read_and_audit_run_with_no_tenant_connection_held():
    request = _request(flag=True)
    tenant_ctx = request.app.state.tenant_pool.acquire.return_value
    held = []

    def open_blocks():
        return tenant_ctx.__aenter__.await_count - tenant_ctx.__aexit__.await_count

    calls = _Calls()

    async def limit(conn, tid):
        held.append(("limit", open_blocks()))
        return 20
    calls.limit = AsyncMock(side_effect=limit)

    async def audit(*a, **k):
        held.append(("audit", open_blocks()))
        return "INSERT 0 1"
    request.app.state.pool.execute = AsyncMock(side_effect=audit)
    await _publish(request, calls=calls)
    assert held == [("limit", 0), ("audit", 0)]


@pytest.mark.asyncio
async def test_replay_returns_the_stored_row_without_a_remote_call_or_finish():
    request, calls = _request(flag=True), _Calls(claim_state="published")
    out = await _publish(request, calls=calls)
    assert out["replayed"] is True
    calls.adapter.assert_not_awaited()
    calls.finish.assert_not_awaited()
    assert request.app.state.tenant_pool.acquire.call_count == 2


# ── flag ownership ────────────────────────────────────────────────────────────

def test_publish_flag_is_used_only_by_publish():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(pub_mod))
    users = {fn.name for fn in ast.walk(tree) if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
             and any(isinstance(n, ast.Name) and n.id == "PUBLISH_FLAG" for n in ast.walk(fn))}
    assert users == {"publish"}
