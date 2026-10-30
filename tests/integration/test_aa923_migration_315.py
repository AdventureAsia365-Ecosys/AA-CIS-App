"""AA-923 WM-32 — CIS 315 (acp_shared.content_label) on real Postgres (conftest replays it like every file).

Each test runs in one transaction that is rolled back.
  * the file applied: RLS on and forced, exactly the three policies, cis_app's grant, no tenant-pool privilege;
  * the CHECKs (subject type, label, defects object, minutes 0..600, reason size, sha256 shape) and the key: one row per
    subject and labeller, a second labeller is a second row;
  * the T9 table content_piece_label (CIS 231) is unchanged by it;
  * as cis_app the upsert of the label routes runs (the X1 parity policy), as aa_app_user nothing is readable.
"""
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "315_content_label.sql")
SHA = "c" * 64
UPSERT = """
    INSERT INTO acp_shared.content_label AS l (subject_type, subject_id, tenant_id, labelled_by, label, defects,
                                               minutes_to_fix, reason, content_sha256, labelled_at)
    VALUES ($1, $2::uuid, $3::uuid, $4, $5, $6::jsonb, $7, $8, $9, now())
    ON CONFLICT (subject_type, subject_id, labelled_by) DO UPDATE SET
        tenant_id = EXCLUDED.tenant_id, label = EXCLUDED.label, defects = EXCLUDED.defects,
        minutes_to_fix = EXCLUDED.minutes_to_fix, reason = EXCLUDED.reason,
        content_sha256 = EXCLUDED.content_sha256, labelled_at = now()
"""


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


async def _refused(conn, exc, sql, *args):
    with pytest.raises(exc):
        async with conn.transaction():
            await conn.execute(sql, *args)


@pytest.mark.asyncio
async def test_file_applied_with_rls_forced_and_three_policies(conn):
    assert await conn.fetchval("SELECT version FROM shared.schema_versions WHERE version = '315'") == "315"
    rls = await conn.fetchrow("SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                              "WHERE oid = 'acp_shared.content_label'::regclass")
    assert tuple(rls) == (True, True)
    policies = {r["policyname"]: r["roles"] for r in await conn.fetch(
        "SELECT policyname, roles FROM pg_policies WHERE schemaname = 'acp_shared' AND tablename = 'content_label'")}
    assert policies == {"content_label_cis_app_all": ["cis_app"], "content_label_cis_owner_all": ["cis_owner"],
                        "tenant_isolation": ["aa_app_user"]}
    # cis_app's grant is in the file (other modules' X1 teardowns revoke cis_app grants in the shared test DB, so the
    # live privilege depends on the module order); the tenant pool never gets one.
    with open(MIGRATION, encoding="utf-8") as f:
        assert "GRANT SELECT, INSERT, UPDATE, DELETE ON acp_shared.content_label TO cis_app;" in f.read()
    for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        assert not await conn.fetchval("SELECT has_table_privilege('aa_app_user', 'acp_shared.content_label', $1)",
                                       priv)
    # the T9 table of CIS 231 keeps its own key
    assert await conn.fetchval(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'content_piece_label_pkey'") \
        == "PRIMARY KEY (piece_id, labelled_by)"


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [
    ("t9_piece", "light", "{}", 5, None, SHA),           # subject type
    ("t2_version", "great", "{}", 5, None, SHA),         # label
    ("t2_version", "light", "[]", 5, None, SHA),         # defects not an object
    ("a1_content", "light", "{}", 601, None, SHA),       # minutes
    ("a1_content", "light", "{}", 5, "x" * 4001, SHA),   # reason size
    ("a1_content", "light", "{}", 5, None, "XYZ"),       # sha shape
])
async def test_checks_refuse_bad_rows(conn, args):
    subject_type, label, defects, minutes, reason, sha = args
    await _refused(conn, asyncpg.CheckViolationError, UPSERT, subject_type, str(uuid.uuid4()), str(uuid.uuid4()),
                   "admin:u1", label, defects, minutes, reason, sha)


@pytest.mark.asyncio
async def test_one_row_per_subject_and_labeller_as_cis_app(conn):
    await conn.execute("GRANT USAGE ON SCHEMA acp_shared TO cis_app")
    await conn.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON acp_shared.content_label TO cis_app")
    await conn.execute("SET LOCAL ROLE cis_app")
    subject, tenant = str(uuid.uuid4()), str(uuid.uuid4())
    await conn.execute(UPSERT, "t2_version", subject, tenant, "admin:u1", "heavy", '{"fact": "heavy"}', 30, "r1", SHA)
    await conn.execute(UPSERT, "t2_version", subject, tenant, "admin:u1", "light", '{"fact": "light"}', 5, "r2", SHA)
    await conn.execute(UPSERT, "t2_version", subject, tenant, "admin:u2", "as_is", "{}", None, None, SHA)
    await conn.execute(UPSERT, "a1_content", subject, tenant, "admin:u1", "reject", "{}", None, None, SHA)
    rows = await conn.fetch("SELECT subject_type, labelled_by, label, minutes_to_fix FROM acp_shared.content_label "
                            "WHERE subject_id = $1::uuid ORDER BY subject_type, labelled_by", subject)
    assert [tuple(r) for r in rows] == [("a1_content", "admin:u1", "reject", None),
                                        ("t2_version", "admin:u1", "light", 5),
                                        ("t2_version", "admin:u2", "as_is", None)]
    await conn.execute("RESET ROLE")
    await conn.execute("SET LOCAL ROLE aa_app_user")
    await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant)
    await _refused(conn, asyncpg.InsufficientPrivilegeError, "SELECT count(*) FROM acp_shared.content_label")
