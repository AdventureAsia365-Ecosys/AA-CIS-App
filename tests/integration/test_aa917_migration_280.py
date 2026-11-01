"""AA-917 (WM-09) — CIS 280 (shared.writing_rule) on real Postgres.

conftest replays 280 like every file. Each test runs in one transaction that is rolled back.

  * the file applied and a re-run is a no-op; RLS is on and forced; exactly the three policies; the grants it leaves
    are exact (cis_app SELECT / INSERT / UPDATE / DELETE, nothing for PUBLIC, aa_app_user or catalog_owner, no column
    grant), also after a stray grant; the AA-859 invariant v3 finds nothing on it; the stage row and the budget row;
  * guard 1 refuses a re-run over a table with other columns;
  * the tenant pool (aa_app_user) has no privilege at all; cis_app and cis_owner see every row; a cis_app read that
    filters tenant_id (as the code does) never resolves another tenant's rule (ADR 0010 decision 10);
  * the CHECKs: scope / tenant_id, kinds, writers, statuses, approval needs an approver, a replacement only on a
    lexicon rule, 25 words per rule (a preference_summary 5 sentences, tenant only), a platform rule from tenant
    evidence needs 3 tenants;
  * the token caps (trigger): approved + active rules <= 400 tokens per writer (platform), <= 250 per tenant; candidates
    do not count; another writer or another tenant has its own budget;
  * one open rule per cluster (re-mining is idempotent), a rejected one does not block a new candidate.
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
TABLE = "shared.writing_rule"
WORDS_25 = " ".join(["abcdef"] * 25)          # 174 chars -> 44 tokens
INSERT = (f"INSERT INTO {TABLE} (kind, scope, tenant_id, writer, cluster_key, rule_text) "
          "VALUES ($1, $2, $3, $4, $5, $6) RETURNING rule_id")


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


async def _rule(conn, *, scope="tenant", tenant=None, writer="t9_blog", kind="writer_instruction", key=None,
                text="Cut stock openers."):
    return await conn.fetchval(INSERT, kind, scope, tenant, writer, key or f"zz917-{uuid.uuid4().hex[:8]}", text)


async def _approve(conn, rule_id):
    await conn.execute(f"UPDATE {TABLE} SET status = 'approved', approved_by = 'zz917-admin', approved_at = now() "
                       "WHERE rule_id = $1", rule_id)


async def _as(conn, role, guc=None):
    # other modules re-create the roles (dropping their grants): re-apply 280 for its grants, give the schema USAGE
    await conn.execute(migration_body(280))
    await conn.execute(f"GRANT USAGE ON SCHEMA shared TO {role}")
    await conn.execute(f"SET LOCAL ROLE {role}")
    if guc is not None:
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", guc)


async def _refused(conn, exc, sql, *args, match=None):
    with pytest.raises(exc, match=match):
        async with conn.transaction():
            await conn.execute(sql, *args)


# ── the file ────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_280_applied_and_rerun_is_a_noop(conn):
    assert await conn.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = '280'"), \
        "conftest swallowed an error in 280 (see its log)"
    await _rule(conn, tenant=uuid.uuid4())
    before = await conn.fetchval(f"SELECT count(*) FROM {TABLE}")
    await conn.execute(migration_body(280))
    assert await conn.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = '280'") == 1
    assert await conn.fetchval(f"SELECT count(*) FROM {TABLE}") == before     # the probe was rolled back


@pytest.mark.asyncio
async def test_280_rls_forced_with_exactly_three_policies(conn):
    await conn.execute(migration_body(280))
    assert tuple(await conn.fetchrow(f"SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                                     f"WHERE oid = '{TABLE}'::regclass")) == (True, True)
    rows = await conn.fetch("SELECT policyname, permissive, roles::text[] AS roles, cmd FROM pg_policies "
                            "WHERE schemaname = 'shared' AND tablename = 'writing_rule' ORDER BY policyname")
    assert [(r["policyname"], r["permissive"], r["roles"], r["cmd"]) for r in rows] == [
        ("tenant_isolation", "PERMISSIVE", ["aa_app_user"], "ALL"),
        ("writing_rule_cis_app_all", "PERMISSIVE", ["cis_app"], "ALL"),
        ("writing_rule_cis_owner_all", "PERMISSIVE", ["cis_owner"], "ALL"),
    ]


@pytest.mark.asyncio
async def test_280_privileges_are_exact(conn):
    await conn.execute(f"GRANT SELECT ON {TABLE} TO catalog_owner, PUBLIC")
    await conn.execute(f"GRANT SELECT, INSERT ON {TABLE} TO aa_app_user")
    await conn.execute(f"GRANT UPDATE (status) ON {TABLE} TO aa_app_user")
    await conn.execute(f"GRANT TRUNCATE, REFERENCES ON {TABLE} TO cis_app")
    await conn.execute("GRANT EXECUTE ON FUNCTION shared.writing_rule_token_cap() TO PUBLIC")
    await conn.execute(migration_body(280))
    rows = await conn.fetch(f"""
        SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END AS r, a.privilege_type AS p
          FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a
         WHERE c.oid = '{TABLE}'::regclass
           AND (a.grantee = 0 OR a.grantee IN ('cis_app'::regrole, 'aa_app_user'::regrole,
                                               'catalog_owner'::regrole))""")
    assert sorted((r["r"], r["p"]) for r in rows) == sorted([
        ("cis_app", "SELECT"), ("cis_app", "INSERT"), ("cis_app", "UPDATE"), ("cis_app", "DELETE"),
    ])
    assert not await conn.fetchval(f"SELECT bool_or(attacl IS NOT NULL) FROM pg_attribute "
                                   f"WHERE attrelid = '{TABLE}'::regclass")
    assert not await conn.fetchval("SELECT has_function_privilege('public', 'shared.writing_rule_token_cap()', "
                                   "'EXECUTE')")
    bad = [r for r in await conn.fetch(NO_PRIVILEGE_WITHOUT_RLS_SQL_V3) if "writing_rule" in r["rel"]]
    assert bad == []


@pytest.mark.asyncio
async def test_280_stage_and_budget_rows(conn):
    row = await conn.fetchrow("SELECT role, provider, model_id, account_route, fallback_model_ids, shadow_model_id, "
                              "shadow_sample_pct, is_active FROM shared.llm_role_config "
                              "WHERE stage = 'writing_memory_mine'")
    # 280's route, as 327 left it (OpenAI API last) and 333 (AA-1026) gave it an acc2 pick before that hop
    assert dict(row) == {"role": "judge", "provider": "openai", "model_id": "gpt-5.6-luna", "account_route": "acc3",
                         "fallback_model_ids": ["gpt-6-luna", "nova-lite", "gpt-6-luna-openai"], "shadow_model_id": None,
                         "shadow_sample_pct": 0, "is_active": True}
    budget = await conn.fetchrow("SELECT per_run_usd, per_day_usd, hard_stop FROM shared.spend_budget "
                                 "WHERE provider = 'bedrock' AND scope = 'job:writing_memory_mine'")
    assert (float(budget["per_run_usd"]), float(budget["per_day_usd"]), budget["hard_stop"]) == (1.0, 2.0, True)
    # an admin change survives a re-run (ON CONFLICT DO NOTHING)
    await conn.execute("UPDATE shared.llm_role_config SET model_id = 'gpt-6-luna' WHERE stage = 'writing_memory_mine'")
    await conn.execute(migration_body(280))
    assert await conn.fetchval("SELECT model_id FROM shared.llm_role_config WHERE stage = 'writing_memory_mine'") \
        == "gpt-6-luna"


@pytest.mark.asyncio
async def test_guard_refuses_a_rerun_over_other_columns(conn):
    await conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN zz917 text")
    await _refused(conn, asyncpg.RaiseError, migration_body(280), match="exists with other columns")


# ── isolation ───────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tenant_pool_has_no_privilege(conn):
    a = uuid.uuid4()
    await _rule(conn, tenant=a)
    await _as(conn, "aa_app_user", str(a))
    for sql in (f"SELECT 1 FROM {TABLE}", f"UPDATE {TABLE} SET status = 'rejected'", f"DELETE FROM {TABLE}"):
        await _refused(conn, asyncpg.InsufficientPrivilegeError, sql, match="permission denied")


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["cis_app", "cis_owner"])
async def test_runtime_and_owner_see_every_row_and_code_filters_tenant(conn, role):
    a, b = uuid.uuid4(), uuid.uuid4()
    ra, rb = await _rule(conn, tenant=a), await _rule(conn, tenant=b)
    rp = await _rule(conn, scope="platform", writer="a1")
    if role == "cis_owner":                       # the Dev shape (245): cis_owner owns it, and FORCE filters the owner
        await conn.execute(f"ALTER TABLE {TABLE} OWNER TO cis_owner")
    await _as(conn, role, str(uuid.uuid4()))                                  # a forged GUC changes nothing for them
    seen = {r["rule_id"] for r in await conn.fetch(f"SELECT rule_id FROM {TABLE}")}
    assert {ra, rb, rp} <= seen
    # the code's read (services/writing_memory/rules.py): tenant_id = $1 explicitly, never another tenant's rule
    own = {r["rule_id"] for r in await conn.fetch(
        f"SELECT rule_id FROM {TABLE} WHERE scope = 'tenant' AND tenant_id = $1", a)}
    assert own == {ra}


# ── the CHECKs ──────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_platform_rule_with_tenant_and_tenant_rule_without_are_refused(conn):
    await _refused(conn, asyncpg.CheckViolationError, INSERT, "writer_instruction", "platform", uuid.uuid4(),
                   "a1", "zz917-x", "Cut it.")
    await _refused(conn, asyncpg.CheckViolationError, INSERT, "writer_instruction", "tenant", None, "t2", "zz917-x",
                   "Cut it.")


@pytest.mark.asyncio
@pytest.mark.parametrize("column,value", [
    ("kind", "lesson"), ("writer", "s1"), ("status", "live"), ("reason_code", "bogus"), ("lint", '[1]'),
])
async def test_checks_refuse_unknown_values(conn, column, value):
    rid = await _rule(conn, tenant=uuid.uuid4())
    cast = "::jsonb" if column == "lint" else ""
    await _refused(conn, asyncpg.CheckViolationError, f"UPDATE {TABLE} SET {column} = $1{cast} WHERE rule_id = $2",
                   value, rid)


@pytest.mark.asyncio
async def test_word_and_sentence_caps(conn):
    t = uuid.uuid4()
    await _rule(conn, tenant=t, text=WORDS_25)                                # 25 words: fine
    await _refused(conn, asyncpg.CheckViolationError, INSERT, "writer_instruction", "tenant", t, "t2", "zz917-26",
                   WORDS_25 + " more")
    five = "One. Two words. Three here! Four? Five."
    await _rule(conn, tenant=t, kind="preference_summary", text=five)
    await _refused(conn, asyncpg.CheckViolationError, INSERT, "preference_summary", "tenant", t, "t2", "zz917-6s",
                   five + " Six.")
    await _refused(conn, asyncpg.CheckViolationError, INSERT, "preference_summary", "platform", None, "t2",
                   "zz917-ps", "One.")


@pytest.mark.asyncio
async def test_replacement_approval_and_tenant_support(conn):
    rid = await _rule(conn, scope="platform", writer="a1")
    await _refused(conn, asyncpg.CheckViolationError,
                   f"UPDATE {TABLE} SET replacement = 'x' WHERE rule_id = $1", rid)        # not a lexicon rule
    lex = await _rule(conn, scope="platform", writer="a1", kind="lexicon", text="nestled")
    await conn.execute(f"UPDATE {TABLE} SET replacement = 'set' WHERE rule_id = $1", lex)
    await _refused(conn, asyncpg.CheckViolationError, f"UPDATE {TABLE} SET status = 'approved' WHERE rule_id = $1",
                   rid)                                                                       # no approver
    await _refused(conn, asyncpg.CheckViolationError, f"UPDATE {TABLE} SET status = 'active', approved_at = now() "
                   "WHERE rule_id = $1", rid)
    await _refused(conn, asyncpg.CheckViolationError, f"UPDATE {TABLE} SET support_tenants = 2 WHERE rule_id = $1",
                   rid)                                                                       # D9: >= 3 tenants
    await conn.execute(f"UPDATE {TABLE} SET support_tenants = 3 WHERE rule_id = $1", rid)
    t = await _rule(conn, tenant=uuid.uuid4())
    await _refused(conn, asyncpg.CheckViolationError, f"UPDATE {TABLE} SET support_tenants = 2 WHERE rule_id = $1", t)


# ── the token caps ──────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_platform_cap_400_tokens_per_writer(conn):
    ids = [await _rule(conn, scope="platform", writer="t9_social", text=WORDS_25) for _ in range(10)]
    assert await conn.fetchval(f"SELECT token_estimate FROM {TABLE} WHERE rule_id = $1", ids[0]) == 44
    for rid in ids[:9]:                                                       # 9 x 44 = 396
        await _approve(conn, rid)
    await _refused(conn, asyncpg.CheckViolationError, f"UPDATE {TABLE} SET status = 'approved', approved_by = 'x', "
                   "approved_at = now() WHERE rule_id = $1", ids[9], match="over the cap of 400")
    # another writer has its own budget; candidates never count
    other = await _rule(conn, scope="platform", writer="t9_blog", text=WORDS_25)
    await _approve(conn, other)
    await _rule(conn, scope="platform", writer="t9_social", text=WORDS_25)


@pytest.mark.asyncio
async def test_tenant_cap_250_tokens_per_tenant_and_writer(conn):
    a, b = uuid.uuid4(), uuid.uuid4()
    ids = [await _rule(conn, tenant=a, writer="t2", text=WORDS_25) for _ in range(6)]
    for rid in ids[:5]:                                                       # 5 x 44 = 220
        await _approve(conn, rid)
    await _refused(conn, asyncpg.CheckViolationError, f"UPDATE {TABLE} SET status = 'active', approved_by = 'x', "
                   "approved_at = now() WHERE rule_id = $1", ids[5], match="over the cap of 250")
    # tenant b has its own budget
    await _approve(conn, await _rule(conn, tenant=b, writer="t2", text=WORDS_25))
    # a rejected rule frees its budget
    await conn.execute(f"UPDATE {TABLE} SET status = 'rejected', rejected_by = 'x' WHERE rule_id = $1", ids[0])
    await _approve(conn, ids[5])


# ── one open rule per cluster ───────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_one_open_rule_per_cluster(conn):
    t = uuid.uuid4()
    rid = await _rule(conn, tenant=t, key="zz917-cluster")
    await _refused(conn, asyncpg.UniqueViolationError, INSERT, "writer_instruction", "tenant", t, "t9_blog",
                   "zz917-cluster", "Another.")
    await _rule(conn, tenant=uuid.uuid4(), key="zz917-cluster")               # another tenant: its own cluster
    await _rule(conn, scope="platform", key="zz917-cluster")                  # the platform: its own cluster
    await conn.execute(f"UPDATE {TABLE} SET status = 'rejected', rejected_by = 'x' WHERE rule_id = $1", rid)
    await _rule(conn, tenant=t, key="zz917-cluster")                          # rejected: a new candidate may come
