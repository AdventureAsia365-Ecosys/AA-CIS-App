"""AA-1016 (AA-1018 part 2) — the tenant AI budget on the gateway's call path and the staff-approval wait.

No database: the budgeted-tenant set and the budget state come from the module's loaders (replaced here), the job
queue from a fake pool. Invented tenants only.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from shared import cost_guard
from shared.jobs import queue
from shared.llm_client import call_log, tenant_budget
from shared.llm_client.call_log import bind_tenant
from shared.llm_client.role_config import StageConfig
from tests.unit.test_aa1002_s3_deployment_routing import World

TENANT = "7a3e1c52-0f4b-4d2e-9c11-5b6a7d8e9f01"
OTHER = "1b2c3d4e-5f60-4718-8a9b-0c1d2e3f4a5b"


def _budget(tenant=TENANT, monthly=10.0, spent=0.0, alert=100, approval=150, hard=True):
    return cost_guard.TenantAIBudget(tenant_id=tenant, monthly_usd=monthly, month_spent_usd=spent, alert_pct=alert,
                                     approval_pct=approval, hard_stop=hard, month_calls=3)


@pytest.fixture
def state(monkeypatch):
    """{tenant: TenantAIBudget}; the loaders count their reads."""
    budgets: dict = {}
    reads = {"tenants": 0, "state": 0}

    def tenants():
        reads["tenants"] += 1
        return frozenset(budgets)

    def load(tid):
        reads["state"] += 1
        return budgets.get(tid)
    monkeypatch.setattr(tenant_budget, "loader_tenants", tenants)
    monkeypatch.setattr(tenant_budget, "loader_state", load)
    tenant_budget.invalidate()
    return budgets, reads


@pytest.fixture
def world(monkeypatch):
    from shared.llm_client import catalog, vendor_route
    hook = vendor_route.circuit_open
    w = World(monkeypatch)
    yield w
    w.reset()
    catalog.invalidate()
    vendor_route.circuit_open = hook


def _stage(monkeypatch, stage, model="gpt-6-luna"):
    cfg = StageConfig(stage=stage, role="writer", provider="claude", model_id=model, account_route="acc3")
    monkeypatch.setattr("shared.llm_client.client.get_stage_config_sync", lambda s: cfg)


# ── the budget rules ───────────────────────────────────────────────────────────────────────────────────────────────

def test_thresholds():
    assert _budget(spent=9.9).state == "ok"
    assert _budget(spent=10.0).state == "alert"
    assert _budget(spent=14.99).state == "alert"
    assert _budget(spent=15.0).state == "approval"
    assert _budget(spent=50.0, hard=False).state == "alert"          # no hard stop: never waits, only alerts
    assert _budget(monthly=0.0, spent=0.01).needs_approval            # a $0 budget: any spend waits
    assert _budget(monthly=0.0, spent=0.0).state == "ok"
    assert "wait for staff approval" in cost_guard.tenant_budget_message(_budget(spent=20.0))


def test_t_stages_only():
    assert tenant_budget.is_t_stage("t9_write") and tenant_budget.is_t_stage("t2_generate")
    assert not tenant_budget.is_t_stage("s1_generate") and not tenant_budget.is_t_stage("a3_atomize")
    assert not tenant_budget.is_t_stage("tp_search_embed") and not tenant_budget.is_t_stage(None)


# ── the gateway check ─────────────────────────────────────────────────────────────────────────────────────────────

def test_no_tenant_bound_reads_nothing(state):
    budgets, reads = state
    budgets[TENANT] = _budget(spent=99.0)
    assert tenant_budget.check("t9_write") is None
    assert reads == {"tenants": 0, "state": 0}


def test_a_tenant_without_a_budget_reads_only_the_cached_tenant_set(state):
    budgets, reads = state
    budgets[OTHER] = _budget(tenant=OTHER, spent=99.0)
    with bind_tenant(TENANT):
        for _ in range(5):
            assert tenant_budget.check("t9_write") is None
    assert reads == {"tenants": 1, "state": 0}


def test_a_tenant_past_approval_waits_on_t_stages_only(state):
    budgets, _ = state
    budgets[TENANT] = _budget(spent=16.0)
    with bind_tenant(TENANT):
        with pytest.raises(tenant_budget.TenantBudgetApprovalRequired) as info:
            tenant_budget.check("t9_write")
        assert tenant_budget.check("a3_atomize").needs_approval        # platform stages are never stopped
    err = info.value
    assert isinstance(err, cost_guard.BudgetExceeded) and isinstance(err, RuntimeError)
    assert str(err).startswith("tenant_ai_budget_approval: Tenant " + TENANT)
    assert tenant_budget.tenant_of_error(str(err)) == TENANT


def test_other_tenants_are_unaffected(state):
    budgets, _ = state
    budgets[TENANT] = _budget(spent=16.0)
    budgets[OTHER] = _budget(tenant=OTHER, spent=1.0)
    with bind_tenant(OTHER):
        assert tenant_budget.check("t9_write").state == "ok"


def test_an_unreadable_state_fails_open(state, monkeypatch):
    budgets, _ = state
    budgets[TENANT] = _budget(spent=16.0)

    def boom(tid):
        raise RuntimeError("db down")
    monkeypatch.setattr(tenant_budget, "loader_state", boom)
    with bind_tenant(TENANT):
        assert tenant_budget.check("t9_write") is None


def test_the_switch_turns_it_off(state, monkeypatch):
    budgets, _ = state
    budgets[TENANT] = _budget(spent=16.0)
    monkeypatch.setenv(tenant_budget.SWITCH_ENV, "off")
    with bind_tenant(TENANT):
        assert tenant_budget.check("t9_write") is None


def test_the_gateway_stops_before_any_attempt_and_logs_a_budget_row(world, monkeypatch, state):
    budgets, _ = state
    budgets[TENANT] = _budget(spent=16.0)
    rows: list = []

    async def record_calls(rows_):              # the keyword _in_background passes is `rows`
        rows.extend(rows_)
    monkeypatch.setattr(call_log, "record_calls", lambda rows: record_calls(rows))
    _stage(monkeypatch, "t9_write")
    with bind_tenant(TENANT):
        with pytest.raises(tenant_budget.TenantBudgetApprovalRequired):
            world.generate(stage="t9_write")
    assert world.calls == []
    call_log.wait_for_pending_writes()
    assert len(rows) == 1
    assert (rows[0]["status"], rows[0]["error_class"], rows[0]["cost_usd"]) == ("refused", "budget", 0.0)


def test_the_gateway_calls_as_before_below_approval(world, monkeypatch, state):
    budgets, _ = state
    budgets[TENANT] = _budget(spent=12.0)                              # alert only
    _stage(monkeypatch, "t9_write")
    with bind_tenant(TENANT):
        resp = world.generate(stage="t9_write")
    assert resp.content == "from acc3" and len(world.calls) == 1


# ── cost_guard reads and the bell ──────────────────────────────────────────────────────────────────────────────────

class Conn:
    def __init__(self, row=None, recent=False, missing=False):
        self.row, self.recent, self.missing = row, recent, missing
        self.executed: list = []

    async def fetchrow(self, sql, *args):
        if self.missing:
            raise type("UndefinedTableError", (Exception,), {})("no table")
        assert "shared.tenant_ai_budget" in sql and args == (TENANT,)
        return self.row

    async def fetch(self, sql, *args):
        if self.missing:
            raise type("UndefinedTableError", (Exception,), {})("no table")
        return [{"tenant_id": TENANT}]

    async def fetchval(self, sql, *args):
        return self.recent

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


def test_the_budget_read_and_the_missing_table():
    row = {"tenant_id": TENANT, "monthly_usd": 10, "alert_pct": 100, "approval_pct": 150, "hard_stop": True,
           "spent": 15.5, "calls": 7}
    b = asyncio.run(cost_guard.tenant_budget_check(Conn(row), TENANT))
    assert b.needs_approval and b.month_calls == 7
    assert asyncio.run(cost_guard.tenant_budget_check(Conn(None), TENANT)) is None
    assert asyncio.run(cost_guard.tenant_budget_check(Conn(missing=True), TENANT)) is None
    assert asyncio.run(cost_guard.budgeted_tenants(Conn(missing=True))) is None
    assert asyncio.run(cost_guard.budgeted_tenants(Conn())) == frozenset({TENANT})


def test_one_bell_per_tenant_and_day(monkeypatch):
    sent = []

    async def email(conn, **kw):
        sent.append(kw)
    from shared import ops_alerts
    monkeypatch.setattr(ops_alerts, "enqueue_spend_budget_email", email)
    conn = Conn()
    assert asyncio.run(cost_guard.maybe_alert_tenant(conn, _budget(spent=16.0))) is True
    sql, args = conn.executed[0]
    assert args[2] == f"tenant_ai_budget:{TENANT}" and "approval" in args[3]
    assert sent and sent[0]["stopped"] is True
    assert asyncio.run(cost_guard.maybe_alert_tenant(Conn(recent=True), _budget(spent=16.0))) is False
    assert asyncio.run(cost_guard.maybe_alert_tenant(Conn(), _budget(spent=1.0))) is False


# ── the wait and the bulk re-queue ─────────────────────────────────────────────────────────────────────────────────

T0 = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
STOP = f"tenant_ai_budget_approval: {cost_guard.tenant_budget_message(_budget(spent=16.0))}"


class FakePool:
    def __init__(self, jobs, budget=None):
        self.jobs, self.budget = jobs, budget
        self.candidate_args = None
        self.updates: list = []

    async def fetch(self, sql, *args):
        assert "FROM shared.job j" in sql
        self.candidate_args = args
        return self.jobs

    async def fetchrow(self, sql, *args):
        assert "status = $3" in sql and args[2] == "stopped_budget"
        self.updates.append(args)
        return {"id": args[0]}

    def acquire(self):
        budget = self.budget

        class Ctx:
            async def __aenter__(self):
                c = MagicMock()

                async def fetchrow(sql, *args):
                    return None if budget is None else {
                        "tenant_id": TENANT, "monthly_usd": budget.monthly_usd, "alert_pct": budget.alert_pct,
                        "approval_pct": budget.approval_pct, "hard_stop": budget.hard_stop,
                        "spent": budget.month_spent_usd, "calls": 1}
                c.fetchrow = fetchrow
                return c

            async def __aexit__(self, *exc):
                return False
        return Ctx()


def _job(i, error=STOP):
    return {"id": f"00000000-0000-0000-0000-00000000000{i}", "kind": "t9_write", "payload": "{}", "error": error,
            "finished_at": T0}


@pytest.fixture
def kinds(monkeypatch):
    from shared.jobs import registry
    monkeypatch.setattr(registry, "get_kind", lambda k: MagicMock(retry_refusal=None))
    monkeypatch.setattr("shared.jobs.worker.load_kinds", lambda: None)


def test_jobs_wait_while_the_tenant_still_needs_approval(kinds):
    pool = FakePool([_job(1), _job(2)], budget=_budget(spent=16.0))
    out = asyncio.run(queue.retry_by_reason(pool, queue.RETRY_REASON_TENANT_BUDGET, since=T0 - timedelta(days=1),
                                            dry_run=False))
    assert pool.candidate_args[0] == "tenant_ai_budget_approval" and pool.candidate_args[4] == "stopped_budget"
    assert out["eligible"] == 0 and out["held_count"] == 2 and pool.updates == []
    assert "still waits for approval" in out["held"][0]["held"]
    assert TENANT in out["tenants_waiting"]


@pytest.mark.parametrize("approved", [_budget(monthly=40.0, spent=16.0), _budget(spent=16.0, hard=False), None])
def test_an_approval_releases_the_jobs(kinds, approved):
    pool = FakePool([_job(1), _job(2)], budget=approved)
    out = asyncio.run(queue.retry_by_reason(pool, queue.RETRY_REASON_TENANT_BUDGET, since=T0 - timedelta(days=1),
                                            dry_run=False, rand=lambda: 0.0))
    assert out["requeued"] == 2 and [u[2] for u in pool.updates] == ["stopped_budget", "stopped_budget"]


def test_a_stop_that_names_no_tenant_stays_held(kinds):
    pool = FakePool([_job(1, error="tenant_ai_budget_approval: something else")])
    out = asyncio.run(queue.retry_by_reason(pool, queue.RETRY_REASON_TENANT_BUDGET, since=T0, dry_run=True))
    assert out["held_count"] == 1 and "names no tenant" in out["held"][0]["held"]


def test_the_route_accepts_only_the_two_reasons():
    from pydantic import ValidationError

    from api.routers.admin_job_runner import RetryLlmDeniedRequest
    assert RetryLlmDeniedRequest().reason == "llm_access_denied"
    assert RetryLlmDeniedRequest(reason="tenant_ai_budget_approval").reason == "tenant_ai_budget_approval"
    with pytest.raises(ValidationError):
        RetryLlmDeniedRequest(reason="anything")
