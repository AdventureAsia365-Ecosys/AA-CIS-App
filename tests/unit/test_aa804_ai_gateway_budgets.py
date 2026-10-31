"""AA-804 (UI part 2) / AA-1017 — GET /admin/ai-gateway/budgets: every spending cap by tier for the Budgets tab of
the admin "AI providers" page (api/services/ai_gateway.py read_budgets, api/routers/admin_ai_gateway.py).

No database: the builders are pure and the reader runs on a fake connection that answers by SQL. The SQL is checked
on PostgreSQL in tests/integration/test_aa804_ai_gateway_sql.py. Connection keys and labels are invented.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import asyncpg
import pytest

from api.routers import admin_ai_gateway as router_mod
from api.services import ai_gateway as gw
from shared import cost_guard
from shared.llm_client import connections as conn_registry

NOW = datetime(2026, 3, 14, 15, 30, tzinfo=timezone.utc)
UPDATED = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)

BUDGET_ROWS = [
    {"provider": "bedrock", "scope": "global", "per_run_usd": None, "per_day_usd": 10.0, "hard_stop": True,
     "alert_pct": 80, "updated_at": UPDATED, "updated_by": "admin-one"},
    {"provider": "dfs", "scope": "global", "per_run_usd": None, "per_day_usd": 5.0, "hard_stop": False,
     "alert_pct": 50, "updated_at": None, "updated_by": None},
    {"provider": "dfs", "scope": "job:segment_research", "per_run_usd": 2.0, "per_day_usd": None, "hard_stop": True,
     "alert_pct": 80, "updated_at": None, "updated_by": None},
    {"provider": "jev", "scope": "global", "per_run_usd": None, "per_day_usd": 1.0, "hard_stop": True,
     "alert_pct": 80, "updated_at": None, "updated_by": None},
    # AA-712: place_extract's run budget is kind scoped, so its $5/day is measured against its own spend only.
    {"provider": "bedrock", "scope": "job:place_extract", "per_run_usd": 2.0, "per_day_usd": 5.0, "hard_stop": True,
     "alert_pct": 80, "updated_at": None, "updated_by": None},
]
DAY_SPEND = {"bedrock": 9.0, "dfs": 1.0, "jev": 1.5}
KIND_SPEND = {"place_extract": 1.0}


class FakeConn:
    """fetch / fetchval answered by SQL fragment; a provider's day spend by its ledger filter."""

    def __init__(self, fail: tuple[str, ...] = ()):
        self.fail = fail
        self.calls: list[str] = []

    def _check(self, sql: str) -> None:
        self.calls.append(sql)
        for frag in self.fail:
            if frag in sql:
                raise OSError("db down")

    async def fetch(self, sql, *args):
        self._check(sql)
        if "FROM shared.spend_budget" in sql:
            return [dict(r) for r in BUDGET_ROWS]
        if "FROM shared.tenant_ai_budget" in sql:       # AA-1016 C4: before migration 321 the table is missing
            raise asyncpg.exceptions.UndefinedTableError('relation "shared.tenant_ai_budget" does not exist')
        raise AssertionError(f"unexpected SQL: {sql[:80]}")

    async def fetchval(self, sql, *args):
        self._check(sql)
        if sql == cost_guard._KIND_DAY_SPEND_SQL:
            self.kind_args = [*getattr(self, "kind_args", []), *args]
            return KIND_SPEND.get(args[0], 0.0)
        for provider, query in cost_guard._DAY_SPEND_SQL.items():
            if sql == query:
                return DAY_SPEND.get(provider, 0.0)
        raise AssertionError(f"unexpected SQL: {sql[:80]}")


class FakePool:
    def __init__(self, conn: FakeConn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _conn(key, kind, label, enabled=True, budget=None, account_id=None):
    return SimpleNamespace(connection_key=key, kind=kind, label=label, enabled=enabled, monthly_budget_usd=budget,
                           resolved_account_id=lambda: account_id)


REGISTRY = {
    "llm-main": _conn("llm-main", "bedrock", "Bedrock main", budget=100.0, account_id="111122223333"),
    "openai-direct": _conn("openai-direct", "openai", "OpenAI API", budget=20.0),
    "anthropic-direct": _conn("anthropic-direct", "anthropic", "Anthropic API", enabled=False),
}


@pytest.fixture
def registry(monkeypatch):
    async def fetch_connections(conn):
        return REGISTRY

    async def policies(conn, *, missing=None):
        return {"openai-direct": cost_guard.ConnectionCostPolicy(connection_key="openai-direct", alert_pct=90,
                                                                  hard_stop=False, stored=True)}

    async def month_spend(conn, keys):
        return {k: v for k, v in {"llm-main": (85.0, 400), "openai-direct": (25.0, 30),
                                  "anthropic-direct": (0.0, 0)}.items() if k in keys}

    monkeypatch.setattr(conn_registry, "fetch_connections", fetch_connections)
    monkeypatch.setattr(cost_guard, "read_cost_policies", policies)
    monkeypatch.setattr(cost_guard, "connection_month_spend", month_spend)


# ── pure ───────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("per_day,spent,alert,expected", [
    (None, 3.0, 80, (None, "no_daily_cap")),
    (10.0, 2.0, 80, (20.0, "ok")),
    (10.0, 8.0, 80, (80.0, "alert")),
    (10.0, 10.0, 80, (100.0, "cap_reached")),
    (0.0, 0.0, 80, (None, "cap_reached")),       # a $0 cap allows no spend
    (0.0, 0.5, 80, (100.0, "cap_reached")),
    (10.0, 9.0, None, (90.0, "ok")),             # no alert % = no alert state
])
def test_budget_state(per_day, spent, alert, expected):
    assert gw.budget_state(per_day, spent, alert) == expected


def test_spend_budget_row_names_the_job_kind_and_uses_the_provider_spend():
    job = gw.spend_budget_row(BUDGET_ROWS[2], {"dfs": 1.0})
    assert job["job_kind"] == "segment_research" and job["per_run_usd"] == 2.0
    assert job["state"] == "no_daily_cap" and job["spent_today_usd"] == 1.0
    glob = gw.spend_budget_row(BUDGET_ROWS[0], DAY_SPEND)
    assert glob["job_kind"] is None and glob["used_pct"] == 90.0 and glob["state"] == "alert"
    assert glob["updated_at"] == UPDATED.isoformat()


@pytest.mark.parametrize("kind_spent,expected", [
    (1.0, (20.0, "ok")),            # the provider spent $9 today, but this kind only $1 of its $5
    (4.0, (80.0, "alert")),
    (5.0, (100.0, "cap_reached")),
])
def test_a_kind_scoped_job_row_reads_its_own_spend(kind_spent, expected):
    row = gw.spend_budget_row(BUDGET_ROWS[4], {"bedrock": 9.0}, {"place_extract": kind_spent})
    assert row["job_kind"] == "place_extract" and row["day_scope"] == "kind"
    assert row["spent_today_usd"] == kind_spent and (row["used_pct"], row["state"]) == expected


def test_a_kind_scoped_job_row_without_kind_spend_counts_zero():
    row = gw.spend_budget_row(BUDGET_ROWS[4], {"bedrock": 50.0})
    assert row["spent_today_usd"] == 0.0 and row["state"] == "ok"


def test_a_provider_scoped_job_row_keeps_the_provider_spend():
    # place_dedupe loads its budget with the default day_scope: its daily cap is checked against all of bedrock.
    r = {**BUDGET_ROWS[4], "scope": "job:place_dedupe"}
    row = gw.spend_budget_row(r, {"bedrock": 9.0}, {"place_dedupe": 0.5})
    assert row["day_scope"] == "provider" and row["spent_today_usd"] == 9.0 and row["state"] == "cap_reached"
    assert gw.spend_budget_row(BUDGET_ROWS[0], DAY_SPEND)["day_scope"] == "provider"


def _kind_scoped_budget_calls() -> set[tuple[str, str]]:
    """Every (provider, job kind) a production caller passes to load_run_budget with day_scope="kind". The kind is a
    string literal, a module constant of the same file, or an attribute of a module imported with `from x import y`."""
    import ast
    import importlib
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    found: set[tuple[str, str]] = set()
    for top in ("api", "services", "shared", "worker"):
        for path in (root / top).rglob("*.py"):
            src = path.read_text(encoding="utf-8")
            if "day_scope" not in src or "load_run_budget" not in src:
                continue
            tree = ast.parse(src)
            consts = {t.id: n.value.value for n in tree.body if isinstance(n, ast.Assign)
                      and isinstance(n.value, ast.Constant) for t in n.targets if isinstance(t, ast.Name)}
            aliases = {(a.asname or a.name): f"{n.module}.{a.name}" for n in ast.walk(tree)
                       if isinstance(n, ast.ImportFrom) and n.module for a in n.names}
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
                scope = next((k.value for k in node.keywords if k.arg == "day_scope"), None)
                if fn != "load_run_budget" or not (isinstance(scope, ast.Constant) and scope.value == "kind"):
                    continue
                provider, kind = node.args[1], node.args[2]
                if isinstance(kind, ast.Constant):
                    value = kind.value
                elif isinstance(kind, ast.Name):
                    value = consts[kind.id]
                elif isinstance(kind, ast.Attribute) and isinstance(kind.value, ast.Name):
                    value = getattr(importlib.import_module(aliases[kind.value.id]), kind.attr)
                else:
                    raise AssertionError(f"{path}: cannot resolve the job kind of a kind-scoped budget")
                found.add((provider.value, value))
    return found


def test_kind_day_scoped_matches_every_kind_scoped_caller():
    found = _kind_scoped_budget_calls()
    assert found, "the scan found no day_scope='kind' caller; fix the scan"
    assert found == set(cost_guard.KIND_DAY_SCOPED), "update shared/cost_guard.py KIND_DAY_SCOPED"


def test_connection_budget_rows_keep_connections_without_a_budget():
    conns = [{"connection_key": "spare", "kind": "anthropic", "label": "Spare", "enabled": False, "account_id": None},
             {"connection_key": "main", "kind": "bedrock", "label": "Main", "enabled": True, "account_id": "1"}]
    monthly = {"main": {"monthly_budget_usd": 50.0, "used_pct": 20.0, "state": "ok"}}
    rows = gw.connection_budget_rows(conns, monthly, {"main": (10.0, 7)}, {})
    assert [r["connection_key"] for r in rows] == ["main", "spare"]        # enabled first, then by kind
    assert rows[0]["monthly_budget_usd"] == 50.0 and rows[0]["month_calls"] == 7 and rows[0]["state"] == "ok"
    assert rows[1]["monthly_budget_usd"] is None and rows[1]["state"] == "no_budget"
    assert rows[1]["alert_pct"] == cost_guard.DEFAULT_CONNECTION_ALERT_PCT and rows[1]["hard_stop"] is True


def test_coverage_lists_every_provider_and_enabled_connection():
    provider_rows = [gw.spend_budget_row(r, DAY_SPEND) for r in BUDGET_ROWS if r["scope"] == "global"]
    conns = [{"connection_key": "main", "kind": "bedrock", "label": "Main", "enabled": True, "account_id": None,
              "monthly_budget_usd": 50.0, "hard_stop": True},
             {"connection_key": "off", "kind": "openai", "label": "Off", "enabled": False, "account_id": None,
              "monthly_budget_usd": None, "hard_stop": True}]
    cov = gw.budget_coverage(provider_rows, conns, cost_guard.PROVIDERS)
    by = {(c["tier"], c["key"]): c for c in cov}
    assert [c["key"] for c in cov if c["tier"] == "provider"] == list(cost_guard.PROVIDERS)
    assert by[("provider", "bedrock")] == {"tier": "provider", "key": "bedrock", "capped": True, "hard_stop": True}
    assert by[("provider", "dfs")]["hard_stop"] is False
    assert by[("provider", "geocoder")]["capped"] is False and by[("provider", "geocoder")]["hard_stop"] is None
    assert ("connection", "off") not in by and by[("connection", "main")]["capped"] is True


# ── read_budgets ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_read_budgets_returns_every_tier(registry):
    conn = FakeConn()
    out = asyncio.run(gw.read_budgets(FakePool(conn), now=NOW))
    assert out["errors"] == [] and out["providers"] == list(cost_guard.PROVIDERS)
    tiers = out["tiers"]
    assert [(r["provider"], r["scope"]) for r in tiers["provider"]] == [("bedrock", "global"), ("dfs", "global"),
                                                                        ("jev", "global")]
    assert sorted(r["scope"] for r in tiers["job"]) == ["job:place_extract", "job:segment_research"]
    assert tiers["tenant"] == [] and out["tenant_ready"] is False
    jobs = {r["scope"]: r for r in tiers["job"]}
    # bedrock spent $9 today (90 % of the provider cap) but place_extract only its own $1 of $5
    assert jobs["job:place_extract"]["day_scope"] == "kind" and jobs["job:place_extract"]["spent_today_usd"] == 1.0
    assert jobs["job:place_extract"]["state"] == "ok" and jobs["job:place_extract"]["used_pct"] == 20.0
    assert jobs["job:segment_research"]["day_scope"] == "provider"
    assert conn.kind_args == ["place_extract"]      # only kind-scoped job rows read their kind's spend
    by = {r["provider"]: r for r in tiers["provider"]}
    assert by["jev"]["state"] == "cap_reached" and by["jev"]["spent_today_usd"] == 1.5
    assert by["dfs"]["state"] == "ok" and by["bedrock"]["state"] == "alert"
    conns = {r["connection_key"]: r for r in tiers["connection"]}
    assert conns["llm-main"]["account_id"] == "111122223333" and conns["llm-main"]["state"] == "alert"
    # openai: over its budget but its policy only alerts (no hard stop) -> alert, not stopped
    assert conns["openai-direct"]["state"] == "alert" and conns["openai-direct"]["alert_pct"] == 90
    assert conns["anthropic-direct"]["state"] == "no_budget" and conns["anthropic-direct"]["enabled"] is False
    assert out["spent_today_usd"]["bedrock"] == 9.0 and out["spent_today_usd"]["geocoder"] == 0.0
    assert set(out["spent_today_usd"]) == set(cost_guard.PROVIDERS)
    cov = {(c["tier"], c["key"]) for c in out["coverage"]}
    assert ("connection", "anthropic-direct") not in cov and ("connection", "openai-direct") in cov


def test_a_failing_tier_keeps_the_other(registry, monkeypatch):
    out = asyncio.run(gw.read_budgets(FakePool(FakeConn(fail=("FROM shared.spend_budget",))), now=NOW))
    assert [e["source"] for e in out["errors"]] == ["spend_budgets"]
    assert "Spend budgets read failed (ref " in out["errors"][0]["error"]
    assert out["tiers"]["provider"] == [] and out["coverage"] == []
    assert len(out["tiers"]["connection"]) == 3

    async def boom(conn):
        raise OSError("no registry")

    monkeypatch.setattr(conn_registry, "fetch_connections", boom)
    out = asyncio.run(gw.read_budgets(FakePool(FakeConn()), now=NOW))
    assert [e["source"] for e in out["errors"]] == ["llm_connections"]
    assert out["tiers"]["connection"] == [] and len(out["tiers"]["provider"]) == 3
    assert all(c["tier"] == "provider" for c in out["coverage"])


def test_the_route_reads_budgets_and_writes_nothing(registry):
    conn = FakeConn()
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=FakePool(conn))))
    out = asyncio.run(router_mod.get_budgets(req))
    assert out["tiers"]["tenant"] == []
    assert not any(w in sql.upper() for sql in conn.calls for w in ("INSERT", "UPDATE ", "DELETE"))
