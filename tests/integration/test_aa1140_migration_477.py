"""AA-1140 (S289 live check) — CIS 477 on a real Postgres (conftest replays every migration): the two free storage
shares of the allocated storage are registered and equal the compiled defaults (20 % / 10 %, pct, maker-checker, a
complete guide); values outside their bounds are refused; the CIS 475 GiB keys stay as the fallback with the new
label and guide and their CIS 475 bounds and defaults; a re-run changes nothing. Runs in a transaction that is rolled
back; invented values only."""
import json
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared import ops_status_thresholds as t  # noqa: E402
from shared import settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M477 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                    "477_aa1140_rds_free_storage_pct.sql")
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
async def test_477_registered_and_the_shares_are_the_compiled_defaults(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '477'") == "477"
    for key, default, bounds in ((t.RDS_FREE_WARN_PCT, 20, (5, 60)), (t.RDS_FREE_CRIT_PCT, 10, (2, 50))):
        row = await conn.fetchrow(ROW, key)
        assert row is not None, key
        d = settings.definition(key)
        assert json.loads(row["d"]) == default == d.default_value
        assert (row["value_type"], row["unit"], (row["lo"], row["hi"])) == ("pct", "%", bounds)
        assert (row["area"], row["scope_kind"], row["impact"], row["approval"]) == (
            "ops", "platform", "low", "maker_checker")
        assert row["evidence_required"] is False and row["env_override"] is None and row["enforced"] is True
        assert row["guided"] is True and row["label"] == d.label and row["description"] == d.description


@pytest.mark.asyncio
async def test_the_gib_keys_stay_as_the_fallback(conn):
    for key, default in ((t.RDS_FREE_STORAGE_WARN_GIB, 15), (t.RDS_FREE_STORAGE_CRIT_GIB, 10)):
        row = await conn.fetchrow(ROW, key)
        d = settings.definition(key)
        assert json.loads(row["d"]) == default and row["unit"] == "GiB" and row["guided"] is True
        assert "fallback" in row["label"] and row["label"] == d.label and row["description"] == d.description


@pytest.mark.asyncio
async def test_a_share_must_stay_inside_its_bounds(conn):
    for key, bad in ((t.RDS_FREE_WARN_PCT, "4"), (t.RDS_FREE_WARN_PCT, "61"), (t.RDS_FREE_CRIT_PCT, "1"),
                     (t.RDS_FREE_CRIT_PCT, '"10"')):
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with conn.transaction():
                await conn.fetchval(INS, key, bad)
    assert await conn.fetchval(INS, t.RDS_FREE_WARN_PCT, "25") is not None


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    body = open(M477, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    before = await conn.fetch("SELECT key, label, description, guide::text AS g FROM shared.setting_definition "
                              "ORDER BY key")
    await conn.execute(body)
    after = await conn.fetch("SELECT key, label, description, guide::text AS g FROM shared.setting_definition "
                             "ORDER BY key")
    assert [tuple(r) for r in after] == [tuple(r) for r in before]
