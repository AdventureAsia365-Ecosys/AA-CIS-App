"""AA-1143 (tech lead decision G3; CIS 455 / 456) — one budget per LLM connection: the rules of a save
(api/services/llm_connection_split.py), the service (api/services/llm_connection_budgets.py: the read, a save with the
CRM copy sent first when the CRM's share goes down and after when it goes up, the CRM refusing a lower share), the
routes' policy and the CIS gateway's spend rule (shared/cost_guard.py: an app with its own share counts only above it).
No database (a fake connection), no network (a fake relay); invented connection keys, apps and amounts only."""
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest

from api.core.staff_authz import POLICY, SPEND_READ, SPEND_WRITE
from api.services import llm_connection_budgets as budgets
from api.services import llm_connection_split as rules
from shared import cost_guard
from shared.llm_client import connection_budget
from shared.llm_client import connections as conn_registry
from shared.llm_client.connections import ProviderConnection

APPS = ["booking", "crm", "tripplanner"]
D = Decimal
REASON = "Give the CRM more room for its drafts this month"


# ── the rules ────────────────────────────────────────────────────────────────────────────────────────────────────

def test_changing_one_share_moves_only_the_remainder():
    plan = rules.plan_edit(APPS, D("100"), {"crm": D("5"), "tripplanner": None, "booking": None}, shares={"crm": 8})
    assert plan["remainder"] == D("92.00") and plan["total"] == D("100")
    assert plan["changes"] == [("crm", D("8.00"))]


def test_decreases_come_first_then_the_total_then_increases():
    plan = rules.plan_edit(APPS, D("100"), {"crm": D("5"), "tripplanner": D("10"), "booking": None},
                           total=60, shares={"crm": 9, "tripplanner": 2})
    assert plan["changes"] == [("tripplanner", D("2.00")), (None, D("60.00")), ("crm", D("9.00"))]


@pytest.mark.parametrize("total, shares, code", [
    (None, {"crm": 101}, "over_allocated"),
    (4, None, "over_allocated"),
    (None, {"cis": 3}, "cis_is_remainder"),
    (None, {"zz-app": 3}, "unknown_app"),
    (None, {"crm": -1}, "amount_invalid"),
    (None, {"crm": "lots"}, "amount_invalid"),
    (2000000, None, "amount_invalid"),
])
def test_refused_edits(total, shares, code):
    with pytest.raises(rules.BudgetEditRefused) as e:
        rules.plan_edit(APPS, D("100"), {"crm": D("5"), "tripplanner": None, "booking": None}, total, shares)
    assert e.value.code == code


def test_a_share_needs_a_total_and_a_reason_needs_twenty_characters():
    with pytest.raises(rules.BudgetEditRefused) as e:
        rules.plan_edit(APPS, None, {a: None for a in APPS}, shares={"crm": 1})
    assert e.value.code == "total_required"
    with pytest.raises(rules.BudgetEditRefused):
        rules.check_reason("too short")
    assert rules.check_reason(f"  {REASON}  ") == REASON


def test_effective_caps_and_the_crm_order():
    caps = rules.effective_caps(D("100"), {"crm": D("5"), "tripplanner": None})
    assert caps == {"crm": 5.0, "tripplanner": None, "cis": 95.0, "total": 100.0}
    assert rules.crm_tightens(D("5"), D("3")) and rules.crm_tightens(D("5"), None)
    assert not rules.crm_tightens(D("5"), D("8")) and not rules.crm_tightens(None, D("1"))


def test_the_crm_copy_names_every_connection_with_a_total():
    rows = {"zz-a": {None: {"budget_usd": D("100")}, "crm": {"budget_usd": D("5")}},
            "zz-b": {None: {"budget_usd": D("20")}}, "zz-c": {"tripplanner": {"budget_usd": D("1")}}}
    assert budgets.crm_copy(rows) == {"zz-a": "5.00", "zz-b": "0.00"}


@pytest.mark.parametrize("has_total, crm, state", [
    (False, {}, budgets.CRM_NOT_SENT),
    (True, {"copy": None, "copy_error": "crm_status_503"}, budgets.CRM_UNKNOWN),
    (True, {"copy": {}}, budgets.CRM_NOT_SENT),
    (True, {"copy": {"zz-a": 5.0}}, budgets.CRM_IN_SYNC),
    (True, {"copy": {"zz-a": 4.0}}, budgets.CRM_DIFFERS),
])
def test_crm_row_state(has_total, crm, state):
    assert budgets.crm_row_state("zz-a", has_total, "5.00", crm) == state


# ── the service, on a fake database ──────────────────────────────────────────────────────────────────────────────

def _registry_row(key, budget):
    return ProviderConnection(connection_key=key, kind="bedrock", auth_mode="assume_role", label=f"Zz {key}",
                              data_scope="aws_only", enabled=True, monthly_budget_usd=budget)


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, world):
        self.w = world

    def transaction(self):
        return _Tx()

    async def fetch(self, sql, *args):
        if "llm_app_profile" in sql:
            return [{"app": a} for a in ("booking", "cis", "crm", "tripplanner")]
        if "llm_connection_budget_current" in sql:
            return [{"connection_key": k, "app": app, "budget_usd": usd, "version": 1, "reason": "zz seeded reason",
                     "actor": "migration:456", "valid_from": "2026-11-19T00:00:00+00:00"}
                    for (k, app), usd in self.w["rows"].items()]
        if "max(version)" in sql:
            return [{"app": app, "version": 1} for (k, app) in self.w["rows"] if k == args[0]]
        if "llm_call_log" in sql:
            return self.w["spend"]
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        self.w["log"].append(("sql", sql.split()[0] + " " + sql.split()[2] if "INSERT" in sql else sql.split()[0]))
        if "shared.llm_connection_budget (" in sql:
            key, apps, usd, versions = args[0], args[1], args[2], args[3]
            self.w["inserted"].append(list(zip(apps, usd, versions)))
            for app, v in zip(apps, usd):
                if v is None:
                    self.w["rows"].pop((key, app), None)
                else:
                    self.w["rows"][(key, app)] = v


class FakePool:
    def __init__(self, world):
        self.conn = FakeConn(world)

    @asynccontextmanager
    async def acquire(self):
        yield self.conn

    async def execute(self, sql, *args):
        return await self.conn.execute(sql, *args)


@pytest.fixture
def world(monkeypatch):
    w = {"registry": {"zz-a": _registry_row("zz-a", 95), "zz-b": _registry_row("zz-b", 7)},
         "rows": {("zz-a", None): D("100"), ("zz-a", "crm"): D("5")},
         "spend": [{"account": "zz-a", "app": "cis", "usd": D("40"), "calls": 3},
                   {"account": "zz-a", "app": "tripplanner", "usd": D("2"), "calls": 1}],
         "crm_put": (200, {}), "log": [], "inserted": []}

    async def fetch_connections(conn, **_):
        return w["registry"]

    async def read_cost_policies(conn, **_):
        return {}

    async def crm_ops_call(method, path, authorization, *, json=None, params=None):
        w["log"].append(("crm", method, path, json))
        if method == "PUT":
            return w["crm_put"]
        if path == budgets.CRM_SHARES_PATH:
            return 200, {"shares": [{"connection_key": "zz-a", "share_usd": "5.00"}],
                         "updated_at": "2026-11-19T00:00:00+00:00"}
        return 200, {"settings": [{"key": rules.CRM_CAP_KEY, "value": {"zz-a": 5}}]}

    monkeypatch.setattr(conn_registry, "fetch_connections", fetch_connections)
    monkeypatch.setattr(conn_registry, "invalidate", lambda: None)
    monkeypatch.setattr(connection_budget, "invalidate", lambda: None)
    monkeypatch.setattr(cost_guard, "read_cost_policies", read_cost_policies)
    monkeypatch.setattr(budgets.crm_console, "crm_ops_call", crm_ops_call)
    return w


@pytest.mark.asyncio
async def test_read_shows_total_shares_remainder_and_the_crm_copy(world):
    out = await budgets.read(FakePool(world), "Bearer zz")
    assert out["ready"] is True and out["apps"] == APPS
    a = {r["connection_key"]: r for r in out["rows"]}["zz-a"]
    assert a["total"]["usd"] == 100.0 and a["shares"]["crm"]["usd"] == 5.0 and a["shares"]["tripplanner"] is None
    assert a["cis_remainder_usd"] == 95.0 and a["cis_cap_in_sync"] is True
    assert a["cis_pool_spent_usd"] == 42.0                     # TripPlanner has no share: inside the CIS part
    assert a["crm_state"] == budgets.CRM_IN_SYNC and a["crm_setting_usd"] == 5.0
    b = {r["connection_key"]: r for r in out["rows"]}["zz-b"]
    assert b["total"] is None and b["cis_cap_usd"] == 7.0 and b["crm_state"] == budgets.CRM_NOT_SENT


@pytest.mark.asyncio
async def test_a_higher_crm_share_writes_cis_first_then_sends_the_copy(world):
    out = await budgets.save(FakePool(world), "Bearer zz", "zz-a", shares={"crm": 8}, reason=REASON,
                             actor="admin:zz")
    assert out["cis_remainder_usd"] == 92.0 and out["crm_sync"] == {"sent": True, "error": None}
    assert world["inserted"] == [[("crm", D("8.00"), 2)]]
    puts = [e for e in world["log"] if e[0] == "crm" and e[1] == "PUT"]
    assert puts[0][3]["shares"] == [{"connection_key": "zz-a", "share_usd": "8.00"}]
    order = [e[0] if e[0] == "crm" else e[1] for e in world["log"]]
    assert order.index("INSERT shared.llm_connection_budget") < order.index("crm")


@pytest.mark.asyncio
async def test_a_lower_crm_share_is_sent_first_and_a_refusal_writes_nothing(world):
    world["crm_put"] = (503, {"detail": "crm_unavailable"})
    with pytest.raises(budgets.CrmRefused):
        await budgets.save(FakePool(world), "Bearer zz", "zz-a", shares={"crm": 2}, reason=REASON, actor="admin:zz")
    assert world["inserted"] == [] and world["rows"][("zz-a", "crm")] == D("5")
    world["crm_put"] = (200, {})
    out = await budgets.save(FakePool(world), "Bearer zz", "zz-a", shares={"crm": 2}, reason=REASON, actor="admin:zz")
    order = [e[0] if e[0] == "crm" else e[1] for e in world["log"] if e[0] == "crm" or "llm_connection" in e[1]]
    assert order[-2:] == ["crm", "INSERT shared.llm_connection_budget"] and out["cis_remainder_usd"] == 98.0


@pytest.mark.asyncio
async def test_a_failed_send_after_a_rise_is_reported_not_raised(world):
    world["crm_put"] = (503, {"detail": "crm_unavailable"})
    out = await budgets.save(FakePool(world), "Bearer zz", "zz-a", shares={"crm": 9}, reason=REASON, actor="admin:zz")
    assert out["crm_sync"] == {"sent": False, "error": "crm_unavailable"}
    assert world["rows"][("zz-a", "crm")] == D("9.00")


@pytest.mark.asyncio
async def test_a_tighten_only_role_may_not_raise_any_part(world):
    with pytest.raises(PermissionError):           # the CRM share down = the CIS part up
        await budgets.save(FakePool(world), "Bearer zz", "zz-a", shares={"crm": 1}, reason=REASON, actor="admin:zz",
                           role="ai_ops")
    out = await budgets.save(FakePool(world), "Bearer zz", "zz-a", total=90, reason=REASON, actor="admin:zz",
                             role="ai_ops")
    assert out["cis_remainder_usd"] == 85.0


@pytest.mark.asyncio
async def test_unknown_connection_and_nothing_to_change(world):
    with pytest.raises(LookupError):
        await budgets.save(FakePool(world), None, "zz-none", total=1, reason=REASON, actor="admin:zz")
    with pytest.raises(rules.BudgetEditRefused):
        await budgets.save(FakePool(world), None, "zz-a", reason=REASON, actor="admin:zz")
    out = await budgets.save(FakePool(world), None, "zz-a", shares={"crm": 5}, reason=REASON, actor="admin:zz")
    assert out["changed"] is False and world["inserted"] == []


# ── policy and the gateway's spend rule ──────────────────────────────────────────────────────────────────────────

def test_route_policy():
    assert POLICY[("GET", "/admin/ai-gateway/connection-budgets")] == SPEND_READ
    assert POLICY[("GET", "/admin/ai-gateway/connection-budgets/{connection_key}/history")] == SPEND_READ
    assert POLICY[("PUT", "/admin/ai-gateway/connection-budgets/{connection_key}")] == SPEND_WRITE
    assert POLICY[("POST", "/admin/ai-gateway/connection-budgets/crm-sync")] == SPEND_WRITE


def test_the_cis_spend_counts_an_app_with_a_share_only_above_it():
    from shared import connection_pool_spend

    sql = " ".join(connection_pool_spend.POOL_SPEND_SQL.split())
    assert "greatest(s.usd - b.budget_usd, 0)" in sql and "llm_connection_budget_current" in sql


@pytest.mark.asyncio
async def test_before_455_the_whole_ledger_counts():
    class UndefinedTableError(Exception):
        pass

    calls = []

    class Conn:
        async def fetch(self, sql, *args):
            calls.append(sql)
            if "llm_connection_budget_current" in sql:
                raise UndefinedTableError("relation does not exist")
            return [{"account": "zz-a", "spent": D("3"), "calls": 2}]

    assert await cost_guard.connection_month_spend(Conn(), ["zz-a"]) == {"zz-a": (3.0, 2)}
    assert len(calls) == 2 and calls[1] == cost_guard._CONNECTION_MONTH_SPEND_SQL


@pytest.mark.asyncio
async def test_a_managed_cis_cap_cannot_be_edited_on_the_connection():
    from api.services import llm_connections

    class Db:
        def __init__(self, migrated, managed):
            self.answers = [migrated, managed]

        async def fetchval(self, sql, *args):
            return self.answers.pop(0)

    with pytest.raises(llm_connections.ConnectionToggleRefused, match="Budgets"):
        await llm_connections._refuse_managed_budget(Db(True, 1), "zz-a", ["monthly_budget_usd"])
    await llm_connections._refuse_managed_budget(Db(True, None), "zz-a", ["monthly_budget_usd"])   # no total yet
    await llm_connections._refuse_managed_budget(Db(False, None), "zz-a", ["monthly_budget_usd"])  # before CIS 455
    await llm_connections._refuse_managed_budget(Db(True, 1), "zz-a", ["rpm_limit"])               # another field
