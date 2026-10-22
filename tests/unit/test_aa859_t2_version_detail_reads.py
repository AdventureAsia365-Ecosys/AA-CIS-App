"""AA-859 D5b — the T2 detail reads on the tenant pool behind T2_VERSION_DETAIL_READS_FLAG.

Routes: GET /v1/tours/{id}/full (tenant-JWT branch; get_tour_full), GET /v1/tours/{id} (get_tour),
GET /v1/tours/versions/{id}/export-docx (export_version_docx).

Same fail-closed matrix as test_aa859_t2_version_reads.py (D5a), plus: flag off runs the pre-AA-859 SQL text
on the admin pool; the /full staff branch (X-Admin-Secret) never reads the flag and never touches the tenant
pool; the export-docx audit row is written on MAIN only after the tenant connection was released; D5b's flag
is its own (reverting it never reverts D5a, and the other way round).
"""
import asyncio
import re
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from fastapi import HTTPException
from structlog.testing import capture_logs

from api.core import aa544_tenant_pool as tp
from api.routers import v1_tours

TENANT_ID = str(uuid.uuid4())
TOUR_ID = str(uuid.uuid4())
VERSION_ID = str(uuid.uuid4())
RAW_TOUR_ID = uuid.uuid4()
NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)
EVENT = "aa859_t2_version_detail_read"
ADMIN_SECRET = "d5b-admin-secret"


# ── fakes ─────────────────────────────────────────────────────────────────────

def _ctx(value=None, enter_error=None):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=value, side_effect=enter_error)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _conn(row=None, guc_echo=None):
    conn = MagicMock()
    conn.tx = _ctx()
    conn.transaction = MagicMock(return_value=conn.tx)

    def fetchval(sql, *args):
        if "set_config" in sql:
            return args[0] if guc_echo is None else guc_echo
        return 1
    conn.fetchval = AsyncMock(side_effect=fetchval)
    conn.fetchrow = AsyncMock(return_value=row)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    return conn


def _request(flag, tenant_conn=None, main_conn=None, tenant_pool=True, acquire_error=None):
    request = MagicMock()
    request.app.state.redis.get = AsyncMock(
        side_effect=lambda key: "true" if flag and key == tp.T2_VERSION_DETAIL_READS_FLAG else "false")
    main = MagicMock()
    main.acquire = MagicMock(return_value=_ctx(main_conn or _conn()))
    main.execute = AsyncMock(return_value="INSERT 0 1")    # write_audit_log(pool, ...)
    request.app.state.pool = main
    if tenant_pool:
        tenant = MagicMock()
        tenant.acquire = MagicMock(return_value=_ctx(tenant_conn or _conn(), enter_error=acquire_error))
        request.app.state.tenant_pool = tenant
    else:
        request.app.state.tenant_pool = None
    return request


def _tour_row(**over):
    row = {"id": uuid.UUID(TOUR_ID), "tour_id": RAW_TOUR_ID, "tenant_id": uuid.UUID(v1_tours._AA_MASTER_TENANT),
           "aa_name": "Ha Long", "aa_subtitle": "Bay", "aa_summary": "S", "aa_highlights": "[]",
           "aa_itineraries": "Day 1", "aa_description": "D", "seo_title": "T", "seo_meta": "M",
           "seo_keywords_used": None, "published_at": NOW}
    row.update(over)
    return row


def _export_row(**over):
    row = {"version_number": 3, "rewritten_content": {"name": "Own name"}, "rewrite_language": "en-US",
           "tour_id": RAW_TOUR_ID, "aa_name": "Ha Long", "aa_subtitle": "Bay", "aa_summary": "S",
           "aa_highlights": "[\"h1\"]", "aa_itineraries": "Day 1", "country": "Vietnam", "duration": "3 days",
           "inclusions": "Boat", "exclusions": None}
    row.update(over)
    return row


@pytest.fixture(autouse=True)
def _tenant_jwt(monkeypatch):
    """The /full handler verifies the Bearer itself (it has two auth modes); here it maps to its `sub`."""
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setattr(v1_tours, "verify_tenant_jwt", lambda token: {"sub": token})


async def _call_full(request, tenant_id):
    return await v1_tours.get_tour_full(TOUR_ID, request, x_admin_secret=None,
                                        credentials=SimpleNamespace(credentials=tenant_id))


async def _call_tour(request, tenant_id):
    return await v1_tours.get_tour(TOUR_ID, request, {"sub": tenant_id})


async def _call_export(request, tenant_id):
    return await v1_tours.export_version_docx(VERSION_ID, request, {"sub": tenant_id})


ROUTES = [
    pytest.param(_call_full, _tour_row, "full", id="GET /v1/tours/{id}/full (tenant)"),
    pytest.param(_call_tour, _tour_row, "tour", id="GET /v1/tours/{id}"),
    pytest.param(_call_export, _export_row, "export_docx", id="GET /v1/tours/versions/{id}/export-docx"),
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
    request.app.state.pool.acquire.assert_not_called()
    assert request.app.state.tenant_pool.acquire.call_args.kwargs == {"timeout": tp.TENANT_POOL_ACQUIRE_TIMEOUT_S}
    assert request.app.state.tenant_pool.acquire.call_count == 1, "one tenant block per request"
    assert any("set_config('app.tenant_id', $1, true)" in sql for sql in _sqls(tenant))
    lines = _lines(logs, route)
    assert len(lines) == 1 and lines[0]["used_tenant_pool"] is True and lines[0]["found"] is True


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


@pytest.mark.parametrize("bad_id", ["not-a-uuid", "{12345678-1234-5678-1234-567812345678}",
                                    "abcdef12-1234-5678-1234-567812345678".upper()])
@pytest.mark.parametrize("call,row,route", ROUTES)
@pytest.mark.asyncio
async def test_flag_on_malformed_tenant_id_is_503_before_any_acquire(call, row, route, bad_id):
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
    assert events[0]["flag_key"] == tp.T2_VERSION_DETAIL_READS_FLAG


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
async def test_missing_flag_key_uses_tenant_pool(call, row, route):
    """S241 D-12: a newly wired route is ON from its first deploy (no key in Redis yet)."""
    request = _request(flag=True, tenant_conn=_conn(row=row()))
    request.app.state.redis.get = AsyncMock(return_value=None)
    with capture_logs() as logs:
        await call(request, TENANT_ID)
    request.app.state.pool.acquire.assert_not_called()
    assert _lines(logs, route)[0]["used_tenant_pool"] is True


@pytest.mark.parametrize("call,row,route", ROUTES)
@pytest.mark.asyncio
async def test_tenant_pool_logs_one_timing_line(call, row, route):
    request = _request(flag=True, tenant_conn=_conn(row=row()))
    with capture_logs() as logs:
        await call(request, TENANT_ID)
    timing = [e for e in logs if e["event"] == "aa859_tenant_pool_timing"]
    assert len(timing) == 1 and timing[0]["flag_key"] == tp.T2_VERSION_DETAIL_READS_FLAG


@pytest.mark.parametrize("call,row,route", ROUTES)
@pytest.mark.asyncio
async def test_not_found_on_tenant_pool_is_404_and_logged(call, row, route):
    tenant = _conn(row=None)
    request = _request(flag=True, tenant_conn=tenant)
    with capture_logs() as logs, pytest.raises(HTTPException) as exc:
        await call(request, TENANT_ID)
    assert exc.value.status_code == 404
    assert tenant.fetchrow.await_count == 1, "nothing else is read for a tour / version that is not the caller's"
    request.app.state.pool.acquire.assert_not_called()
    request.app.state.pool.execute.assert_not_awaited()
    assert _lines(logs, route)[0]["found"] is False


# ── /full: the tenant branch reads raw + seo in the same block; the staff branch is untouched ─────────

@pytest.mark.asyncio
async def test_full_tenant_branch_reads_raw_and_seo_in_the_tenant_block_and_projects_content_only():
    tenant = _conn()
    tenant.fetchrow = AsyncMock(side_effect=[_tour_row(), {"tour_id": RAW_TOUR_ID, "country": "Vietnam",
                                                           "duration": "3 days"},
                                             {"top_keywords": [], "keyword_search": "ha long",
                                              "keyword_ideas": [], "fetched_at": NOW}])
    request = _request(flag=True, tenant_conn=tenant)
    out = await _call_full(request, TENANT_ID)
    sqls = [c.args[0] for c in tenant.fetchrow.call_args_list]
    assert [("published_tours" in s, "raw_tours" in s, "seo_context" in s) for s in sqls] == [
        (True, False, False), (False, True, False), (False, False, True)]
    assert all(c.args[1] == RAW_TOUR_ID for c in tenant.fetchrow.call_args_list[1:])
    assert set(out) == {"published", "raw", "seo"}, "tenant JWT: no generated / quality (AA-796)"
    assert not any("generated_content" in s or "quality_scores" in s for s in sqls)
    assert out["seo"]["keyword_search"] == "ha long"


@pytest.mark.asyncio
async def test_full_staff_branch_never_reads_the_flag_nor_the_tenant_pool():
    main = _conn()
    main.fetchrow = AsyncMock(side_effect=[_tour_row(), {"tour_id": RAW_TOUR_ID}, {"id": uuid.uuid4()},
                                           {"score_overall": 9}, None])
    request = _request(flag=True, main_conn=main)
    with capture_logs() as logs:
        out = await v1_tours.get_tour_full(TOUR_ID, request, x_admin_secret=ADMIN_SECRET, credentials=None)
    request.app.state.redis.get.assert_not_awaited()
    request.app.state.tenant_pool.acquire.assert_not_called()
    assert set(out) == {"published", "raw", "generated", "quality", "seo"}
    assert main.fetchrow.call_args_list[0].args[2] == v1_tours._AA_MASTER_TENANT
    assert not [e for e in logs if e["event"] == EVENT], "the staff branch is not a tenant read"


@pytest.mark.asyncio
async def test_full_staff_branch_unknown_tour_is_404():
    request = _request(flag=True, main_conn=_conn(row=None))
    with pytest.raises(HTTPException) as exc:
        await v1_tours.get_tour_full(TOUR_ID, request, x_admin_secret=ADMIN_SECRET, credentials=None)
    assert exc.value.status_code == 404
    request.app.state.tenant_pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_full_without_credentials_is_401_before_any_pool():
    request = _request(flag=True)
    with pytest.raises(HTTPException) as exc:
        await v1_tours.get_tour_full(TOUR_ID, request, x_admin_secret=None, credentials=None)
    assert exc.value.status_code == 401
    request.app.state.tenant_pool.acquire.assert_not_called()
    request.app.state.pool.acquire.assert_not_called()


# ── export-docx: the audit row is written on MAIN after the tenant block ──────

@pytest.mark.asyncio
async def test_export_audit_is_written_on_main_after_the_tenant_connection_is_released():
    request = _request(flag=True, tenant_conn=_conn(row=_export_row()))
    tenant_ctx = request.app.state.tenant_pool.acquire.return_value
    seen = []

    async def audit_execute(sql, *args):
        seen.append(tenant_ctx.__aexit__.await_count)
        return "INSERT 0 1"
    request.app.state.pool.execute = AsyncMock(side_effect=audit_execute)
    resp = await _call_export(request, TENANT_ID)
    assert seen == [1], "audit_log written once, after the tenant connection was released"
    assert "tour_" in resp.headers["content-disposition"] and "_v3.docx" in resp.headers["content-disposition"]


@pytest.mark.asyncio
async def test_export_audit_failure_does_not_fail_the_download():
    request = _request(flag=True, tenant_conn=_conn(row=_export_row()))
    request.app.state.pool.execute = AsyncMock(side_effect=asyncpg.PostgresError("audit down"))
    resp = await _call_export(request, TENANT_ID)
    assert resp.status_code == 200


# ── flag off is the pre-AA-859 SQL ────────────────────────────────────────────

# The inline SQL of v1_tours.py before D5b (d6547175), for the flag-off path.
_OLD_TOUR_VIA_VERSION = """
    SELECT pt.* FROM gold_aa_internal.published_tours pt
    JOIN gold_aa_internal.tenant_tour_versions ttv
        ON ttv.published_tour_id = pt.id
    WHERE pt.id = $1::uuid AND ttv.tenant_id = $2::uuid
    LIMIT 1
"""
_OLD_RAW = "SELECT * FROM silver_aa_internal.raw_tours WHERE tour_id = $1::uuid"
_OLD_SEO = """
    SELECT top_keywords, keyword_search, keyword_ideas, fetched_at
    FROM silver_aa_internal.seo_context
    WHERE tour_id = $1::uuid
    ORDER BY fetched_at DESC LIMIT 1
"""
_OLD_EXPORT = """
    SELECT ttv.version_number, ttv.rewritten_content, ttv.rewrite_language,
           pt.tour_id, pt.aa_name, pt.aa_subtitle, pt.aa_summary, pt.aa_highlights,
           pt.aa_itineraries,
           rt.country, rt.duration, rt.inclusions, rt.exclusions
    FROM gold_aa_internal.tenant_tour_versions ttv
    JOIN gold_aa_internal.published_tours pt ON pt.id = ttv.published_tour_id
    LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
    WHERE ttv.id = $1::uuid AND ttv.tenant_id = $2::uuid
"""


def _norm(sql):
    return re.sub(r"\s+", " ", sql).strip()


@pytest.mark.asyncio
async def test_flag_off_runs_the_pre_aa859_sql():
    main = _conn()
    main.fetchrow = AsyncMock(side_effect=[_tour_row(), {"tour_id": RAW_TOUR_ID}, None, _tour_row(),
                                           _export_row()])
    await _call_full(_request(flag=False, main_conn=main), TENANT_ID)
    await _call_tour(_request(flag=False, main_conn=main), TENANT_ID)
    await _call_export(_request(flag=False, main_conn=main), TENANT_ID)
    sqls = [_norm(c.args[0]) for c in main.fetchrow.call_args_list]
    assert sqls == [_norm(_OLD_TOUR_VIA_VERSION), _norm(_OLD_RAW), _norm(_OLD_SEO), _norm(_OLD_TOUR_VIA_VERSION),
                    _norm(_OLD_EXPORT)]


# ── flag ownership ────────────────────────────────────────────────────────────

def _flag_users(flag_name):
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(v1_tours))
    return {fn.name for fn in ast.walk(tree) if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(isinstance(n, ast.Name) and n.id == flag_name for n in ast.walk(fn))}


def test_d5b_flag_is_used_only_by_the_three_d5b_handlers():
    """Each verified flag keeps its routes: a route added under it would move with no flip of its own."""
    assert _flag_users("T2_VERSION_DETAIL_READS_FLAG") == {"get_tour_full", "get_tour", "export_version_docx"}
    assert tp.T2_VERSION_DETAIL_READS_FLAG != tp.T2_VERSION_READS_FLAG
    assert tp.T2_VERSION_DETAIL_READS_FLAG in tp.ALL_TENANT_POOL_FLAGS
