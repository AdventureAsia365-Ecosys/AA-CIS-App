"""AA-1148 (S290) — CIS 478 on a real Postgres (conftest replays every migration): the three raw_tours columns (the
supplier soft key, the entry origin with its CHECK and default, the structured facts that must be an object), the
partial supplier index, the four content workspace settings equal to the compiled defaults with complete guides, and a
re-run that changes nothing. Runs in a transaction that is rolled back; invented names only."""
import json
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.content_workspace import policy  # noqa: E402
from shared import settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M478 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                    "478_aa1148_manual_tours_supplier_link.sql")
MASTER = "00000000-0000-0000-0000-000000000001"
ROW = ("SELECT default_value::text AS d, value_type, min_value::float8 AS lo, max_value::float8 AS hi, area, "
       "scope_kind, approval, env_override, enforced, label, description, "
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


async def _insert(conn, **cols):
    names = ["tour_id", "tenant_id", "batch_id", "src_name", *cols]
    vals = [uuid.uuid4(), MASTER, uuid.uuid4(), "Zz Quillmoor Ridge Walk", *cols.values()]
    ph = ", ".join(f"${i + 1}" for i in range(len(vals)))
    await conn.execute(f"INSERT INTO silver_aa_internal.raw_tours ({', '.join(names)}) VALUES ({ph})", *vals)
    return vals[0]


@pytest.mark.asyncio
async def test_478_registered_with_the_columns_and_the_index(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '478'") == "478"
    cols = {r["column_name"]: r for r in await conn.fetch(
        "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_schema = 'silver_aa_internal' AND table_name = 'raw_tours' "
        "AND column_name IN ('supplier_id', 'entry_origin', 'manual_facts')")}
    assert cols["supplier_id"]["data_type"] == "uuid" and cols["supplier_id"]["is_nullable"] == "YES"
    assert cols["entry_origin"]["is_nullable"] == "NO" and "sheet" in cols["entry_origin"]["column_default"]
    assert cols["manual_facts"]["data_type"] == "jsonb"
    idx = await conn.fetchval("SELECT indexdef AS ok FROM pg_indexes WHERE schemaname = 'silver_aa_internal' "
                              "AND indexname = 'raw_tours_supplier_idx'")
    assert idx and "WHERE (supplier_id IS NOT NULL)" in idx


@pytest.mark.asyncio
async def test_a_new_row_is_a_sheet_row_and_the_checks_refuse_bad_values(conn):
    tid = await _insert(conn)
    row = await conn.fetchrow("SELECT entry_origin, supplier_id, manual_facts FROM silver_aa_internal.raw_tours "
                              "WHERE tour_id = $1", tid)
    assert (row["entry_origin"], row["supplier_id"], row["manual_facts"]) == ("sheet", None, None)
    await _insert(conn, entry_origin="manual", manual_facts=json.dumps({"v": 1, "days": []}))
    with pytest.raises(asyncpg.CheckViolationError):
        async with conn.transaction():
            await _insert(conn, entry_origin="typed")
    with pytest.raises(asyncpg.CheckViolationError):
        async with conn.transaction():
            await _insert(conn, manual_facts=json.dumps(["not", "an", "object"]))


@pytest.mark.asyncio
async def test_the_settings_are_the_compiled_defaults(conn):
    expected = {policy.MAX_DAYS_KEY: (21, (1, 60)), policy.MIN_INCLUSIONS_KEY: (3, (0, 20)),
                policy.MIN_EXCLUSIONS_KEY: (1, (0, 20)), policy.BULK_MAX_KEY: (100, (1, 500))}
    for key, (default, bounds) in expected.items():
        row = await conn.fetchrow(ROW, key)
        assert row is not None, key
        d = settings.definition(key)
        assert json.loads(row["d"]) == default == d.default_value
        assert (row["value_type"], (row["lo"], row["hi"])) == ("int", bounds)
        assert (row["area"], row["scope_kind"], row["approval"]) == ("content", "platform", "single")
        assert row["env_override"] is None and row["enforced"] is True and row["guided"] is True
        assert row["label"] == d.label and row["description"] == d.description


@pytest.mark.asyncio
async def test_a_rerun_changes_nothing(conn):
    with open(M478, encoding="utf-8") as f:
        sql = f.read()
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    await conn.execute(sql.replace("BEGIN;", "", 1).replace("COMMIT;", ""))
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '478'") == 1
