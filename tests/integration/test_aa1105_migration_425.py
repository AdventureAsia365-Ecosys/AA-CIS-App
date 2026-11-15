"""AA-1105 (S281) — CIS 425: the academy web's per-visitor certificate check limit
(cis.learn.verify_visitor_requests_per_minute) in shared.setting_definition (conftest replays every migration).
Registered; the row equals the compiled default the reader serves (6 requests); bounds 1..300 and platform scope;
no evidence, single approval, no env override; enforced since CIS 429 like the other cis.learn.* keys the API reads;
a label and a complete guide; a value over the bound is refused; a re-run changes nothing. Runs in a transaction that
is rolled back; invented values only."""
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
M429 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                    "425_aa1105_learn_verify_visitor_setting.sql")
KEY = "cis.learn.verify_visitor_requests_per_minute"
INS = ("INSERT INTO shared.setting_value (key, value, version, proposed_by, reason) "
       "VALUES ($1, $2::jsonb, 1, 'admin:zz-a', 'zz test: a reason of at least twenty characters') RETURNING id")
ROW = ("SELECT default_value::text AS d, min_value AS lo, max_value AS hi, unit, value_type, scope_kind, impact, "
       "approval, evidence_required, env_override, enforced, area, label, cache_s, "
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
async def test_429_registered_and_the_row_is_the_compiled_default(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '425'") == "425"
    r = await conn.fetchrow(ROW, KEY)
    d = settings.definition(KEY)
    assert json.loads(r["d"]) == 6 == d.default_value
    assert (r["value_type"], int(r["lo"]), int(r["hi"]), r["unit"]) == ("int", 1, 300, "requests")
    assert (r["scope_kind"], r["impact"], r["approval"], r["area"]) == ("platform", "medium", "single", "learn")
    assert r["evidence_required"] is False and r["env_override"] is None and r["cache_s"] == 60
    assert r["enforced"] is True and d.enforced is True   # CIS 429
    assert r["guided"] is True and r["label"] == d.label


@pytest.mark.asyncio
async def test_the_default_is_below_the_per_address_default(conn):
    per_address = json.loads(await conn.fetchval(
        "SELECT default_value::text AS ok FROM shared.setting_definition "
        "WHERE key = 'cis.learn.verify_requests_per_minute'"))
    assert json.loads((await conn.fetchrow(ROW, KEY))["d"]) < per_address


@pytest.mark.asyncio
async def test_a_value_outside_the_bounds_is_refused(conn):
    for bad in ("0", "301"):
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with conn.transaction():
                await conn.fetchval(INS, KEY, bad)
    assert await conn.fetchval(INS, KEY, "12") is not None


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    body = open(M429, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    await conn.execute(body)
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before
