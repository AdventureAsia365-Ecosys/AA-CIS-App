"""AA-1180 — the hourly usage rollup equals the ledgers exactly when a ledger's cost has 8 decimals (real Postgres).

Jev's ledger (shared.decision_log.cost_usd numeric(12,8)) holds sub-micro-dollar costs; before CIS 494 the rollup's
cost_usd was numeric(18,6) and every hourly row was rounded, so GET /admin/usage-explorer/rollup/check was red for
every window with Jev calls. Here: Jev rows of 0.00000078 and 0.03332039 USD and LLM rows of 6 decimals in a few hours,
roll_hours, then check_totals is ok, the jev and llm totals equal the ledgers' to the last decimal, and the rollup row
holds the exact 8-decimal sum.

Needs CIS 494 (the wave's migration-only PR merges first): skipped while the column's scale is below 8. The fixture
rows live in May 2024 (no other test writes there) and are deleted at the end. Names are invented.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.services import usage_rollup as ur  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
STAGE = "a3_aa1180_probe"
T0 = datetime(2024, 5, 14, 0, tzinfo=timezone.utc)
T1 = datetime(2024, 5, 15, 0, tzinfo=timezone.utc)
JEV_COSTS = (Decimal("0.00000078"), Decimal("0.03332039"), Decimal("0.03331961"), Decimal("0.00000001"))
LLM_COSTS = (Decimal("0.000123"), Decimal("0.004567"))
SCALE_SQL = """SELECT numeric_scale AS s FROM information_schema.columns
                WHERE table_schema = 'shared' AND table_name = 'ai_usage_hourly' AND column_name = 'cost_usd'"""


def _at(h, m=0):
    return T0 + timedelta(hours=h, minutes=m)


async def _cleanup(c):
    await c.execute("DELETE FROM shared.decision_log WHERE stage = $1", STAGE)
    await c.execute("DELETE FROM shared.llm_call_log WHERE stage = $1", STAGE)
    await c.execute("DELETE FROM shared.ai_usage_hourly WHERE hour >= $1 AND hour < $2", T0, T1)


@pytest_asyncio.fixture
async def pool():
    c = await asyncpg.connect(DSN)
    try:
        scale = await c.fetchval(SCALE_SQL)      # None while CIS 372 is missing
    finally:
        await c.close()
    if (scale or 0) < ur.COST_SCALE:
        pytest.skip("CIS 494 is not applied on this database (the wave's migration-only PR merges first)")
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    async with p.acquire() as c:
        await _cleanup(c)
    yield p
    async with p.acquire() as c:
        await _cleanup(c)
    await p.close()


async def _rows(c):
    for i, cost in enumerate(JEV_COSTS):
        await c.execute(
            """INSERT INTO shared.decision_log (created_at, stage, question_key, subject_key, mode, zone, model,
                                                latency_ms, cost_usd, error, cached)
               VALUES ($1, $2, 'aa1180_q', 'aa1180_subject', 'shadow', 'accept', 'probe-jev', 200, $3, NULL, false)""",
            _at(3 + i % 2, 10 + i), STAGE, cost)
    for i, cost in enumerate(LLM_COSTS):
        await c.execute(
            """INSERT INTO shared.llm_call_log (stage, role, model, cost_usd, quality_signal, provider, connection_key,
                                                created_at, tokens_in, tokens_out, status, attempt_no, fallback_used,
                                                latency_ms, app)
               VALUES ($1, 'writer', 'probe-model', $2, '{}'::jsonb, 'bedrock-satellite', 'acc-probe', $3, 7, 9,
                       'ok', 1, false, 300, 'cis')""",
            STAGE, cost, _at(3, 30 + i))


@pytest.mark.asyncio
async def test_rollup_equals_the_ledgers_with_eight_decimal_costs(pool):
    async with pool.acquire() as c:
        await _rows(c)
    await ur.roll_hours(pool, T0, T1)
    check = await ur.check_totals(pool, T0, T1)
    assert check["ok"], check
    by_source = {s["source"]: s for s in check["sources"]}
    assert Decimal(by_source["jev"]["ledger"]["cost_usd"]) == Decimal(by_source["jev"]["rollup"]["cost_usd"])
    assert by_source["llm"]["equal"] and by_source["jev"]["equal"]

    async with pool.acquire() as c:
        rolled = await c.fetch(
            "SELECT hour, cost_usd FROM shared.ai_usage_hourly WHERE source = 'jev' AND stage = $1 ORDER BY hour",
            STAGE)
    assert [r["hour"] for r in rolled] == [_at(3), _at(4)]
    assert [r["cost_usd"] for r in rolled] == [JEV_COSTS[0] + JEV_COSTS[2], JEV_COSTS[1] + JEV_COSTS[3]]
    assert sum(r["cost_usd"] for r in rolled) == sum(JEV_COSTS) == Decimal("0.06664079")


@pytest.mark.asyncio
async def test_a_sub_micro_dollar_row_alone_is_not_rounded_away(pool):
    async with pool.acquire() as c:
        await c.execute(
            """INSERT INTO shared.decision_log (created_at, stage, question_key, subject_key, mode, zone, model,
                                                latency_ms, cost_usd, error, cached)
               VALUES ($1, $2, 'aa1180_q', 'aa1180_subject', 'shadow', 'accept', 'probe-jev', 200, 0.00000001,
                       NULL, false)""",
            _at(7, 5), STAGE)
    await ur.roll_hours(pool, _at(7), _at(8))
    async with pool.acquire() as c:
        got = await c.fetchval(
            "SELECT cost_usd FROM shared.ai_usage_hourly WHERE source = 'jev' AND stage = $1 AND hour = $2",
            STAGE, _at(7))
    assert got == Decimal("0.00000001")
    assert (await ur.check_totals(pool, _at(7), _at(8)))["ok"]
