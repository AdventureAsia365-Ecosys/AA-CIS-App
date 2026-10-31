"""AA-642 (S0) — migration 318 (spend_budget seed: jev global per day, dfs job:s1_rewrite per run) against real
Postgres, read back with the code's own loader.

conftest applies every migration, 318 included. Each test re-runs it (idempotent: ON CONFLICT DO NOTHING) and checks:
the two rows and their values; a row an admin already changed is never overwritten; the other dfs rows are untouched;
cost_guard.load_run_budget reads the caps the way decide() and process_seo() use them.
"""
import asyncio
import os
import sys

import asyncpg
import psycopg2
import psycopg2.extras
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from shared import cost_guard  # noqa: E402

M318 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "318_spend_budget_jev_dfs_seed.sql")
SEEDED = (("jev", "global"), ("dfs", "job:s1_rewrite"))


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute(open(M318, encoding="utf-8").read())
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        # put the seed back as the migration wrote it (a test may have changed a row)
        c.execute("DELETE FROM shared.spend_budget WHERE (provider, scope) IN (('jev', 'global'), "
                  "('dfs', 'job:s1_rewrite'))")
        c.execute(open(M318, encoding="utf-8").read())
        conn.close()


class _Pool:
    """The one-connection pool shape cost_guard.load_run_budget acquires from."""

    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return self

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


def _budget(provider: str, kind: str):
    async def go():
        conn = await asyncpg.connect(host=DB_HOST, port=DB_PORT, database=DB_NAME, user=DB_USER, password=DB_PASS)
        try:
            return await cost_guard.load_run_budget(_Pool(conn), provider, kind)
        finally:
            await conn.close()
    return asyncio.run(go())


def test_the_two_rows_are_seeded_with_the_step0_values(cur):
    cur.execute("SELECT provider, scope, per_run_usd, per_day_usd, hard_stop, alert_pct, updated_by "
                "FROM shared.spend_budget WHERE (provider, scope) IN (('jev', 'global'), ('dfs', 'job:s1_rewrite')) "
                "ORDER BY provider")
    rows = {(r["provider"], r["scope"]): r for r in cur.fetchall()}
    assert set(rows) == set(SEEDED)
    jev, dfs = rows[("jev", "global")], rows[("dfs", "job:s1_rewrite")]
    assert (jev["per_run_usd"], float(jev["per_day_usd"]), jev["hard_stop"], jev["alert_pct"]) == (None, 5.0, True, 80)
    assert (float(dfs["per_run_usd"]), dfs["per_day_usd"], dfs["hard_stop"]) == (0.5, None, True)
    assert jev["updated_by"] == dfs["updated_by"] == "migration-318"
    cur.execute("SELECT 1 AS ok FROM shared.schema_versions WHERE version = '318'")
    assert cur.fetchone()["ok"] == 1


def test_a_row_an_admin_changed_is_never_overwritten(cur):
    cur.execute("UPDATE shared.spend_budget SET per_day_usd = 7.50, updated_by = 'admin-test' "
                "WHERE provider = 'jev' AND scope = 'global'")
    cur.execute(open(M318, encoding="utf-8").read())
    cur.execute("SELECT per_day_usd, updated_by FROM shared.spend_budget WHERE provider = 'jev' AND scope = 'global'")
    row = cur.fetchone()
    assert (float(row["per_day_usd"]), row["updated_by"]) == (7.5, "admin-test")


def test_no_other_spend_budget_row_is_written(cur):
    cur.execute("SELECT provider, scope FROM shared.spend_budget WHERE updated_by = 'migration-318'")
    assert {(r["provider"], r["scope"]) for r in cur.fetchall()} == set(SEEDED)


def test_the_loader_reads_the_jev_daily_cap_as_decide_does(cur):
    b = _budget("jev", "decide")
    assert b.per_day_usd == 5.0 and b.day_is_hard and b.per_run_usd is None


def test_the_loader_reads_the_s1_rewrite_per_run_cap_as_process_seo_does(cur):
    b = _budget("dfs", "s1_rewrite")
    assert b.per_run_usd == 0.5 and b.hard_stop
    with pytest.raises(cost_guard.BudgetExceeded):
        b.charge(0.45)
        b.check(0.09)
