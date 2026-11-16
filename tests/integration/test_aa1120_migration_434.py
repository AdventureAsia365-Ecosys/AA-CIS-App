"""AA-1120 (S282) — CIS 434: the research cache windows in shared.setting_definition (conftest replays every
migration). Registered; each row equals the compiled default the reader serves; integers within their bounds only;
platform scope, single approval, no evidence, enforced; each key carries its label and a complete guide; a re-run
changes nothing. Runs in a transaction that is rolled back; invented values only."""
import json
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.seo_intelligence import research_settings as rs  # noqa: E402
from shared import settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M434 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                    "434_aa1120_research_cache_settings.sql")
INS = ("INSERT INTO shared.setting_value (key, value, version, proposed_by, reason, evidence_ref) "
       "VALUES ($1, $2::jsonb, 1, 'admin:zz-a', 'zz test: a reason of at least twenty characters', NULL) RETURNING id")
EXPECTED = {
    rs.VOLUME_FRESH_DAYS: (365, 30, 730),
    rs.SERP_FRESH_DAYS: (365, 30, 730),
    rs.QUEUE_RECOVER_DAYS: (7, 0, 30),
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
async def test_434_registered_and_each_row_is_the_compiled_default(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '434'") == "434"
    assert set(EXPECTED) == set(rs.ALL)
    for key, (default, lo, hi) in EXPECTED.items():
        row = await conn.fetchrow(
            "SELECT default_value::text AS d, min_value::float AS lo, max_value::float AS hi, unit, area, scope_kind, "
            "impact, approval, evidence_required, env_override, enforced, label, "
            "shared.setting_guide_complete(guide) AS guided FROM shared.setting_definition WHERE key = $1", key)
        assert row is not None, key
        d = settings.definition(key)
        assert json.loads(row["d"]) == default == d.default_value
        assert (row["lo"], row["hi"], row["unit"]) == (lo, hi, "days")
        assert (row["area"], row["scope_kind"], row["impact"], row["approval"]) == ("seo", "platform", "medium",
                                                                                     "single")
        assert row["evidence_required"] is False and row["env_override"] is None and row["enforced"] is True
        assert row["guided"] is True and row["label"] == d.label


@pytest.mark.asyncio
async def test_a_value_must_stay_within_its_bounds(conn):
    await _refused(conn, rs.VOLUME_FRESH_DAYS, "29")
    await _refused(conn, rs.SERP_FRESH_DAYS, "731")
    await _refused(conn, rs.QUEUE_RECOVER_DAYS, "31")
    assert await conn.fetchval(INS, rs.QUEUE_RECOVER_DAYS, "0") is not None
    assert await conn.fetchval(INS, rs.VOLUME_FRESH_DAYS, "180") is not None


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    body = open(M434, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    await conn.execute(body)
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before
