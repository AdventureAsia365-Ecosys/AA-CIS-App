"""AA-1127 (S285, C2 "spend truth"; tech lead decision S285 E2) — CIS 461: the three settings of the daily Cost Explorer
check in shared.setting_definition (conftest replays every migration). Registered; each row equals the compiled default
the reader serves; values within their bounds only; platform scope, single approval, no evidence, enforced; a label and
a complete guide; a re-run changes nothing. Runs in a transaction that is rolled back; invented values only."""
import json
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.services import cost_explorer_daily_settings as daily  # noqa: E402
from shared import settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M461 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                    "461_aa1127_cost_explorer_daily_settings.sql")
INS = ("INSERT INTO shared.setting_value (key, value, version, proposed_by, reason, evidence_ref) "
       "VALUES ($1, $2::jsonb, 1, 'admin:zz-a', 'zz test: a reason of at least twenty characters', NULL) RETURNING id")
EXPECTED = {
    daily.ENABLED: (True, None, None, None, "medium"),
    daily.RUN_HOUR_UTC: (1, 0, 23, "hour (UTC)", "low"),
    daily.STALE_AFTER_HOURS: (36, 1, 720, "hours", "low"),
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


async def _refused(c, *args):
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        async with c.transaction():
            await c.fetchval(INS, *args)


@pytest.mark.asyncio
async def test_461_registered_and_each_row_is_the_compiled_default(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '461'") == "461"
    for key, (default, lo, hi, unit, impact) in EXPECTED.items():
        row = await conn.fetchrow(
            "SELECT default_value::text AS d, min_value::float AS lo, max_value::float AS hi, unit, area, scope_kind, "
            "impact, approval, evidence_required, env_override, enforced, label, "
            "shared.setting_guide_complete(guide) AS guided FROM shared.setting_definition WHERE key = $1", key)
        assert row is not None, key
        d = settings.definition(key)
        assert json.loads(row["d"]) == default == d.default_value
        assert (row["lo"], row["hi"], row["unit"]) == (lo, hi, unit)
        assert (row["area"], row["scope_kind"], row["impact"], row["approval"]) == ("spend", "platform", impact,
                                                                                    "single")
        assert row["evidence_required"] is False and row["env_override"] is None and row["enforced"] is True
        assert row["guided"] is True and row["label"] == d.label


@pytest.mark.asyncio
async def test_a_value_must_stay_within_its_bounds(conn):
    await _refused(conn, daily.RUN_HOUR_UTC, "24")
    await _refused(conn, daily.RUN_HOUR_UTC, "-1")
    await _refused(conn, daily.STALE_AFTER_HOURS, "0")
    await _refused(conn, daily.STALE_AFTER_HOURS, "721")
    assert await conn.fetchval(INS, daily.RUN_HOUR_UTC, "6") is not None
    assert await conn.fetchval(INS, daily.STALE_AFTER_HOURS, "48") is not None
    assert await conn.fetchval(INS, daily.ENABLED, "false") is not None


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    body = open(M461, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    await conn.execute(body)
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before

