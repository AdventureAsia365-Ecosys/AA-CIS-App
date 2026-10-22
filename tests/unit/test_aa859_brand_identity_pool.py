"""AA-859 wave 3 — the tenant branch of GET / POST /admin/brand-identity on the tenant pool behind
BRAND_IDENTITY_FLAG (needs CIS 262).

- Tenant Bearer: the fail-closed matrix (flag on = tenant pool, 503 on every "no tenant context / connection"
  case, never the admin pool; explicit OFF = admin pool), one `aa859_brand_identity` line per request.
- POST on the tenant pool: one transaction (acquire_scoped_conn's), the advisory lock first, then MAX + 1,
  deactivate, INSERT, markets upsert; a duplicate version is a 409, not a 500; check_saved_cta (notify_admins)
  runs on MAIN after the tenant connection was released. Flag off: the admin pool in its own transaction, no lock.
- X-Admin-Secret staff branch (AA master brand): never reads the flag, never touches the tenant pool, logs nothing.
"""
import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
from fastapi import HTTPException
from structlog.testing import capture_logs

from api.core import aa544_tenant_pool as tp
from api.routers import admin_pipeline as ap

TENANT_ID = str(uuid.uuid4())
EVENT = "aa859_brand_identity"


# ── fakes ─────────────────────────────────────────────────────────────────────

def _ctx(value=None, enter_error=None):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=value, side_effect=enter_error)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _conn(row=None, history=(), current_version=2, guc_echo=None, insert_error=None):
    conn = MagicMock()
    conn.tx = _ctx()
    conn.transaction = MagicMock(return_value=conn.tx)

    def fetchval(sql, *args):
        if "set_config" in sql:
            return args[0] if guc_echo is None else guc_echo
        return current_version
    conn.fetchval = AsyncMock(side_effect=fetchval)
    conn.fetchrow = AsyncMock(return_value=row)
    conn.fetch = AsyncMock(return_value=list(history))

    async def execute(sql, *args):
        if insert_error is not None and "INSERT INTO shared.tenant_brand_rules" in sql:
            raise insert_error
        return "OK"
    conn.execute = AsyncMock(side_effect=execute)
    return conn


def _request(flag, tenant_conn=None, main_conn=None, tenant_pool=True, acquire_error=None):
    request = MagicMock()
    request.app.state.redis.get = AsyncMock(
        side_effect=lambda key: "true" if flag and key == tp.BRAND_IDENTITY_FLAG else "false")
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


def _brand_row(**over):
    row = {"system_prompt": "p", "style_guide": "s", "forbidden_words": "[]", "version": 2, "updated_at": None,
           "created_at": None, "brand_name": "Terra", "brand_type": "", "core_idea": "", "customer_segment": "",
           "customer_mindset": "", "voice_examples": "[]", "good_examples": "", "target_markets": ["UK"],
           "default_cta": "", "is_active": True}
    row.update(over)
    return row


@pytest.fixture(autouse=True)
def _no_cta_notice():
    """check_saved_cta reads / writes notifications on MAIN; patched to record when it runs."""
    with patch("services.acp_shared.cta_review_notice.check_saved_cta", AsyncMock(return_value=None)) as m:
        yield m


async def _get(request, tenant_id, tenant_caller=True):
    return await ap.get_brand_identity(request, tenant_id=tenant_id, tenant_caller=tenant_caller)


async def _post(request, tenant_id, tenant_caller=True, **body):
    body = ap.BrandIdentityUpdate(**({"system_prompt": "Warm, plain words.", "target_markets": ["UK"]} | body))
    return await ap.update_brand_identity(body, request, tenant_id=tenant_id, tenant_caller=tenant_caller)


ROUTES = [
    pytest.param(_get, "get", id="GET /admin/brand-identity (tenant)"),
    pytest.param(_post, "post", id="POST /admin/brand-identity (tenant)"),
]


def _lines(logs, route):
    return [e for e in logs if e["event"] == EVENT and e["route"] == route]


def _execs(conn):
    return [c.args for c in conn.execute.call_args_list]


# ── who is a tenant caller ────────────────────────────────────────────────────

def test_caller_is_tenant_only_with_a_bearer():
    assert ap._brand_caller_is_tenant(SimpleNamespace(credentials="tok")) is True
    assert ap._brand_caller_is_tenant(None) is False


# ── fail-closed matrix (tenant Bearer) ────────────────────────────────────────

@pytest.mark.parametrize("call,route", ROUTES)
@pytest.mark.asyncio
async def test_flag_on_runs_on_tenant_pool_and_logs_once(call, route):
    tenant = _conn(row=_brand_row(), history=[_brand_row()])
    request = _request(flag=True, tenant_conn=tenant)
    with capture_logs() as logs:
        await call(request, TENANT_ID)
    request.app.state.pool.acquire.assert_not_called()
    assert request.app.state.tenant_pool.acquire.call_args.kwargs == {"timeout": tp.TENANT_POOL_ACQUIRE_TIMEOUT_S}
    assert request.app.state.tenant_pool.acquire.call_count == 1, "one tenant block per request"
    lines = _lines(logs, route)
    assert len(lines) == 1 and lines[0]["used_tenant_pool"] is True
    timing = [e for e in logs if e["event"] == "aa859_tenant_pool_timing"]
    assert len(timing) == 1 and timing[0]["flag_key"] == tp.BRAND_IDENTITY_FLAG


@pytest.mark.parametrize("call,route", ROUTES)
@pytest.mark.asyncio
async def test_flag_off_stays_on_admin_pool_and_logs_it(call, route):
    request = _request(flag=False, main_conn=_conn(row=_brand_row()))
    with capture_logs() as logs:
        await call(request, TENANT_ID)
    request.app.state.tenant_pool.acquire.assert_not_called()
    lines = _lines(logs, route)
    assert len(lines) == 1 and lines[0]["used_tenant_pool"] is False


@pytest.mark.parametrize("call,route", ROUTES)
@pytest.mark.asyncio
async def test_flag_on_without_tenant_pool_is_503(call, route):
    request = _request(flag=True, tenant_pool=False)
    with pytest.raises(HTTPException) as exc:
        await call(request, TENANT_ID)
    assert exc.value.status_code == 503
    request.app.state.pool.acquire.assert_not_called()


@pytest.mark.parametrize("bad_id", ["not-a-uuid", "t-1", "{12345678-1234-5678-1234-567812345678}"])
@pytest.mark.parametrize("call,route", ROUTES)
@pytest.mark.asyncio
async def test_flag_on_malformed_tenant_id_is_503_before_any_acquire(call, route, bad_id):
    request = _request(flag=True)
    with pytest.raises(HTTPException) as exc:
        await call(request, bad_id)
    assert exc.value.status_code == 503
    request.app.state.tenant_pool.acquire.assert_not_called()
    request.app.state.pool.acquire.assert_not_called()


@pytest.mark.parametrize("call,route", ROUTES)
@pytest.mark.asyncio
async def test_flag_on_guc_not_applied_is_503(call, route):
    tenant = _conn(row=_brand_row(), guc_echo="")
    request = _request(flag=True, tenant_conn=tenant)
    with pytest.raises(HTTPException) as exc:
        await call(request, TENANT_ID)
    assert exc.value.status_code == 503
    tenant.fetchrow.assert_not_awaited()
    tenant.execute.assert_not_awaited()
    request.app.state.pool.acquire.assert_not_called()


@pytest.mark.parametrize("error,reason", [
    (asyncpg.TooManyConnectionsError('too many connections for role "aa_app_user"'), "too_many_connections"),
    (asyncio.TimeoutError(), "acquire_timeout"),
])
@pytest.mark.parametrize("call,route", ROUTES)
@pytest.mark.asyncio
async def test_flag_on_no_tenant_connection_is_503_with_metric_event(call, route, error, reason):
    request = _request(flag=True, acquire_error=error)
    with capture_logs() as logs, pytest.raises(HTTPException) as exc:
        await call(request, TENANT_ID)
    assert exc.value.status_code == 503
    request.app.state.pool.acquire.assert_not_called()
    events = [e for e in logs if e["event"] == "aa817_tenant_pool_unavailable"]
    assert len(events) == 1 and events[0]["reason"] == reason and events[0]["flag_key"] == tp.BRAND_IDENTITY_FLAG


@pytest.mark.parametrize("call,route", ROUTES)
@pytest.mark.asyncio
async def test_redis_error_or_missing_key_keeps_tenant_pool(call, route):
    """S241 D-12: an unreadable flag or a missing key is ON -- the tenant pool, never the admin pool."""
    for get in (AsyncMock(side_effect=ConnectionError("redis down")), AsyncMock(return_value=None)):
        request = _request(flag=True, tenant_conn=_conn(row=_brand_row()))
        request.app.state.redis.get = get
        with capture_logs() as logs:
            await call(request, TENANT_ID)
        request.app.state.pool.acquire.assert_not_called()
        assert _lines(logs, route)[0]["used_tenant_pool"] is True


# ── GET ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_reads_active_row_and_history_with_the_tenant_condition():
    tenant = _conn(row=_brand_row(), history=[_brand_row(), _brand_row(version=1, is_active=False)])
    out = await _get(_request(flag=True, tenant_conn=tenant), TENANT_ID)
    assert out["configured"] is True and out["version"] == 2 and len(out["history"]) == 2
    sql, tid = tenant.fetchrow.call_args.args
    assert ap._BRAND_TENANT_COND in sql and "is_active = true" in sql and tid == TENANT_ID
    assert ap._BRAND_TENANT_COND in tenant.fetch.call_args.args[0]


@pytest.mark.asyncio
async def test_get_unconfigured_reads_no_history():
    tenant = _conn(row=None)
    with capture_logs() as logs:
        out = await _get(_request(flag=True, tenant_conn=tenant), TENANT_ID)
    assert out["configured"] is False
    tenant.fetch.assert_not_awaited()
    assert _lines(logs, "get")[0]["configured"] is False


# ── POST on the tenant pool ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_post_on_tenant_pool_locks_first_and_writes_in_one_transaction(_no_cta_notice):
    tenant = _conn(current_version=2)
    request = _request(flag=True, tenant_conn=tenant)
    tenant_ctx = request.app.state.tenant_pool.acquire.return_value
    released_before_cta = []
    _no_cta_notice.side_effect = lambda *a, **k: released_before_cta.append(tenant_ctx.__aexit__.await_count)
    out = await _post(request, TENANT_ID)
    assert out["version"] == 3 and out["markets"]["countries"] == ["UK"]
    assert tenant.transaction.call_count == 1, "only acquire_scoped_conn's transaction (no nested one)"
    execs = _execs(tenant)
    assert execs[0] == (ap._BRAND_LOCK_SQL, ap.AA859_BRAND_LOCK_CLASS, TENANT_ID), "advisory lock first"
    assert [a[0] for a in execs[1:]] == [ap._BRAND_DEACTIVATE_SQL, ap._BRAND_INSERT_SQL,
                                         ap._BRAND_MARKETS_UPSERT_SQL]
    assert tenant.fetchval.call_args_list[-1].args == (ap._BRAND_MAX_VERSION_SQL, TENANT_ID)
    insert = execs[2]
    assert insert[1] == TENANT_ID and insert[5] == 3, "next version = MAX + 1 read under the lock"
    assert released_before_cta == [1], "check_saved_cta (notify_admins) on MAIN after the tenant block"
    request.app.state.pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_post_duplicate_version_on_tenant_pool_is_409_not_500():
    tenant = _conn(insert_error=asyncpg.UniqueViolationError("uq_tenant_brand_name_version"))
    request = _request(flag=True, tenant_conn=tenant)
    with capture_logs() as logs, pytest.raises(HTTPException) as exc:
        await _post(request, TENANT_ID)
    assert exc.value.status_code == 409 and exc.value.detail == ap.BRAND_VERSION_CONFLICT_DETAIL
    assert [e for e in logs if e["event"] == "aa859_brand_identity_version_conflict"]
    assert not any("tenant_seo_config" in a[0] for a in _execs(tenant)), "nothing after the failed INSERT"
    assert tenant.tx.__aexit__.call_args.args[0] is HTTPException, "the tenant transaction rolls back"


@pytest.mark.asyncio
async def test_post_flag_off_runs_the_old_statements_in_their_own_transaction():
    main = _conn(current_version=0)
    request = _request(flag=False, main_conn=main)
    out = await _post(request, TENANT_ID)
    assert out["version"] == 1
    assert main.transaction.call_count == 1, "flag off keeps the pre-AA-859 transaction"
    assert not any(a[0] == ap._BRAND_LOCK_SQL for a in _execs(main)), "no lock on the admin path"
    request.app.state.tenant_pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_post_flag_off_duplicate_version_still_raises_as_before():
    main = _conn(insert_error=asyncpg.UniqueViolationError("uq"))
    with pytest.raises(asyncpg.UniqueViolationError):
        await _post(_request(flag=False, main_conn=main), TENANT_ID)


# ── staff branch (X-Admin-Secret): main pool, no flag ─────────────────────────

@pytest.mark.parametrize("call", [_get, _post])
@pytest.mark.asyncio
async def test_staff_branch_never_reads_the_flag_nor_the_tenant_pool(call):
    main = _conn(row=_brand_row())
    request = _request(flag=True, main_conn=main)
    with capture_logs() as logs:
        await call(request, ap._AA_INTERNAL_TENANT_ID, tenant_caller=False)
    request.app.state.redis.get.assert_not_awaited()
    request.app.state.tenant_pool.acquire.assert_not_called()
    assert request.app.state.pool.acquire.call_count == 1
    assert not [e for e in logs if e["event"] == EVENT]


@pytest.mark.asyncio
async def test_direct_call_without_the_dependency_is_the_staff_path():
    """Existing callers / tests pass tenant_id only: the Depends default is not True, so the main pool."""
    main = _conn(row=_brand_row())
    request = _request(flag=True, main_conn=main)
    await ap.get_brand_identity(request, tenant_id=TENANT_ID)
    request.app.state.tenant_pool.acquire.assert_not_called()
    request.app.state.redis.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_staff_post_keeps_aa_internal_markets_out_of_seo_config():
    main = _conn()
    await _post(_request(flag=True, main_conn=main), ap._AA_INTERNAL_TENANT_ID, tenant_caller=False)
    assert not any("tenant_seo_config" in a[0] for a in _execs(main))


# ── flag ownership ────────────────────────────────────────────────────────────

def test_brand_identity_flag_is_used_only_by_the_two_brand_identity_handlers():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(ap))
    users = {fn.name for fn in ast.walk(tree) if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
             and any(isinstance(n, ast.Name) and n.id == "BRAND_IDENTITY_FLAG" for n in ast.walk(fn))}
    assert users == {"get_brand_identity", "update_brand_identity"}
