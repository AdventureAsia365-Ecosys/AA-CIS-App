"""AA-1127 (S284, UI audit batch C1) — CIS 448: the AI gateway idle window in shared.setting_definition (conftest replays
every migration). Registered; the row equals the compiled default the reader serves; values within their bounds only;
platform scope, single approval, no evidence, enforced; a label and a complete guide; a re-run changes nothing. Runs in
a transaction that is rolled back; invented values only."""
import json
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.services import gateway_health_settings as gh  # noqa: E402
from shared import settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M448 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "448_aa1127_gateway_idle_setting.sql")
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


async def _refused(c, *args):
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        async with c.transaction():
            await c.fetchval(INS, *args)


@pytest.mark.asyncio
async def test_448_registered_and_the_row_is_the_compiled_default(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '448'") == "448"
    assert gh.ALL == (gh.IDLE_AFTER_HOURS,)
    row = await conn.fetchrow(
        "SELECT default_value::text AS d, min_value::float AS lo, max_value::float AS hi, unit, area, scope_kind, "
        "impact, approval, evidence_required, env_override, enforced, label, "
        "shared.setting_guide_complete(guide) AS guided FROM shared.setting_definition WHERE key = $1",
        gh.IDLE_AFTER_HOURS)
    assert row is not None
    d = settings.definition(gh.IDLE_AFTER_HOURS)
    assert json.loads(row["d"]) == 6 == d.default_value
    assert (row["lo"], row["hi"], row["unit"]) == (1, 168, "hours")
    assert (row["area"], row["scope_kind"], row["impact"], row["approval"]) == ("ai", "platform", "low", "single")
    assert row["evidence_required"] is False and row["env_override"] is None and row["enforced"] is True
    assert row["guided"] is True and row["label"] == d.label


@pytest.mark.asyncio
async def test_a_value_must_stay_within_its_bounds(conn):
    await _refused(conn, gh.IDLE_AFTER_HOURS, "0")
    await _refused(conn, gh.IDLE_AFTER_HOURS, "169")
    assert await conn.fetchval(INS, gh.IDLE_AFTER_HOURS, "24") is not None


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    body = open(M448, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    await conn.execute(body)
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before
