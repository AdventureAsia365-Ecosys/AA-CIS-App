"""AA-1168 follow-up (S286 settings-rule audit) — CIS 463 (the AI disclosure wording setting) and CIS 464
(acp_shared.publish_log.sent_message; conftest replays every migration). 463: registered, the row equals the compiled
default the reader serves, platform scope, maker_checker, enforced, a label and a complete guide, a re-run changes
nothing. 464: registered, the column is a nullable text with its comment, a publish row stores and reads it, a re-run
changes nothing. Runs in a transaction that is rolled back; invented values only."""
import json
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.acp_content_writing import ai_disclosure_setting as ads  # noqa: E402
from shared import settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MIGRATIONS = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")
M463 = os.path.join(MIGRATIONS, "463_aa1168_ai_disclosure_setting.sql")
M464 = os.path.join(MIGRATIONS, "464_aa1168_publish_log_sent_message.sql")
TENANT = uuid.UUID("0000aa11-6800-4000-8000-000000000463")


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
async def test_463_registered_and_the_row_is_the_compiled_default(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '463'") == "463"
    row = await conn.fetchrow(
        "SELECT default_value::text AS d, value_type, area, scope_kind, impact, approval, evidence_required, "
        "env_override, enforced, label, shared.setting_guide_complete(guide) AS guided "
        "FROM shared.setting_definition WHERE key = $1", ads.KEY)
    assert row is not None
    d = settings.definition(ads.KEY)
    assert json.loads(row["d"]) == d.default_value == ads.DEFAULT
    assert (row["value_type"], row["area"], row["scope_kind"], row["impact"], row["approval"]) == (
        "json", "content", "platform", "high", "maker_checker")
    assert row["evidence_required"] is False and row["env_override"] is None and row["enforced"] is True
    assert row["guided"] is True and row["label"] == d.label


@pytest.mark.asyncio
async def test_463_rerun_changes_nothing(conn):
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    await conn.execute(_body(M463))
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before


@pytest.mark.asyncio
async def test_464_registered_and_the_column_is_a_nullable_text(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '464'") == "464"
    col = await conn.fetchrow(
        "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_schema = 'acp_shared' AND table_name = 'publish_log' AND column_name = 'sent_message'")
    assert col is not None
    assert (col["data_type"], col["is_nullable"], col["column_default"]) == ("text", "YES", None)
    comment = await conn.fetchval(
        "SELECT col_description('acp_shared.publish_log'::regclass, a.attnum) AS ok FROM pg_attribute a "
        "WHERE a.attrelid = 'acp_shared.publish_log'::regclass AND a.attname = 'sent_message'")
    assert comment and "CIS 464" in comment


@pytest.mark.asyncio
async def test_464_a_publish_row_stores_the_sent_message(conn):
    await conn.execute("SET LOCAL session_replication_role = replica")   # no tenant / piece parents in this test
    pid = await conn.fetchval(
        "INSERT INTO acp_shared.publish_log (piece_id, tenant_id, channel, status, idempotency_key, requested_at, "
        "sent_message) VALUES ($1, $2, 'facebook', 'requested', 'zz-key-464', now(), $3) RETURNING publish_id",
        uuid.uuid4(), TENANT, "A quiet lake at dawn.\n\nWritten with help. #ZzTag")
    assert await conn.fetchval("SELECT sent_message AS ok FROM acp_shared.publish_log WHERE publish_id = $1",
                               pid) == "A quiet lake at dawn.\n\nWritten with help. #ZzTag"


@pytest.mark.asyncio
async def test_464_rerun_changes_nothing(conn):
    await conn.execute(_body(M464))
    n = await conn.fetchval(
        "SELECT count(*) AS n FROM information_schema.columns "
        "WHERE table_schema = 'acp_shared' AND table_name = 'publish_log' AND column_name = 'sent_message'")
    assert n == 1
