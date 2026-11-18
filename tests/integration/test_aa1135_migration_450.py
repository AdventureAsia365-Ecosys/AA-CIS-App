"""AA-1135 (S284) — CIS 450: the two custom-date-range limits of Platform › Delivery in shared.setting_definition
(conftest replays every migration). Registered; each row equals the compiled default the reader serves; values within
their bounds only; platform scope, single approval, no evidence, enforced; each key carries its label and a complete
guide; a re-run changes nothing. Runs in a transaction that is rolled back; invented values only."""
import json
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.reports import delivery_settings as ds  # noqa: E402
from shared import settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M450 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                    "450_aa1135_delivery_range_settings.sql")
INS = ("INSERT INTO shared.setting_value (key, value, version, proposed_by, reason, evidence_ref) "
       "VALUES ($1, $2::jsonb, 1, 'admin:zz-a', 'zz test: a reason of at least twenty characters', NULL) RETURNING id")
EXPECTED = {
    ds.CUSTOM_RANGE_MAX_DAYS: (90, 1, 400, "days"),
    ds.CUSTOM_RANGE_CACHE_ENTRIES: (20, 1, 200, "ranges"),
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
async def test_450_registered_and_each_row_is_the_compiled_default(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '450'") == "450"
    assert set(EXPECTED) == set(ds.RANGE_ALL)
    assert not set(ds.RANGE_ALL) & (set(ds.ALL) | set(ds.QUEUE_ALL))
    for key, (default, lo, hi, unit) in EXPECTED.items():
        row = await conn.fetchrow(
            "SELECT default_value::text AS d, min_value::float AS lo, max_value::float AS hi, unit, area, scope_kind, "
            "impact, approval, evidence_required, env_override, enforced, value_type, label, "
            "shared.setting_guide_complete(guide) AS guided FROM shared.setting_definition WHERE key = $1", key)
        assert row is not None, key
        d = settings.definition(key)
        assert json.loads(row["d"]) == default == d.default_value
        assert (row["lo"], row["hi"], row["unit"], row["value_type"]) == (lo, hi, unit, "int")
        assert (row["area"], row["scope_kind"], row["impact"], row["approval"]) == ("delivery", "platform", "low",
                                                                                     "single")
        assert row["evidence_required"] is False and row["env_override"] is None and row["enforced"] is True
        assert row["guided"] is True and row["label"] == d.label


@pytest.mark.asyncio
async def test_a_value_must_stay_within_its_bounds(conn):
    await _refused(conn, ds.CUSTOM_RANGE_MAX_DAYS, "0")
    await _refused(conn, ds.CUSTOM_RANGE_MAX_DAYS, "401")
    await _refused(conn, ds.CUSTOM_RANGE_CACHE_ENTRIES, "201")
    assert await conn.fetchval(INS, ds.CUSTOM_RANGE_MAX_DAYS, "30") is not None
    assert await conn.fetchval(INS, ds.CUSTOM_RANGE_CACHE_ENTRIES, "5") is not None


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    body = open(M450, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    await conn.execute(body)
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before
