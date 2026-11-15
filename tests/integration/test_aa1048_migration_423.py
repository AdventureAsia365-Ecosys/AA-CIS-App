"""AA-1048 (S281 lane l8) — CIS 423: the three step bars of the two-step claim-instrument evaluation and the fresh-pool
sheet size in shared.setting_definition (conftest replays every migration). Registered; the rows equal the compiled
defaults the reader serves; the defaults equal the pre-registration file; enforced since CIS 429 (the two-step code is
on main); the bars need evidence on a change; each key carries its label and a complete guide; a re-run changes nothing.
Runs in a transaction that is rolled back."""
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
M423 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "423_aa1048_two_step_settings.sql")
KEYS = {"cis.quality.claim_two_step_route_tp_min_pct": 95, "cis.quality.claim_two_step_judge_tp_keep_min_pct": 95,
        "cis.quality.claim_two_step_judge_fp_drop_min_pct": 40, "cis.quality.claim_fresh_pool_sample_max": 150}
BARS = {"cis.quality.claim_two_step_route_tp_min_pct", "cis.quality.claim_two_step_judge_tp_keep_min_pct",
        "cis.quality.claim_two_step_judge_fp_drop_min_pct"}


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
async def test_423_registered_and_the_rows_are_the_compiled_defaults(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '423'") == "423"
    rows = {r["key"]: r for r in await conn.fetch(
        "SELECT key, default_value, enforced, approval, evidence_required, label, "
        "shared.setting_guide_complete(guide) AS guided FROM shared.setting_definition WHERE key = ANY($1)", list(KEYS))}
    assert set(rows) == set(KEYS)
    for key, default in KEYS.items():
        assert json.loads(rows[key]["default_value"]) == default == settings.definition(key).default_value
        assert rows[key]["enforced"] is True and settings.definition(key).enforced is True   # CIS 429
        assert rows[key]["approval"] == "maker_checker"
        assert rows[key]["evidence_required"] is (key in BARS)
        assert rows[key]["guided"] is True and rows[key]["label"] == settings.definition(key).label


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    body = open(M423, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    await conn.execute(body)
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before
