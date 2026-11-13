"""S279 thresholds L1 (tech lead Q1) — tighten-only on the delegated pages: a role without settings.write (ai_ops on the
AI gateway budgets, customer_success on a tenant's RPM and publish limit) may lower or harden a cap, never raise,
remove or soften it; the administrator may do both. No DB: fakes only."""
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api.services.settings_registry import loosened, tighten_only


@pytest.mark.parametrize("role,expected", [("admin", False), ("ai_ops", True), ("customer_success", True),
                                           (None, False), ("", False), (object(), False)])
def test_who_is_tighten_only(role, expected):
    assert tighten_only(role) is expected


@pytest.mark.parametrize("before,after,worse", [
    (None, {"per_day_usd": 99, "hard_stop": True}, []),                          # a new cap only tightens
    (None, {"per_day_usd": 5, "hard_stop": False}, ["hard_stop"]),
    ({"per_day_usd": 10, "hard_stop": True}, {"per_day_usd": 5, "hard_stop": True}, []),
    ({"per_day_usd": 10, "hard_stop": True}, {"per_day_usd": 11, "hard_stop": True}, ["per_day_usd"]),
    ({"per_day_usd": 10, "hard_stop": True}, {"per_day_usd": None, "hard_stop": True}, ["per_day_usd"]),
    ({"per_day_usd": None, "hard_stop": False}, {"per_day_usd": 3, "hard_stop": True}, []),
    ({"per_day_usd": 10, "hard_stop": True}, {"per_day_usd": 10, "hard_stop": False}, ["hard_stop"]),
    ({"per_day_usd": 10, "alert_pct": 80}, {"per_day_usd": 10, "alert_pct": 90}, ["alert_pct"]),
])
def test_loosened(before, after, worse):
    assert loosened(before, after, caps=("per_day_usd",), stops=("hard_stop",), up_is_looser=("alert_pct",)) == worse


class _Conn:
    def __init__(self, row=None):
        self.row, self.executed = row, []

    async def fetchval(self, sql, *a):
        return json.dumps(self.row) if self.row is not None else None

    async def execute(self, sql, *a):
        self.executed.append(a)


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return _Ctx()


def _request(conn):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_Pool(conn))))


@pytest.mark.asyncio
async def test_ai_ops_may_lower_a_provider_cap_but_not_raise_it(monkeypatch):
    from api.routers import admin as admin_module
    from api.routers import admin_budgets as ab
    monkeypatch.setattr(admin_module, "ADMIN_SECRET", "s")
    row = {"provider": "dfs", "scope": "global", "per_run_usd": None, "per_day_usd": 10.0, "hard_stop": True,
           "alert_pct": 80}
    conn = _Conn(row)
    out = await ab.upsert_budget("dfs", "global", ab.BudgetUpdate(per_day_usd=5.0, hard_stop=True), _request(conn),
                                 x_admin_secret="s", x_admin_user_id="u", x_admin_role="ai_ops")
    assert out["per_day_usd"] == 5.0 and conn.executed
    with pytest.raises(HTTPException) as e:
        await ab.upsert_budget("dfs", "global", ab.BudgetUpdate(per_day_usd=20.0, hard_stop=True), _request(_Conn(row)),
                               x_admin_secret="s", x_admin_user_id="u", x_admin_role="ai_ops")
    assert e.value.status_code == 403 and "per_day_usd" in e.value.detail
    out = await ab.upsert_budget("dfs", "global", ab.BudgetUpdate(per_day_usd=20.0, hard_stop=True),
                                 _request(_Conn(row)), x_admin_secret="s", x_admin_user_id="u", x_admin_role="admin")
    assert out["per_day_usd"] == 20.0


@pytest.mark.asyncio
async def test_ai_ops_may_not_remove_a_central_cap():
    from api.routers import admin_budgets as ab
    with pytest.raises(HTTPException) as e:
        await ab.delete_central_budget("app:cis", _request(_Conn()), x_admin_user_id="u", x_admin_role="ai_ops")
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_ai_ops_may_not_remove_a_tenant_budget():
    from api.routers import admin_ai_gateway as gw
    with pytest.raises(HTTPException) as e:
        await gw.delete_tenant_budget(_request(_Conn()), tenant_id="00000000-0000-4000-8000-00000000a279",
                                      x_admin_user_id="u", x_admin_role="ai_ops")
    assert e.value.status_code == 403


# ── S279 review: the other caps the audit names (app profile, capacity, connection limits, posts per week) ──────────

def test_loosened_down_is_looser():
    kinds = {"down_is_looser": ("low_balance_usd",)}
    assert loosened({"low_balance_usd": 10}, {"low_balance_usd": 5}, **kinds) == ["low_balance_usd"]
    assert loosened({"low_balance_usd": 10}, {"low_balance_usd": None}, **kinds) == ["low_balance_usd"]
    assert loosened({"low_balance_usd": 10}, {"low_balance_usd": 20}, **kinds) == []
    assert loosened({"low_balance_usd": None}, {"low_balance_usd": 1}, **kinds) == []


def _profile_request(monkeypatch, profile):
    from api.routers import admin_ai_gateway as gw
    from shared.llm_client import app_profile

    async def read_all(conn):
        return {profile.app: profile}

    async def update(pool, app, fields, *, actor):
        return {"app": app, **fields}
    monkeypatch.setattr(app_profile, "read_all", read_all)
    monkeypatch.setattr(app_profile, "update", update)
    return gw, _request(_Conn())


@pytest.mark.asyncio
@pytest.mark.parametrize("fields,ok", [
    ({"ai_enabled": False, "reason": "zz incident"}, True),        # turn AI off: a stop
    ({"bulk_paused": True}, True),
    ({"default_daily_budget_usd": 2.0}, True),                      # lower the default budget
    ({"ai_enabled": True}, False),                                  # switch AI back on
    ({"bulk_paused": False}, False),                                # un-pause bulk work
    ({"default_daily_budget_usd": 9.0}, False),                     # raise the budget
    ({"default_daily_budget_usd": None}, False),                    # clear the budget
])
async def test_ai_ops_on_the_app_profile_only_tightens(monkeypatch, fields, ok):
    from shared.llm_client import app_profile

    gw, request = _profile_request(monkeypatch, app_profile.AppProfile(
        app="cis", ai_enabled=False if fields.get("ai_enabled") is True else True,
        bulk_paused=fields.get("bulk_paused") is False, default_daily_budget_usd=5.0, disabled_reason="zz earlier"))
    body = gw.AppProfilePatch(**fields)
    if ok:
        assert (await gw.patch_app_profile(body, request, app="cis", x_admin_user_id="u", x_admin_role="ai_ops"))
    else:
        with pytest.raises(HTTPException) as e:
            await gw.patch_app_profile(body, request, app="cis", x_admin_user_id="u", x_admin_role="ai_ops")
        assert e.value.status_code == 403
    # the administrator may do both
    assert await gw.patch_app_profile(body, request, app="cis", x_admin_user_id="u", x_admin_role="admin")


@pytest.mark.asyncio
@pytest.mark.parametrize("body,ok", [
    ({"tpm_limit": 50000, "bulk_pct": 40, "prod_guard": True}, True),        # lower the limit and a share
    ({"tpm_limit": 200000, "bulk_pct": 50, "prod_guard": True}, False),      # raise the limit
    ({"tpm_limit": None, "bulk_pct": 50, "prod_guard": True}, False),        # drop the limit
    ({"tpm_limit": 100000, "bulk_pct": 80, "prod_guard": True}, False),      # widen the bulk share
    ({"tpm_limit": 100000, "bulk_pct": 50, "prod_guard": False}, False),     # switch the prod guard off
])
async def test_ai_ops_on_capacity_only_tightens(monkeypatch, body, ok):
    from api.routers import admin_ai_gateway as gw
    from shared.llm_client import capacity

    old = capacity.Limit(connection_key="zz-conn", model_key="*", tpm_limit=100000, bulk_pct=50, prod_guard=True)

    async def read_all(conn):
        return [old]

    async def put(pool, connection_key, model_key, fields, *, actor):
        return {"connection_key": connection_key, **fields}
    monkeypatch.setattr(capacity, "read_all", read_all)
    monkeypatch.setattr(capacity, "put", put)
    req = _request(_Conn())
    b = gw.CapacityLimitBody(**body)
    if ok:
        assert await gw.put_capacity(b, req, connection_key="zz-conn", model_key="*", x_admin_user_id="u",
                                     x_admin_role="ai_ops")
    else:
        with pytest.raises(HTTPException) as e:
            await gw.put_capacity(b, req, connection_key="zz-conn", model_key="*", x_admin_user_id="u",
                                  x_admin_role="ai_ops")
        assert e.value.status_code == 403
    assert await gw.put_capacity(b, req, connection_key="zz-conn", model_key="*", x_admin_user_id="u",
                                 x_admin_role="admin")
    # a new row (another model) only adds a limit
    assert await gw.put_capacity(b, req, connection_key="zz-conn", model_key="zz-model", x_admin_user_id="u",
                                 x_admin_role="ai_ops")
    with pytest.raises(HTTPException) as e:
        await gw.delete_capacity(req, connection_key="zz-conn", model_key="*", x_admin_user_id="u",
                                 x_admin_role="ai_ops")
    assert e.value.status_code == 403


class _ConnRow(SimpleNamespace):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("changes,policy,ok", [
    ({"rpm_limit": 30}, {}, True),
    ({"rpm_limit": 120}, {}, False),
    ({"monthly_budget_usd": None}, {}, False),
    ({"daily_credit_limit": 500}, {}, True),
    ({"max_retries": 4}, {}, False),
    ({"low_balance_usd": 1.0}, {}, False),
    ({"timeout_s": 300, "enabled": True}, {}, True),                         # not limits
    ({}, {"budget_hard_stop": False}, False),
    ({}, {"budget_alert_pct": 95}, False),
    ({}, {"budget_alert_pct": 60}, True),
])
async def test_ai_ops_on_connection_limits_only_tightens(changes, policy, ok):
    from api.services import llm_connections

    c = _ConnRow(rpm_limit=60, max_concurrency=4, monthly_budget_usd=100.0, daily_credit_limit=1000, max_retries=2,
                 low_balance_usd=5.0)

    class _Db:
        async def fetchrow(self, sql, *a):
            return {"alert_pct": 80, "hard_stop": True}
    if ok:
        await llm_connections._refuse_loosening(_Db(), c, "zz-conn", changes, policy, "ai_ops")
    else:
        with pytest.raises(PermissionError):
            await llm_connections._refuse_loosening(_Db(), c, "zz-conn", changes, policy, "ai_ops")
    await llm_connections._refuse_loosening(_Db(), c, "zz-conn", changes, policy, "admin")


@pytest.mark.asyncio
async def test_customer_success_may_not_raise_posts_per_week(monkeypatch):
    from uuid import UUID

    from api.routers import admin as admin_module

    saved = []

    async def fetch(tenant_id, pool):
        return SimpleNamespace(markets=["US"], channels=["blog"], capacity_posts_per_week=3)

    async def save(tenant_id, markets, channels, posts, pool):
        saved.append(posts)

    async def audit(*a, **k):
        return None
    monkeypatch.setattr(admin_module, "ADMIN_SECRET", "s")
    monkeypatch.setattr(admin_module, "fetch_tenant_planning_config", fetch)
    monkeypatch.setattr(admin_module, "save_tenant_planning_config", save)
    monkeypatch.setattr(admin_module, "write_audit_log", audit)
    tid = UUID("00000000-0000-4000-8000-00000000a279")

    def body(n):
        return admin_module.TenantConfigRequest(markets=["US"], channels=["blog"], posts_per_week=n)
    await admin_module.update_tenant_config(tid, body(2), _request(_Conn()), x_admin_secret="s",
                                            x_admin_user_id="u", x_admin_role="customer_success")
    with pytest.raises(HTTPException) as e:
        await admin_module.update_tenant_config(tid, body(5), _request(_Conn()), x_admin_secret="s",
                                                x_admin_user_id="u", x_admin_role="customer_success")
    assert e.value.status_code == 403
    await admin_module.update_tenant_config(tid, body(5), _request(_Conn()), x_admin_secret="s",
                                            x_admin_user_id="u", x_admin_role="admin")
    assert saved == [2, 5]
