"""AA-1167 — CIS 467: the writer prompts' untrusted-data fence switch cis.llm.prompt_data_fence and the prompt
versions s1_generate v4 and t9_write v9 (conftest replays every migration). Registered; the setting row equals the
compiled default the reader serves (bool, off), platform scope, maker_checker, enforced, a label and a complete guide;
both prompt rows equal the manifest entries; a re-run changes nothing. Runs in a transaction that is rolled back."""
import json
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.ai_safety import prompt_fence as pf  # noqa: E402
from shared import settings  # noqa: E402
from shared.llm_client import prompt_registry  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MIGRATIONS = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")
M467 = os.path.join(MIGRATIONS, "467_aa1167_prompt_data_fence.sql")
ROWS_467 = {
    ("s1_generate", "v4"): ("29093d733db81a16413937cc770338b8e97e6bb16649e887bbf8235c9b3a4e69",
                            "services/content_generation/s1_writer_v2.py"),
    ("t9_write", "v9"): ("adeba69b62318b048af0a22ecf3add676393a1c5e4b9f52dab561950cde8f348",
                         "services/acp_content_writing/prompts.py"),
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


def _body(path: str) -> str:
    return open(path, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]


@pytest.mark.asyncio
async def test_467_registered_and_the_setting_row_is_the_compiled_default(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '467'") == "467"
    row = await conn.fetchrow(
        "SELECT default_value::text AS d, value_type, area, scope_kind, impact, approval, evidence_required, "
        "env_override, enforced, label, shared.setting_guide_complete(guide) AS guided "
        "FROM shared.setting_definition WHERE key = $1", pf.KEY)
    assert row is not None
    d = settings.definition(pf.KEY)
    assert json.loads(row["d"]) is d.default_value is pf.DEFAULT is False
    assert (row["value_type"], row["area"], row["scope_kind"], row["impact"], row["approval"]) == (
        "bool", "ai", "platform", "high", "maker_checker")
    assert row["evidence_required"] is False and row["env_override"] is None and row["enforced"] is True
    assert row["guided"] is True and row["label"] == d.label


@pytest.mark.asyncio
async def test_467_rerun_changes_no_setting(conn):
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    await conn.execute(_body(M467))
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before


@pytest.mark.asyncio
async def test_467_registers_the_manifest_entries(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '467'") == "467"
    manifest = prompt_registry.load_manifest()
    for (stage, version), (sha, path) in ROWS_467.items():
        row = await conn.fetchrow(
            "SELECT prompt_sha256, source_path, created_by FROM shared.prompt_version "
            "WHERE stage = $1 AND version = $2", stage, version)
        assert row is not None, (stage, version)
        assert (row["prompt_sha256"], row["source_path"], row["created_by"]) == (sha, path, "migration-467")
        # the code PR's manifest names the same rows (the migration-only PR ships before the manifest bump)
        entry = next((e for e in manifest.get(stage, []) if e["version"] == version), None)
        assert entry is None or (entry["prompt_sha256"], entry["source_path"]) == (sha, path)


@pytest.mark.asyncio
async def test_467_rerun_changes_no_prompt_row(conn):
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.prompt_version")
    await conn.execute(_body(M467))
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.prompt_version") == before
