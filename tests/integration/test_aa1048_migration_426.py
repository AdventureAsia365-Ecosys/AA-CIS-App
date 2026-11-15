"""AA-1048 (S281 lane l13) — CIS 426: the R1 firing sample size of unlabelled claims in shared.setting_definition
(conftest replays every migration). Registered; the row equals the compiled default the reader serves; enforced
since CIS 427 (the firing code is on main); maker-checker; it carries its label and a complete guide; a re-run changes
nothing. Runs in a transaction that is rolled back."""
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
M427 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                    "426_aa1048_claim_fire_sample_setting.sql")
KEY = "cis.quality.claim_fire_sample_max"


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
async def test_427_registered_and_the_row_is_the_compiled_default(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '426'") == "426"
    row = await conn.fetchrow(
        "SELECT default_value, enforced, approval, evidence_required, label, min_value, max_value, value_type, "
        "shared.setting_guide_complete(guide) AS guided FROM shared.setting_definition WHERE key = $1", KEY)
    assert row is not None
    assert json.loads(row["default_value"]) == 600 == settings.definition(KEY).default_value
    assert row["enforced"] is True and settings.definition(KEY).enforced is True   # CIS 427
    assert row["approval"] == "maker_checker" and row["evidence_required"] is False
    assert row["value_type"] == "int" and float(row["min_value"]) == 1 and float(row["max_value"]) == 3000
    assert row["guided"] is True and row["label"] == settings.definition(KEY).label


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    body = open(M427, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    await conn.execute(body)
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before
