"""AA-1077 (S280) — CIS 418: the blog lengthening repair switch cis.t9.blog_length_extend in shared.setting_definition
(conftest replays every migration). Registered; the row equals the compiled default the reader serves (off = main's
path); off | on only; platform + tenant scope (a tenant value runs the audit-tenant trial); a change needs its
evidence; the key carries its label and a complete guide; a re-run changes nothing. Runs in a transaction that is
rolled back; invented values only."""
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
M418 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "418_t9_blog_length_extend_setting.sql")
KEY = sk.T9_BLOG_LENGTH_EXTEND
INS = ("INSERT INTO shared.setting_value (key, value, version, proposed_by, reason, evidence_ref) "
       "VALUES ($1, $2::jsonb, 1, 'admin:zz-a', 'zz test: a reason of at least twenty characters', $3) RETURNING id")


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
async def test_418_registered_and_the_row_is_the_compiled_default(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '418'") == "418"
    row = await conn.fetchrow(
        "SELECT default_value::text AS d, allowed_values::text AS allowed, scope_kind, impact, approval, "
        "evidence_required, env_override, enforced, label, shared.setting_guide_complete(guide) AS guided "
        "FROM shared.setting_definition WHERE key = $1", KEY)
    assert row is not None
    d = settings.definition(KEY)
    assert json.loads(row["d"]) == "off" == d.default_value
    assert json.loads(row["allowed"]) == ["off", "on"]
    assert row["scope_kind"] == "platform+tenant" == d.scope_kind
    assert (row["impact"], row["approval"], row["evidence_required"]) == ("high", "maker_checker", True)
    assert row["env_override"] == "T9_LENGTH_EXTEND" and row["enforced"] is True
    assert row["guided"] is True and row["label"] == d.label


@pytest.mark.asyncio
async def test_a_change_needs_its_evidence_and_must_be_off_or_on(conn):
    await _refused(conn, KEY, '"on"', None)                      # no evidence reference
    await _refused(conn, KEY, '"maybe"', "replay:zz-run-1")      # not off / on
    assert await conn.fetchval(INS, KEY, '"on"', "trial:zz-run-2") is not None


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    body = open(M418, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    await conn.execute(body)
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before
