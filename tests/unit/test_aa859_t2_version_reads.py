"""AA-859 W5 (PR-D5a) — T2 version reads on the tenant pool behind T2_VERSION_READS_FLAG.

Routes: GET /v1/tours/pool (browse_pool), GET /v1/tours/my-versions (list_my_versions),
GET /v1/tours/versions/{id} (get_version).

Same fail-closed matrix as test_aa817_fail_closed_routes.py (kept in its own file so the parallel AA-859
PRs do not conflict on ROUTES), plus: flag off runs the pre-AA-859 SQL text (LEFT JOIN shared.job, admin
pool); on the tenant pool shared.job is never read (aa_app_user has no SELECT on it after 241) and the job
status comes from one MAIN query keyed by the job ids the tenant rows returned, after the tenant
connection was released (plan D2).
"""
import asyncio
import re
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from fastapi import HTTPException
from structlog.testing import capture_logs

from api.core import aa544_tenant_pool as tp
from api.routers import v1_tours

TENANT_ID = str(uuid.uuid4())
VERSION_ID = str(uuid.uuid4())
PT_ID = uuid.uuid4()
JOB_A, JOB_B = uuid.uuid4(), uuid.uuid4()
NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)
EVENT = "aa859_t2_version_read"


# ── fakes ─────────────────────────────────────────────────────────────────────

def _ctx(value=None, enter_error=None):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=value, side_effect=enter_error)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _conn(rows=(), row=None, total=1, guc_echo=None):
    conn = MagicMock()
    conn.tx = _ctx()
    conn.transaction = MagicMock(return_value=conn.tx)

    def fetchval(sql, *args):
        if "set_config" in sql:
            return args[0] if guc_echo is None else guc_echo
        return total
    conn.fetchval = AsyncMock(side_effect=fetchval)
    conn.fetch = AsyncMock(return_value=list(rows))
    conn.fetchrow = AsyncMock(return_value=row)
    return conn


def _request(flag, tenant_conn=None, main_conn=None, tenant_pool=True, acquire_error=None):
    request = MagicMock()
    request.app.state.redis.get = AsyncMock(
        side_effect=lambda key: "true" if flag and key == tp.T2_VERSION_READS_FLAG else "false")
    main = MagicMock()
    main.acquire = MagicMock(return_value=_ctx(main_conn or _conn()))
    request.app.state.pool = main
    if tenant_pool:
        tenant = MagicMock()
        tenant.acquire = MagicMock(return_value=_ctx(tenant_conn or _conn(), enter_error=acquire_error))
        request.app.state.tenant_pool = tenant
    else:
        request.app.state.tenant_pool = None
    return request


def _version_row(job_id=JOB_A, **over):
    row = {"id": uuid.UUID(VERSION_ID), "tenant_id": uuid.UUID(TENANT_ID), "published_tour_id": PT_ID,
           "version_number": 1, "status": "ai_generated", "edit_source": "ai_generated",
           "rewrite_language": "en-US", "created_at": NOW, "rewritten_content": {}, "job_id": job_id,
           "tour_id": uuid.uuid4(), "aa_name": "Ha Long", "country": "Vietnam", "job_status": None}
    row.update(over)
    return row


def _my_row(job_id):
    return {"id": uuid.uuid4(), "version_number": 1, "status": "ai_generated", "edit_source": "ai_generated",
            "rewrite_language": "en-US", "created_at": NOW, "rewritten_content": {},
            "published_tour_id": PT_ID, "tour_id": uuid.uuid4(), "aa_name": "Ha Long", "country": "Vietnam",
            "duration": "3 days", "_job_id": job_id, "job_status": None}


def _tenant(tenant_id):
    return {"sub": tenant_id}


async def _call_pool(request, tenant_id):
    return await v1_tours.browse_pool(request, _tenant(tenant_id), page=1, page_size=20, country=None,
                                      search=None, duration_min=None, duration_max=None, sort="newest")


async def _call_my_versions(request, tenant_id):
    return await v1_tours.list_my_versions(request, _tenant(tenant_id), page=1, page_size=20, status=None,
                                           country=None)


async def _call_version(request, tenant_id):
    return await v1_tours.get_version(VERSION_ID, request, _tenant(tenant_id))


ROUTES = [
    pytest.param(_call_pool, lambda: None, "pool", id="GET /v1/tours/pool"),
    pytest.param(_call_my_versions, lambda: None, "my_versions", id="GET /v1/tours/my-versions"),
    pytest.param(_call_version, lambda: _version_row(job_id=None), "version", id="GET /v1/tours/versions/{id}"),
]


def _lines(logs, route):
    return [e for e in logs if e["event"] == EVENT and e["route"] == route]


def _sqls(conn):
    return [c.args[0] for m in (conn.fetch, conn.fetchrow, conn.fetchval) for c in m.call_args_list]


# ── fail-closed matrix ────────────────────────────────────────────────────────

@pytest.mark.parametrize("call,row,route", ROUTES)
@pytest.mark.asyncio
async def test_flag_on_runs_on_tenant_pool_and_logs_once(call, row, route):
    tenant = _conn(row=row())
    request = _request(flag=True, tenant_conn=tenant)
    with capture_logs() as logs:
        await call(request, TENANT_ID)
    request.app.state.pool.acquire.assert_not_called()  # no job id in these rows: no MAIN read either
    assert request.app.state.tenant_pool.acquire.call_args.kwargs == {"timeout": tp.TENANT_POOL_ACQUIRE_TIMEOUT_S}
    assert request.app.state.tenant_pool.acquire.call_count == 1, "one tenant block per request"
    assert any("set_config('app.tenant_id', $1, true)" in sql for sql in _sqls(tenant))
    assert not any("shared.job" in sql for sql in _sqls(tenant)), "aa_app_user has no SELECT on shared.job"
    lines = _lines(logs, route)
    assert len(lines) == 1 and lines[0]["used_tenant_pool"] is True


@pytest.mark.parametrize("call,row,route", ROUTES)
@pytest.mark.asyncio
async def test_flag_off_stays_on_admin_pool_and_logs_it(call, row, route):
    request = _request(flag=False, main_conn=_conn(row=row()))
    with capture_logs() as logs:
        await call(request, TENANT_ID)
    request.app.state.tenant_pool.acquire.assert_not_called()
    lines = _lines(logs, route)
    assert len(lines) == 1 and lines[0]["used_tenant_pool"] is False
    assert not [e for e in logs if e["event"] == "aa859_tenant_pool_timing"]


@pytest.mark.parametrize("call,row,route", ROUTES)
@pytest.mark.asyncio
async def test_flag_on_without_tenant_pool_is_503(call, row, route):
    request = _request(flag=True, tenant_pool=False)
    with pytest.raises(HTTPException) as exc:
        await call(request, TENANT_ID)
    assert exc.value.status_code == 503
    request.app.state.pool.acquire.assert_not_called()


@pytest.mark.parametrize("bad_id", [None, "", "not-a-uuid", "{12345678-1234-5678-1234-567812345678}"])
@pytest.mark.parametrize("call,row,route", ROUTES)
@pytest.mark.asyncio
async def test_flag_on_missing_or_malformed_tenant_id_is_503_before_any_acquire(call, row, route, bad_id):
    request = _request(flag=True)
    with pytest.raises(HTTPException) as exc:
        await call(request, bad_id)
    assert exc.value.status_code == 503
    request.app.state.tenant_pool.acquire.assert_not_called()
    request.app.state.pool.acquire.assert_not_called()


@pytest.mark.parametrize("call,row,route", ROUTES)
@pytest.mark.asyncio
async def test_flag_on_guc_not_applied_is_503(call, row, route):
    tenant = _conn(row=row(), guc_echo="")
    request = _request(flag=True, tenant_conn=tenant)
    with pytest.raises(HTTPException) as exc:
        await call(request, TENANT_ID)
    assert exc.value.status_code == 503
    tenant.fetch.assert_not_awaited()
    tenant.fetchrow.assert_not_awaited()
    request.app.state.pool.acquire.assert_not_called()


@pytest.mark.parametrize("error,reason", [
    (asyncpg.TooManyConnectionsError('too many connections for role "aa_app_user"'), "too_many_connections"),
    (asyncio.TimeoutError(), "acquire_timeout"),
])
@pytest.mark.parametrize("call,row,route", ROUTES)
@pytest.mark.asyncio
async def test_flag_on_no_tenant_connection_is_503_with_metric_event(call, row, route, error, reason):
    request = _request(flag=True, acquire_error=error)
    with capture_logs() as logs, pytest.raises(HTTPException) as exc:
        await call(request, TENANT_ID)
    assert exc.value.status_code == 503
    request.app.state.pool.acquire.assert_not_called()
    events = [e for e in logs if e["event"] == "aa817_tenant_pool_unavailable"]
    assert len(events) == 1 and events[0]["reason"] == reason
    assert events[0]["flag_key"] == tp.T2_VERSION_READS_FLAG


@pytest.mark.parametrize("call,row,route", ROUTES)
@pytest.mark.asyncio
async def test_redis_error_keeps_tenant_pool_loudly(call, row, route):
    """S241 D-12: an unreadable flag is ON -- the tenant pool, never the admin pool."""
    request = _request(flag=True, tenant_conn=_conn(row=row()))
    request.app.state.redis.get = AsyncMock(side_effect=ConnectionError("redis down"))
    with capture_logs() as logs:
        await call(request, TENANT_ID)
    request.app.state.pool.acquire.assert_not_called()
    failed = [e for e in logs if e["event"] == "aa817_tenant_flag_read_failed"]
    assert failed and failed[0]["fallback"] == "tenant_pool"
    assert _lines(logs, route)[0]["used_tenant_pool"] is True


@pytest.mark.parametrize("call,row,route", ROUTES)
@pytest.mark.asyncio
async def test_tenant_pool_logs_one_timing_line(call, row, route):
    request = _request(flag=True, tenant_conn=_conn(row=row()))
    with capture_logs() as logs:
        await call(request, TENANT_ID)
    timing = [e for e in logs if e["event"] == "aa859_tenant_pool_timing"]
    assert len(timing) == 1 and timing[0]["flag_key"] == tp.T2_VERSION_READS_FLAG


# ── flag off is the pre-AA-859 SQL ────────────────────────────────────────────

# The inline SQL of v1_tours.py before AA-859 (b478a18b), for the flag-off path of the two job-joined reads.
_OLD_MY_VERSIONS_ROWS = """
    SELECT ttv.id, ttv.version_number, ttv.status, ttv.edit_source, ttv.rewrite_language, ttv.created_at,
           ttv.rewritten_content, pt.id AS published_tour_id, pt.tour_id, pt.aa_name, rt.country, rt.duration,
           -- AA-652 — the rewrite job's status (queued/running/succeeded/failed/...),
           -- NULL for manual edits and pre-AA-652 rows. No job error text (tenant-safe).
           j.status AS job_status
    FROM gold_aa_internal.tenant_tour_versions ttv
    JOIN gold_aa_internal.published_tours pt ON pt.id = ttv.published_tour_id
    LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
    LEFT JOIN shared.job j ON j.id = ttv.job_id
    WHERE ttv.tenant_id = $1::uuid
    ORDER BY ttv.created_at DESC
    LIMIT $2 OFFSET $3
"""
_OLD_VERSION = """
    SELECT ttv.*, pt.tour_id, pt.aa_name, pt.aa_subtitle, pt.aa_summary,
           pt.aa_description, pt.aa_highlights, pt.aa_itineraries,
           pt.seo_title AS aa_seo_title, pt.seo_meta AS aa_seo_meta,
           rt.country, rt.duration, rt.price_raw, rt.inclusions, rt.exclusions,
           j.status AS job_status  -- AA-652
    FROM gold_aa_internal.tenant_tour_versions ttv
    JOIN gold_aa_internal.published_tours pt ON pt.id = ttv.published_tour_id
    LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
    LEFT JOIN shared.job j ON j.id = ttv.job_id
    WHERE ttv.id = $1::uuid AND ttv.tenant_id = $2::uuid
"""


def _norm(sql):
    return re.sub(r"\s+", " ", sql).strip()


@pytest.mark.asyncio
async def test_flag_off_runs_the_pre_aa859_sql():
    main = _conn(row=_version_row(job_status="succeeded"))
    await _call_my_versions(_request(flag=False, main_conn=main), TENANT_ID)
    await _call_version(_request(flag=False, main_conn=main), TENANT_ID)
    sqls = {_norm(s) for s in _sqls(main)}
    assert _norm(_OLD_MY_VERSIONS_ROWS) in sqls
    assert _norm(_OLD_VERSION) in sqls


# ── job status on MAIN, after the tenant block ────────────────────────────────

def _main_after_release(request, rows):
    """MAIN conn whose acquire records whether the tenant connection was already released."""
    main = _conn(rows=rows)
    tenant_ctx = request.app.state.tenant_pool.acquire.return_value
    seen = []

    def acquire():
        seen.append(tenant_ctx.__aexit__.await_count)
        return _ctx(main)
    request.app.state.pool.acquire = MagicMock(side_effect=acquire)
    return main, seen


@pytest.mark.asyncio
async def test_my_versions_job_status_is_one_main_query_keyed_by_rls_job_ids():
    tenant = _conn(rows=[_my_row(JOB_A), _my_row(None), _my_row(JOB_B), _my_row(JOB_A)], total=4)
    request = _request(flag=True, tenant_conn=tenant)
    main, seen = _main_after_release(request, [{"id": JOB_A, "status": "succeeded"},
                                               {"id": JOB_B, "status": "failed"}])
    out = await _call_my_versions(request, TENANT_ID)
    assert seen == [1], "MAIN acquired once, after the tenant connection was released"
    sql, ids = main.fetch.call_args.args
    assert sql == v1_tours._JOB_STATUS_SQL and ids == sorted({str(JOB_A), str(JOB_B)})
    assert [d["job_status"] for d in out["data"]] == ["succeeded", None, "failed", "succeeded"]
    flag_off = await _call_my_versions(_request(flag=False, main_conn=_conn(
        rows=[{k: v for k, v in _my_row(JOB_A).items() if k != "_job_id"}])), TENANT_ID)
    assert list(out["data"][0]) == list(flag_off["data"][0]), "same keys, same order as the joined form"


@pytest.mark.asyncio
async def test_version_job_status_is_read_on_main_after_the_tenant_block():
    history = [{"id": uuid.UUID(VERSION_ID), "version_number": 1, "status": "ai_generated",
                "edit_source": "ai_generated", "created_at": NOW}]
    tenant = _conn(row=_version_row(job_id=JOB_A), rows=history)
    request = _request(flag=True, tenant_conn=tenant)
    main, seen = _main_after_release(request, [{"id": JOB_A, "status": "running"}])
    out = await _call_version(request, TENANT_ID)
    assert seen == [1]
    assert main.fetch.call_args.args == (v1_tours._JOB_STATUS_SQL, [str(JOB_A)])
    assert out["job_status"] == "running" and out["job_id"] == JOB_A
    assert len(out["version_history"]) == 1


@pytest.mark.asyncio
async def test_version_not_found_on_tenant_pool_is_404_without_history_or_main():
    tenant = _conn(row=None)
    request = _request(flag=True, tenant_conn=tenant)
    with capture_logs() as logs, pytest.raises(HTTPException) as exc:
        await _call_version(request, TENANT_ID)
    assert exc.value.status_code == 404
    tenant.fetch.assert_not_awaited()
    request.app.state.pool.acquire.assert_not_called()
    assert _lines(logs, "version")[0]["found"] is False


# ── flag ownership: D5b must not ride on the D5a flag ─────────────────────────

def test_t2_version_reads_flag_is_used_only_by_the_three_d5a_handlers():
    """Review of PR-D5a: once this flag is verified and left on, any route added under it moves to the
    tenant pool the moment its deploy rolls out, with no flip step or live check of its own, and reverting
    it would also revert D5a. D5b (/{id}/full tenant branch, /{id}, /versions/{id}/export-docx) needs its
    own flag constant in aa544_tenant_pool.py; this test fails if it reuses T2_VERSION_READS_FLAG."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(v1_tours))
    users = {fn.name for fn in ast.walk(tree) if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
             and any(isinstance(n, ast.Name) and n.id == "T2_VERSION_READS_FLAG" for n in ast.walk(fn))}
    assert users == {"browse_pool", "list_my_versions", "get_version"}
