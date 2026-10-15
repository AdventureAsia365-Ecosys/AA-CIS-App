"""AA-819 (AUP 2026-11) — per-tenant daily publish limit: services/acp_publish/daily_limit.py, the 429 in
POST /v1/publish-log/{piece_id}/publish, and the admin override (PATCH /admin/tenants/{id},
GET /admin/tenants, GET /admin/tenants/{id}/usage).

The counting SQL and claim() under the limit run against real Postgres in
tests/integration/test_aa819_daily_publish_limit.py; here asyncpg is mocked.
"""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api.routers import admin
from api.routers import v1_publish as pub
from services.acp_publish import daily_limit
from services.acp_publish.idempotency import Claim
from services.acp_shared.audit_log import AdminTenantAuditAction

_SECRET = "test-admin-secret"
TENANT_ID = uuid.uuid4()
PIECE_ID = uuid.uuid4()
TENANT = {"sub": str(TENANT_ID), "role": "tenant"}


@pytest.fixture(autouse=True)
def _fresh_column_cache(monkeypatch):
    """The column probe is cached per process; every test starts with an unknown answer."""
    daily_limit._forget_column()
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", _SECRET)
    yield
    daily_limit._forget_column()


class _TxnCM:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


def _conn():
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_TxnCM())
    return conn


def _request(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    request = MagicMock()
    request.app.state.pool = pool
    request.app.state.redis = None
    return request


def _fetchval_router(column_exists: bool, override=None, count=0, plan_id=None):
    """conn.fetchval answering by SQL text: the pg_attribute probe, the override read, the count."""
    async def fetchval(sql, *args):
        if "pg_attribute" in sql:
            return column_exists
        if "SELECT daily_publish_limit FROM shared.tenants" in sql:
            if not column_exists:
                raise asyncpg.UndefinedColumnError("column \"daily_publish_limit\" does not exist")
            return override
        if "acp_shared.publish_log" in sql:
            return count
        return plan_id
    return AsyncMock(side_effect=fetchval)


# ── daily_limit helpers ──────────────────────────────────────────────────────────────────────

class TestHelpers:
    def test_default_is_20_and_max_is_a_typo_guard(self):
        assert daily_limit.DEFAULT_DAILY_PUBLISH_LIMIT == 20
        assert daily_limit.DAILY_PUBLISH_LIMIT_MAX == 1000

    def test_seconds_until_reset_is_to_utc_midnight(self):
        now = datetime(2026, 11, 12, 22, 30, tzinfo=timezone.utc)
        assert daily_limit.seconds_until_reset(now) == 90 * 60
        assert daily_limit.seconds_until_reset(datetime(2026, 11, 12, 23, 59, 59, 999999,
                                                        tzinfo=timezone.utc)) == 1

    def test_limit_message_is_clear(self):
        msg = daily_limit.limit_message(20, 20, datetime(2026, 11, 12, 21, 15, tzinfo=timezone.utc))
        assert msg.startswith("Daily publish limit reached")
        assert "20 post(s) per day" in msg and "published 20 today" in msg
        assert "00:00 UTC (in 2 h 45 min)" in msg
        # no gate / quality wording reaches the tenant (tenant-sees-content-only)
        assert "gate" not in msg.lower() and "AUP" not in msg

    def test_limit_message_under_an_hour(self):
        msg = daily_limit.limit_message(2, 2, datetime(2026, 11, 12, 23, 50, tzinfo=timezone.utc))
        assert "(in 10 min)" in msg

    def test_count_sql_counts_today_utc_and_never_failed(self):
        sql = " ".join(daily_limit.COUNT_TODAY_SQL.split())
        assert "status IN ('requested', 'published')" in sql
        assert "status = 'unpublished' AND published_at IS NOT NULL" in sql
        assert "'failed'" not in sql
        assert "date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'" in sql
        assert "tenant_id = $1::uuid" in sql


class TestColumnProbe:
    @pytest.mark.asyncio
    async def test_present_is_cached_for_the_process(self):
        conn = _conn()
        conn.fetchval = AsyncMock(return_value=True)
        assert await daily_limit.override_column_exists(conn) is True
        assert await daily_limit.override_column_exists(conn) is True
        assert conn.fetchval.await_count == 1

    @pytest.mark.asyncio
    async def test_missing_is_rechecked_after_the_interval(self, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(daily_limit.time, "monotonic", lambda: clock[0])
        conn = _conn()
        conn.fetchval = AsyncMock(side_effect=[False, True])
        assert await daily_limit.override_column_exists(conn) is False
        clock[0] += daily_limit.COLUMN_RECHECK_SECONDS - 1
        assert await daily_limit.override_column_exists(conn) is False  # still cached
        assert conn.fetchval.await_count == 1
        clock[0] += 2
        assert await daily_limit.override_column_exists(conn) is True  # migration 230 applied meanwhile
        assert conn.fetchval.await_count == 2

    @pytest.mark.asyncio
    async def test_before_migration_230_every_tenant_gets_the_default(self):
        conn = _conn()
        conn.fetchval = _fetchval_router(column_exists=False)
        assert await daily_limit.tenant_override(conn, TENANT_ID) is None
        assert await daily_limit.effective_limit(conn, TENANT_ID) == 20
        # only the probe ran: the column is never selected while it is missing
        assert all("pg_attribute" in c.args[0] for c in conn.fetchval.await_args_list)

    @pytest.mark.asyncio
    async def test_undefined_column_after_a_stale_probe_falls_back_to_the_default(self):
        """The probe said yes (cached), then the column is gone (a rollback of 230): default, no 500."""
        conn = _conn()
        conn.fetchval = AsyncMock(side_effect=[True, asyncpg.UndefinedColumnError("gone")])
        assert await daily_limit.effective_limit(conn, TENANT_ID) == 20
        assert daily_limit._column["exists"] is False

    @pytest.mark.asyncio
    async def test_override_wins_over_the_default(self):
        conn = _conn()
        conn.fetchval = _fetchval_router(column_exists=True, override=2)
        assert await daily_limit.effective_limit(conn, TENANT_ID) == 2

    @pytest.mark.asyncio
    async def test_null_override_is_the_default(self):
        conn = _conn()
        conn.fetchval = _fetchval_router(column_exists=True, override=None)
        assert await daily_limit.effective_limit(conn, TENANT_ID) == 20


# ── POST /v1/publish-log/{piece_id}/publish — the 429 ────────────────────────────────────────

def _piece_row():
    return {"piece_id": str(PIECE_ID), "content_text": "## Hello\nReal content.",
            "status": "approved", "channel": "blog", "angle_name": "The Chosen Angle"}


def _publish_pool(*fetchrow_returns, fetchval):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=list(fetchrow_returns))
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = fetchval
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    req = MagicMock()
    req.app.state.pool = pool
    return req, conn


class TestPublishRoute:
    @pytest.mark.asyncio
    async def test_limit_reached_is_429_with_message_and_retry_after(self):
        req, _ = _publish_pool(_piece_row(), {"secret_key": "acp/cms/x"},
                               fetchval=_fetchval_router(column_exists=False))
        claim = AsyncMock(return_value=Claim("limited", used=20, limit=20))
        create = AsyncMock()
        with patch.object(pub, "_get_secret", return_value={"wp_url": "https://b.com", "username": "u",
                                                            "app_password": "p"}), \
             patch.object(pub, "claim", claim), \
             patch("services.acp_s4_blog.cms.wordpress.WordPressAdapter.create_post", create):
            with pytest.raises(HTTPException) as e:
                await pub.publish(PIECE_ID, req, TENANT)
        assert e.value.status_code == 429
        assert e.value.detail.startswith("Daily publish limit reached")
        assert "20 post(s) per day" in e.value.detail
        assert 1 <= int(e.value.headers["Retry-After"]) <= 86400
        # the code default reached claim() (migration 230 not applied in this test)
        assert claim.call_args.kwargs["daily_limit"] == 20
        create.assert_not_awaited()  # nothing posted

    @pytest.mark.asyncio
    async def test_the_tenant_override_is_what_claim_enforces(self):
        req, _ = _publish_pool(_piece_row(), {"secret_key": "acp/cms/x"},
                               fetchval=_fetchval_router(column_exists=True, override=2))
        claim = AsyncMock(return_value=Claim("limited", used=2, limit=2))
        with patch.object(pub, "_get_secret", return_value={"wp_url": "https://b.com", "username": "u",
                                                            "app_password": "p"}), \
             patch.object(pub, "claim", claim):
            with pytest.raises(HTTPException) as e:
                await pub.publish(PIECE_ID, req, TENANT)
        assert e.value.status_code == 429 and "2 post(s) per day" in e.value.detail
        assert claim.call_args.kwargs["daily_limit"] == 2

    @pytest.mark.asyncio
    async def test_in_flight_is_still_409_not_429(self):
        req, _ = _publish_pool(_piece_row(), {"secret_key": "acp/cms/x"},
                               fetchval=_fetchval_router(column_exists=False))
        with patch.object(pub, "_get_secret", return_value={"wp_url": "https://b.com", "username": "u",
                                                            "app_password": "p"}), \
             patch.object(pub, "claim", AsyncMock(return_value=Claim("in_progress"))):
            with pytest.raises(HTTPException) as e:
                await pub.publish(PIECE_ID, req, TENANT)
        assert e.value.status_code == 409


# ── idempotency.claim() — where the limit is checked (mocked; the real SQL is in integration) ──

def _claim_conn(rows, count):
    conn = _conn()
    conn.fetch = AsyncMock(return_value=rows)
    conn.fetchval = AsyncMock(side_effect=lambda sql, *a: count if "publish_log" in sql and "count" in sql
                              else str(uuid.uuid4()))
    return conn


class TestClaimUnderLimit:
    KEY = "k1"

    async def _claim(self, conn, limit=20):
        from services.acp_publish.idempotency import claim
        return await claim(conn, piece_id=str(PIECE_ID), tenant_id=str(TENANT_ID), channel="blog",
                           key=self.KEY, daily_limit=limit)

    @pytest.mark.asyncio
    async def test_replay_never_counts_or_refuses(self):
        row = {"publish_id": "p1", "idempotency_key": self.KEY, "status": "published", "stale": False}
        conn = _claim_conn([row], count=99)
        c = await self._claim(conn)
        assert c.state == "published"
        assert not any("pg_advisory_xact_lock" in str(x.args[0]) for x in conn.execute.await_args_list)

    @pytest.mark.asyncio
    async def test_in_flight_retry_never_counts_or_refuses(self):
        row = {"publish_id": "p1", "idempotency_key": self.KEY, "status": "requested", "stale": False}
        c = await self._claim(_claim_conn([row], count=99))
        assert c.state == "in_progress"

    @pytest.mark.asyncio
    async def test_stale_reclaim_of_a_counted_row_is_not_refused(self):
        row = {"publish_id": "p1", "idempotency_key": self.KEY, "status": "requested", "stale": True}
        c = await self._claim(_claim_conn([row], count=99))
        assert c.state == "claimed" and c.prior_attempt

    @pytest.mark.asyncio
    async def test_new_publish_at_the_limit_is_limited_under_the_tenant_lock(self):
        conn = _claim_conn([], count=20)
        c = await self._claim(conn)
        assert (c.state, c.used, c.limit) == ("limited", 20, 20)
        assert any("pg_advisory_xact_lock" in x.args[0] for x in conn.execute.await_args_list)

    @pytest.mark.asyncio
    async def test_new_publish_under_the_limit_is_claimed(self):
        c = await self._claim(_claim_conn([], count=19))
        assert c.state == "claimed" and not c.prior_attempt

    @pytest.mark.asyncio
    async def test_failed_row_turned_back_into_requested_is_checked(self):
        row = {"publish_id": "p1", "idempotency_key": self.KEY, "status": "failed", "stale": False}
        c = await self._claim(_claim_conn([row], count=20))
        assert c.state == "limited"

    @pytest.mark.asyncio
    async def test_no_limit_given_keeps_the_aa726_behaviour(self):
        from services.acp_publish.idempotency import claim
        conn = _claim_conn([], count=999)
        c = await claim(conn, piece_id=str(PIECE_ID), tenant_id=str(TENANT_ID), channel="blog", key="k")
        assert c.state == "claimed"


# ── PATCH /admin/tenants/{id} — the override ─────────────────────────────────────────────────

def _audit_rows(conn):
    out = []
    for c in conn.execute.call_args_list:
        if "acp_shared.audit_log" in c.args[0]:
            _sql, _tid, actor, action, _rtype, _rid, details = c.args
            out.append((action, actor, json.loads(details)))
    return out


def _before(**kw):
    row = {"plan_tier": "starter", "plan_id": uuid.uuid4(), "rate_limit_rpm": 60,
           "is_active": True, "cancelled_at": None}
    row.update(kw)
    return row


class TestAdminOverride:
    async def _patch(self, conn, body, role="admin"):
        return await admin.update_tenant(
            TENANT_ID, admin.UpdateTenantRequest(**body), _request(conn),
            x_admin_secret=_SECRET, x_admin_user_id="u-1", x_admin_role=role,
        )

    @pytest.mark.asyncio
    async def test_set_override_alone_updates_and_audits(self):
        conn = _conn()
        conn.fetchrow.return_value = _before()
        conn.fetchval = _fetchval_router(column_exists=True, override=None)
        out = await self._patch(conn, {"daily_publish_limit": 2})
        assert out["changed"] == ["daily_publish_limit"]
        assert out["daily_publish_limit"] == 2 and out["daily_publish_limit_effective"] == 2
        upd = [c for c in conn.execute.call_args_list if "SET daily_publish_limit = $2" in c.args[0]]
        assert len(upd) == 1 and upd[0].args[1:] == (TENANT_ID, 2)
        assert _audit_rows(conn) == [(AdminTenantAuditAction.DAILY_PUBLISH_LIMIT_CHANGED, "admin:u-1",
                                      {"before": {"daily_publish_limit": None},
                                       "after": {"daily_publish_limit": 2}, "default": 20})]

    @pytest.mark.asyncio
    async def test_explicit_null_clears_the_override(self):
        conn = _conn()
        conn.fetchrow.return_value = _before()
        conn.fetchval = _fetchval_router(column_exists=True, override=5)
        out = await self._patch(conn, {"daily_publish_limit": None})
        assert out["changed"] == ["daily_publish_limit"]
        assert out["daily_publish_limit"] is None and out["daily_publish_limit_effective"] == 20
        upd = [c for c in conn.execute.call_args_list if "SET daily_publish_limit = $2" in c.args[0]]
        assert upd[0].args[1:] == (TENANT_ID, None)

    @pytest.mark.asyncio
    async def test_same_value_is_a_no_op(self):
        conn = _conn()
        conn.fetchrow.return_value = _before()
        conn.fetchval = _fetchval_router(column_exists=True, override=2)
        out = await self._patch(conn, {"daily_publish_limit": 2})
        assert out["status"] == "unchanged" and _audit_rows(conn) == []

    @pytest.mark.asyncio
    async def test_key_left_out_keeps_the_override_and_the_response_shape(self):
        conn = _conn()
        conn.fetchrow.return_value = _before()
        conn.fetchval = _fetchval_router(column_exists=True, override=2)
        out = await self._patch(conn, {"rate_limit_rpm": 120})
        assert out["changed"] == ["rate_limit"] and "daily_publish_limit" not in out
        assert not any("daily_publish_limit" in c.args[0] for c in conn.execute.call_args_list)

    @pytest.mark.asyncio
    async def test_before_migration_230_the_override_is_409_with_a_clear_message(self):
        conn = _conn()
        conn.fetchrow.return_value = _before()
        conn.fetchval = _fetchval_router(column_exists=False)
        with pytest.raises(HTTPException) as e:
            await self._patch(conn, {"daily_publish_limit": 2})
        assert e.value.status_code == 409 and "migration 230" in e.value.detail and "20/day" in e.value.detail
        conn.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_override_needs_admin_role(self):
        with pytest.raises(HTTPException) as e:
            await self._patch(_conn(), {"daily_publish_limit": 2}, role="reviewer")
        assert e.value.status_code == 403

    def test_bounds_validated(self):
        for bad in (0, -1, daily_limit.DAILY_PUBLISH_LIMIT_MAX + 1):
            with pytest.raises(ValidationError):
                admin.UpdateTenantRequest(daily_publish_limit=bad)
        assert admin.UpdateTenantRequest(daily_publish_limit=None).model_fields_set == {"daily_publish_limit"}

    @pytest.mark.asyncio
    async def test_nothing_to_update_still_422(self):
        with pytest.raises(HTTPException) as e:
            await self._patch(_conn(), {})
        assert e.value.status_code == 422 and "daily_publish_limit" in e.value.detail


class TestAdminReads:
    @pytest.mark.asyncio
    async def test_overrides_map_is_empty_before_migration_230(self):
        conn = _conn()
        conn.fetchval = _fetchval_router(column_exists=False)
        assert await admin._publish_limit_overrides(conn) == {}
        conn.fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_overrides_map_after_migration_230(self):
        other = uuid.uuid4()
        conn = _conn()
        conn.fetchval = _fetchval_router(column_exists=True)
        conn.fetch = AsyncMock(return_value=[{"tenant_id": other, "daily_publish_limit": 3}])
        assert await admin._publish_limit_overrides(conn) == {other: 3}
        assert "daily_publish_limit IS NOT NULL" in conn.fetch.await_args.args[0]
