"""AA-817 — tenant content-piece reads on the aa_app_user pool: acquire_scoped_conn(fail_closed=True)
and service.fetch_latest_piece_for_request(request=...).

Flag explicitly off (an OFF value, or the TENANT_POOL_FORCE_ADMIN_FLAGS env override): the admin pool,
exactly as before. Flag on -- "true", a missing key, or a flag that cannot be read (S241 D-12, fail
closed): the tenant pool inside one transaction with the GUC set LOCAL; no tenant pool, no tenant id, or
a GUC that did not take is a 503, never a fallback to the admin pool (which RLS does not filter).
fail_closed defaults to True; only an explicit fail_closed=False keeps the old fallback. One flag decision
per request (memoised on request.state).
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from structlog.testing import capture_logs

from api.core import aa544_tenant_pool as tp
from services.acp_content_writing import service

TENANT_ID = uuid4()
REQUEST_ID = uuid4()
OPTION_ID = uuid4()


def _ctx(value):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=value)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _pool(conn):
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_ctx(conn))
    return pool


def _conn(guc_echo=None):
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_ctx(None))
    conn.fetchval = AsyncMock(side_effect=lambda sql, value: value if guc_echo is None else guc_echo)
    return conn


def _request(flag, tenant_conn=None, main_conn=None, tenant_pool=True):
    request = MagicMock()
    request.app.state.redis.get = AsyncMock(return_value="true" if flag else "false")
    request.app.state.pool = _pool(main_conn or _conn())
    request.app.state.tenant_pool = _pool(tenant_conn or _conn()) if tenant_pool else None
    return request


async def _enter(request, tenant_id, **kw):
    async with tp.acquire_scoped_conn(request, tenant_id, tp.CONTENT_PIECE_READS_FLAG, **kw) as (conn, used):
        return conn, used


@pytest.mark.asyncio
async def test_flag_off_uses_admin_pool():
    main = _conn()
    request = _request(flag=False, main_conn=main)
    conn, used = await _enter(request, str(TENANT_ID), fail_closed=True)
    assert conn is main and used is False
    request.app.state.tenant_pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_flag_on_uses_tenant_pool_with_local_guc():
    tenant = _conn()
    request = _request(flag=True, tenant_conn=tenant)
    conn, used = await _enter(request, str(TENANT_ID), fail_closed=True)
    assert conn is tenant and used is True
    tenant.transaction.assert_called_once()
    sql, value = tenant.fetchval.call_args[0]
    assert "set_config('app.tenant_id', $1, true)" in sql and value == str(TENANT_ID)
    request.app.state.pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_flag_on_without_tenant_pool_is_503_not_admin_pool():
    request = _request(flag=True, tenant_pool=False)
    with pytest.raises(HTTPException) as exc:
        await _enter(request, str(TENANT_ID), fail_closed=True)
    assert exc.value.status_code == 503
    request.app.state.pool.acquire.assert_not_called()


@pytest.mark.parametrize("tenant_id", ["", None])
@pytest.mark.asyncio
async def test_flag_on_without_tenant_id_is_503(tenant_id):
    request = _request(flag=True)
    with pytest.raises(HTTPException) as exc:
        await _enter(request, tenant_id, fail_closed=True)
    assert exc.value.status_code == 503
    request.app.state.tenant_pool.acquire.assert_not_called()
    request.app.state.pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_guc_not_applied_is_503():
    request = _request(flag=True, tenant_conn=_conn(guc_echo=""))
    with pytest.raises(HTTPException) as exc:
        await _enter(request, str(TENANT_ID), fail_closed=True)
    assert exc.value.status_code == 503


# ── S241 decision D-12: the flag read fails closed ────────────────────────────────────────────────

async def _hang(_key):
    await asyncio.sleep(10)


@pytest.mark.parametrize("redis_value,expect_tenant_pool", [
    ("true", True),
    (None, True),              # missing key = ON
    ("false", False),          # the one explicit OFF value = the rollback
    ("FALSE", False),
    (" false ", False),
    (b"false", False),
    ("0", False),              # hand-typed OFF values accepted too (S241 review)
    ("off", False),
    (" NO ", False),
    ("garbage", True),         # unrecognised = ON (logged at ERROR)
    ("disabled", True),
    ("1", True),
])
@pytest.mark.asyncio
async def test_flag_value_encoding(redis_value, expect_tenant_pool):
    tenant, main = _conn(), _conn()
    request = _request(flag=True, tenant_conn=tenant, main_conn=main)
    request.app.state.redis.get = AsyncMock(return_value=redis_value)
    conn, used = await _enter(request, str(TENANT_ID), fail_closed=True)
    assert used is expect_tenant_pool
    assert conn is (tenant if expect_tenant_pool else main)


@pytest.mark.asyncio
async def test_redis_error_uses_tenant_pool_and_logs_read_failed():
    tenant = _conn()
    request = _request(flag=True, tenant_conn=tenant)
    request.app.state.redis.get = AsyncMock(side_effect=ConnectionError("redis down"))
    with capture_logs() as logs:
        conn, used = await _enter(request, str(TENANT_ID), fail_closed=True)
    assert conn is tenant and used is True
    request.app.state.pool.acquire.assert_not_called()
    failed = [e for e in logs if e["event"] == "aa817_tenant_flag_read_failed"]
    assert len(failed) == 1 and failed[0]["log_level"] == "error"
    assert failed[0]["fallback"] == "tenant_pool" and failed[0]["reason"] == "redis_error"
    assert failed[0]["flag_key"] == tp.CONTENT_PIECE_READS_FLAG


@pytest.mark.asyncio
async def test_redis_timeout_uses_tenant_pool(monkeypatch):
    monkeypatch.setattr(tp, "FLAG_READ_TIMEOUT_S", 0.01)
    tenant = _conn()
    request = _request(flag=True, tenant_conn=tenant)
    request.app.state.redis.get = _hang
    with capture_logs() as logs:
        conn, used = await _enter(request, str(TENANT_ID), fail_closed=True)
    assert conn is tenant and used is True
    request.app.state.pool.acquire.assert_not_called()
    failed = [e for e in logs if e["event"] == "aa817_tenant_flag_read_failed"]
    assert len(failed) == 1 and failed[0]["reason"] == "timeout"


@pytest.mark.asyncio
async def test_no_redis_client_uses_tenant_pool():
    tenant = _conn()
    request = _request(flag=True, tenant_conn=tenant)
    request.app.state.redis = None
    with capture_logs() as logs:
        conn, used = await _enter(request, str(TENANT_ID), fail_closed=True)
    assert conn is tenant and used is True
    failed = [e for e in logs if e["event"] == "aa817_tenant_flag_read_failed"]
    assert len(failed) == 1 and failed[0]["reason"] == "client_missing"


@pytest.mark.asyncio
async def test_missing_key_uses_tenant_pool_without_error_log():
    tenant = _conn()
    request = _request(flag=True, tenant_conn=tenant)
    request.app.state.redis.get = AsyncMock(return_value=None)
    with capture_logs() as logs:
        conn, used = await _enter(request, str(TENANT_ID), fail_closed=True)
    assert conn is tenant and used is True
    request.app.state.pool.acquire.assert_not_called()
    assert not [e for e in logs if e["event"].startswith("aa817_tenant_flag_")]


@pytest.mark.asyncio
async def test_unrecognised_value_is_on_and_logs_error():
    """Most likely a hand-typed rollback that did not take: ERROR, not WARNING."""
    request = _request(flag=True)
    request.app.state.redis.get = AsyncMock(return_value="ture")
    with capture_logs() as logs:
        _, used = await _enter(request, str(TENANT_ID), fail_closed=True)
    assert used is True
    logged = [e for e in logs if e["event"] == "aa817_tenant_flag_unrecognized"]
    assert len(logged) == 1 and logged[0]["log_level"] == "error" and logged[0]["treated_as"] == "on"
    assert logged[0]["off_values"] == sorted(tp.FLAG_OFF_VALUES)


@pytest.mark.asyncio
async def test_redis_error_without_tenant_pool_is_503_not_admin_pool():
    """Redis and the tenant pool both down: still never the admin pool."""
    request = _request(flag=True, tenant_pool=False)
    request.app.state.redis.get = AsyncMock(side_effect=ConnectionError("redis down"))
    with pytest.raises(HTTPException) as exc:
        await _enter(request, str(TENANT_ID), fail_closed=True)
    assert exc.value.status_code == 503
    request.app.state.pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_explicit_fail_open_keeps_admin_pool_fallback():
    """Only an explicit fail_closed=False (no production caller) still falls back on a missing tenant pool."""
    main = _conn()
    request = _request(flag=True, main_conn=main, tenant_pool=False)
    conn, used = await _enter(request, str(TENANT_ID), fail_closed=False)
    assert conn is main and used is False


@pytest.mark.asyncio
async def test_fail_closed_is_the_default():
    """A newly wired route that leaves out the keyword cannot reach the admin-pool fallback by omission."""
    request = _request(flag=True, tenant_pool=False)
    with pytest.raises(HTTPException) as exc:
        await _enter(request, str(TENANT_ID))
    assert exc.value.status_code == 503
    request.app.state.pool.acquire.assert_not_called()


# ── one decision per request (memoised on request.state) ─────────────────────

@pytest.mark.asyncio
async def test_flag_is_read_once_per_request_and_reused_by_later_blocks():
    """A rollback read in block 1 holds for the whole request even if Redis fails later (no pool switch
    after an irreversible side effect), with one Redis round-trip and no ERROR line."""
    main = _conn()
    request = _request(flag=False, main_conn=main)
    request.app.state.redis.get = AsyncMock(side_effect=["false", ConnectionError("blip"), ConnectionError("blip")])
    with capture_logs() as logs:
        for _ in range(3):
            conn, used = await _enter(request, str(TENANT_ID))
            assert conn is main and used is False
    request.app.state.redis.get.assert_awaited_once()
    request.app.state.tenant_pool.acquire.assert_not_called()
    assert not [e for e in logs if e["event"] == "aa817_tenant_flag_read_failed"]


@pytest.mark.asyncio
async def test_failed_read_is_logged_once_per_request():
    request = _request(flag=True)
    request.app.state.redis.get = AsyncMock(side_effect=ConnectionError("redis down"))
    with capture_logs() as logs:
        for _ in range(3):
            _, used = await _enter(request, str(TENANT_ID))
            assert used is True
    request.app.state.redis.get.assert_awaited_once()
    assert len([e for e in logs if e["event"] == "aa817_tenant_flag_read_failed"]) == 1


@pytest.mark.asyncio
async def test_cache_is_per_flag_and_per_request():
    request = _request(flag=True)
    request.app.state.redis.get = AsyncMock(side_effect=lambda key: "false" if key == tp.UNPUBLISH_FLAG else "true")
    assert await tp._flag_enabled(request, tp.UNPUBLISH_FLAG) is False
    assert await tp._flag_enabled(request, tp.CONTENT_PIECE_READS_FLAG) is True
    assert request.app.state.redis.get.await_count == 2
    other = _request(flag=True)  # a new request reads Redis again
    assert await tp._flag_enabled(other, tp.UNPUBLISH_FLAG) is True
    other.app.state.redis.get.assert_awaited_once()


@pytest.mark.asyncio
async def test_request_without_state_is_not_cached_but_still_works():
    class _Bare:  # no .state attribute at all
        pass
    request = _Bare()
    request.app = MagicMock()
    request.app.state.redis.get = AsyncMock(return_value="false")
    assert await tp._flag_enabled(request, tp.UNPUBLISH_FLAG) is False
    assert await tp._flag_enabled(request, tp.UNPUBLISH_FLAG) is False
    assert request.app.state.redis.get.await_count == 2


# ── durable override: TENANT_POOL_FORCE_ADMIN_FLAGS ───────────────────────────

def test_parse_force_admin():
    assert tp._parse_force_admin(None) == frozenset()
    assert tp._parse_force_admin("") == frozenset()
    assert tp._parse_force_admin(" aa859:unpublish_pool , ,aa859:piece_edit_pool") == frozenset(
        {tp.UNPUBLISH_FLAG, tp.PIECE_EDIT_FLAG})


@pytest.mark.asyncio
async def test_force_admin_override_wins_over_redis_and_logs_once(monkeypatch):
    """Survives a Redis flush: the key reads as missing (= ON) but the env override keeps it OFF."""
    monkeypatch.setattr(tp, "_FORCE_ADMIN_FLAGS", frozenset({tp.CONTENT_PIECE_READS_FLAG}))
    monkeypatch.setattr(tp, "_force_admin_logged", set())
    main = _conn()
    with capture_logs() as logs:
        for _ in range(2):
            request = _request(flag=True, main_conn=main)
            request.app.state.redis.get = AsyncMock(return_value=None)
            conn, used = await _enter(request, str(TENANT_ID))
            assert conn is main and used is False
            request.app.state.redis.get.assert_not_awaited()
            request.app.state.tenant_pool.acquire.assert_not_called()
    forced = [e for e in logs if e["event"] == "aa817_tenant_flag_forced_admin"]
    assert len(forced) == 1 and forced[0]["log_level"] == "info"
    assert forced[0]["flag_key"] == tp.CONTENT_PIECE_READS_FLAG


@pytest.mark.asyncio
async def test_force_admin_override_leaves_other_flags_alone(monkeypatch):
    monkeypatch.setattr(tp, "_FORCE_ADMIN_FLAGS", frozenset({tp.UNPUBLISH_FLAG}))
    tenant = _conn()
    request = _request(flag=True, tenant_conn=tenant)
    conn, used = await _enter(request, str(TENANT_ID))
    assert conn is tenant and used is True


# ── flag inventory ─────────────────────────────────────────────────────────────

def test_every_flag_constant_is_in_the_inventory():
    """A new *_FLAG constant must join ALL_TENANT_POOL_FLAGS (and the aa859_flag.py ALLOWED set), so it
    cannot ship without tooling to SET it false."""
    constants = {v for k, v in vars(tp).items() if k.endswith("_FLAG") and isinstance(v, str)}
    assert constants == set(tp.ALL_TENANT_POOL_FLAGS)
    assert len(constants) == 12  # 11 (S241) + D5b (S247)


def _piece_row():
    return {
        "piece_id": uuid4(), "tenant_id": TENANT_ID, "angle_gate_request_id": REQUEST_ID,
        "attempt_number": 1, "content_text": "Body", "status": "approved", "held_reason": None,
        "gate_ledger": [], "repair_log": [], "created_at": datetime(2026, 10, 15, tzinfo=timezone.utc),
        "seo_title": None,
        "meta_description": None, "slug": None, "route_hub_name": None, "route_segment_count": None,
        "flags": [],
    }


@pytest.mark.asyncio
async def test_latest_piece_runs_on_tenant_pool_when_flag_on():
    tenant = _conn()
    tenant.fetchrow = AsyncMock(side_effect=[{"option_id": OPTION_ID}, _piece_row()])
    request = _request(flag=True, tenant_conn=tenant)
    result = await service.fetch_latest_piece_for_request(TENANT_ID, REQUEST_ID, request.app.state.pool,
                                                          request=request)
    assert result["ready_state"] == "ready"
    assert [c[0][1:] for c in tenant.fetchrow.call_args_list] == [
        (REQUEST_ID, TENANT_ID), (REQUEST_ID, TENANT_ID, OPTION_ID)]
    request.app.state.pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_latest_piece_same_shape_on_both_pools():
    rows = [{"option_id": OPTION_ID}, _piece_row()]
    tenant, main = _conn(), _conn()
    tenant.fetchrow = AsyncMock(side_effect=list(rows))
    main.fetchrow = AsyncMock(side_effect=list(rows))
    on = await service.fetch_latest_piece_for_request(
        TENANT_ID, REQUEST_ID, None, request=_request(flag=True, tenant_conn=tenant))
    off = await service.fetch_latest_piece_for_request(
        TENANT_ID, REQUEST_ID, None, request=_request(flag=False, main_conn=main))
    assert on == off


@pytest.mark.asyncio
async def test_latest_piece_flag_on_without_tenant_pool_is_503():
    request = _request(flag=True, tenant_pool=False)
    with pytest.raises(HTTPException) as exc:
        await service.fetch_latest_piece_for_request(TENANT_ID, REQUEST_ID, request.app.state.pool,
                                                     request=request)
    assert exc.value.status_code == 503
