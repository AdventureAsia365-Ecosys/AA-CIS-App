"""AA-1113 (S284) — CIS 453: the provider budget per LLM connection in shared.setting_definition (conftest replays every
migration). Registered; the row is the compiled default the reader serves; a value is a JSON object whose entries stay
within 0..1000000; platform scope, single approval, enforced, guided; a re-run changes nothing. Runs in a transaction
that is rolled back; invented connection keys and values only."""
import json
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.services import llm_connection_split as split  # noqa: E402
from shared import settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M453 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                    "453_aa1113_connection_provider_budget_setting.sql")
INS = ("INSERT INTO shared.setting_value (key, value, version, proposed_by, reason, evidence_ref) "
       "VALUES ($1, $2::jsonb, 1, 'admin:zz-a', 'zz test: a reason of at least twenty characters', NULL) RETURNING id")


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
async def test_453_registered_and_the_row_is_the_compiled_default(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '453'") == "453"
    row = await conn.fetchrow(
        "SELECT app, value_type, default_value::text AS d, min_value::float AS lo, max_value::float AS hi, unit, area, "
        "scope_kind, impact, approval, evidence_required, env_override, enforced, label, "
        "shared.setting_guide_complete(guide) AS guided FROM shared.setting_definition WHERE key = $1",
        split.PROVIDER_BUDGET_KEY)
    assert row is not None
    d = settings.definition(split.PROVIDER_BUDGET_KEY)
    assert row["app"] == "cis" and row["value_type"] == "json" == d.value_type
    assert json.loads(row["d"]) == {} == d.default_value
    assert (row["lo"], row["hi"], row["unit"]) == (0.0, 1000000.0, "USD/month")
    assert (row["area"], row["scope_kind"], row["impact"], row["approval"]) == ("spend", "platform", "medium", "single")
    assert row["evidence_required"] is False and row["env_override"] is None and row["enforced"] is True
    assert row["guided"] is True and row["label"] == d.label


@pytest.mark.asyncio
async def test_a_value_is_an_object_of_amounts_within_bounds(conn):
    async with conn.transaction():
        assert await conn.fetchval(INS, split.PROVIDER_BUDGET_KEY, json.dumps({"zz-a": 100, "zz-b": 0}))
    for bad in ({"zz-a": -1}, {"zz-a": 1000001}, {"zz-a": "lots"}, "zz"):
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with conn.transaction():
                await conn.fetchval(INS, split.PROVIDER_BUDGET_KEY, json.dumps(bad))


@pytest.mark.asyncio
async def test_453_re_run_changes_nothing(conn):
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    with open(M453, encoding="utf-8") as f:
        body = f.read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    await conn.execute(body)
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before
