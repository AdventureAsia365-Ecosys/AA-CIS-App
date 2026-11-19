"""AA-1127 (C2 "spend truth"; tech lead decision S285 E2) — the freshness read of the Spend page's "AWS figures stale"
badge (api/services/cost_explorer_daily.py FRESHNESS_SQL) on the real Cost Explorer snapshot tables (CIS 248). Runs in
a transaction that is rolled back; invented values only."""
import os
import sys
from datetime import datetime, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.services import cost_explorer_daily as daily  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    async def fetchrow(self, sql, *args):
        return await self.conn.fetchrow(sql, *args)


@pytest.mark.asyncio
async def test_the_freshness_read_runs_on_the_real_tables(conn):
    fetched = datetime(2026, 11, 18, 1, 5, tzinfo=timezone.utc)
    await conn.execute(
        "INSERT INTO shared.cost_explorer_snapshot (account_id, service, period_start, period_end, amount_usd, unit, "
        "fetched_at) VALUES ('zz-example', 'Example Service', '2026-11-17', '2026-11-18', 1.25, 'USD', $1)", fetched)
    await conn.execute(
        "INSERT INTO shared.cost_explorer_app_snapshot (account_id, app_tag, service, period_start, period_end, "
        "amount_usd, unit, fetched_at) VALUES ('zz-example', 'zz', 'Example Service', '2026-11-17', '2026-11-18', "
        "1.25, 'USD', $1)", fetched)
    now = datetime(2026, 11, 18, 9, 5, tzinfo=timezone.utc)
    out = await daily.freshness(_Pool(conn), now=now)
    # other rows may exist in the shared test database: the newest fetch is at least ours
    assert out["service_fetched_at"] >= fetched.isoformat() and out["app_fetched_at"] >= fetched.isoformat()
    assert "error" not in out
    if out["oldest_newest_at"] == fetched.isoformat():   # the usual case: the throwaway database holds only ours
        assert out["age_hours"] == 8.0 and out["stale"] is False


@pytest.mark.asyncio
async def test_an_empty_snapshot_table_reads_stale(conn):
    await conn.execute("DELETE FROM shared.cost_explorer_app_snapshot")   # rolled back with the test transaction
    out = await daily.freshness(_Pool(conn), now=datetime(2026, 11, 18, 9, 5, tzinfo=timezone.utc))
    assert out["app_fetched_at"] is None and out["stale"] is True and out["age_hours"] is None
