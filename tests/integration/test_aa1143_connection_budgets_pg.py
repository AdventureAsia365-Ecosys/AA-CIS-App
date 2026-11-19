"""AA-1143 (decision G3) — the connection budget service and the CIS spend rule on real Postgres (CIS 455 replayed by
conftest; skipped before main has it). A save is one INSERT the table's trigger checks (the remainder lands in
monthly_budget_usd), versions count up per app, the audit row is written; the read shows the rows, the spend per app
and the CIS remainder's spend; cost_guard counts an app with its own share only above it. The CRM relay is faked (no
network). Runs in transactions that are rolled back or cleaned up; invented connection keys only."""
import os
import sys
from contextlib import asynccontextmanager
from decimal import Decimal

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.services import llm_connection_budgets as budgets  # noqa: E402
from shared import cost_guard  # noqa: E402
from shared.llm_client import connection_budget  # noqa: E402
from shared.llm_client import connections as conn_registry  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
KEY = "zz-budget-pg"
REASON = "zz test: a change of at least twenty characters"
SENT: list = []                                              # the faked crm.ops.v1 calls of the running test


class _OnePool:
    """A pool over one connection (inside the test's transaction)."""

    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn

    async def execute(self, sql, *args):
        return await self.conn.execute(sql, *args)


@pytest_asyncio.fixture
async def conn(monkeypatch):
    c = await asyncpg.connect(DSN)
    if not await c.fetchval("SELECT to_regclass('shared.llm_connection_budget') IS NOT NULL AS ok"):
        await c.close()
        pytest.skip("needs CIS 455 (shared.llm_connection_budget)")
    tr = c.transaction()
    await tr.start()
    sent = SENT
    sent.clear()

    async def crm_ops_call(method, path, authorization, *, json=None, params=None):
        sent.append((method, path, json))
        return (200, {}) if method == "PUT" else (503, {"detail": "crm_unavailable"})

    monkeypatch.setattr(budgets.crm_console, "crm_ops_call", crm_ops_call)
    monkeypatch.setattr(conn_registry, "invalidate", lambda: None)
    monkeypatch.setattr(connection_budget, "invalidate", lambda: None)
    await c.execute("""INSERT INTO shared.llm_provider_connection
                           (connection_key, kind, auth_mode, label, data_scope, enabled, monthly_budget_usd, updated_by)
                       VALUES ($1, 'mock', 'none', 'Zz budget', 'aws_only', false, 30, 'test')""", KEY)
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _cap(conn):
    return await conn.fetchval("SELECT monthly_budget_usd AS usd FROM shared.llm_provider_connection "
                               "WHERE connection_key = $1", KEY)


async def _spend(conn, app, usd):
    await conn.execute("""INSERT INTO shared.llm_call_log (stage, role, model, account, app, cost_usd, status,
                                                           quality_signal, created_at)
                          VALUES ('adhoc_zz_budget', 'writer', 'zz-writer', $1, $2, $3, 'ok', '{}'::jsonb, now())""",
                       KEY, app, usd)


@pytest.mark.asyncio
async def test_a_first_total_then_a_share_moves_the_cis_cap(conn):
    pool = _OnePool(conn)
    out = await budgets.save(pool, "Bearer zz", KEY, total=40, reason=REASON, actor="admin:zz")
    assert out["cis_remainder_usd"] == 40.0 and await _cap(conn) == Decimal("40.00")
    out = await budgets.save(pool, "Bearer zz", KEY, shares={"crm": 6, "tripplanner": 4}, reason=REASON,
                             actor="admin:zz")
    assert out["cis_remainder_usd"] == 30.0 and await _cap(conn) == Decimal("30.00")
    out = await budgets.save(pool, "Bearer zz", KEY, shares={"crm": 2}, reason=REASON, actor="admin:zz")
    versions = await conn.fetch("SELECT app, version FROM shared.llm_connection_budget WHERE connection_key = $1 "
                                "ORDER BY id", KEY)
    assert [(r["app"], r["version"]) for r in versions] == [(None, 1), ("crm", 1), ("tripplanner", 1), ("crm", 2)]
    assert await _cap(conn) == Decimal("34.00")
    puts = [s for s in SENT if s[0] == "PUT"]
    assert puts[-1][2]["shares"][KEY] == "2.00"
    assert await conn.fetchval("SELECT count(*) AS n FROM acp_shared.audit_log WHERE action = $1 AND resource_id = $2",
                               budgets.AUDIT_ACTION, KEY) == 3


@pytest.mark.asyncio
async def test_read_and_the_cis_spend_rule(conn):
    pool = _OnePool(conn)
    await budgets.save(pool, "Bearer zz", KEY, total=40, shares={"tripplanner": 5}, reason=REASON, actor="admin:zz")
    await _spend(conn, "cis", Decimal("10"))
    await _spend(conn, None, Decimal("1"))
    await _spend(conn, "tripplanner", Decimal("7"))           # 2 above its share
    out = await budgets.read(pool, "Bearer zz")
    row = next(r for r in out["rows"] if r["connection_key"] == KEY)
    assert row["total"]["usd"] == 40.0 and row["shares"]["tripplanner"]["usd"] == 5.0
    assert row["cis_remainder_usd"] == 35.0 and row["cis_cap_in_sync"] is True
    assert row["cis_pool_spent_usd"] == pytest.approx(13.0) and row["spend"]["tripplanner"] == pytest.approx(7.0)
    assert out["crm"]["copy_error"] == "crm_unavailable"
    spent = await cost_guard.connection_month_spend(conn, [KEY])
    assert spent[KEY][0] == pytest.approx(13.0)


@pytest.mark.asyncio
async def test_shares_above_the_total_are_refused_by_the_rules_before_the_database(conn):
    pool = _OnePool(conn)
    await budgets.save(pool, "Bearer zz", KEY, total=10, reason=REASON, actor="admin:zz")
    with pytest.raises(budgets.rules.BudgetEditRefused):
        await budgets.save(pool, "Bearer zz", KEY, shares={"tripplanner": 11}, reason=REASON, actor="admin:zz")
