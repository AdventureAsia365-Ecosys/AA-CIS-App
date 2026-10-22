"""AA-797 (S235) — admin tenant writes: PATCH body, three tenant states, audit on every write,
offboard is soft and restorable, /usage on live columns. Mocked asyncpg (same pool/conn shape as
test_aa309_tenant_onboarding.py); the real-SQL checks live in
tests/integration/test_aa797_tenant_admin.py."""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from api.routers import admin
from services.acp_shared.audit_log import AdminTenantAuditAction
from shared.llm_client.bedrock_satellite import ACC1_ACCOUNT_ID

_SECRET = "test-admin-secret"
TENANT_ID = uuid.uuid4()
PLAN_ID = uuid.uuid4()
CANCELLED = datetime(2026, 10, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _admin_secret(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", _SECRET)


class _TxnCM:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


def _conn():
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_TxnCM())
    return conn


def _request(conn, redis=None):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    request = MagicMock()
    request.app.state.pool = pool
    request.app.state.redis = redis
    return request


def _audit_rows(conn):
    """(action, actor, details) for every acp_shared.audit_log INSERT made through conn."""
    out = []
    for c in conn.execute.call_args_list:
        if "acp_shared.audit_log" in c.args[0]:
            _sql, _tid, actor, action, _rtype, _rid, details = c.args
            out.append((action, actor, json.loads(details)))
    return out


def _before(**kw):
    row = {"plan_tier": "starter", "plan_id": PLAN_ID, "rate_limit_rpm": 60,
           "is_active": True, "cancelled_at": None}
    row.update(kw)
    return row


class TestTenantStatus:
    def test_three_states(self):
        assert admin.tenant_status(True, None) == "active"
        assert admin.tenant_status(False, None) == "suspended"
        assert admin.tenant_status(False, CANCELLED) == "offboarded"
        # cancelled_at wins even if is_active were somehow left true
        assert admin.tenant_status(True, CANCELLED) == "offboarded"


class TestUpdateTenant:
    async def _patch(self, conn, body, role="admin", redis=None, user="u-1"):
        return await admin.update_tenant(
            TENANT_ID, admin.UpdateTenantRequest(**body), _request(conn, redis),
            x_admin_secret=_SECRET, x_admin_user_id=user, x_admin_role=role,
        )

    @pytest.mark.asyncio
    async def test_empty_body_is_422(self):
        with pytest.raises(HTTPException) as e:
            await self._patch(_conn(), {})
        assert e.value.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_plan_tier_is_400(self):
        with pytest.raises(HTTPException) as e:
            await self._patch(_conn(), {"plan_tier": "platinum"})
        assert e.value.status_code == 400

    @pytest.mark.asyncio
    async def test_non_admin_role_is_403(self):
        for role in (None, "reviewer", "content"):
            with pytest.raises(HTTPException) as e:
                await self._patch(_conn(), {"is_active": False}, role=role)
            assert e.value.status_code == 403

    @pytest.mark.asyncio
    async def test_offboarded_tenant_is_409(self):
        conn = _conn()
        conn.fetchrow.return_value = _before(is_active=False, cancelled_at=CANCELLED)
        with pytest.raises(HTTPException) as e:
            await self._patch(conn, {"is_active": True})
        assert e.value.status_code == 409
        assert not any("UPDATE shared.tenants" in c.args[0] for c in conn.execute.call_args_list)

    @pytest.mark.asyncio
    async def test_suspend_writes_status_and_audit_and_busts_cache(self):
        conn = _conn()
        conn.fetchrow.return_value = _before()
        redis = AsyncMock()
        out = await self._patch(conn, {"is_active": False}, redis=redis)

        assert out["changed"] == ["status"] and out["tenant_status"] == "suspended"
        upd = [c for c in conn.execute.call_args_list if "SET is_active = $2" in c.args[0]]
        assert len(upd) == 1 and upd[0].args[1:] == (TENANT_ID, False)
        assert _audit_rows(conn) == [(AdminTenantAuditAction.STATUS_CHANGED, "admin:u-1",
                                      {"before": "active", "after": "suspended"})]
        redis.delete.assert_awaited_once_with(f"tenant_meta:{TENANT_ID}")

    @pytest.mark.asyncio
    async def test_plan_change_sets_plan_id_and_rpm(self):
        new_plan = uuid.uuid4()
        conn = _conn()
        conn.fetchrow.return_value = _before()
        conn.fetchval.return_value = new_plan  # _resolve_plan_id
        out = await self._patch(conn, {"plan_tier": "business"})

        assert out["changed"] == ["plan"]
        upd = [c for c in conn.execute.call_args_list if "plan_id = $3" in c.args[0]]
        assert len(upd) == 1 and upd[0].args[1:] == (TENANT_ID, "business", new_plan, 1000)
        [(action, actor, details)] = _audit_rows(conn)
        assert action == AdminTenantAuditAction.PLAN_CHANGED and actor == "admin:u-1"
        assert details["before"]["plan_tier"] == "starter"
        assert details["after"] == {"plan_tier": "business", "rate_limit_rpm": 1000, "plan_id": str(new_plan)}

    @pytest.mark.asyncio
    async def test_plan_without_active_membership_row_is_400(self):
        conn = _conn()
        conn.fetchrow.return_value = _before()
        conn.fetchval.return_value = None
        with pytest.raises(HTTPException) as e:
            await self._patch(conn, {"plan_tier": "growth"})
        assert e.value.status_code == 400

    @pytest.mark.asyncio
    async def test_same_values_are_a_no_op(self):
        conn = _conn()
        conn.fetchrow.return_value = _before()
        out = await self._patch(conn, {"is_active": True, "plan_tier": "starter"}, user=None)
        assert out["status"] == "unchanged" and out["changed"] == []
        assert _audit_rows(conn) == []

    def test_body_not_query_params(self):
        """The S234 bug: is_active was a query param, so the UI's JSON body was ignored."""
        route = next(r for r in admin.router.routes
                     if getattr(r, "path", "") == "/admin/tenants/{tenant_id}" and "PATCH" in r.methods)
        assert [p.name for p in route.dependant.query_params] == []
        assert [p.name for p in route.dependant.body_params] == ["body"]


class TestRoutes:
    def test_bare_delete_route_is_gone(self):
        deletes = [r.path for r in admin.router.routes
                   if "DELETE" in getattr(r, "methods", set()) and r.path.startswith("/admin/tenants")]
        assert deletes == []


class TestListTenants:
    @pytest.mark.asyncio
    async def test_default_is_active_only_and_status_derived(self):
        conn = _conn()
        base = {
            "tenant_id": TENANT_ID, "name": "T", "slug": "t", "plan_tier": "growth", "posts_per_week": 3,
            "country": None, "rate_limit_rpm": 300, "created_at": CANCELLED,
            "api_calls_used": 0, "quota_tours_pct": 0, "quota_calls_pct": 0, "tours_overage": 0,
            "overage_usd": 0, "llm_cost_usd": 0, "tours_quota_monthly": 10, "api_calls_quota_monthly": 0,
            "price_usd_monthly": 0, "source_active": 0, "source_superseded": 0, "source_trashed": 0,
            "master_active": 0, "master_inactive": 0, "master_trashed": 0,
        }
        conn.fetch.return_value = [
            {**base, "is_active": True, "cancelled_at": None},
            {**base, "is_active": False, "cancelled_at": None},
            {**base, "is_active": False, "cancelled_at": CANCELLED},
        ]
        out = await admin.list_tenants(_request(conn), x_admin_secret=_SECRET, include_inactive=False)
        sql, flag = conn.fetch.call_args.args
        assert "WHERE t.is_active = true OR $1::boolean" in sql and flag is False
        assert [t["status"] for t in out["tenants"]] == ["active", "suspended", "offboarded"]
        assert out["tenants"][2]["cancelled_at"] == CANCELLED.isoformat()


class TestGenerateKey:
    @pytest.mark.asyncio
    async def test_offboarded_is_409(self):
        conn = _conn()
        conn.fetchrow.return_value = {"tenant_id": TENANT_ID, "name": "T", "cancelled_at": CANCELLED}
        with pytest.raises(HTTPException) as e:
            await admin.generate_api_key(TENANT_ID, _request(conn), x_admin_secret=_SECRET,
                                         x_admin_user_id="u", x_admin_role="admin")
        assert e.value.status_code == 409
        assert "restore" in e.value.detail
        assert conn.execute.call_count == 0

    @pytest.mark.asyncio
    async def test_rotation_is_audited(self):
        conn = _conn()
        conn.fetchrow.return_value = {"tenant_id": TENANT_ID, "name": "T", "cancelled_at": None}
        out = await admin.generate_api_key(TENANT_ID, _request(conn), x_admin_secret=_SECRET,
                                           x_admin_user_id="u-9", x_admin_role="admin")
        assert out.api_key.startswith("cis_")
        assert _audit_rows(conn) == [(AdminTenantAuditAction.KEY_ROTATED, "admin:u-9", {"name": "T"})]


class TestOffboard:
    @pytest.mark.asyncio
    async def test_aa_internal_tenant_is_refused(self):
        with pytest.raises(HTTPException) as e:
            await admin.offboard_tenant(admin.AA_INTERNAL_TENANT, admin.OffboardRequest(reason="x"),
                                        _request(_conn()), x_admin_secret=_SECRET, x_admin_role="admin")
        assert e.value.status_code == 400

    @pytest.mark.asyncio
    async def test_requires_admin_role(self):
        with pytest.raises(HTTPException) as e:
            await admin.offboard_tenant(str(TENANT_ID), admin.OffboardRequest(reason="x"),
                                        _request(_conn()), x_admin_secret=_SECRET, x_admin_role="reviewer")
        assert e.value.status_code == 403

    @pytest.mark.asyncio
    async def test_no_stale_acc1_bucket_note_and_queued_only_cancel(self, monkeypatch):
        conn = _conn()
        conn.fetchrow.side_effect = [
            {"tenant_id": TENANT_ID, "name": "T", "cancelled_at": None},
            {"reason": "AA test tenant", "added_at": CANCELLED, "added_by": "migration-181"},
        ]
        conn.fetch.return_value = [{"id": "j1", "kind": "t2_rewrite"}]
        hook = AsyncMock()
        monkeypatch.setattr("shared.jobs.registry.run_terminal_hook", hook)
        out = await admin.offboard_tenant(str(TENANT_ID), admin.OffboardRequest(reason="left"),
                                          _request(conn), x_admin_secret=_SECRET,
                                          x_admin_user_id="u-2", x_admin_role="admin")
        assert out["cancelled_jobs"] == 1
        assert ACC1_ACCOUNT_ID not in json.dumps(out) and "acp-cis" not in json.dumps(out)
        cancel_sql = conn.fetch.call_args.args[0]
        assert "WHERE status = 'queued'" in cancel_sql and "running" not in cancel_sql
        [(action, actor, details)] = _audit_rows(conn)
        assert action == AdminTenantAuditAction.OFFBOARD and actor == "admin:u-2"
        assert ACC1_ACCOUNT_ID not in json.dumps(details)
        assert details["cancelled_jobs"] == [{"id": "j1", "kind": "t2_rewrite"}]
        assert details["jev_allowlist"] == {"reason": "AA test tenant", "added_at": CANCELLED.isoformat(),
                                            "added_by": "migration-181"}
        hook.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_offboard_is_soft_state_only(self, monkeypatch):
        """Nghiệp 14/10: restore must bring data + settings back, so offboard deletes nothing but
        the Jev allow-list row (snapshotted in the audit row) and only UPDATEs shared.tenants."""
        conn = _conn()
        conn.fetchrow.side_effect = [{"tenant_id": TENANT_ID, "name": "T", "cancelled_at": None}, None]
        conn.fetch.return_value = []
        monkeypatch.setattr("shared.jobs.registry.run_terminal_hook", AsyncMock())
        await admin.offboard_tenant(str(TENANT_ID), admin.OffboardRequest(reason="left"), _request(conn),
                                    x_admin_secret=_SECRET, x_admin_role="admin")
        sqls = [c.args[0] for c in conn.execute.call_args_list] + [c.args[0] for c in conn.fetch.call_args_list]
        deletes = [q for q in sqls if "DELETE" in q.upper()]
        assert deletes == ["DELETE FROM shared.jev_tenant_allowlist WHERE tenant_id = $1::uuid"]
        updates = [q for q in sqls if "UPDATE" in q.upper()]
        assert len(updates) == 2 and "shared.tenants" in updates[0] and "shared.job" in updates[1]
        tenant_upd = updates[0]
        for kept in ("plan_id", "plan_tier", "posts_per_week", "rate_limit_rpm"):
            assert kept not in tenant_upd
        for table in ("brand_rules", "tenant_config", "tenant_tour_versions", "content_piece", "users"):
            assert not any(table in q for q in sqls)

    @pytest.mark.asyncio
    async def test_blank_reason_is_400_with_neutral_wording(self):
        with pytest.raises(HTTPException) as e:
            await admin.offboard_tenant(str(TENANT_ID), admin.OffboardRequest(reason="  "),
                                        _request(_conn()), x_admin_secret=_SECRET, x_admin_role="admin")
        assert e.value.status_code == 400
        assert "GDPR" not in e.value.detail and "audit trail" in e.value.detail

    def test_openapi_wording_has_no_gdpr_claim(self):
        """AA-839: the endpoint is a soft, restorable offboarding, not a GDPR erasure; the published
        schema (/admin/openapi.json, /admin/api-docs) must not say otherwise."""
        from api import main

        op = main.app.openapi()["paths"]["/admin/tenants/{tenant_id}/offboard"]["post"]
        text = f"{op.get('summary', '')} {op.get('description', '')}"
        assert "offboarding" in op["summary"].lower()
        assert "gdpr" not in text.lower()


class TestRestore:
    async def _restore(self, conn, role="admin", redis=None):
        return await admin.restore_tenant(TENANT_ID, _request(conn, redis=redis), x_admin_secret=_SECRET,
                                          x_admin_user_id="u-5", x_admin_role=role)

    def _offboarded(self):
        return {"tenant_id": TENANT_ID, "name": "T", "is_active": False, "cancelled_at": CANCELLED,
                "cancellation_reason": "contract ended"}

    @pytest.mark.asyncio
    async def test_requires_admin_role(self):
        with pytest.raises(HTTPException) as e:
            await self._restore(_conn(), role="reviewer")
        assert e.value.status_code == 403

    @pytest.mark.asyncio
    async def test_not_found_is_404(self):
        conn = _conn()
        conn.fetchrow.return_value = None
        with pytest.raises(HTTPException) as e:
            await self._restore(conn)
        assert e.value.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.parametrize("is_active", [True, False])
    async def test_not_offboarded_is_409(self, is_active):
        conn = _conn()
        conn.fetchrow.return_value = {**self._offboarded(), "is_active": is_active, "cancelled_at": None}
        with pytest.raises(HTTPException) as e:
            await self._restore(conn)
        assert e.value.status_code == 409
        assert ("active" if is_active else "suspended") in e.value.detail
        assert conn.execute.call_count == 0

    @pytest.mark.asyncio
    async def test_happy_path_new_key_markers_cleared_settings_untouched(self):
        conn = _conn()
        conn.fetchrow.return_value = self._offboarded()
        conn.fetch.return_value = [{"id": "j1", "kind": "t9_write"}]
        conn.fetchval.return_value = json.dumps({"jev_allowlist": {
            "reason": "AA test tenant", "added_at": CANCELLED.isoformat(), "added_by": "migration-181"}})
        conn.execute.side_effect = lambda sql, *a: "INSERT 0 1" if "INSERT" in sql else "UPDATE 1"
        redis = AsyncMock()
        out = await self._restore(conn, redis=redis)

        assert out.status == "active" and out.api_key.startswith("cis_")
        assert out.previous_offboarded_at == CANCELLED.isoformat() and out.previous_reason == "contract ended"
        assert out.jev_allowlist_restored is True
        assert out.cancelled_jobs == [{"id": "j1", "kind": "t9_write"}]
        [upd] = [c for c in conn.execute.call_args_list if "UPDATE shared.tenants" in c.args[0]]
        sql = upd.args[0]
        assert "cancelled_at = NULL" in sql and "is_active = TRUE" in sql
        for kept in ("plan_id", "plan_tier", "rate_limit_rpm", "posts_per_week"):
            assert kept not in sql
        assert upd.args[2] != "" and not upd.args[2].startswith("REVOKED_")
        [(action, actor, details)] = _audit_rows(conn)
        assert action == AdminTenantAuditAction.RESTORED == "tenant.restored" and actor == "admin:u-5"
        assert details["previous_offboarded_at"] == CANCELLED.isoformat()
        assert details["cancelled_jobs_not_rerun"] == [{"id": "j1", "kind": "t9_write"}]
        assert "jev_allowlist" in details["restored"]
        redis.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_allowlist_snapshot_restores_without_it(self):
        conn = _conn()
        conn.fetchrow.return_value = self._offboarded()
        conn.fetch.return_value = []
        conn.fetchval.return_value = None
        out = await self._restore(conn)
        assert out.jev_allowlist_restored is False and out.cancelled_jobs == []
        assert not any("jev_tenant_allowlist" in c.args[0] for c in conn.execute.call_args_list)


class TestUsageSql:
    @pytest.mark.asyncio
    async def test_reads_tenant_api_usage_not_the_drifted_view(self):
        conn = _conn()
        conn.fetchrow.side_effect = [
            {"name": "T", "slug": "t", "plan_tier": "growth", "plan_id": PLAN_ID, "rate_limit_rpm": 300,
             "is_active": True, "cancelled_at": None},
            {"versions": 4, "tours": 2},
        ]
        conn.fetch.return_value = [{"month": CANCELLED, "total_calls": 3, "successful_calls": 2,
                                    "rate_limited_calls": 1, "avg_response_ms": None}]
        conn.fetchval.return_value = 30
        out = await admin.get_tenant_usage(TENANT_ID, _request(conn), x_admin_secret=_SECRET, months=3)
        usage_sql = conn.fetch.call_args.args[0]
        assert "shared.tenant_api_usage" in usage_sql and "v_tenant_monthly_usage" not in usage_sql
        assert out["limits"]["tours_per_month"] == 30
        assert out["t2_versions"] == 4 and out["t2_tours"] == 2
        assert out["monthly_usage"][0]["avg_response_ms"] is None
