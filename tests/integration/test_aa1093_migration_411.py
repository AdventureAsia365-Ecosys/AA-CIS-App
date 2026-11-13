"""AA-1093 lane L2 — CIS 411: the three settings of the tenant erasure saga in shared.setting_definition (conftest replays
407 and 411). Registered; the rows equal the compiled defaults the reader serves (shared/settings/compiled_defaults.json,
generated from the seed); enforced is false until the saga code ships; a re-run changes nothing; the definition guard
still refuses a write by cis_app. Runs in a transaction that is rolled back."""
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared import settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M411 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "411_tenant_erasure_saga_settings.sql")
KEYS = {"cis.erasure.crm_window_days": 90, "cis.erasure.saga_approval_valid_hours": 24,
        "cis.erasure.saga_apply_enabled": False}


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
async def test_411_registered_and_the_rows_are_the_compiled_defaults(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '411'") == "411"
    rows = {r["key"]: r for r in await conn.fetch(
        "SELECT key, default_value, enforced, approval, impact FROM shared.setting_definition WHERE key = ANY($1)",
        list(KEYS))}
    assert set(rows) == set(KEYS)
    for key, default in KEYS.items():
        import json
        assert json.loads(rows[key]["default_value"]) == default == settings.definition(key).default_value
        assert rows[key]["enforced"] is False and settings.definition(key).enforced is False
        assert rows[key]["approval"] == "maker_checker" and rows[key]["impact"] == "high"


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    body = open(M411, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    await conn.execute(body)
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before
