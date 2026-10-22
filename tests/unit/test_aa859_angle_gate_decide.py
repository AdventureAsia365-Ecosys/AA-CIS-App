"""AA-859 W3 (plan PR-D3) — POST /v1/angle-gate/requests/{id}/choose and /reopen behind ANGLE_GATE_DECIDE_FLAG.

The AA-817 fail-closed matrix for these two routes (same cases as test_aa817_fail_closed_routes.py, own fakes
because the decision reads through fetchval and re-reads on the main pool after commit):
  * flag on: the tenant pool, GUC set LOCAL, every decision statement on that one connection, one log event
    with used_tenant_pool=True, then (AA-859 wave 4) with ANGLE_GATE_READ_FLAG on too the response re-read on the
    SAME connection inside the same transaction (no second acquire that could 503 after a saved choice); in a
    mixed flag state the re-read is a second, sequential block, only after the decide block was released;
  * flag off: the main pool with the route's own transaction, used_tenant_pool=False;
  * flag on + no tenant pool / GUC not applied / no tenant connection: 503, the main pool is never touched
    and nothing is re-read;
  * a Redis error falls back to the main pool loudly; a query error is not "pool unavailable" and rolls the
    tenant transaction back; domain errors (404 / 409 / 422) roll it back too and skip the re-read;
  * a malformed JWT sub fails at the route's UUID() parse before any pool is touched (old behaviour).
"""
import asyncio
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg
import pytest
from fastapi import HTTPException
from structlog.testing import capture_logs

from api.core import aa544_tenant_pool as tp
from api.routers import v1_angle_gate as ag
from services.acp_angle_gate import service

TENANT_ID = uuid4()
REQUEST_ID = uuid4()
FLAG = tp.ANGLE_GATE_DECIDE_FLAG
READ_FLAG = tp.ANGLE_GATE_READ_FLAG   # AA-859 wave 4: the re-read's own flag


# ── fakes ─────────────────────────────────────────────────────────────────────

def _ctx(value=None, enter_error=None, events=None, name=None):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=value, side_effect=enter_error)

    async def _exit(*exc):
        if events is not None:
            events.append(f"{name}_released")
        return False
    ctx.__aexit__ = AsyncMock(side_effect=_exit)
    return ctx


class _DecideConn:
    """A connection whose fetchval answers set_config (echo, or `guc_echo`) and the decision SQL: the locked
    status, the option check, reopen's RETURNING and status re-read."""

    def __init__(self, status="pending_choice", guc_echo=None, option_exists=True, error=None):
        self.status, self.guc_echo, self.option_exists, self.error = status, guc_echo, option_exists, error
        self.tx = _ctx()
        self.transaction = MagicMock(return_value=self.tx)
        self.sql = []
        self.execute = AsyncMock(side_effect=self._execute)
        self.fetchval = AsyncMock(side_effect=self._fetchval)

    async def _fetchval(self, sql, *args):
        if "set_config('app.tenant_id'" in sql:
            return args[0] if self.guc_echo is None else self.guc_echo
        self.sql.append(sql)
        if self.error is not None:
            raise self.error
        if sql in (service._CHOOSE_LOCK_SQL, service._REQUEST_STATUS_SQL):
            return self.status
        if sql == service._OPTION_EXISTS_SQL:
            return 1 if self.option_exists else None
        if sql == service._REOPEN_SQL:
            return args[0] if self.status == "approved" else None
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def _execute(self, sql, *args):
        self.sql.append(sql)
        if sql == service._OPTION_CHOOSE_SQL:
            return "UPDATE 3"
        if sql == service._REQUEST_APPROVE_SQL:
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute: {sql}")


def _request(flag, tenant_conn=None, main_conn=None, tenant_pool=True, acquire_error=None, events=None):
    request = MagicMock()
    request.app.state.redis.get = AsyncMock(
        side_effect=lambda key: "true" if flag and key in (FLAG, READ_FLAG) else "false")
    main = MagicMock()
    main.acquire = MagicMock(return_value=_ctx(main_conn or _DecideConn(), events=events, name="main"))
    request.app.state.pool = main
    if tenant_pool:
        tenant = MagicMock()
        tenant.acquire = MagicMock(return_value=_ctx(tenant_conn or _DecideConn(), enter_error=acquire_error,
                                                     events=events, name="tenant"))
        request.app.state.tenant_pool = tenant
    else:
        request.app.state.tenant_pool = None
    return request


def _fetch_request_stub(events=None):
    """Stands in for service.fetch_request_on (the re-read's reads on the block's connection)."""
    async def _fetch(conn, tenant_id, request_id):
        if events is not None:
            events.append("fetch_request")
        return {"request_id": str(request_id), "tenant_id": str(tenant_id), "status": "re-read", "goal": "g"}
    return AsyncMock(side_effect=_fetch)


@contextmanager
def _reread(stub):
    """Patch the wave-4 re-read: fetch_request_on stubbed, the job-status step a pass-through."""
    async def _passthrough(result, pool):
        return result
    with patch.object(service, "fetch_request_on", new=stub), \
            patch.object(service, "with_generation_status", new=AsyncMock(side_effect=_passthrough)):
        yield stub


async def _choose(request, sub=None, idx=1):
    sub = str(TENANT_ID) if sub is None else sub
    return await ag.choose(REQUEST_ID, ag.ChooseBody(idx=idx), request, tenant={"sub": sub})


async def _reopen(request, sub=None):
    return await ag.reopen(REQUEST_ID, request, tenant={"sub": str(TENANT_ID) if sub is None else sub})


# (call, status the request is in for a success, decision SQL in order, log event)
ROUTES = [
    pytest.param(_choose, "pending_choice",
                 [service._CHOOSE_LOCK_SQL, service._OPTION_EXISTS_SQL, service._OPTION_CHOOSE_SQL,
                  service._REQUEST_APPROVE_SQL], "aa859_angle_gate_choose", id="choose"),
    pytest.param(_reopen, "approved", [service._REOPEN_SQL], "aa859_angle_gate_reopen", id="reopen"),
]


# ── per-route fail closed ─────────────────────────────────────────────────────

@pytest.mark.parametrize("call,status,decision_sql,event", ROUTES)
@pytest.mark.asyncio
async def test_flag_on_decides_and_rereads_on_one_tenant_connection(call, status, decision_sql, event):
    events = []
    tenant = _DecideConn(status=status)
    request = _request(flag=True, tenant_conn=tenant, events=events)
    with capture_logs() as logs, _reread(_fetch_request_stub(events)) as fetch:
        result = await call(request)
    assert result["status"] == "re-read"
    assert tenant.sql == decision_sql, "every decision statement ran on the one tenant connection, in order"
    assert request.app.state.tenant_pool.acquire.call_args.kwargs == {"timeout": tp.TENANT_POOL_ACQUIRE_TIMEOUT_S}
    # one acquire, one transaction (acquire_scoped_conn's own, GUC LOCAL): decide + re-read; the route opens none
    request.app.state.tenant_pool.acquire.assert_called_once()
    assert tenant.transaction.call_count == 1
    assert tenant.tx.__aexit__.call_args[0][0] is None, "the tenant transaction committed"
    assert events == ["fetch_request", "tenant_released"], "the re-read runs before the decide connection is back"
    assert fetch.call_args[0] == (tenant, TENANT_ID, REQUEST_ID), "re-read on the decide's own connection"
    request.app.state.pool.acquire.assert_not_called()  # the job-status step is stubbed; nothing else uses MAIN
    lines = [entry for entry in logs if entry["event"] == event]
    assert len(lines) == 1 and lines[0]["used_tenant_pool"] is True
    assert lines[0]["request_id"] == str(REQUEST_ID) and lines[0]["tenant_id"] == str(TENANT_ID)
    timing = [entry for entry in logs if entry["event"] == "aa859_tenant_pool_timing"]
    assert [t["flag_key"] for t in timing] == [FLAG]
    reread = [entry for entry in logs if entry["event"] == "aa859_angle_gate_reread"]
    assert len(reread) == 1 and reread[0]["used_tenant_pool"] is True and reread[0]["same_conn"] is True


@pytest.mark.parametrize("call,status,decision_sql,event", ROUTES)
@pytest.mark.asyncio
async def test_saved_decision_never_waits_on_a_second_tenant_acquire(call, status, decision_sql, event):
    """Review S247: a second acquire for the re-read could time out (max 2 per task, 5 s) and answer 503 for a
    choice already saved (the portal's retry then gets 409). With both flags on there is no second acquire: a
    pool whose next acquire fails does not change the answer."""
    tenant = _DecideConn(status=status)
    request = _request(flag=True, tenant_conn=tenant)
    first = request.app.state.tenant_pool.acquire.return_value
    request.app.state.tenant_pool.acquire = MagicMock(
        side_effect=[first, _ctx(enter_error=asyncio.TimeoutError())])
    with capture_logs() as logs, _reread(_fetch_request_stub()):
        result = await call(request)
    assert result["status"] == "re-read"
    assert request.app.state.tenant_pool.acquire.call_count == 1
    assert tenant.tx.__aexit__.call_args[0][0] is None, "the decision committed"
    assert not [entry for entry in logs if entry["event"] == "aa817_tenant_pool_unavailable"]


@pytest.mark.asyncio
async def test_reread_error_rolls_the_decision_back():
    """Decide and re-read share one transaction: a failing re-read never leaves a saved choice behind an error."""
    tenant = _DecideConn(status="pending_choice")
    request = _request(flag=True, tenant_conn=tenant)
    stub = AsyncMock(side_effect=asyncpg.QueryCanceledError("statement timeout"))
    with _reread(stub), pytest.raises(asyncpg.QueryCanceledError):
        await _choose(request)
    assert issubclass(tenant.tx.__aexit__.call_args[0][0], asyncpg.QueryCanceledError)


@pytest.mark.asyncio
async def test_decide_flag_off_read_flag_on_rereads_in_its_own_tenant_block():
    """Mixed state (decide reverted, read on): the decision on MAIN in its own transaction, then the re-read on
    the tenant pool in a separate block."""
    events = []
    main = _DecideConn(status="pending_choice")
    request = _request(flag=True, main_conn=main, events=events)
    request.app.state.redis.get = AsyncMock(side_effect=lambda key: "true" if key == READ_FLAG else "false")
    with capture_logs() as logs, _reread(_fetch_request_stub(events)):
        await _choose(request)
    main.transaction.assert_called_once()
    assert events == ["main_released", "fetch_request", "tenant_released"]
    assert [e["used_tenant_pool"] for e in logs if e["event"] == "aa859_angle_gate_choose"] == [False]
    assert [e["used_tenant_pool"] for e in logs if e["event"] == "aa859_angle_gate_reread"] == [True]


@pytest.mark.parametrize("call,status,decision_sql,event", ROUTES)
@pytest.mark.asyncio
async def test_flag_off_decides_on_main_in_its_own_transaction(call, status, decision_sql, event):
    main = _DecideConn(status=status)
    request = _request(flag=False, main_conn=main)
    with capture_logs() as logs, _reread(_fetch_request_stub()):
        await call(request)
    request.app.state.tenant_pool.acquire.assert_not_called()
    assert main.sql == decision_sql
    main.transaction.assert_called_once()
    assert main.tx.__aexit__.call_args[0][0] is None, "the main-pool transaction committed"
    lines = [entry for entry in logs if entry["event"] == event]
    assert len(lines) == 1 and lines[0]["used_tenant_pool"] is False
    assert not [entry for entry in logs if entry["event"] == "aa859_tenant_pool_timing"]


@pytest.mark.parametrize("call,status,decision_sql,event", ROUTES)
@pytest.mark.asyncio
async def test_flag_on_without_tenant_pool_is_503(call, status, decision_sql, event):
    request = _request(flag=True, tenant_pool=False)
    with _reread(_fetch_request_stub()) as fetch, \
            pytest.raises(HTTPException) as exc:
        await call(request)
    assert exc.value.status_code == 503
    request.app.state.pool.acquire.assert_not_called()
    fetch.assert_not_called()


@pytest.mark.parametrize("call,status,decision_sql,event", ROUTES)
@pytest.mark.asyncio
async def test_flag_on_guc_not_applied_is_503(call, status, decision_sql, event):
    tenant = _DecideConn(status=status, guc_echo="")
    request = _request(flag=True, tenant_conn=tenant)
    with _reread(_fetch_request_stub()) as fetch, \
            pytest.raises(HTTPException) as exc:
        await call(request)
    assert exc.value.status_code == 503
    assert tenant.sql == [], "no decision statement without the tenant GUC"
    request.app.state.pool.acquire.assert_not_called()
    fetch.assert_not_called()


@pytest.mark.parametrize("error,reason", [
    (asyncpg.TooManyConnectionsError('too many connections for role "aa_app_user"'), "too_many_connections"),
    (asyncio.TimeoutError(), "acquire_timeout"),
    (ConnectionRefusedError(111, "refused"), "connect_error"),
    (asyncpg.InterfaceError("pool is closed"), "pool_closed"),
])
@pytest.mark.parametrize("call,status,decision_sql,event", ROUTES)
@pytest.mark.asyncio
async def test_flag_on_no_tenant_connection_is_503_with_metric_event(call, status, decision_sql, event, error,
                                                                       reason):
    request = _request(flag=True, acquire_error=error)
    with capture_logs() as logs, _reread(_fetch_request_stub()) as fetch, \
            pytest.raises(HTTPException) as exc:
        await call(request)
    assert exc.value.status_code == 503
    request.app.state.pool.acquire.assert_not_called()
    fetch.assert_not_called()
    unavailable = [entry for entry in logs if entry["event"] == "aa817_tenant_pool_unavailable"]
    assert len(unavailable) == 1 and unavailable[0]["reason"] == reason and unavailable[0]["flag_key"] == FLAG


@pytest.mark.parametrize("call,status,decision_sql,event", ROUTES)
@pytest.mark.asyncio
async def test_redis_error_keeps_tenant_pool_loudly(call, status, decision_sql, event):
    """S241 D-12: an unreadable flag is ON -- the decision runs on the tenant pool, never on MAIN."""
    tenant = _DecideConn(status=status)
    request = _request(flag=True, tenant_conn=tenant)
    request.app.state.redis.get = AsyncMock(side_effect=ConnectionError("redis down"))
    with capture_logs() as logs, _reread(_fetch_request_stub()):
        await call(request)
    # both flags read as ON: the decide and (AA-859 wave 4) the re-read on one tenant connection
    assert request.app.state.tenant_pool.acquire.call_count == 1
    request.app.state.pool.acquire.assert_not_called()
    failed = [entry for entry in logs if entry["event"] == "aa817_tenant_flag_read_failed"]
    assert failed and failed[0]["flag_key"] == FLAG and failed[0]["log_level"] == "error"
    assert failed[0]["fallback"] == "tenant_pool"
    assert [entry["used_tenant_pool"] for entry in logs if entry["event"] == event] == [True]


@pytest.mark.parametrize("call,status,decision_sql,event", ROUTES)
@pytest.mark.asyncio
async def test_query_error_propagates_and_rolls_back(call, status, decision_sql, event):
    """A missing grant (e.g. a 241 rollback with the flag still on) shows as the Postgres error, not a 503."""
    tenant = _DecideConn(status=status, error=asyncpg.InsufficientPrivilegeError("permission denied"))
    request = _request(flag=True, tenant_conn=tenant)
    with capture_logs() as logs, _reread(_fetch_request_stub()) as fetch, \
            pytest.raises(asyncpg.InsufficientPrivilegeError):
        await call(request)
    assert not [entry for entry in logs if entry["event"] == "aa817_tenant_pool_unavailable"]
    fetch.assert_not_called()


@pytest.mark.parametrize("bad_sub", ["not-a-uuid", "1", "' OR true --", ""])
@pytest.mark.parametrize("call,status,decision_sql,event", ROUTES)
@pytest.mark.asyncio
async def test_malformed_sub_fails_before_any_pool(call, status, decision_sql, event, bad_sub):
    request = _request(flag=True)
    with _reread(_fetch_request_stub()), pytest.raises(ValueError):
        await call(request, sub=bad_sub)
    request.app.state.tenant_pool.acquire.assert_not_called()
    request.app.state.pool.acquire.assert_not_called()


# ── domain errors inside the tenant transaction ───────────────────────────────

@pytest.mark.parametrize("call,conn_kwargs,status_code,error", [
    pytest.param(_choose, {"status": None}, 404, service.RequestNotFoundError, id="choose-not-found"),
    pytest.param(_choose, {"status": "approved"}, 409, service.WrongStatusError, id="choose-wrong-status"),
    pytest.param(_choose, {"option_exists": False}, 422, service.AngleGateError, id="choose-no-option"),
    pytest.param(_reopen, {"status": None}, 404, service.RequestNotFoundError, id="reopen-not-found"),
    pytest.param(_reopen, {"status": "reusable"}, 409, service.WrongStatusError, id="reopen-wrong-status"),
])
@pytest.mark.asyncio
async def test_domain_error_rolls_back_the_tenant_transaction(call, conn_kwargs, status_code, error):
    tenant = _DecideConn(**conn_kwargs)
    request = _request(flag=True, tenant_conn=tenant)
    with capture_logs() as logs, _reread(_fetch_request_stub()) as fetch, \
            pytest.raises(HTTPException) as exc:
        await call(request)
    assert exc.value.status_code == status_code
    fetch.assert_not_called()
    request.app.state.pool.acquire.assert_not_called()
    # the tenant transaction is acquire_scoped_conn's: it exited with the domain error (rollback)
    assert issubclass(tenant.tx.__aexit__.call_args[0][0], error)
    assert len([entry for entry in logs if entry["event"].startswith("aa859_angle_gate_")]) == 1


@pytest.mark.asyncio
async def test_choose_bad_idx_is_422_before_any_option_sql():
    tenant = _DecideConn()
    request = _request(flag=True, tenant_conn=tenant)
    with _reread(_fetch_request_stub()), pytest.raises(HTTPException) as exc:
        await _choose(request, idx=7)
    assert exc.value.status_code == 422
    assert tenant.sql == [service._CHOOSE_LOCK_SQL]


# ── SQL shape (the constants the integration tests mutate) ────────────────────

def test_every_decision_statement_keeps_its_tenant_filter():
    for sql in (service._CHOOSE_LOCK_SQL, service._REQUEST_APPROVE_SQL, service._REOPEN_SQL,
                service._REQUEST_STATUS_SQL):
        assert "tenant_id = $2::uuid" in sql
    # The option statements (no tenant_id column) filter through the parent request, so on the main pool they
    # do not depend on running after the lock step to stay inside the tenant.
    assert "r.tenant_id = $3::uuid" in service._OPTION_TENANT_FILTER
    for sql in (service._OPTION_EXISTS_SQL, service._OPTION_CHOOSE_SQL):
        assert sql.endswith(f"AND {service._OPTION_TENANT_FILTER}")
    assert service._CHOOSE_LOCK_SQL.rstrip().endswith("FOR UPDATE"), "the request row is locked first"
    assert "status IN ('pending_choice', 'reusable')" in service._REQUEST_APPROVE_SQL
    assert "AND status = 'approved'" in service._REOPEN_SQL and "RETURNING request_id" in service._REOPEN_SQL


def test_option_write_touches_only_the_granted_column():
    """241 grants aa_app_user UPDATE (chosen) only on angle_gate_option."""
    sql = service._OPTION_CHOOSE_SQL
    assert sql.startswith("UPDATE acp_shared.angle_gate_option SET chosen = (idx = $2) WHERE")
    set_clause = sql.split("SET", 1)[1].split("WHERE", 1)[0]
    assert set_clause.strip() == "chosen = (idx = $2)" and "," not in set_clause


def test_route_uses_its_own_flag():
    assert FLAG == "aa859:angle_gate_decide_pool"
    assert READ_FLAG == "aa859:angle_gate_read_pool"


@pytest.mark.asyncio
async def test_reread_flag_off_keeps_the_decision_on_the_tenant_pool():
    """Each flag reverts on its own: the re-read on MAIN (READ_FLAG false) does not move the decision."""
    tenant = _DecideConn(status="pending_choice")
    request = _request(flag=True, tenant_conn=tenant)
    request.app.state.redis.get = AsyncMock(side_effect=lambda key: "true" if key == FLAG else "false")
    with capture_logs() as logs, _reread(_fetch_request_stub()):
        await _choose(request)
    request.app.state.tenant_pool.acquire.assert_called_once()
    request.app.state.pool.acquire.assert_called_once()
    assert [e["used_tenant_pool"] for e in logs if e["event"] == "aa859_angle_gate_choose"] == [True]
    assert [e["used_tenant_pool"] for e in logs if e["event"] == "aa859_angle_gate_reread"] == [False]
