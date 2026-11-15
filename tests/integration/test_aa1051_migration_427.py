"""AA-1051 + AA-1096 follow-up (S281 lane l14) — CIS 427: the settings registry keys whose code is on main are marked
enforced (conftest replays every migration). Registered; each key of the block is enforced in the table and in the
compiled defaults the reader serves, with the same description; the two keys no code reads (the trash window, the
learner retention window) stay not enforced; every CIS key the table marks enforced is named by code (the settings-lint
rule, on the rows the database holds); a re-run changes nothing. Runs in a transaction that is rolled back."""
import json
import os
import pathlib
import re
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER

from shared import settings

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M427 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "427_aa1051_settings_enforced_keys.sql")
NOT_READ = ("cis.content.trash_retention_days", "cis.learn.learner_retention_days")
EDIT_RE = re.compile(r"\$edit\$(.*?)\$edit\$", re.DOTALL)


def _block() -> list[dict]:
    return json.loads(EDIT_RE.search(pathlib.Path(M427).read_text(encoding="utf-8")).group(1))


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
async def test_427_registered_and_the_block_keys_are_enforced(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '427'") == "427"
    block = _block()
    enforced = sorted(r["key"] for r in block if r.get("enforced") is True)
    assert len(enforced) == 34 and not set(enforced) & set(NOT_READ)
    rows = {r["key"]: r for r in await conn.fetch(
        "SELECT key, enforced, description FROM shared.setting_definition WHERE key = ANY($1::text[])",
        [r["key"] for r in block] + list(NOT_READ))}
    assert set(rows) == {r["key"] for r in block} | set(NOT_READ)
    for key in enforced:
        assert rows[key]["enforced"] is True and settings.definition(key).enforced is True, key
    for key in NOT_READ:
        assert rows[key]["enforced"] is False and settings.definition(key).enforced is False, key
    for key, r in rows.items():
        assert r["description"] == settings.definition(key).description, key
        assert "marked enforced by a later migration" not in r["description"], key


@pytest.mark.asyncio
async def test_every_enforced_cis_key_in_the_table_is_named_by_code(conn):
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    parts = []
    for top in ("api", "services", "shared", "worker"):
        for root, _dirs, files in os.walk(os.path.join(repo, top)):
            if os.path.join("shared", "settings") in root or "__pycache__" in root or "migrations" in root:
                continue
            parts += [pathlib.Path(root, f).read_text(encoding="utf-8") for f in files if f.endswith(".py")]
    code = "\n".join(parts)
    keys = [r["key"] for r in await conn.fetch(
        "SELECT key FROM shared.setting_definition WHERE app = 'cis' AND enforced ORDER BY key")]
    assert keys
    assert [k for k in keys if f'"{k}"' not in code] == []


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    body = pathlib.Path(M427).read_text(encoding="utf-8").replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    sql = "SELECT key, enforced, description FROM shared.setting_definition ORDER BY key"
    before = await conn.fetch(sql)
    await conn.execute(body)
    after = await conn.fetch(sql)
    assert [tuple(r) for r in before] == [tuple(r) for r in after]
