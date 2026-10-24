"""AA-916 (WM-03) — CIS 277 (shared.writing_signal) on real Postgres.

conftest replays 277 like every file. Each test runs in one transaction that is rolled back.

  * the file applied and a re-run is a no-op; RLS is on and forced; exactly the three policies; the grants it leaves
    are exact (cis_app SELECT / INSERT / UPDATE / DELETE, aa_app_user SELECT / INSERT, nothing for PUBLIC or
    catalog_owner, no column grant), also after a stray grant; the AA-859 invariant v3 finds nothing on it;
  * guard 1 refuses a re-run over a table with other columns;
  * the tenant pool (aa_app_user, app.tenant_id): its own tenant rows only, never a platform row or another tenant's,
    nothing under an empty or forged GUC; it may insert only for its own tenant and only scope tenant; no UPDATE,
    DELETE or TRUNCATE (ADR 0010 decision 10);
  * cis_app (parity policy) and cis_owner (FORCE: its own parity policy, X1 S21) see every row;
  * the CHECKs: scope / tenant_id, writer, action, actor_type, reason codes, fact_status default, exemplar_pref,
    locator shape, hash required with the text and lower-case hex.
"""
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from _rls_fixture_helpers import NO_PRIVILEGE_WITHOUT_RLS_SQL_V3, migration_body  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TABLE = "shared.writing_signal"
H = "a" * 64
REASON_CODES = ("fact_not_in_source", "optional_as_included", "wrong_place_or_time", "voice_off_brand", "ai_phrasing",
                "format_channel", "repair_residue", "style_only")
INSERT = (f"INSERT INTO {TABLE} (writer, channel, scope, tenant_id, field, before_text, after_text, before_sha256, "
          "after_sha256, action, actor_type, source_table, source_id) "
          "VALUES ($1, 'blog', $2, $3, 'content_text', 'before', 'after', $4, $4, $5, 'tenant', 'content_piece', "
          "'zz916') RETURNING signal_id")


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


async def _signal(conn, *, scope="tenant", tenant=None, writer="t9_blog", action="tenant_edit"):
    return await conn.fetchval(INSERT, writer, scope, tenant, H, action)


async def _seed(conn):
    """Two tenants with one signal each, plus one A1 platform signal (inserted by the superuser)."""
    a, b = uuid.uuid4(), uuid.uuid4()
    await _signal(conn, tenant=a)
    await _signal(conn, tenant=b)
    await _signal(conn, scope="platform", writer="a1", action="field_edit")
    return a, b


async def _as(conn, role, guc=None):
    # other modules re-create the roles (dropping their grants): re-apply 277 for its grants, give the schema USAGE
    await conn.execute(migration_body(277))
    await conn.execute(f"GRANT USAGE ON SCHEMA shared TO {role}")
    await conn.execute(f"SET LOCAL ROLE {role}")
    if guc is not None:
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", guc)


async def _refused(conn, exc, sql, *args, match=None):
    with pytest.raises(exc, match=match):
        async with conn.transaction():
            await conn.execute(sql, *args)


async def _visible(conn):
    return {r["t"] for r in await conn.fetch(f"SELECT DISTINCT tenant_id::text AS t FROM {TABLE}")}  # no WHERE


# ── the file ────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_277_applied_and_rerun_is_a_noop(conn):
    assert await conn.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = '277'"), \
        "conftest swallowed an error in 277 (see its log)"
    await _signal(conn, tenant=uuid.uuid4())
    before = await conn.fetchval(f"SELECT count(*) FROM {TABLE}")
    await conn.execute(migration_body(277))
    assert await conn.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = '277'") == 1
    assert await conn.fetchval(f"SELECT count(*) FROM {TABLE}") == before     # the probe was rolled back


@pytest.mark.asyncio
async def test_277_rls_forced_with_exactly_three_policies(conn):
    await conn.execute(migration_body(277))
    assert tuple(await conn.fetchrow(f"SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                                     f"WHERE oid = '{TABLE}'::regclass")) == (True, True)
    rows = await conn.fetch("SELECT policyname, permissive, roles::text[] AS roles, cmd FROM pg_policies "
                            "WHERE schemaname = 'shared' AND tablename = 'writing_signal' ORDER BY policyname")
    assert [(r["policyname"], r["permissive"], r["roles"], r["cmd"]) for r in rows] == [
        ("tenant_isolation", "PERMISSIVE", ["aa_app_user"], "ALL"),
        ("writing_signal_cis_app_all", "PERMISSIVE", ["cis_app"], "ALL"),
        ("writing_signal_cis_owner_all", "PERMISSIVE", ["cis_owner"], "ALL"),
    ]


@pytest.mark.asyncio
async def test_277_privileges_are_exact(conn):
    await conn.execute(f"GRANT SELECT ON {TABLE} TO catalog_owner, PUBLIC")
    await conn.execute(f"GRANT UPDATE, DELETE, TRUNCATE ON {TABLE} TO aa_app_user")
    await conn.execute(f"GRANT UPDATE (exemplar_pref) ON {TABLE} TO aa_app_user")
    await conn.execute(f"GRANT TRUNCATE, REFERENCES ON {TABLE} TO cis_app")
    await conn.execute(migration_body(277))
    rows = await conn.fetch(f"""
        SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END AS r, a.privilege_type AS p
          FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a
         WHERE c.oid = '{TABLE}'::regclass
           AND (a.grantee = 0 OR a.grantee IN ('cis_app'::regrole, 'aa_app_user'::regrole,
                                               'catalog_owner'::regrole))""")
    assert sorted((r["r"], r["p"]) for r in rows) == sorted([
        ("aa_app_user", "SELECT"), ("aa_app_user", "INSERT"),
        ("cis_app", "SELECT"), ("cis_app", "INSERT"), ("cis_app", "UPDATE"), ("cis_app", "DELETE"),
    ])
    assert not await conn.fetchval(f"SELECT bool_or(attacl IS NOT NULL) FROM pg_attribute "
                                   f"WHERE attrelid = '{TABLE}'::regclass")
    bad = [r for r in await conn.fetch(NO_PRIVILEGE_WITHOUT_RLS_SQL_V3) if "writing_signal" in r["rel"]]
    assert bad == []


@pytest.mark.asyncio
async def test_guard_refuses_a_rerun_over_other_columns(conn):
    await conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN zz916 text")
    await _refused(conn, asyncpg.RaiseError, migration_body(277), match="exists with other columns")


# ── the tenant pool ─────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tenant_pool_reads_its_own_tenant_rows_only(conn):
    a, b = await _seed(conn)
    await _as(conn, "aa_app_user", str(a))
    assert await _visible(conn) == {str(a)}                                   # not b, not the platform row
    for guc, expected in ((str(b), {str(b)}), (str(uuid.uuid4()), set()), ("", set())):
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", guc)
        assert await _visible(conn) == expected, guc


@pytest.mark.asyncio
async def test_tenant_pool_inserts_for_its_own_tenant_only(conn):
    a, b = uuid.uuid4(), uuid.uuid4()
    await _as(conn, "aa_app_user", str(a))
    sid = await _signal(conn, tenant=a)                                       # INSERT … RETURNING (the T9 edit path)
    assert sid is not None
    rls = "row-level security"
    await _refused(conn, asyncpg.InsufficientPrivilegeError, INSERT, "t9_blog", "tenant", b, H, "tenant_edit",
                   match=rls)
    await _refused(conn, asyncpg.InsufficientPrivilegeError, INSERT, "a1", "platform", None, H, "field_edit",
                   match=rls)
    await conn.execute("SELECT set_config('app.tenant_id', '', true)")
    await _refused(conn, asyncpg.InsufficientPrivilegeError, INSERT, "t9_blog", "tenant", a, H, "tenant_edit",
                   match=rls)


@pytest.mark.asyncio
async def test_tenant_pool_cannot_update_delete_or_truncate(conn):
    a, _ = await _seed(conn)
    await _as(conn, "aa_app_user", str(a))
    for sql in (f"UPDATE {TABLE} SET exemplar_pref = 'use'", f"DELETE FROM {TABLE}", f"TRUNCATE {TABLE}"):
        await _refused(conn, asyncpg.InsufficientPrivilegeError, sql, match="permission denied")


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["cis_app", "cis_owner"])
async def test_runtime_and_owner_see_every_row(conn, role):
    a, b = await _seed(conn)
    if role == "cis_owner":                       # the Dev shape (245): cis_owner owns it, and FORCE filters the owner
        await conn.execute(f"ALTER TABLE {TABLE} OWNER TO cis_owner")
    await _as(conn, role, str(uuid.uuid4()))                                  # a forged GUC changes nothing for them
    assert {str(a), str(b), None} <= await _visible(conn)
    if role == "cis_app":                                                     # the WM-07 toggle and the erasure
        assert await conn.execute(f"UPDATE {TABLE} SET exemplar_pref = 'never' WHERE tenant_id = $1", a) \
            == "UPDATE 1"
        assert await conn.execute(f"DELETE FROM {TABLE} WHERE tenant_id = $1", b) == "DELETE 1"


# ── the CHECKs ──────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("column,value", [
    ("writer", "s1"), ("action", "approve"), ("actor_type", "robot"), ("reason_code", "bogus"),
    ("fact_status", "unknown"), ("exemplar_pref", "maybe"), ("locator", '[1, 2]'),
    ("before_sha256", "A" * 64), ("after_sha256", "abc"),
])
async def test_checks_refuse_unknown_values(conn, column, value):
    sid = await _signal(conn, tenant=uuid.uuid4())
    cast = "::jsonb" if column == "locator" else ""
    await _refused(conn, asyncpg.CheckViolationError, f"UPDATE {TABLE} SET {column} = $1{cast} WHERE signal_id = $2",
                   value, sid)


@pytest.mark.asyncio
async def test_checks_scope_and_hash_rules(conn):
    t = uuid.uuid4()
    await _refused(conn, asyncpg.CheckViolationError, INSERT, "a1", "platform", t, H, "field_edit")
    await _refused(conn, asyncpg.CheckViolationError, INSERT, "t9_blog", "tenant", None, H, "tenant_edit")
    await _refused(conn, asyncpg.CheckViolationError, INSERT, "t9_blog", "tenant", t, None, "tenant_edit")
    sid = await _signal(conn, tenant=t)
    row = await conn.fetchrow(f"SELECT fact_status, exemplar_pref, reason_code FROM {TABLE} WHERE signal_id = $1",
                              sid)
    assert tuple(row) == ("pending", None, None)
    for code in REASON_CODES:
        await conn.execute(f"UPDATE {TABLE} SET reason_code = $1 WHERE signal_id = $2", code, sid)
    # a reason-only signal (reject / dismiss) carries no text and no hash
    await conn.execute(f"INSERT INTO {TABLE} (writer, scope, reason_code, action, actor_type, source_table, source_id) "
                       "VALUES ('a1', 'platform', 'voice_off_brand', 'reject', 'admin', 'review_queue', 'zz916')")
