"""AA-1025 A7 — the admin writes of the central LLM caps (api/routers/admin_budgets.py): PUT
/admin/budgets/llm/{scope} with a month cap, DELETE /admin/budgets/llm/{scope}, the scope rules, the 409 before
migration 340. No database: a fake pool answers. Invented scopes only.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import asyncpg
import pytest
from fastapi import HTTPException

from api.routers import admin_budgets as ab
from shared.llm_client import central_budget


class Conn:
    def __init__(self, fail=None, status="DELETE 1"):
        self.fail, self.status = fail, status
        self.executed: list = []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        if self.fail is not None:
            raise self.fail
        return self.status


class Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _req(conn):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=Pool(conn))))


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr(ab, "verify_admin_secret", lambda secret: None)


@pytest.mark.parametrize("scope", ["app:cis", "stage:t9_write", "job:place_extract", "eval", "adhoc"])
def test_llm_scopes_are_valid(scope):
    ab._valid("llm", scope, ab.BudgetUpdate(per_day_usd=1.0, per_month_usd=10.0))


@pytest.mark.parametrize("provider,scope,body", [
    ("llm", "global", None),
    ("llm", "tenant:x", None),
    ("llm", "stage:t9_write", ab.BudgetUpdate(per_run_usd=1.0)),     # a run cap is for a job scope only
    ("bedrock", "stage:t9_write", None),                            # a provider row keeps global / job:<kind>
    ("bedrock", "global", ab.BudgetUpdate(per_month_usd=5.0)),      # a month cap is for an llm row only
])
def test_invalid_combinations_are_refused(provider, scope, body):
    with pytest.raises(HTTPException) as e:
        ab._valid(provider, scope, body)
    assert e.value.status_code == 422


def test_budget_row_reads_a_row_with_or_without_the_month_column():
    row = ab.budget_row({"provider": "dfs", "scope": "global", "per_day_usd": "10.00", "hard_stop": True,
                         "alert_pct": 80})
    assert row["per_day_usd"] == 10.0 and row["per_month_usd"] is None
    assert ab.budget_row({"provider": "llm", "scope": "eval", "per_month_usd": 25})["per_month_usd"] == 25.0


def test_put_a_central_cap_writes_the_month_column_and_drops_the_cache(monkeypatch):
    dropped = []
    monkeypatch.setattr(central_budget, "invalidate", lambda: dropped.append(True))
    conn = Conn()
    body = ab.BudgetUpdate(per_day_usd=0.01, hard_stop=True)
    out = asyncio.run(ab.upsert_budget("llm", "stage:t9_write", body, _req(conn), "s", "admin-x"))
    sql, args = conn.executed[0]
    assert sql == ab.UPSERT_CENTRAL_SQL and args[:5] == ("llm", "stage:t9_write", None, 0.01, None)
    assert out["per_month_usd"] is None and dropped == [True]


def test_a_central_cap_without_hard_stop_only_alerts(monkeypatch):
    # T-8 (S263): a Dev central cap never blocks traffic unless the caller asks for a hard stop.
    monkeypatch.setattr(central_budget, "invalidate", lambda: None)
    conn = Conn()
    asyncio.run(ab.upsert_budget("llm", "app:cis", ab.BudgetUpdate(per_day_usd=5.0), _req(conn), "s", None))
    assert conn.executed[0][1][5] is False


def test_a_provider_row_without_hard_stop_still_stops_hard():
    conn = Conn()
    asyncio.run(ab.upsert_budget("dfs", "global", ab.BudgetUpdate(per_day_usd=10.0), _req(conn), "s", None))
    assert conn.executed[0][1][4] is True


def test_put_a_provider_row_keeps_its_old_statement(monkeypatch):
    conn = Conn()
    out = asyncio.run(ab.upsert_budget("dfs", "global", ab.BudgetUpdate(per_day_usd=10.0), _req(conn), "s", None))
    assert conn.executed[0][0] == ab.UPSERT_SQL and "per_month_usd" not in out


@pytest.mark.parametrize("err", [asyncpg.CheckViolationError("spend_budget_provider_check"),
                                 asyncpg.UndefinedColumnError("per_month_usd")])
def test_a_central_cap_before_migration_340_is_a_409(err):
    with pytest.raises(HTTPException) as e:
        asyncio.run(ab.upsert_budget("llm", "adhoc", ab.BudgetUpdate(per_month_usd=2.0), _req(Conn(fail=err)), "s",
                                     None))
    assert e.value.status_code == 409 and "migration 340" in e.value.detail


def test_delete_a_central_cap(monkeypatch):
    dropped = []
    monkeypatch.setattr(central_budget, "invalidate", lambda: dropped.append(True))
    conn = Conn()
    out = asyncio.run(ab.delete_central_budget("stage:t9_write", _req(conn), "admin-x"))
    assert out == {"provider": "llm", "scope": "stage:t9_write", "deleted": True} and dropped == [True]
    assert conn.executed[0][1] == ("llm", "stage:t9_write")
    with pytest.raises(HTTPException) as e:
        asyncio.run(ab.delete_central_budget("stage:t9_write", _req(Conn(status="DELETE 0")), None))
    assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:
        asyncio.run(ab.delete_central_budget("global", _req(Conn()), None))
    assert e.value.status_code == 422
