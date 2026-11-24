"""AA-1154 (A1) — CIS 487 on a real Postgres (conftest replays every migration): the AI gateway stage ops_assist and its
own month cap (10 USD, hard stop) exist with the values the tech lead decided (S285 AI2 / AI3); a re-run keeps an
admin's edit; the runtime role reads both rows. Runs in a transaction that is rolled back."""
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M487 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                    "487_aa1154_ops_assist_stage_and_budget.sql")
STAGE_ROW = ("SELECT role, provider, model_id, account_route, is_active, fallback_model_ids, shadow_model_id, "
             "shadow_sample_pct, allowed_scopes, route_deadline_s, degrade_mode, pii_class, free_text "
             "FROM shared.llm_role_config WHERE stage = $1")
STAGE = "ops_assist"
SCOPE = "stage:ops_assist"
BUDGET_ROW = ("SELECT per_run_usd, per_day_usd, per_month_usd::float8 AS month, hard_stop, alert_pct "
              "FROM shared.spend_budget WHERE provider = 'llm' AND scope = $1")


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


@pytest.mark.asyncio
async def test_487_registered_with_the_stage_and_its_month_cap(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '487'") == "487"
    row = await conn.fetchrow(STAGE_ROW, STAGE)
    assert row is not None
    assert (row["role"], row["provider"], row["model_id"], row["account_route"]) == ("writer", "claude", "haiku-5-5",
                                                                                     None)
    assert row["is_active"] is True and list(row["fallback_model_ids"]) == [] and row["shadow_model_id"] is None
    assert row["shadow_sample_pct"] == 0 and sorted(row["allowed_scopes"]) == ["aws_only", "vendor_direct"]
    assert (row["route_deadline_s"], row["degrade_mode"], row["pii_class"], row["free_text"]) == (
        25, "basic_narration", "content", False)
    b = await conn.fetchrow(BUDGET_ROW, SCOPE)
    assert b is not None
    assert (b["per_run_usd"], b["per_day_usd"], b["month"], b["hard_stop"], b["alert_pct"]) == (None, None, 10.0,
                                                                                                True, 80)


@pytest.mark.asyncio
async def test_a_rerun_keeps_an_admins_edit(conn):
    await conn.execute("UPDATE shared.llm_role_config SET is_active = false WHERE stage = $1", STAGE)
    await conn.execute("UPDATE shared.spend_budget SET per_month_usd = 4 WHERE provider = 'llm' AND scope = $1",
                       SCOPE)
    sql = open(M487, encoding="utf-8").read().replace("BEGIN;", "").replace("COMMIT;", "")
    await conn.execute(sql)
    assert await conn.fetchval("SELECT is_active AS ok FROM shared.llm_role_config WHERE stage = $1",
                               STAGE) is False
    assert (await conn.fetchrow(BUDGET_ROW, SCOPE))["month"] == 4.0


@pytest.mark.asyncio
async def test_the_runtime_role_reads_both_rows(conn):
    # The test database has no Dev grants (Dev: cis_app SELECT on both tables, db-schema-reference); give them inside
    # the rolled-back transaction only.
    await conn.execute("GRANT USAGE ON SCHEMA shared TO cis_app")
    await conn.execute("GRANT SELECT ON shared.llm_role_config, shared.spend_budget TO cis_app")
    await conn.execute("SET LOCAL ROLE cis_app")
    assert await conn.fetchval("SELECT is_active AS ok FROM shared.llm_role_config WHERE stage = $1", STAGE) is True
    assert await conn.fetchval("SELECT hard_stop AS ok FROM shared.spend_budget WHERE provider = 'llm' AND scope = $1",
                               SCOPE) is True
