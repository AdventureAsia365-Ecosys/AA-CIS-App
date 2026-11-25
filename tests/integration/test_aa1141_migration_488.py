"""AA-1141 (S292, status page noise) — CIS 488 on a real Postgres (conftest replays every migration): the setting
cis.ops.role_connections_samples equals its compiled default with a complete guide, values outside 1-12 are refused,
and a re-run changes nothing. Runs in a transaction that is rolled back; invented values only."""
import json
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared import ops_thresholds as t  # noqa: E402
from shared import settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M488 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                    "488_aa1141_ops_role_connections_samples.sql")
INS = ("INSERT INTO shared.setting_value (key, value, version, proposed_by, reason, evidence_ref) "
       "VALUES ($1, $2::jsonb, 1, 'admin:zz-a', 'zz test: a reason of at least twenty characters', NULL) RETURNING id")
ROW = ("SELECT default_value::text AS d, value_type, unit, min_value::float8 AS lo, max_value::float8 AS hi, area, "
       "scope_kind, impact, approval, evidence_required, env_override, enforced, label, description, "
       "shared.setting_guide_complete(guide) AS guided FROM shared.setting_definition WHERE key = $1")


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
async def test_488_registered(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '488'") == "488"


@pytest.mark.asyncio
async def test_488_key_is_the_compiled_default(conn):
    row = await conn.fetchrow(ROW, t.ROLE_CONNECTIONS_SAMPLES)
    assert row is not None
    d = settings.definition(t.ROLE_CONNECTIONS_SAMPLES)
    assert json.loads(row["d"]) == 3 == d.default_value
    assert (row["value_type"], row["unit"], row["lo"], row["hi"]) == ("int", "samples", 1.0, 12.0)
    assert (row["area"], row["scope_kind"], row["impact"], row["approval"]) == (
        "ops", "platform", "medium", "maker_checker")
    assert row["evidence_required"] is False and row["env_override"] is None and row["enforced"] is True
    assert row["guided"] is True and row["label"] == d.label and row["description"] == d.description


@pytest.mark.asyncio
async def test_a_value_must_stay_inside_its_bounds(conn):
    for bad in ("0", "13", "2.5", '"3"'):
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with conn.transaction():
                await conn.fetchval(INS, t.ROLE_CONNECTIONS_SAMPLES, bad)
    assert await conn.fetchval(INS, t.ROLE_CONNECTIONS_SAMPLES, "1") is not None


@pytest.mark.asyncio
async def test_rerun_488_changes_nothing(conn):
    body = open(M488, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    q = "SELECT key, label, description, guide::text AS g FROM shared.setting_definition ORDER BY key"
    before = await conn.fetch(q)
    await conn.execute(body)
    assert [tuple(r) for r in await conn.fetch(q)] == [tuple(r) for r in before]
