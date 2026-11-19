"""AA-1111 (S285, slice S2) — CIS 459: the administrator MFA switch in shared.setting_definition (conftest replays every
migration). Registered; the row equals the compiled default the reader serves (off); a bool only; platform scope,
maker-checker, high impact, no env override (an env var must never turn MFA off), enforced; a label and a complete
guide; a re-run changes nothing. Runs in a transaction that is rolled back; invented values only."""
import json
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.core import staff_mfa_settings as sm  # noqa: E402
from shared import settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M459 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "459_aa1111_admin_mfa_setting.sql")
INS = ("INSERT INTO shared.setting_value (key, value, version, proposed_by, reason, evidence_ref) "
       "VALUES ($1, $2::jsonb, 1, 'admin:zz-a', 'zz test: a reason of at least twenty characters', NULL) RETURNING id")


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
async def test_459_registered_and_the_row_is_the_compiled_default(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '459'") == "459"
    assert sm.ALL == (sm.ADMIN_MFA_REQUIRED,)
    row = await conn.fetchrow(
        "SELECT default_value::text AS d, value_type, area, scope_kind, impact, approval, evidence_required, "
        "env_override, enforced, label, shared.setting_guide_complete(guide) AS guided "
        "FROM shared.setting_definition WHERE key = $1",
        sm.ADMIN_MFA_REQUIRED)
    assert row is not None
    d = settings.definition(sm.ADMIN_MFA_REQUIRED)
    assert json.loads(row["d"]) is False and d.default_value is False
    assert row["value_type"] == "bool"
    assert (row["area"], row["scope_kind"], row["impact"], row["approval"]) == (
        "platform", "platform", "high", "maker_checker")
    assert row["evidence_required"] is False and row["env_override"] is None and row["enforced"] is True
    assert row["guided"] is True and row["label"] == d.label


@pytest.mark.asyncio
async def test_a_value_must_be_a_bool(conn):
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        async with conn.transaction():
            await conn.fetchval(INS, sm.ADMIN_MFA_REQUIRED, '"yes"')
    assert await conn.fetchval(INS, sm.ADMIN_MFA_REQUIRED, "true") is not None


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    body = open(M459, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    await conn.execute(body)
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before
