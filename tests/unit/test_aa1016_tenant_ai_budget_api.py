"""AA-1016 UI / AA-900 UI / AA-804 F5 (S262 slice C4) — the tenant AI budget endpoints, the Experiments read and the
"Last changed" actor names of the Budgets tab.

  GET / PUT / DELETE /admin/ai-gateway/tenant-budgets/{tenant_id}   api/services/tenant_ai_budget.py
  GET /admin/ai-gateway/experiments                                 api/services/ai_gateway_experiments.py
  ai_gateway.actor_view / actor_names                               the Budgets tab's "Last changed" column

No database: a fake connection answers by SQL fragment and records every statement (the audit row is checked by its
arguments). The SQL runs on PostgreSQL in tests/integration/test_aa1016_tenant_ai_budget_sql.py. Names are invented.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import asyncpg
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api.core import staff_authz
from api.routers import admin_ai_gateway as router_mod
from api.services import ai_gateway as gw
from api.services import ai_gateway_experiments as exp
from api.services import tenant_ai_budget as tb

NOW = datetime(2026, 3, 14, 15, 30, tzinfo=timezone.utc)
TENANT = "3c9e1a2b-4d5f-4a6b-8c7d-0e1f2a3b4c5d"
STAFF = "7a1b2c3d-4e5f-4a6b-9c8d-1e2f3a4b5c6d"


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    """fetch / fetchrow / fetchval / execute answered by SQL fragment; ``missing`` raises UndefinedTableError for a
    fragment (the table before migration 321); ``denied`` raises InsufficientPrivilegeError."""

    def __init__(self, *, tenant_name="Lotus Trails", row=None, spend=None, missing=(), denied=(), prev=None,
                 source=None, gateway=None, active=None):
        self.tenant_name = tenant_name
        self.active = active or []          # S280: ACTIVE_TENANTS_SQL (tenants without a row)
        self.row = row
        # AA-1028 slice 3: the effective budget (EFFECTIVE_SOURCE_SQL, then cost_guard's read)
        self.source = source or {"resolved_source": None, "override_enabled": None, "has_row": row is not None}
        self.gateway = gateway
        self.spend = spend or []
        self.missing = missing
        self.denied = denied
        self.prev = prev
        self.calls: list[tuple[str, tuple]] = []

    def _check(self, sql, args):
        self.calls.append((sql, args))
        for frag in self.missing:
            if frag in sql:
                raise asyncpg.exceptions.UndefinedTableError(f"{frag} does not exist")
        for frag in self.denied:
            if frag in sql:
                raise asyncpg.exceptions.InsufficientPrivilegeError("permission denied")

    def transaction(self):
        return _Tx()

    async def fetch(self, sql, *args):
        self._check(sql, args)
        if sql == tb.TIER_SQL:
            return [self.row] if self.row else []
        if sql == tb.MONTH_SPEND_SQL:
            return self.spend
        if sql == tb.ACTIVE_TENANTS_SQL:
            return self.active
        if sql == gw.ACTOR_NAMES_SQL:
            return [{"id": i, "name": "Mai Tran"} for i in args[0] if i == STAFF]
        raise AssertionError(f"unexpected SQL: {sql[:80]}")

    async def fetchrow(self, sql, *args):
        self._check(sql, args)
        if sql == tb.ONE_SQL:
            return self.row
        if sql == tb.LOCK_SQL:
            return self.prev
        if sql == tb.EFFECTIVE_SOURCE_SQL:
            return self.source
        if "LEFT JOIN shared.v_tenant_entitlement_resolved e" in sql:      # cost_guard._TENANT_BUDGET_SQL
            return self.gateway
        raise AssertionError(f"unexpected SQL: {sql[:80]}")

    async def fetchval(self, sql, *args):
        self._check(sql, args)
        if sql == tb.TENANT_EXISTS_SQL:
            return self.tenant_name
        raise AssertionError(f"unexpected SQL: {sql[:80]}")

    async def execute(self, sql, *args):
        self._check(sql, args)
        return "OK"

    def ran(self, sql):
        return [a for s, a in self.calls if s == sql]


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _req(conn):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=FakePool(conn))))


ROW = {"tenant_id": TENANT, "tenant_name": "Lotus Trails", "monthly_usd": 20.0, "alert_pct": 80, "approval_pct": 120,
       "hard_stop": True, "notes": "pilot", "created_at": NOW, "updated_at": NOW, "updated_by": f"admin:{STAFF}"}


# ── route policy ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_policy_reads_are_spend_read_and_writes_spend_write():
    p = staff_authz.POLICY
    assert p[("GET", "/admin/ai-gateway/experiments")] == staff_authz.SPEND_READ
    # AA-1035 R2: the Tenant 360 tile also reads for customer_success's book (tenant.book.account.read, scoped);
    # never for support (S267 review)
    assert p[("GET", "/admin/ai-gateway/tenant-budgets/{tenant_id}")] == (staff_authz.SPEND_READ,
                                                                          staff_authz.TENANT_BOOK_ACCOUNT_READ)
    assert p[("PUT", "/admin/ai-gateway/tenant-budgets/{tenant_id}")] == staff_authz.SPEND_WRITE
    assert p[("DELETE", "/admin/ai-gateway/tenant-budgets/{tenant_id}")] == staff_authz.SPEND_WRITE
    for role in ("content", "viewer"):
        assert staff_authz.SPEND_WRITE not in staff_authz.ROLE_PERMS[role]


# ── pure ───────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("monthly,spent,alert,approval,stop,expected", [
    (None, 5.0, 100, 150, False, (None, "unlimited")),
    (10.0, 2.0, 100, 150, False, (20.0, "ok")),
    (10.0, 8.0, 80, 150, False, (80.0, "alert")),
    (10.0, 15.0, 100, 150, True, (150.0, "approval")),
    (10.0, 15.0, 100, 150, False, (150.0, "over")),
    # AA-1028 slice 3 (S266 review): a 0 budget is "no AI spend", past every threshold before the first call
    (0.0, 0.0, 100, 150, True, (None, "approval")),
    (0.0, 0.0, 100, 150, False, (None, "over")),
    (0.0, 0.5, 100, 150, True, (None, "approval")),
])
def test_budget_state(monthly, spent, alert, approval, stop, expected):
    assert tb.budget_state(monthly, spent, alert, approval, stop) == expected


def test_month_window_wraps_december():
    assert tb.month_window(datetime(2026, 12, 9, tzinfo=timezone.utc)) == (
        datetime(2026, 12, 1, tzinfo=timezone.utc), datetime(2027, 1, 1, tzinfo=timezone.utc))


def test_budget_view_carries_spend_and_state():
    v = tb.budget_view(ROW, (17.5, 42))
    assert v["month_spent_usd"] == 17.5 and v["month_calls"] == 42 and v["used_pct"] == 87.5
    assert v["state"] == "alert" and v["updated_at"] == NOW.isoformat() and v["tenant_name"] == "Lotus Trails"
    assert tb.budget_view({**ROW, "monthly_usd": None})["state"] == "unlimited"


@pytest.mark.parametrize("value,names,expected", [
    (None, {}, ("none", None)),
    (STAFF, {STAFF: "Mai Tran"}, ("staff", "Mai Tran")),
    (STAFF.upper(), {STAFF: "Mai Tran"}, ("staff", "Mai Tran")),
    (STAFF, {}, ("staff", None)),                        # a former staff member: the page says "a staff member"
    (f"admin:{STAFF}", {STAFF: "Mai Tran"}, ("staff", "Mai Tran")),   # the actor form of the newer write routes
    ("migration-226", {}, ("migration", "Migration 226")),
    ("migration-280-wm-seed", {}, ("migration", "Migration 280")),
    ("ops-tester", {}, ("other", "ops-tester")),
    # C4 polish N4: an older "<username>:<reference>" value is a login; named only through the staff directory
    ("mai:ops12", {"user:mai": "Mai Tran"}, ("login", "Mai Tran")),
    ("Mai:OPS12", {"user:mai": "Mai Tran"}, ("login", "Mai Tran")),
    ("bao:ops12", {"user:mai": "Mai Tran"}, ("login", None)),   # not a staff username: the page says "staff (ops12)"
    ("Break-glass", {}, ("other", "Break-glass")),
    ("note: two words", {}, ("other", "note: two words")),     # not a login form: free text, as is
])
def test_actor_view(value, names, expected):
    out = gw.actor_view(value, names)
    assert (out["updated_by_kind"], out["updated_by_name"]) == expected


def test_actor_names_queries_only_staff_ids():
    class Conn:
        def __init__(self):
            self.args = None

        async def fetch(self, sql, *args):
            assert sql == gw.ACTOR_NAMES_SQL
            self.args = args
            return [{"id": STAFF, "name": "Mai Tran"}]

    c = Conn()
    assert asyncio.run(gw.actor_names(c, ["migration-226", None, STAFF.upper(), STAFF])) == {STAFF: "Mai Tran"}
    assert c.args == ([STAFF], [])
    assert asyncio.run(gw.actor_names(Conn(), ["migration-226", "", "Break-glass"])) == {}


def test_actor_names_also_resolves_login_usernames():
    """C4 polish N4: "<username>:<reference>" values are looked up by username (lower case) in the same query."""
    class Conn:
        def __init__(self):
            self.args = None

        async def fetch(self, sql, *args):
            assert sql == gw.ACTOR_NAMES_SQL
            self.args = args
            return [{"id": STAFF, "username": "mai", "name": "Mai Tran"}]

    c = Conn()
    names = asyncio.run(gw.actor_names(c, ["Mai:ops12", "bao:ops3", f"admin:{STAFF}", "migration-226"]))
    assert c.args == ([STAFF], ["bao", "mai"])
    assert names == {STAFF: "Mai Tran", "user:mai": "Mai Tran"}
    assert gw.actor_view("Mai:ops12", names)["updated_by_name"] == "Mai Tran"


def test_spend_budget_row_names_its_actor():
    r = {"provider": "dfs", "scope": "global", "per_run_usd": None, "per_day_usd": 10.0, "hard_stop": False,
         "alert_pct": 80, "updated_at": None, "updated_by": STAFF}
    out = gw.spend_budget_row(r, {"dfs": 1.0}, {}, {STAFF: "Mai Tran"})
    assert out["updated_by_kind"] == "staff" and out["updated_by_name"] == "Mai Tran" and out["updated_by"] == STAFF


# ── body ───────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_body_defaults_and_rounding():
    b = router_mod.TenantBudgetBody(monthly_usd=12.345, notes="  ")
    assert b.model_dump() == {"monthly_usd": 12.35, "alert_pct": 100, "approval_pct": 150, "hard_stop": True,
                              "notes": None}


@pytest.mark.parametrize("body", [
    {"monthly_usd": -1}, {"alert_pct": 0}, {"approval_pct": 1001}, {"alert_pct": 120, "approval_pct": 110},
    {"notes": "x" * 2001}, {"tenant_id": TENANT}, {"monthly_usd": 2_000_000},
])
def test_body_refuses_bad_values(body):
    with pytest.raises(ValidationError):
        router_mod.TenantBudgetBody(**body)


# ── GET ────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_get_one_with_budget_and_spend():
    conn = FakeConn(row=ROW, spend=[{"tenant_id": TENANT, "calls": 4, "cost_usd": 5.0}])
    out = asyncio.run(router_mod.get_tenant_budget(_req(conn), tenant_id=TENANT.upper()))
    assert out["ready"] is True and out["tenant_name"] == "Lotus Trails" and out["month_spent_usd"] == 5.0
    assert out["budget"]["used_pct"] == 25.0 and out["budget"]["state"] == "ok"
    assert out["budget"]["updated_by_name"] == "Mai Tran" and out["budget"]["updated_by_kind"] == "staff"
    assert out["defaults"] == tb.DEFAULTS and out["month_start"].endswith("-01T00:00:00+00:00")
    assert conn.ran(tb.TENANT_EXISTS_SQL) == [(TENANT,)]        # lower-cased before any read


def _gw(monthly, spent, *, alert=80, approval=100, stop=True, source="plan"):
    return {"tenant_id": TENANT, "monthly_usd": monthly, "alert_pct": alert, "approval_pct": approval,
            "hard_stop": stop, "source": source, "spent": spent, "calls": 3}


def test_get_one_without_a_row_shows_the_plan_budget_the_gateway_applies():
    # AA-1028 slice 3 (S266 review): a priced tenant with no row is not "unlimited": its plan's 15 USD hold at 100 %
    conn = FakeConn(row=None, spend=[{"tenant_id": TENANT, "calls": 3, "cost_usd": 15.0}],
                    source={"resolved_source": "plan", "override_enabled": None, "has_row": False},
                    gateway=_gw(15, 15.0))
    out = asyncio.run(tb.read_one(FakePool(conn), TENANT, now=NOW))
    assert out["budget"] is None
    assert out["effective"] == {"source": "plan", "monthly_usd": 15.0, "alert_pct": 80, "approval_pct": 100,
                                "hard_stop": True, "used_pct": 100.0, "state": "approval"}


def test_get_one_names_a_winning_subscription_override():
    conn = FakeConn(row=ROW, spend=[], source={"resolved_source": "override", "override_enabled": True,
                                                "has_row": True},
                    gateway=_gw(0.01, 0.0, alert=100, approval=150, stop=False, source="override"))
    out = asyncio.run(tb.read_one(FakePool(conn), TENANT, now=NOW))
    assert out["effective"]["source"] == "subscription_override" and out["effective"]["monthly_usd"] == 0.01
    assert out["budget"]["monthly_usd"] == ROW["monthly_usd"]           # the row edited here, shown as it is


def test_get_one_effective_is_unlimited_without_any_budget():
    conn = FakeConn(row=None, spend=[], gateway=None)
    out = asyncio.run(tb.read_one(FakePool(conn), TENANT, now=NOW))
    assert out["effective"]["source"] == "none" and out["effective"]["state"] == "unlimited"


def test_get_one_before_the_resolver_has_no_effective_block():
    conn = FakeConn(row=ROW, spend=[], missing=("v_tenant_entitlement_resolved",))
    out = asyncio.run(tb.read_one(FakePool(conn), TENANT, now=NOW))
    assert out["ready"] is True and out["effective"] is None and out["budget"] is not None


@pytest.mark.parametrize("resolved,override,has_row,expected", [
    ("override", True, True, "subscription_override"),
    (None, False, False, "subscription_override"),      # a disabled override is served as 0 USD (cost_guard)
    ("override", None, True, "budget_row"),
    (None, None, True, "budget_row"),                   # a row while no content subscription is live
    ("plan", None, False, "plan"),
    ("trial", None, False, "plan"),
    ("plan", True, False, "plan"),                      # an override that does not apply (no live content)
    (None, None, False, "none"),
])
def test_effective_source(resolved, override, has_row, expected):
    assert tb.effective_source(resolved, override, has_row) == expected


def test_get_one_before_migration_321_is_not_ready():
    conn = FakeConn(missing=("FROM shared.tenant_ai_budget",))
    out = asyncio.run(tb.read_one(FakePool(conn), TENANT, now=NOW))
    assert out["ready"] is False and out["budget"] is None and out["month_start"] == "2026-03-01T00:00:00+00:00"


def test_get_one_unknown_tenant_is_404():
    with pytest.raises(HTTPException) as e:
        asyncio.run(router_mod.get_tenant_budget(_req(FakeConn(tenant_name=None)), tenant_id=TENANT))
    assert e.value.status_code == 404


def test_tier_rows_and_not_ready():
    conn = FakeConn(row=ROW, spend=[{"tenant_id": TENANT, "calls": 9, "cost_usd": 30.0}])
    out = asyncio.run(tb.read_tier(FakePool(conn), now=NOW))
    assert out["ready"] is True and out["rows"][0]["state"] == "approval" and out["rows"][0]["used_pct"] == 150.0
    spend_args = conn.ran(tb.MONTH_SPEND_SQL)[0]
    assert spend_args == ([TENANT], datetime(2026, 3, 1, tzinfo=timezone.utc),
                          datetime(2026, 4, 1, tzinfo=timezone.utc))
    out = asyncio.run(tb.read_tier(FakePool(FakeConn(missing=("FROM shared.tenant_ai_budget",))), now=NOW))
    assert out == {"ready": False, "rows": []}


def test_tier_lists_the_budget_in_force_of_tenants_without_a_row():
    """S280 (AA-1096): the gateway enforces the resolved budget (override > row > plan), so the Budgets tab lists an
    active tenant whose plan gives it a budget even without a row (has_row False, effective source plan), and keeps
    a tenant with no budget at all out of the list."""
    other = "5d1e2f3a-4b5c-4d6e-8f7a-9b0c1d2e3f4a"
    conn = FakeConn(row=None, spend=[{"tenant_id": other, "calls": 2, "cost_usd": 6.0}],
                    source={"resolved_source": "plan", "override_enabled": None, "has_row": False},
                    gateway=_gw(15, 6.0), active=[{"tenant_id": other, "tenant_name": "Bamboo Roads"}])
    out = asyncio.run(tb.read_tier(FakePool(conn), now=NOW))
    (row,) = out["rows"]
    assert row["tenant_id"] == other and row["has_row"] is False and row["monthly_usd"] is None
    assert row["effective"]["source"] == "plan" and row["effective"]["monthly_usd"] == 15.0
    assert row["month_spent_usd"] == 6.0 and row["state"] == row["effective"]["state"]
    none = FakeConn(row=None, gateway=None, active=[{"tenant_id": other, "tenant_name": "Bamboo Roads"}])
    assert asyncio.run(tb.read_tier(FakePool(none), now=NOW))["rows"] == []


def test_tier_row_carries_the_effective_budget():
    conn = FakeConn(row=ROW, spend=[], source={"resolved_source": "override", "override_enabled": True,
                                                "has_row": True},
                    gateway=_gw(0.01, 0.0, alert=100, approval=150, stop=False, source="override"))
    (row,) = asyncio.run(tb.read_tier(FakePool(conn), now=NOW))["rows"]
    assert row["has_row"] is True and row["effective"]["source"] == "subscription_override"


# ── PUT / DELETE ───────────────────────────────────────────────────────────────────────────────────────────────────

def _audit(conn):
    rows = conn.ran(tb.AUDIT_SQL)
    assert len(rows) == 1
    tenant, actor, action, resource_type, resource_id, details = rows[0]
    return tenant, actor, action, resource_type, resource_id, json.loads(details)


def test_put_creates_and_audits():
    conn = FakeConn()
    body = router_mod.TenantBudgetBody(monthly_usd=25, alert_pct=80, approval_pct=120, hard_stop=True)
    out = asyncio.run(router_mod.put_tenant_budget(body, _req(conn), tenant_id=TENANT, x_admin_user_id=STAFF))
    assert out == {"tenant_id": TENANT, "created": True, "monthly_usd": 25.0, "alert_pct": 80, "approval_pct": 120,
                   "hard_stop": True, "notes": None}
    (args,) = conn.ran(tb.UPSERT_SQL)
    assert args == (TENANT, 25.0, 80, 120, True, None, f"admin:{STAFF}")
    tenant, actor, action, rtype, rid, details = _audit(conn)
    assert (tenant, actor, action, rtype, rid) == (TENANT, f"admin:{STAFF}", "tenant_ai_budget_set",
                                                   "shared.tenant_ai_budget", TENANT)
    assert details["before"] is None and details["after"]["monthly_usd"] == 25.0
    # the existing row is read under lock before the write (one transaction)
    sqls = [s for s, _ in conn.calls]
    assert sqls.index(tb.LOCK_SQL) < sqls.index(tb.UPSERT_SQL) < sqls.index(tb.AUDIT_SQL)


def test_put_change_keeps_the_before_values():
    prev = {"monthly_usd": 10.0, "alert_pct": 100, "approval_pct": 150, "hard_stop": False, "notes": None}
    conn = FakeConn(prev=prev)
    body = router_mod.TenantBudgetBody(monthly_usd=None)
    out = asyncio.run(router_mod.put_tenant_budget(body, _req(conn), tenant_id=TENANT, x_admin_user_id=None))
    assert out["created"] is False and out["monthly_usd"] is None
    _t, actor, _a, _r, _i, details = _audit(conn)
    assert actor == "admin" and details["before"]["monthly_usd"] == 10.0 and details["after"]["monthly_usd"] is None


def test_put_unknown_tenant_is_404_and_writes_nothing():
    conn = FakeConn(tenant_name=None)
    with pytest.raises(HTTPException) as e:
        asyncio.run(router_mod.put_tenant_budget(router_mod.TenantBudgetBody(), _req(conn), tenant_id=TENANT,
                                                 x_admin_user_id=STAFF))
    assert e.value.status_code == 404 and not conn.ran(tb.UPSERT_SQL) and not conn.ran(tb.AUDIT_SQL)


@pytest.mark.parametrize("kw", [{"missing": ("FROM shared.tenant_ai_budget",)},
                                {"denied": ("INSERT INTO shared.tenant_ai_budget",)}])
def test_put_before_migration_321_is_503(kw):
    conn = FakeConn(**kw)
    with pytest.raises(HTTPException) as e:
        asyncio.run(router_mod.put_tenant_budget(router_mod.TenantBudgetBody(monthly_usd=5), _req(conn),
                                                 tenant_id=TENANT, x_admin_user_id=STAFF))
    assert e.value.status_code == 503 and "migration 321" in e.value.detail and not conn.ran(tb.AUDIT_SQL)


def test_delete_removes_and_audits():
    prev = {"monthly_usd": 10.0, "alert_pct": 100, "approval_pct": 150, "hard_stop": False, "notes": "pilot"}
    conn = FakeConn(prev=prev)
    out = asyncio.run(router_mod.delete_tenant_budget(_req(conn), tenant_id=TENANT, x_admin_user_id=STAFF))
    assert out == {"tenant_id": TENANT, "deleted": True}
    assert conn.ran(tb.DELETE_SQL) == [(TENANT,)]
    _t, _actor, action, _r, _i, details = _audit(conn)
    assert action == "tenant_ai_budget_cleared" and details == {"before": prev, "after": None}


def test_delete_without_a_row_writes_nothing():
    conn = FakeConn()
    out = asyncio.run(router_mod.delete_tenant_budget(_req(conn), tenant_id=TENANT, x_admin_user_id=STAFF))
    assert out == {"tenant_id": TENANT, "deleted": False}
    assert not conn.ran(tb.DELETE_SQL) and not conn.ran(tb.AUDIT_SQL)


def test_delete_before_migration_321_is_503():
    conn = FakeConn(missing=("FROM shared.tenant_ai_budget",))
    with pytest.raises(HTTPException) as e:
        asyncio.run(router_mod.delete_tenant_budget(_req(conn), tenant_id=TENANT, x_admin_user_id=STAFF))
    assert e.value.status_code == 503


# ── experiments ────────────────────────────────────────────────────────────────────────────────────────────────────

class ExpConn:
    def __init__(self, *, missing=(), fail=()):
        self.missing, self.fail = missing, fail

    async def fetch(self, sql, *args):
        if any(f in sql for f in self.missing):
            raise asyncpg.exceptions.UndefinedTableError("missing")
        if any(f in sql for f in self.fail):
            raise OSError("db down")
        if sql == exp.PROMPT_VERSIONS_SQL:
            return [{"stage": "a1_rewrite", "version": "v3", "prompt_sha256": "ab" * 32, "source_path": "p/a1.md",
                     "notes": None, "created_at": NOW, "created_by": "migration-321"}]
        if sql == exp.EVAL_RUNS_SQL:
            assert args == (exp.EVAL_RUN_LIMIT,)
            return [{"id": "r1", "pipeline": "writing_memory", "verdict": "pass", "created_at": NOW,
                     "finished_at": None, "cost_usd": 0.42}]
        raise AssertionError(sql[:80])


def test_experiments_reads_both_lists():
    out = asyncio.run(router_mod.get_experiments(_req(ExpConn())))
    pv, er = out["prompt_versions"], out["eval_runs"]
    assert pv["ready"] and pv["rows"][0]["sha_short"] == "ab" * 6 and pv["rows"][0]["created_at"] == NOW.isoformat()
    assert er["ready"] and er["rows"][0]["created_at"] == NOW.isoformat() and er["rows"][0]["cost_usd"] == 0.42


def test_experiments_before_321_and_a_failing_list():
    out = asyncio.run(exp.read(FakePool(ExpConn(missing=("shared.prompt_version",)))))
    assert out["prompt_versions"] == {"ready": False, "rows": [], "limit": exp.PROMPT_VERSION_LIMIT, "error": None}
    assert out["eval_runs"]["ready"] is True
    out = asyncio.run(exp.read(FakePool(ExpConn(fail=("shared.prompt_eval_runs",)))))
    assert out["eval_runs"]["ready"] is False and "read failed (ref " in out["eval_runs"]["error"]
    assert out["prompt_versions"]["ready"] is True


def test_experiments_never_writes():
    for sql in (exp.PROMPT_VERSIONS_SQL, exp.EVAL_RUNS_SQL):
        assert not any(w in sql.upper() for w in ("INSERT", "UPDATE ", "DELETE"))
    assert "prompt_text" not in exp.PROMPT_VERSIONS_SQL


@pytest.mark.parametrize("values,ok", [
    ({"monthly_usd": None, "alert_pct": 80, "approval_pct": 100, "hard_stop": True}, False),   # no amount = unlimited
    ({"monthly_usd": 20.0, "alert_pct": 80, "approval_pct": 100, "hard_stop": True}, False),   # above the plan's 15
    ({"monthly_usd": 10.0, "alert_pct": 80, "approval_pct": 100, "hard_stop": True}, True),    # lower: tightens
    ({"monthly_usd": 10.0, "alert_pct": 90, "approval_pct": 100, "hard_stop": True}, False),   # later bell
])
def test_tighten_only_first_row_is_compared_with_the_plan_budget(values, ok):
    """S280 (AA-1096): a tenant without a row is held by its plan's included spend, so a tighten-only role's first row
    must not loosen that (before: any first row with the hard stop passed, even "no amount")."""
    conn = FakeConn(row=None, prev=None, source={"resolved_source": "plan", "override_enabled": None,
                                                  "has_row": False}, gateway=_gw(15, 3.0))
    if ok:
        assert asyncio.run(tb.upsert(FakePool(conn), TENANT, values, actor="admin:x", tighten_only=True))["created"]
    else:
        with pytest.raises(PermissionError):
            asyncio.run(tb.upsert(FakePool(conn), TENANT, values, actor="admin:x", tighten_only=True))
