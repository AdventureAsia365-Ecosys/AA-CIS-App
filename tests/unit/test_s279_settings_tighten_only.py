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
