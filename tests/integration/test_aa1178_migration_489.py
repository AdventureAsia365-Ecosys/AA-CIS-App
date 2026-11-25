"""AA-1178 — CIS 489 on a real Postgres (conftest replays every migration): the two job runner lane caps equal the
compiled defaults with a complete guide, values outside their bounds refused, a re-run of 489 changes nothing.
Runs in a transaction that is rolled back; invented values only."""
import json
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared import settings  # noqa: E402
from shared.jobs import lanes  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M489 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "489_aa1178_job_lane_settings.sql")
INS = ("INSERT INTO shared.setting_value (key, value, version, proposed_by, reason, evidence_ref) "
       "VALUES ($1, $2::jsonb, 1, 'admin:zz-a', 'zz test: a reason of at least twenty characters', NULL) RETURNING id")
ROW = ("SELECT default_value::text AS d, value_type, unit, min_value::float8 AS lo, max_value::float8 AS hi, area, "
       "scope_kind, impact, approval, evidence_required, env_override, enforced, label, description, "
       "shared.setting_guide_complete(guide) AS guided FROM shared.setting_definition WHERE key = $1")
EXPECTED = {
    lanes.BACKGROUND_RUNNING_KEY: (1, "int", "jobs", (1, 4)),
    lanes.MONITOR_RUNNING_KEY: (2, "int", "jobs", (1, 4)),
}


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


def test_every_reader_key_is_seeded():
    assert set(EXPECTED) == set(lanes.ALL)


def test_the_accessors_serve_the_compiled_defaults():
    assert lanes.background_max_running() == 1 and lanes.monitor_max_running() == 2
    assert lanes.monitor_slots_bound() == 4


@pytest.mark.asyncio
async def test_489_registered(conn):
    assert await conn.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version IN ('489')") == 1


@pytest.mark.asyncio
async def test_489_keys_are_the_compiled_defaults(conn):
    for key, (default, vtype, unit, bounds) in EXPECTED.items():
        row = await conn.fetchrow(ROW, key)
        assert row is not None, key
        d = settings.definition(key)
        assert json.loads(row["d"]) == default == d.default_value, key
        assert (row["value_type"], row["unit"], (row["lo"], row["hi"])) == (vtype, unit, bounds), key
        assert (row["area"], row["scope_kind"], row["impact"], row["approval"]) == (
            "ops", "platform", "medium", "maker_checker")
        assert row["evidence_required"] is False and row["env_override"] is None and row["enforced"] is True
        assert row["guided"] is True and row["label"] == d.label and row["description"] == d.description


@pytest.mark.asyncio
async def test_a_value_must_stay_inside_its_bounds(conn):
    for key in lanes.ALL:
        for bad in ("0", "5", "1.5", '"2"'):
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                async with conn.transaction():
                    await conn.fetchval(INS, key, bad)
    assert await conn.fetchval(INS, lanes.MONITOR_RUNNING_KEY, "3") is not None
    assert await conn.fetchval(INS, lanes.BACKGROUND_RUNNING_KEY, "2") is not None


@pytest.mark.asyncio
async def test_rerun_489_changes_nothing(conn):
    body = open(M489, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    q = "SELECT key, label, description, guide::text AS g FROM shared.setting_definition ORDER BY key"
    before = await conn.fetch(q)
    await conn.execute(body)
    assert [tuple(r) for r in await conn.fetch(q)] == [tuple(r) for r in before]
