"""AA-1169 — CIS 490: the blog writer's CLAIM CHECK switch cis.t9.blog_claim_rule and the prompt version t9_write v10
(conftest replays every migration). Registered; the setting row equals the compiled default the reader serves (bool,
off), content area, platform scope, maker_checker, enforced, a label and a complete guide; the prompt row equals the
manifest entry when the code PR's manifest has it; a re-run changes nothing. Runs in a transaction that is rolled
back."""
import json
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.acp_content_writing import claim_rule  # noqa: E402
from shared import settings  # noqa: E402
from shared.llm_client import prompt_registry  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MIGRATIONS = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")
M490 = os.path.join(MIGRATIONS, "490_aa1169_blog_claim_rule.sql")
ROW_490 = ("t9_write", "v10", "28f5a801e90ed5a2ec881c2a6d2efc29706cc59435d6d28e35d451e3f65613fc",
           "services/acp_content_writing/prompts.py")


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
async def test_490_registered_and_the_setting_row_is_the_compiled_default(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '490'") == "490"
    row = await conn.fetchrow(
        "SELECT default_value::text AS d, value_type, area, scope_kind, impact, approval, evidence_required, "
        "env_override, enforced, label, shared.setting_guide_complete(guide) AS guided "
        "FROM shared.setting_definition WHERE key = $1", claim_rule.KEY)
    assert row is not None
    d = settings.definition(claim_rule.KEY)
    assert json.loads(row["d"]) is d.default_value is claim_rule.DEFAULT is False
    assert (row["value_type"], row["area"], row["scope_kind"], row["impact"], row["approval"]) == (
        "bool", "content", "platform", "high", "maker_checker")
    assert row["evidence_required"] is False and row["env_override"] is None and row["enforced"] is True
    assert row["guided"] is True and row["label"] == d.label


@pytest.mark.asyncio
async def test_490_rerun_changes_no_setting(conn):
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    await conn.execute(_body(M490))
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before


@pytest.mark.asyncio
async def test_490_registers_the_prompt_row(conn):
    stage, version, sha, path = ROW_490
    row = await conn.fetchrow(
        "SELECT prompt_sha256, source_path, created_by FROM shared.prompt_version WHERE stage = $1 AND version = $2",
        stage, version)
    assert row is not None
    assert (row["prompt_sha256"], row["source_path"], row["created_by"]) == (sha, path, "migration-490")
    # the code PR's manifest names the same row (the migration-only PR ships before the manifest bump)
    entry = next((e for e in prompt_registry.load_manifest().get(stage, []) if e["version"] == version), None)
    assert entry is None or (entry["prompt_sha256"], entry["source_path"]) == (sha, path)


@pytest.mark.asyncio
async def test_490_rerun_changes_no_prompt_row(conn):
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.prompt_version")
    await conn.execute(_body(M490))
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.prompt_version") == before
