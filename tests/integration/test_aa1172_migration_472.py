"""AA-1172 — CIS 472: the Facts Entry instruction guard switch cis.llm.fact_instruction_guard (conftest replays every
migration). Registered; the setting row equals the compiled default the reader serves (bool, off), platform scope,
maker_checker, enforced, a label and a complete guide; a re-run changes nothing. Runs in a transaction that is rolled
back."""
import json
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.ai_safety import fact_guard as fg  # noqa: E402
from shared import settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MIGRATIONS = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")
M472 = os.path.join(MIGRATIONS, "472_aa1172_fact_instruction_guard.sql")


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


def _body(path: str) -> str:
    return open(path, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]


@pytest.mark.asyncio
async def test_472_registered_and_the_setting_row_is_the_compiled_default(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '472'") == "472"
    row = await conn.fetchrow(
        "SELECT default_value::text AS d, value_type, area, scope_kind, impact, approval, evidence_required, "
        "env_override, enforced, label, shared.setting_guide_complete(guide) AS guided "
        "FROM shared.setting_definition WHERE key = $1", fg.KEY)
    assert row is not None
    d = settings.definition(fg.KEY)
    assert json.loads(row["d"]) is d.default_value is fg.DEFAULT is False
    assert (row["value_type"], row["area"], row["scope_kind"], row["impact"], row["approval"]) == (
        "bool", "ai", "platform", "high", "maker_checker")
    assert row["evidence_required"] is False and row["env_override"] is None and row["enforced"] is True
    assert row["guided"] is True and row["label"] == d.label


@pytest.mark.asyncio
async def test_472_rerun_changes_no_setting(conn):
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    await conn.execute(_body(M472))
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before
