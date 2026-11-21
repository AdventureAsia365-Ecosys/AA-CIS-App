"""AA-1096 lane l5 (S281) — CIS 424: the one-release env overrides of the settings registry are retired (conftest
replays every migration). Registered; no definition names an env var; each edited row equals the compiled default the
reader serves (description and guide included); the guides stay complete; a re-run changes nothing. Runs in a
transaction that is rolled back; invented values only."""
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
HERE = os.path.dirname(__file__)
M428 = os.path.join(HERE, "..", "..", "api", "migrations", "424_aa1096_retire_settings_env_overrides.sql")
RETIRED = os.path.join(HERE, "..", "unit", "snapshots", "aa1096_retired_env_overrides.json")


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
async def test_428_registered_and_no_definition_names_an_env_var(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '424'") == "424"
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition "
                               "WHERE env_override IS NOT NULL") == 0


@pytest.mark.asyncio
async def test_the_edited_rows_equal_the_compiled_defaults(conn):
    retired = json.load(open(RETIRED, encoding="utf-8"))
    rows = {r["key"]: r for r in await conn.fetch(
        "SELECT key, description, guide::text AS guide, shared.setting_guide_complete(guide) AS guided "
        "FROM shared.setting_definition WHERE key = ANY($1::text[]) OR app = 'tripplanner'", list(retired))}
    assert set(retired) <= set(rows)
    for key, r in rows.items():
        assert r["description"] == settings.definition(key).description, key
        assert json.loads(r["guide"]) == settings.guide(key) and r["guided"] is True, key
        assert "emergency" not in r["description"] and "env var" not in r["description"], key


def _edited_later() -> set:
    """Keys whose description / guide a later file changes (a settings-guide or settings-edit block after 424, e.g. CIS
    468): a replay of 424 alone puts its own text back on those, which the later file then owns."""
    from scripts.settings import gen_compiled_defaults as gen
    keys: set = set()
    for path in sorted(gen.MIGRATIONS.glob("*.sql")):
        m = gen.FILE_RE.match(path.name)
        if not m or int(m.group(1)) <= 424:
            continue
        text = path.read_text(encoding="utf-8")
        for block in gen.GUIDE_RE.findall(text) + gen.EDIT_RE.findall(text):
            keys |= {r["key"] for r in json.loads(block)}
    return keys


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    body = open(M428, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    sql = ("SELECT key, env_override, description, guide::text AS g FROM shared.setting_definition "
           "WHERE NOT key = ANY($1::text[]) ORDER BY key")
    later = sorted(_edited_later())
    before = await conn.fetch(sql, later)
    await conn.execute(body)
    after = await conn.fetch(sql, later)
    assert [tuple(r) for r in before] == [tuple(r) for r in after]
