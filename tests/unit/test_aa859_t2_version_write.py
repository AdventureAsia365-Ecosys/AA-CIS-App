"""AA-859 PR-D6 (W6) — PATCH /v1/tours/versions/{id} on the tenant pool behind T2_VERSION_WRITE_FLAG.

The AA-817 fail-closed matrix (tests/unit/test_aa817_fail_closed_routes.py) run for this route through its
own parametrization (that file's ROUTES list is left to the PRs already in it, so sibling AA-859 PRs do not
conflict on it), plus the route's own rules: flag on takes the advisory lock before MAX + 1 and maps a
duplicate version number to 409; flag off runs the old statements (no lock, no transaction of its own, a
UniqueViolation still propagates); the removed PATCH /v1/tours/{tour_id}/approve is gone from the router.
Real-Postgres checks (RLS, forgotten WHERE, concurrent edits) are in
tests/integration/test_aa859_t2_version_write.py.
"""
import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest
from fastapi import HTTPException
from structlog.testing import capture_logs

import tests.unit.test_aa817_fail_closed_routes as fc
from api.core import aa544_tenant_pool as tp
from api.routers import v1_tours

VERSION_ID = str(uuid4())
PUBLISHED_TOUR_ID = uuid4()
EVENT = "aa859_t2_version_write"


def _version_row():
    return {"id": VERSION_ID, "published_tour_id": PUBLISHED_TOUR_ID, "version_number": 2,
            "rewritten_content": "{}"}


async def _call_approve(request, tenant_id):
    body = v1_tours.VersionActionRequest(action="approve")
    return await v1_tours.update_version(VERSION_ID, body, request,
                                         tenant={"sub": str(tenant_id) if tenant_id else ""})


ROUTE = [pytest.param(tp.T2_VERSION_WRITE_FLAG, _call_approve, _version_row, EVENT, id="t2_version_write")]
_MATRIX = [
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


@pytest.mark.parametrize("name", _MATRIX)
@pytest.mark.parametrize("flag_key,call,row,event", ROUTE)
@pytest.mark.asyncio
async def test_fail_closed_matrix(name, flag_key, call, row, event):
    await getattr(fc, name)(flag_key, call, row, event)


@pytest.mark.parametrize("error,reason", [
    (asyncpg.TooManyConnectionsError('too many connections for role "aa_app_user"'), "too_many_connections"),
    (asyncio.TimeoutError(), "acquire_timeout"),
    (ConnectionRefusedError(111, "refused"), "connect_error"),
    (asyncpg.InterfaceError("pool is closed"), "pool_closed"),
])
@pytest.mark.parametrize("flag_key,call,row,event", ROUTE)
@pytest.mark.asyncio
async def test_no_tenant_connection_is_503(flag_key, call, row, event, error, reason):
    await fc.test_flag_on_no_tenant_connection_is_503_with_metric_event(flag_key, call, row, event, error, reason)


@pytest.mark.parametrize("bad_id", ["not-a-uuid", "urn:uuid:12345678-1234-5678-1234-567812345678",
                                    "12345678-1234-5678-1234-56781234567A"])
@pytest.mark.parametrize("flag_key,call,row,event", ROUTE)
@pytest.mark.asyncio
async def test_malformed_tenant_id_is_503(flag_key, call, row, event, bad_id):
    await fc.test_flag_on_malformed_tenant_id_is_503_before_any_acquire(flag_key, call, row, event, bad_id)


# ── the route's own rules ─────────────────────────────────────────────────────

def _edit_conn(insert_error=None):
    """A connection whose INSERT returns a new id (or raises), recording every statement in order."""
    conn = fc._conn(fetchrow=_version_row())
    conn.calls = []
    new_id = uuid4()

    async def fetchval(sql, *args):
        conn.calls.append(sql)
        if "set_config('app.tenant_id'" in sql:
            return args[0]
        if sql is v1_tours._VERSION_NEXT_NUMBER_SQL:
            return 3
        if sql is v1_tours._VERSION_EDIT_INSERT_SQL:
            if insert_error:
                raise insert_error
            return new_id
        raise AssertionError(f"unexpected fetchval {sql!r}")

    async def execute(sql, *args):
        conn.calls.append(sql)
        return "SELECT 1"

    conn.fetchval = AsyncMock(side_effect=fetchval)
    conn.execute = AsyncMock(side_effect=execute)
    return conn


async def _edit(request):
    body = v1_tours.VersionActionRequest(action="edit", edited_content={"name": "x"})
    return await v1_tours.update_version(VERSION_ID, body, request, tenant={"sub": str(fc.TENANT_ID)})


@pytest.mark.asyncio
async def test_flag_on_edit_takes_the_lock_before_the_number():
    conn = _edit_conn()
    out = await _edit(fc._request(flag=True, tenant_conn=conn))
    assert out["status"] == "edited" and out["version_number"] == 3
    order = [c for c in conn.calls if c in (v1_tours._VERSION_EDIT_LOCK_SQL, v1_tours._VERSION_NEXT_NUMBER_SQL,
                                            v1_tours._VERSION_EDIT_INSERT_SQL)]
    assert order == [v1_tours._VERSION_EDIT_LOCK_SQL, v1_tours._VERSION_NEXT_NUMBER_SQL,
                     v1_tours._VERSION_EDIT_INSERT_SQL]
    lock_args = conn.execute.call_args_list[0].args
    assert lock_args == (v1_tours._VERSION_EDIT_LOCK_SQL, v1_tours.AA859_TTV_LOCK_CLASS, str(fc.TENANT_ID),
                         str(PUBLISHED_TOUR_ID))


def test_lock_is_the_two_int_form_and_the_number_has_no_locking_clause():
    """Two-int key space (never meets the one-bigint locks of t2_rewrite, slate, ...); FOR UPDATE on an
    aggregate is 0A000 in PostgreSQL."""
    assert "pg_advisory_xact_lock($1::int, hashtext(" in v1_tours._VERSION_EDIT_LOCK_SQL
    assert "FOR UPDATE" not in v1_tours._VERSION_NEXT_NUMBER_SQL.upper()
    assert isinstance(v1_tours.AA859_TTV_LOCK_CLASS, int) and -2**31 <= v1_tours.AA859_TTV_LOCK_CLASS < 2**31


def test_every_statement_filters_on_the_tenant():
    for sql in (v1_tours._VERSION_OWN_ROW_SQL, v1_tours._VERSION_NEXT_NUMBER_SQL):
        assert "tenant_id = $" in sql
    assert "AND tenant_id = $3::uuid" in v1_tours._VERSION_STATUS_UPDATE_SQL


@pytest.mark.asyncio
async def test_flag_on_duplicate_version_number_is_409_not_500():
    conn = _edit_conn(insert_error=asyncpg.UniqueViolationError("duplicate key value"))
    with capture_logs() as logs, pytest.raises(HTTPException) as exc:
        await _edit(fc._request(flag=True, tenant_conn=conn))
    assert exc.value.status_code == 409 and exc.value.detail == v1_tours.VERSION_CONFLICT_DETAIL
    assert exc.value.detail.startswith("version_conflict")
    assert [e for e in logs if e["event"] == "aa859_t2_version_conflict"]
    assert conn.tx.__aexit__.call_args[0][0] is HTTPException, "the tenant transaction exits (rolls back)"


@pytest.mark.asyncio
async def test_flag_off_edit_is_the_old_statements():
    """No advisory lock, no transaction of its own, and a duplicate number propagates as before."""
    conn = _edit_conn()
    out = await _edit(fc._request(flag=False, main_conn=conn))
    assert out["version_number"] == 3
    assert v1_tours._VERSION_EDIT_LOCK_SQL not in conn.calls
    conn.transaction.assert_not_called()

    conn = _edit_conn(insert_error=asyncpg.UniqueViolationError("duplicate key value"))
    with pytest.raises(asyncpg.UniqueViolationError):
        await _edit(fc._request(flag=False, main_conn=conn))


@pytest.mark.parametrize("flag", [True, False])
@pytest.mark.asyncio
async def test_foreign_or_missing_version_is_404(flag):
    conn = fc._conn(fetchrow=None)
    request = fc._request(flag=flag, tenant_conn=conn) if flag else fc._request(flag=False, main_conn=conn)
    with pytest.raises(HTTPException) as exc:
        await _call_approve(request, fc.TENANT_ID)
    assert exc.value.status_code == 404
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_bad_action_is_400_before_any_acquire():
    request = fc._request(flag=True)
    body = v1_tours.VersionActionRequest(action="delete")
    with pytest.raises(HTTPException) as exc:
        await v1_tours.update_version(VERSION_ID, body, request, tenant={"sub": str(fc.TENANT_ID)})
    assert exc.value.status_code == 400
    request.app.state.tenant_pool.acquire.assert_not_called()
    request.app.state.pool.acquire.assert_not_called()


def test_tour_approve_route_is_removed():
    """PATCH /v1/tours/{tour_id}/approve had no caller and updated 0 rows for every tenant (AA-859 PR-D6)."""
    paths = {(m, r.path) for r in v1_tours.router.routes for m in getattr(r, "methods", ())}
    assert ("PATCH", "/v1/tours/{tour_id}/approve") not in paths
    assert ("PATCH", "/v1/tours/versions/{version_id}") in paths
    assert not hasattr(v1_tours, "approve_tour_edit")
