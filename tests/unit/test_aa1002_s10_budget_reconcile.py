"""AA-1002 (gateway slice S10) — the monthly budget per LLM connection and the nightly cost reconciliation.

- cost_guard.ConnectionBudget: states (ok / alert / stopped), the defaults without a policy row, the bell once per 24 h.
- connection_budget (the call path): a connection without a budget is never read; the state is cached; a read failure
  fails open; LLM_CONNECTION_BUDGETS=off switches it off.
- The route: a connection over its hard budget is left out (the next deployment / model answers); when every way in
  is over budget the error is a BudgetExceeded (the job stops as stopped_budget), never a retryable outage.
- services/llm_cost/reconcile.py: the tolerance, the Anthropic / OpenAI usage parsers (fake HTTP, never a real call),
  the stored Cost Explorer snapshot for Bedrock, not_configured without a usage key, the upsert + prune + bell.
- The admin: the policy fields of PATCH (validated before any secret is read, upserted in the transaction, audited),
  GET /admin/llm-connections/costs, POST /admin/llm-connections/reconcile, the nightly schedule.
"""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from fastapi import HTTPException

from api.core import ops_metrics_sampler, staff_authz
from api.routers import admin_llm_ops as ops
from api.services import llm_connections
from services.jobs import llm_cost_reconcile_job as job
from services.llm_cost import reconcile
from shared import cost_guard
from shared.llm_client import catalog, connection_budget, connections, vendor_route
from shared.llm_client.connections import BUILTIN_CONNECTIONS, ProviderConnection
from tests.unit import test_aa1002_s3_deployment_routing as s3
from tests.unit.test_aa1002_s3_deployment_routing import _dep, _org, _route

TODAY = date(2026, 10, 30)


@pytest.fixture
def world(monkeypatch):
    """The S3 routing world (registry + fake providers), restored afterwards."""
    hook = vendor_route.circuit_open
    w = s3.World(monkeypatch)
    yield w
    w.reset()
    catalog.invalidate()
    vendor_route.circuit_open = hook


def _budget(key="acc3", cap=10.0, spent=0.0, alert=80, hard=True):
    return cost_guard.ConnectionBudget(connection_key=key, monthly_budget_usd=cap, month_spent_usd=spent,
                                       alert_pct=alert, hard_stop=hard)


# ── cost_guard.ConnectionBudget ───────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("spent, alert, hard, state", [
    (1.0, 80, True, "ok"), (8.0, 80, True, "alert"), (10.0, 80, True, "stopped"), (12.0, 80, False, "alert"),
    (5.0, 50, True, "alert"),
])
def test_budget_states(spent, alert, hard, state):
    b = _budget(spent=spent, alert=alert, hard=hard)
    assert b.state == state
    assert b.stops == (state == "stopped")


def test_a_zero_budget_means_no_spend_allowed():
    assert _budget(cap=0.0, spent=0.0).stops                    # reached at once
    assert _budget(cap=0.0, spent=0.0).used_pct is None
    assert _budget(cap=0.0, spent=0.01).used_pct == 100.0


def test_connection_budgets_reads_spend_and_defaults_without_a_policy_row():
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[
        [{"connection_key": "openai-direct", "alert_pct": 50, "hard_stop": False, "usage_secret_name": None,
          "usage_scope_id": None, "notes": None, "updated_at": None, "updated_by": "admin:u"}],
        [{"account": "acc3", "spent": Decimal("9.5"), "calls": 3}],
    ])
    out = asyncio.run(cost_guard.connection_budgets(conn, {"acc3": 10, "openai-direct": 5, "acc2": None}))
    assert set(out) == {"acc3", "openai-direct"}                  # acc2 has no budget: never enforced
    assert out["acc3"].alert_pct == 80 and out["acc3"].hard_stop and out["acc3"].state == "alert"
    assert out["openai-direct"].month_spent_usd == 0.0 and not out["openai-direct"].hard_stop
    assert conn.fetch.await_args_list[1].args[1] == ["acc3", "openai-direct"]


def test_connection_budgets_without_any_budget_reads_nothing():
    conn = MagicMock()
    conn.fetch = AsyncMock()
    assert asyncio.run(cost_guard.connection_budgets(conn, {"acc3": None})) == {}
    conn.fetch.assert_not_awaited()


def test_policies_before_migration_316_are_the_defaults():
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=asyncpg.exceptions.UndefinedTableError("missing"))
    assert asyncio.run(cost_guard.read_cost_policies(conn)) == {}
    marker = object()
    assert asyncio.run(cost_guard.read_cost_policies(conn, missing=marker)) is marker


def test_the_alert_bell_is_written_once_per_24h_and_queues_the_email(monkeypatch):
    from shared import ops_alerts
    email = AsyncMock(return_value="job-1")
    monkeypatch.setattr(ops_alerts, "enqueue_spend_budget_email", email)
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    assert asyncio.run(cost_guard.maybe_alert_connection(conn, _budget(spent=10.0))) is True
    args = conn.execute.await_args.args
    assert args[2] == cost_guard.BUDGET_ALERT_EVENT and args[3] == "llm_connection:acc3"
    assert "leaves it out of every route" in json.loads(args[4])["message"]
    assert email.await_args.kwargs["stopped"] is True
    conn.fetchval = AsyncMock(return_value=1)                    # an unread bell of the last 24 h
    assert asyncio.run(cost_guard.maybe_alert_connection(conn, _budget(spent=10.0))) is False
    assert asyncio.run(cost_guard.maybe_alert_connection(conn, _budget(spent=1.0))) is False


def test_the_vendor_providers_have_a_day_spend():
    assert {"anthropic", "azure_openai"} <= set(cost_guard.PROVIDERS)
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=Decimal("1.25"))
    assert asyncio.run(cost_guard.day_spend(conn, "anthropic")) == 1.25
    assert "provider = 'anthropic'" in conn.fetchval.await_args.args[0]


# ── connection_budget: the call path ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def registry(monkeypatch):
    rows = dict(BUILTIN_CONNECTIONS)
    monkeypatch.setattr(connections, "registry_rows_sync", lambda: dict(rows))
    connection_budget.invalidate()
    yield rows
    connection_budget.invalidate()


def test_no_budget_means_no_database_read(registry, monkeypatch):
    run = MagicMock()
    monkeypatch.setattr(connections, "run_sync", run)
    assert connection_budget.budget_of("acc3") is None
    assert connection_budget.stops(registry["acc3"]) is None
    run.assert_not_called()


def test_the_state_is_loaded_once_and_cached(registry, monkeypatch):
    import dataclasses
    registry["acc3"] = dataclasses.replace(registry["acc3"], monthly_budget_usd=10.0)
    run = MagicMock(return_value={"acc3": _budget(spent=11.0)})
    monkeypatch.setattr(connections, "run_sync", run)
    assert connection_budget.stops(registry["acc3"]).state == "stopped"
    assert connection_budget.stops(registry["acc3"]) is not None
    assert run.call_count == 1
    monkeypatch.setenv(connection_budget.SWITCH_ENV, "off")
    assert connection_budget.stops(registry["acc3"]) is None


def test_an_unreadable_state_fails_open(registry, monkeypatch):
    import dataclasses
    registry["acc3"] = dataclasses.replace(registry["acc3"], monthly_budget_usd=10.0)
    run = MagicMock(side_effect=OSError("db down"))
    monkeypatch.setattr(connections, "run_sync", run)
    assert connection_budget.stops(registry["acc3"]) is None
    assert connection_budget.stops(registry["acc3"]) is None
    assert run.call_count == 1                                   # the failure is cached for its window too


def test_the_loader_alerts_each_alerting_connection(monkeypatch):
    calls = []
    monkeypatch.setattr(cost_guard, "connection_budgets",
                        AsyncMock(return_value={"acc3": _budget(spent=9.0), "acc2": _budget("acc2", spent=1.0)}))
    monkeypatch.setattr(cost_guard, "maybe_alert_connection", AsyncMock(side_effect=lambda c, b: calls.append(b)))
    fake = MagicMock()
    fake.close = AsyncMock()
    monkeypatch.setattr(connections, "_connect", AsyncMock(return_value=fake))
    out = asyncio.run(connection_budget._load((("acc2", 10.0), ("acc3", 10.0))))
    assert set(out) == {"acc2", "acc3"} and [b.connection_key for b in calls] == ["acc3"]
    fake.close.assert_awaited_once()


# ── the route ─────────────────────────────────────────────────────────────────────────────────────────────────────

def _over(monkeypatch, *keys):
    monkeypatch.setattr(connection_budget, "stops",
                        lambda c: _budget(c.connection_key, spent=20.0) if c.connection_key in keys else None)


def test_a_connection_over_budget_is_left_out_and_the_next_deployment_answers(world, monkeypatch):
    world.conns["bedrock-llm-org"] = _org()
    world.deps.append(_dep("sonnet-5-5", "bedrock-llm-org", "us.anthropic.claude-sonnet-5-5", 20))
    world.reset()
    _over(monkeypatch, "acc3")
    assert world.generate(model_tier="sonnet-5-5").content == "from bedrock-llm-org"
    assert [c[1] for c in world.calls] == ["bedrock-llm-org"]


def test_every_way_in_over_budget_is_a_budget_stop_without_a_call(world, monkeypatch):
    _over(monkeypatch, "acc3")
    with pytest.raises(cost_guard.BudgetExceeded) as e:
        world.generate(model_tier="sonnet-5-5")
    assert isinstance(e.value, RuntimeError) and e.value.connection_keys == ("acc3",)
    assert world.calls == []


def test_a_route_whose_every_model_is_over_budget_stops_the_job(world, monkeypatch):
    _route(monkeypatch, "sonnet-5-5", "gpt-6-luna")
    _over(monkeypatch, "acc3")
    with pytest.raises(connection_budget.ConnectionBudgetExceeded):
        world.generate(stage="adhoc_s3")
    assert world.calls == []


def test_a_route_moves_to_a_model_on_another_connection(world, monkeypatch):
    world.conns["bedrock-llm-org"] = _org()
    world.deps.append(_dep("gpt-6-luna", "bedrock-llm-org", "us.openai.gpt-6-luna", 20))
    world.reset()
    _route(monkeypatch, "sonnet-5-5", "gpt-6-luna")
    _over(monkeypatch, "acc3")
    resp = world.generate(stage="adhoc_s3")
    assert resp.content == "from bedrock-llm-org" and resp.fallback_used


def test_a_mixed_failure_stays_a_retryable_outage(world, monkeypatch):
    _route(monkeypatch, "sonnet-5-5", "gpt-6-luna")
    monkeypatch.setattr(connection_budget, "stops", lambda c: None)
    world.fail = {"acc3": RuntimeError("acc3 outage")}
    with pytest.raises(RuntimeError) as e:
        world.generate(stage="adhoc_s3")
    assert not isinstance(e.value, cost_guard.BudgetExceeded)


def test_the_plan_names_budget_skips_after_the_circuit(world, monkeypatch):
    _over(monkeypatch, "acc3")
    plan = vendor_route.deployment_plan(catalog.get_model_sync("sonnet-5-5"))
    assert list(plan) == [] and [k for k, _ in plan.budget_skips] == ["acc3"] and plan.circuit_skips == []
    vendor_route.circuit_open = lambda conn, model: conn == "acc3"
    plan = vendor_route.deployment_plan(catalog.get_model_sync("sonnet-5-5"))
    assert plan.circuit_skips and plan.budget_skips == []           # an open circuit keeps its own error class


# ── reconcile: pure parts and the usage parsers ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("logged, provider, status", [
    (1.00, 1.02, "ok"), (1.00, 1.20, "mismatch"), (10.0, 10.4, "ok"), (10.0, 11.0, "mismatch"), (0.0, 0.0, "ok"),
])
def test_compare_tolerance(logged, provider, status):
    assert reconcile.compare(logged, provider)[0] == status


def test_compare_without_a_figure_is_no_data():
    assert reconcile.compare(1.0, None) == ("no_data", None, None)
    assert reconcile.compare(1.5, 1.0)[1:] == (0.5, 50.0)


def test_window_is_complete_utc_days():
    assert reconcile.window(3, today=TODAY) == (date(2026, 10, 27), date(2026, 10, 30))
    with pytest.raises(ValueError):
        reconcile.window(1, end_day=TODAY, today=TODAY)
    with pytest.raises(ValueError):
        reconcile.window(32, today=TODAY)


class FakeHttp:
    def __init__(self, pages):
        self.pages = list(pages)
        self.requests = []

    def get(self, url, params=None, headers=None):
        self.requests.append((url, list(params), dict(headers)))
        body = self.pages.pop(0)
        resp = MagicMock(status_code=200, text="")
        resp.json.return_value = body
        return resp


def test_anthropic_cost_report_sums_cents_per_day_with_pagination_and_workspace():
    http = FakeHttp([
        {"data": [{"starting_at": "2026-10-28T00:00:00Z", "results": [
            {"currency": "USD", "amount": "123.50", "workspace_id": "wrkspc_A"},
            {"currency": "USD", "amount": "1000", "workspace_id": "wrkspc_B"}]}],
         "has_more": True, "next_page": "page_2"},
        {"data": [{"starting_at": "2026-10-29T00:00:00Z", "results": [
            {"currency": "USD", "amount": "50", "workspace_id": "wrkspc_A"}]}], "has_more": False},
    ])
    out = reconcile.fetch_anthropic_costs("adm-test", date(2026, 10, 28), date(2026, 10, 30), "wrkspc_A", http=http)
    assert out == {date(2026, 10, 28): 1.235, date(2026, 10, 29): 0.5}
    url, params, headers = http.requests[0]
    assert url == reconcile.ANTHROPIC_COST_URL and ("group_by[]", "workspace_id") in params
    assert headers["anthropic-version"] == reconcile.ANTHROPIC_VERSION and headers["x-api-key"] == "adm-test"
    assert ("page", "page_2") in http.requests[1][1]


def test_openai_costs_sums_usd_per_day():
    ts = int(datetime(2026, 10, 29, tzinfo=timezone.utc).timestamp())
    http = FakeHttp([{"data": [{"start_time": ts, "results": [{"amount": {"value": 0.25, "currency": "usd"}},
                                                               {"amount": {"value": 0.5, "currency": "usd"}}]}],
                      "has_more": False}])
    out = reconcile.fetch_openai_costs("adm-test", date(2026, 10, 29), date(2026, 10, 30), "proj_1", http=http)
    assert out == {date(2026, 10, 29): 0.75}
    url, params, headers = http.requests[0]
    assert url == reconcile.OPENAI_COSTS_URL and ("project_ids", "proj_1") in params
    assert headers["Authorization"] == "Bearer adm-test"


def test_a_source_http_error_is_a_source_error_without_the_key():
    http = MagicMock()
    http.get.return_value = MagicMock(status_code=401, text="invalid x-api-key")
    with pytest.raises(reconcile.SourceError) as e:
        reconcile.fetch_anthropic_costs("adm-secret-value", TODAY - timedelta(days=1), TODAY, http=http)
    assert "adm-secret-value" not in str(e.value) and "401" in str(e.value)


def test_the_usage_key_is_refused_outside_aa_llm_before_any_read(monkeypatch):
    reader = MagicMock(return_value="adm-test")
    monkeypatch.setattr(connections, "_fetch_secret", reader)
    for bad in (None, "aa-cis/dev/rds", "aa-llm/dev/../x"):
        with pytest.raises(connections.ConnectionCredentialError):
            connections.resolve_usage_key("anthropic-direct", bad)
    reader.assert_not_called()
    reader.return_value = json.dumps({"admin_key": "adm-json"})
    assert connections.resolve_usage_key("anthropic-direct", "aa-llm/dev/anthropic-usage") == "adm-json"
    reader.return_value = json.dumps({"other": "x"})
    with pytest.raises(connections.ConnectionCredentialError):
        connections.resolve_usage_key("anthropic-direct", "aa-llm/dev/anthropic-usage")


# ── reconcile: one run against a fake database ────────────────────────────────────────────────────────────────────

def _anthropic_conn(enabled=False):
    return ProviderConnection(connection_key="anthropic-direct", kind="anthropic", auth_mode="api_key",
                              label="anthropic", data_scope="vendor_direct", enabled=enabled,
                              secret_name="aa-llm/dev/anthropic-direct", base_url="https://api.anthropic.com")


def _conn_row(c: ProviderConnection) -> dict:
    return {k: getattr(c, k) for k in connections._COLUMNS}


class ReconcileDB:
    """Routes each statement by its text; records the writes."""

    def __init__(self, conns, policies=(), logged=(), ce=(), policy_table=True):
        self.conns = conns
        self.policies = list(policies)
        self.logged = list(logged)
        self.ce = list(ce)
        self.policy_table = policy_table
        self.executed: list = []

    async def fetch(self, sql, *args):
        if "FROM shared.llm_provider_connection" in sql:
            return [_conn_row(c) for c in self.conns]
        if "FROM shared.llm_connection_cost_policy" in sql:
            if not self.policy_table:
                raise asyncpg.exceptions.UndefinedTableError("missing")
            return self.policies
        if "FROM shared.llm_call_log" in sql:
            return self.logged
        if "FROM shared.cost_explorer_snapshot" in sql:
            return [r for r in self.ce if r["account_id"] == args[0]]
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        if "shared.llm_cost_reconcile" in sql and not self.policy_table:
            raise asyncpg.exceptions.UndefinedTableError("missing")
        return None

    async def execute(self, sql, *args):
        self.executed.append((" ".join(sql.split()), args))
        return "DELETE 2" if sql.strip().startswith("DELETE") else "INSERT 0 1"


class Pool:
    def __init__(self, db):
        self.db = db

    @asynccontextmanager
    async def _acq(self):
        yield self.db

    def acquire(self):
        return self._acq()


def _upserts(db):
    return {(a[0], a[1]): a for s, a in db.executed if s.startswith("INSERT INTO shared.llm_cost_reconcile")}


def test_a_run_writes_one_row_per_connection_and_day(monkeypatch):
    monkeypatch.setenv("BEDROCK_ACCT_PRIMARY", "444455556666")
    d1, d2 = date(2026, 10, 28), date(2026, 10, 29)
    settled = datetime(2026, 10, 30, 12, tzinfo=timezone.utc) + timedelta(days=2)
    db = ReconcileDB(
        conns=[BUILTIN_CONNECTIONS["acc3"], BUILTIN_CONNECTIONS["acc2"], BUILTIN_CONNECTIONS["openai-direct"],
               _anthropic_conn(),
               ProviderConnection(connection_key="mock-1", kind="mock", auth_mode="none", label="m",
                                  data_scope="aws_only")],
        policies=[{"connection_key": "openai-direct", "alert_pct": 80, "hard_stop": True,
                   "usage_secret_name": "aa-llm/dev/openai-usage", "usage_scope_id": None, "notes": None,
                   "updated_at": None, "updated_by": "admin:u"}],
        logged=[{"account": "acc3", "day": d1, "usd": Decimal("2.00"), "calls": 10},
                {"account": "openai-direct", "day": d2, "usd": Decimal("1.00"), "calls": 4}],
        ce=[{"account_id": "444455556666", "service": "Claude Sonnet 4.6 (Amazon Bedrock Edition)",
             "period_start": d1, "amount_usd": Decimal("2.01"), "fetched_at": settled},
            {"account_id": "444455556666", "service": "Amazon EC2", "period_start": d1,
             "amount_usd": Decimal("9"), "fetched_at": settled}],
    )
    monkeypatch.setattr(connections, "_fetch_secret", MagicMock(return_value="adm-test"))
    openai = MagicMock(return_value={d2: 1.5})
    anthropic = MagicMock()
    out = asyncio.run(reconcile.reconcile(Pool(db), days=2, today=TODAY, job_id="00000000-0000-0000-0000-00000000000a",
                                          fetchers={"openai": openai, "anthropic": anthropic},
                                          own_account=lambda: "111122223333"))
    rows = _upserts(db)
    assert len(rows) == 8 and not any(k[0] == "mock-1" for k in rows)          # 4 connections x 2 days
    acc3 = rows[("acc3", d1)]
    assert acc3[2:4] == ("aws_cost_explorer", "ok") and acc3[6] == 2.01      # EC2 is not Bedrock
    assert json.loads(acc3[9])["provisional"] is False
    assert rows[("acc3", d2)][3] == "no_data"
    assert rows[("acc2", d1)][3] == "no_data"                                 # acc2 = the task's own account
    assert rows[("openai-direct", d2)][3] == "mismatch" and rows[("openai-direct", d2)][7] == -0.5
    assert rows[("anthropic-direct", d1)][3] == "not_configured"
    anthropic.assert_not_called()                                             # no usage key: no request
    assert openai.call_args.args[0] == "adm-test"
    assert out["mismatches"] == [{"connection_key": "openai-direct", "day": "2026-10-29", "logged_usd": 1.0,
                                  "provider_usd": 1.5, "diff_usd": -0.5}]
    assert out["pruned"] == 2 and out["bell"] is True
    prune = [a for s, a in db.executed if s.startswith("DELETE")][0]
    assert prune[0] == TODAY - timedelta(days=reconcile.RETENTION_DAYS)
    assert "adm-test" not in json.dumps(out, default=str) and "adm-test" not in repr(db.executed)


def test_a_provisional_ce_day_is_never_alerted(monkeypatch):
    monkeypatch.setenv("BEDROCK_ACCT_PRIMARY", "444455556666")
    d1 = date(2026, 10, 29)
    db = ReconcileDB(conns=[BUILTIN_CONNECTIONS["acc3"]],
                     logged=[{"account": "acc3", "day": d1, "usd": Decimal("5"), "calls": 1}],
                     ce=[{"account_id": "444455556666", "service": "Amazon Bedrock", "period_start": d1,
                          "amount_usd": Decimal("1"), "fetched_at": datetime(2026, 10, 30, 3, tzinfo=timezone.utc)}])
    out = asyncio.run(reconcile.reconcile(Pool(db), days=1, today=TODAY, fetchers={}))
    row = _upserts(db)[("acc3", d1)]
    assert row[3] == "mismatch" and json.loads(row[9])["provisional"] is True
    assert out["mismatches"] == [] and "bell" not in out


def test_a_failing_source_is_that_connections_error_only(monkeypatch):
    db = ReconcileDB(conns=[BUILTIN_CONNECTIONS["openai-direct"]],
                     policies=[{"connection_key": "openai-direct", "alert_pct": 80, "hard_stop": True,
                                "usage_secret_name": "aa-llm/dev/openai-usage", "usage_scope_id": None,
                                "notes": None, "updated_at": None, "updated_by": "x"}])
    monkeypatch.setattr(connections, "_fetch_secret", MagicMock(side_effect=PermissionError("AccessDenied")))
    asyncio.run(reconcile.reconcile(Pool(db), days=1, today=TODAY, fetchers={"openai": MagicMock()}))
    row = _upserts(db)[("openai-direct", date(2026, 10, 29))]
    assert row[3] == "error" and "secret unreadable" in json.loads(row[9])["error"]


def test_an_azure_connection_has_no_source():
    db = ReconcileDB(conns=[ProviderConnection(connection_key="azure-1", kind="azure_openai", auth_mode="api_key",
                                               label="a", data_scope="vendor_direct",
                                               secret_name="aa-llm/dev/azure-1")])
    asyncio.run(reconcile.reconcile(Pool(db), days=1, today=TODAY))
    row = _upserts(db)[("azure-1", date(2026, 10, 29))]
    assert row[2:4] == ("none", "unsupported")


def test_before_migration_316_the_run_says_so():
    db = ReconcileDB(conns=[], policy_table=False)
    with pytest.raises(reconcile.ReconcileUnavailable):
        asyncio.run(reconcile.reconcile(Pool(db), days=1, today=TODAY))


# ── the job and its schedule ──────────────────────────────────────────────────────────────────────────────────────

def test_the_kind_is_registered_as_background():
    from shared.jobs.registry import get_kind
    k = get_kind(job.KIND)
    assert k is not None and k.background and not k.deploy_blocking and k.concurrency == 1


@pytest.mark.parametrize("payload", [{"days": 0}, {"days": 40}, {"days": "3"}, {"end_day": "yesterday"}])
def test_bad_payloads_are_non_retryable(payload):
    from shared.jobs.registry import NonRetryable
    with pytest.raises(NonRetryable):
        job._parse_payload(payload)


def test_the_nightly_run_is_queued_once_a_day_from_02_utc(monkeypatch):
    enqueue = AsyncMock(return_value=("j-1", True))
    monkeypatch.setattr(job, "enqueue", enqueue)
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=False)
    early = datetime(2026, 10, 30, 1, 59, tzinfo=timezone.utc)
    assert asyncio.run(job.enqueue_nightly(pool, now=early))[1] is False
    pool.fetchval.assert_not_awaited()
    late = datetime(2026, 10, 30, 2, 5, tzinfo=timezone.utc)
    assert asyncio.run(job.enqueue_nightly(pool, now=late)) == ("j-1", True, TODAY)
    kw = enqueue.await_args.kwargs
    assert kw["idempotency_key"] == "llm_cost_reconcile:2026-10-30"
    assert enqueue.await_args.args[2] == {"trigger": "schedule", "days": 3, "end_day": "2026-10-29"}
    pool.fetchval = AsyncMock(return_value=True)
    assert asyncio.run(job.enqueue_nightly(pool, now=late))[1] is False


def test_the_sampler_part_respects_the_switch(monkeypatch):
    called = AsyncMock(return_value=("j-1", True, TODAY))
    monkeypatch.setattr(job, "enqueue_nightly", called)
    out: dict = {}
    ts = datetime(2026, 10, 30, 3, tzinfo=timezone.utc)
    asyncio.run(ops_metrics_sampler._llm_cost_reconcile_part(MagicMock(), {job.KIND: object()}, ts, out))
    assert out == {"llm_cost_reconcile": {"job_id": "j-1", "day": "2026-10-30"}}
    monkeypatch.setenv(job.SWITCH_ENV, "off")
    out = {}
    asyncio.run(ops_metrics_sampler._llm_cost_reconcile_part(MagicMock(), {job.KIND: object()}, ts, out))
    assert out == {} and called.await_count == 1


# ── the admin ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_new_routes_are_admin_only():
    for route in (("GET", "/admin/llm-connections/costs"), ("POST", "/admin/llm-connections/reconcile")):
        assert staff_authz.POLICY[route] == staff_authz.AI_ADMIN


class PolicyDB:
    """A connection row + its policy row, inside a transaction (the S4 FakeDB, plus the policy table)."""

    def __init__(self, conn: ProviderConnection, policy=None, policy_table=True):
        self.row = _conn_row(conn)
        self.row["updated_at"] = None
        self.policy = policy
        self.policy_table = policy_table
        self.executed: list = []
        self.in_tx = False

    @asynccontextmanager
    async def _tx(self):
        self.in_tx = True
        try:
            yield
        finally:
            self.in_tx = False

    def transaction(self):
        return self._tx()

    async def fetchrow(self, sql, key):
        if "llm_connection_cost_policy" in sql:
            assert self.in_tx and "FOR UPDATE" in sql
            if not self.policy_table:
                raise asyncpg.exceptions.UndefinedTableError("missing")
            return self.policy
        return self.row if key == self.row["connection_key"] else None

    async def execute(self, sql, *args):
        assert self.in_tx
        self.executed.append((" ".join(sql.split()), args))


def _patch(db, changes, key="anthropic-direct"):
    return asyncio.run(llm_connections.update_connection(db, key, changes, actor="admin:u-1"))


def test_policy_fields_upsert_in_the_transaction_with_one_audit_row(monkeypatch):
    reader = MagicMock(return_value=json.dumps({"admin_key": "adm-test"}))
    monkeypatch.setattr(connections, "_fetch_secret", reader)
    db = PolicyDB(_anthropic_conn())
    out = _patch(db, {"budget_alert_pct": 90, "usage_secret_name": "aa-llm/dev/anthropic-usage",
                      "usage_scope_id": "wrkspc_01"})
    assert out["changed"] and sorted(out["changed_fields"]) == ["budget_alert_pct", "usage_scope_id",
                                                                 "usage_secret_name"]
    reader.assert_called_once_with("aa-llm/dev/anthropic-usage")             # read once, before the transaction
    sql, args = db.executed[0]
    assert sql.startswith("INSERT INTO shared.llm_connection_cost_policy") and "ON CONFLICT" in sql
    assert args == ("anthropic-direct", 90, True, "aa-llm/dev/anthropic-usage", "wrkspc_01", "admin:u-1")
    audit = json.loads(db.executed[1][1][4])
    assert audit["after"]["budget_alert_pct"] == 90 and audit["before"]["usage_secret_name"] is None
    assert "adm-test" not in repr(db.executed)


def test_the_same_policy_writes_nothing():
    db = PolicyDB(_anthropic_conn(), policy={"alert_pct": 80, "hard_stop": True, "usage_secret_name": None,
                                             "usage_scope_id": None})
    out = _patch(db, {"budget_alert_pct": 80, "budget_hard_stop": True})
    assert out["changed"] is False and db.executed == []


@pytest.mark.parametrize("conn, changes, match", [
    (BUILTIN_CONNECTIONS["acc3"], {"usage_secret_name": "aa-llm/dev/x"}, "has no usage key"),
    (None, {"usage_secret_name": "aa-cis/dev/rds"}, "usage key refused"),
    (None, {"usage_scope_id": "bad id"}, "usage_scope_id"),
    (None, {"budget_alert_pct": 0}, "budget_alert_pct"),
])
def test_policy_refusals_read_no_secret_and_write_nothing(monkeypatch, conn, changes, match):
    reader = MagicMock()
    monkeypatch.setattr(connections, "_fetch_secret", reader)
    db = PolicyDB(conn or _anthropic_conn())
    with pytest.raises(llm_connections.ConnectionToggleRefused, match=match):
        _patch(db, changes, key=(conn or _anthropic_conn()).connection_key)
    reader.assert_not_called()
    assert db.executed == []


def test_policy_fields_before_migration_316_answer_503():
    db = PolicyDB(_anthropic_conn(), policy_table=False)
    with pytest.raises(llm_connections.ConnectionEditUnavailable, match="316"):
        _patch(db, {"budget_hard_stop": False})


def test_the_patch_body_takes_the_policy_fields_and_refuses_nulls():
    body = ops.LlmConnectionPatch(budget_alert_pct=70, budget_hard_stop=False, usage_secret_name=" aa-llm/dev/u ",
                                  usage_scope_id="")
    assert body.changes() == {"budget_alert_pct": 70, "budget_hard_stop": False, "usage_secret_name": "aa-llm/dev/u",
                              "usage_scope_id": None}
    from pydantic import ValidationError
    for bad in ({"budget_alert_pct": None}, {"budget_hard_stop": None}, {"budget_alert_pct": 101}):
        with pytest.raises(ValidationError):
            ops.LlmConnectionPatch(**bad)


def test_the_listing_carries_the_policy_and_the_reconcile_source():
    out = llm_connections.build({"acc3": BUILTIN_CONNECTIONS["acc3"], "anthropic-direct": _anthropic_conn()}, [],
                                {"anthropic-direct": cost_guard.ConnectionCostPolicy(
                                    connection_key="anthropic-direct", alert_pct=60,
                                    usage_secret_name="aa-llm/dev/anthropic-usage", stored=True)})
    rows = {r["connection_key"]: r for r in out["connections"]}
    assert out["cost_policy_ready"] is True
    assert rows["acc3"]["reconcile_source"] == "aws_cost_explorer" and rows["acc3"]["reconcile_configured"]
    assert rows["acc3"]["cost_policy"]["alert_pct"] == 80 and not rows["acc3"]["usage_key_supported"]
    assert rows["anthropic-direct"]["cost_policy"]["alert_pct"] == 60
    assert rows["anthropic-direct"]["reconcile_configured"] and rows["anthropic-direct"]["usage_key_supported"]
    assert llm_connections.build({}, [])["cost_policy_ready"] is False


def test_costs_shows_budgets_and_the_latest_reconcile(monkeypatch):
    import dataclasses
    acc3 = dataclasses.replace(BUILTIN_CONNECTIONS["acc3"], monthly_budget_usd=10.0)
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[
        [_conn_row(acc3), _conn_row(_anthropic_conn())],
        [],                                                                    # policies
        [{"account": "acc3", "spent": Decimal("8.5"), "calls": 7}],            # month spend
        [{"connection_key": "acc3", "day": date(2026, 10, 29), "source": "aws_cost_explorer", "status": "ok",
          "logged_usd": Decimal("1"), "logged_calls": 3, "provider_usd": Decimal("1.01"), "diff_usd": Decimal("-0.01"),
          "diff_pct": Decimal("-0.99"), "details": "{}", "checked_at": datetime(2026, 10, 30, 2, tzinfo=timezone.utc)}],
    ])
    out = asyncio.run(llm_connections.costs(conn, days=14))
    rows = {r["connection_key"]: r for r in out["connections"]}
    assert out["schema_ready"] and out["tolerance"] == {"usd": 0.05, "pct": 5.0}
    assert rows["acc3"]["budget"]["state"] == "alert" and rows["acc3"]["budget"]["month_calls"] == 7
    assert rows["acc3"]["reconcile"]["last"]["provider_usd"] == 1.01
    assert rows["anthropic-direct"]["budget"] is None
    assert rows["anthropic-direct"]["reconcile"] == {"source": "anthropic_cost_report", "configured": False,
                                                     "last": None, "days": []}


def test_the_reconcile_route_queues_a_run_and_refuses_a_bad_window(monkeypatch):
    from tests.unit.test_aa1002_s2_admin_switch import _request
    enqueue = AsyncMock(return_value=("j-9", True))
    monkeypatch.setattr(job, "enqueue_manual", enqueue)
    monkeypatch.setattr(ops, "verify_admin_secret", lambda s: None)
    out = asyncio.run(ops.post_llm_cost_reconcile(_request(MagicMock()), ops.LlmCostReconcileRun(days=5),
                                                  x_admin_secret="s", x_admin_user_id="u-1"))
    assert out["job_id"] == "j-9" and enqueue.await_args.kwargs == {"days": 5, "end_day": None, "actor": "admin:u-1"}
    future = datetime.now(timezone.utc).date()
    with pytest.raises(HTTPException) as e:
        asyncio.run(ops.post_llm_cost_reconcile(_request(MagicMock()), ops.LlmCostReconcileRun(end_day=future),
                                                x_admin_secret="s", x_admin_user_id="u-1"))
    assert e.value.status_code == 422 and enqueue.await_count == 1
