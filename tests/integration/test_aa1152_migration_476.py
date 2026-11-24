"""AA-1152 (S289; S285 B2 option C) — CIS 476: TripPlanner's window for an agency's own prices in
shared.setting_definition (conftest replays every migration). Registered; the row equals the compiled default and the
TripPlanner code default (6 hours, AA-TripPlanner-Web backend/tests/test_s279_settings.py); an int of 2..168 only;
platform scope, maker-checker, medium impact, no env override, enforced; a label and a complete guide; the TripPlanner
role reads it through catalog.v_app_settings_v1 like its other keys (tested for every key in test_aa1096_migration_415);
a re-run changes nothing. Runs in a transaction that is rolled back; invented values only."""
import json
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared import settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M476 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                    "476_aa1152_tp_agency_price_stale_hours.sql")
KEY = "tripplanner.prices.agency_stale_hours"
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
async def test_476_registered_and_the_row_is_the_compiled_default(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '476'") == "476"
    row = await conn.fetchrow(
        "SELECT app, default_value::text AS d, value_type, unit, min_value::float8 AS lo, max_value::float8 AS hi, "
        "area, scope_kind, impact, approval, evidence_required, env_override, enforced, label, "
        "shared.setting_guide_complete(guide) AS guided FROM shared.setting_definition WHERE key = $1", KEY)
    assert row is not None
    d = settings.definition(KEY)
    assert json.loads(row["d"]) == 6 == d.default_value
    assert (row["app"], row["value_type"], row["unit"], row["lo"], row["hi"]) == ("tripplanner", "int", "hours", 2, 168)
    assert (row["area"], row["scope_kind"], row["impact"], row["approval"]) == (
        "prices", "platform", "medium", "maker_checker")
    assert row["evidence_required"] is False and row["env_override"] is None and row["enforced"] is True
    assert row["guided"] is True and row["label"] == d.label


@pytest.mark.asyncio
async def test_a_value_must_be_whole_hours_inside_the_bounds(conn):
    for bad in ('"6"', "1", "169", "2.5"):
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with conn.transaction():
                await conn.fetchval(INS, KEY, bad)
    assert await conn.fetchval(INS, KEY, "12") is not None


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    body = open(M476, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    await conn.execute(body)
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before
