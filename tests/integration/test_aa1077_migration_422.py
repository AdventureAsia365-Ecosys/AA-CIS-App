"""AA-1077 (S281) — CIS 422: what the blog lengthening repair may add (cis.t9.blog_extend_material) and how many
sections it asks for after a repair round shrank the blog under the floor (cis.t9.blog_extend_shrink_sections), in
shared.setting_definition (conftest replays every migration). Registered; each row equals the compiled default the
reader serves (source_facts = today's repair; 3 sections); allowed values and bounds; platform + tenant scope (a
tenant value runs the audit-tenant trial); a change needs its evidence; the keys carry a label and a complete guide; a
re-run changes nothing. Runs in a transaction that is rolled back; invented values only."""
import json
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.quality import settings_keys as sk  # noqa: E402
from shared import settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M422 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                    "422_t9_blog_extend_material_settings.sql")
MATERIAL = sk.T9_BLOG_EXTEND_MATERIAL
SHRINK = sk.T9_BLOG_EXTEND_SHRINK_SECTIONS
INS = ("INSERT INTO shared.setting_value (key, value, version, proposed_by, reason, evidence_ref) "
       "VALUES ($1, $2::jsonb, 1, 'admin:zz-a', 'zz test: a reason of at least twenty characters', $3) RETURNING id")
ROW = ("SELECT default_value::text AS d, allowed_values::text AS allowed, min_value AS lo, max_value AS hi, "
       "value_type, scope_kind, impact, approval, evidence_required, env_override, enforced, label, "
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


async def _refused(c, *args):
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        async with c.transaction():
            await c.fetchval(INS, *args)


@pytest.mark.asyncio
async def test_422_registered_and_the_rows_are_the_compiled_defaults(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '422'") == "422"
    m = await conn.fetchrow(ROW, MATERIAL)
    d = settings.definition(MATERIAL)
    assert json.loads(m["d"]) == "source_facts" == d.default_value
    assert json.loads(m["allowed"]) == ["source_facts", "with_background"] == list(d.allowed_values)
    assert (m["value_type"], m["scope_kind"], m["impact"]) == ("enum", "platform+tenant", "high")
    s = await conn.fetchrow(ROW, SHRINK)
    d2 = settings.definition(SHRINK)
    assert json.loads(s["d"]) == 3 == d2.default_value
    assert (s["value_type"], int(s["lo"]), int(s["hi"]), s["scope_kind"]) == ("int", 0, 6, "platform+tenant")
    for row, defn in ((m, d), (s, d2)):
        assert (row["approval"], row["evidence_required"], row["env_override"]) == ("maker_checker", True, None)
        assert row["enforced"] is True and row["guided"] is True and row["label"] == defn.label


@pytest.mark.asyncio
async def test_a_change_needs_its_evidence_and_a_valid_value(conn):
    await _refused(conn, MATERIAL, '"with_background"', None)        # no evidence reference
    await _refused(conn, MATERIAL, '"anything"', "replay:zz-run-1")   # not an allowed value
    await _refused(conn, SHRINK, "7", "replay:zz-run-2")              # over the bound
    await _refused(conn, SHRINK, "-1", "replay:zz-run-3")             # under the bound
    assert await conn.fetchval(INS, MATERIAL, '"with_background"', "trial:zz-run-4") is not None
    assert await conn.fetchval(INS, SHRINK, "0", "trial:zz-run-5") is not None


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    body = open(M422, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    await conn.execute(body)
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before
