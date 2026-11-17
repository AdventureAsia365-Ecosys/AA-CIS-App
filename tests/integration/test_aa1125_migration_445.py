"""AA-1125 (S283) — CIS 445: the six merge-queue thresholds of Platform › Delivery in shared.setting_definition (conftest
replays every migration). Registered; each row equals the compiled default the reader serves; values within their
bounds only; platform scope, single approval, no evidence, enforced; each key carries its label and a complete guide;
every amber value is on the far side of its green value; a re-run changes nothing. Runs in a transaction that is rolled
back; invented values only."""
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
M445 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                    "445_aa1125_delivery_queue_settings.sql")
INS = ("INSERT INTO shared.setting_value (key, value, version, proposed_by, reason, evidence_ref) "
       "VALUES ($1, $2::jsonb, 1, 'admin:zz-a', 'zz test: a reason of at least twenty characters', NULL) RETURNING id")
EXPECTED = {
    ds.QUEUE_WAIT_GREEN_HOURS: (1, 0.1, 168, "hours"),
    ds.QUEUE_WAIT_AMBER_HOURS: (4, 0.1, 336, "hours"),
    ds.QUEUE_EJECTION_GREEN_PCT: (10, 0, 100, "%"),
    ds.QUEUE_EJECTION_AMBER_PCT: (25, 0, 100, "%"),
    ds.QUEUE_FLAKY_GREEN_PCT: (5, 0, 100, "%"),
    ds.QUEUE_FLAKY_AMBER_PCT: (15, 0, 100, "%"),
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
async def test_445_registered_and_each_row_is_the_compiled_default(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '445'") == "445"
    assert set(EXPECTED) == set(ds.QUEUE_ALL)
    assert not set(ds.QUEUE_ALL) & set(ds.ALL)
    for key, (default, lo, hi, unit) in EXPECTED.items():
        row = await conn.fetchrow(
            "SELECT default_value::text AS d, min_value::float AS lo, max_value::float AS hi, unit, area, scope_kind, "
            "impact, approval, evidence_required, env_override, enforced, label, "
            "shared.setting_guide_complete(guide) AS guided FROM shared.setting_definition WHERE key = $1", key)
        assert row is not None, key
        d = settings.definition(key)
        assert json.loads(row["d"]) == default == d.default_value
        assert (row["lo"], row["hi"], row["unit"]) == (lo, hi, unit)
        assert (row["area"], row["scope_kind"], row["impact"], row["approval"]) == ("delivery", "platform", "low",
                                                                                     "single")
        assert row["evidence_required"] is False and row["env_override"] is None and row["enforced"] is True
        assert row["guided"] is True and row["label"] == d.label


def test_amber_defaults_sit_beyond_green():
    for _metric, (green, amber, higher_is_better) in ds.QUEUE_BANDS.items():
        g, a = settings.definition(green).default_value, settings.definition(amber).default_value
        assert (a < g) if higher_is_better else (a > g), (green, amber)


@pytest.mark.asyncio
async def test_a_value_must_stay_within_its_bounds(conn):
    await _refused(conn, ds.QUEUE_EJECTION_GREEN_PCT, "101")
    await _refused(conn, ds.QUEUE_WAIT_GREEN_HOURS, "0.05")
    await _refused(conn, ds.QUEUE_WAIT_AMBER_HOURS, "400")
    assert await conn.fetchval(INS, ds.QUEUE_FLAKY_GREEN_PCT, "3") is not None
    assert await conn.fetchval(INS, ds.QUEUE_WAIT_GREEN_HOURS, "0.5") is not None


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    body = open(M445, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    await conn.execute(body)
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before
