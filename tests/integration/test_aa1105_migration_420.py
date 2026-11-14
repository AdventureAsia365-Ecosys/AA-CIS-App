"""AA-1105 (S280) — CIS 420: schema learn (the AA Academy learner records) and the cis.learn.* settings (conftest
replays 420 with the other files). Registered; the nine tables of the design ERD with their keys; the CIS RLS template
on every table with a tenant_id (ENABLE, tenant_isolation TO aa_app_user, <table>_cis_app_all TO cis_app); cis_app DML
only, nothing for aa_app_user or PUBLIC; the default privileges FOR ROLE cis_owner the isolation probe expects; the
statement append-only guard (UPDATE / DELETE / TRUNCATE refused, a DELETE only under the retention purge flag, also
through the learner cascade); the twelve settings equal the compiled defaults, not enforced, with a complete guide;
a re-run changes nothing. Runs in a transaction that is rolled back."""
import json
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared import settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M420 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "420_aa1105_learn_schema.sql")
TENANT_TABLES = ("learner", "enrollment", "statement", "lesson_progress", "quiz_attempt", "review_card", "lab_run",
                 "certificate")
TABLES = TENANT_TABLES + ("content_review_item",)
SETTINGS = {
    "cis.learn.quiz_pass_mark_pct": 80, "cis.learn.final_quiz_pass_mark_pct": 80,
    "cis.learn.quiz_retake_delay_hours": 24, "cis.learn.quiz_items_per_attempt": 10,
    "cis.learn.final_quiz_items_per_module": 2, "cis.learn.recert_months": 12,
    "cis.learn.recert_paths": ["engineer-on-duty", "tech-lead"], "cis.learn.video_url_ttl_seconds": 300,
    "cis.learn.learner_retention_days": 365, "cis.learn.review_target_retention": 0.9,
    "cis.learn.review_max_cards_per_day": 20, "cis.learn.verify_requests_per_minute": 30,
}
SHA = "a" * 64


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


async def _learner(conn, **kw) -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO learn.learner (iss, sub, kind, tenant_id, display_name) VALUES ($1, $2, $3, $4, $5) "
        "RETURNING learner_id",
        kw.get("iss", "https://cognito-idp.example.invalid/pool"), kw.get("sub", str(uuid.uuid4())),
        kw.get("kind", "staff"), kw.get("tenant_id"), kw.get("display_name", "Sample Learner"))


async def _refused(conn, error, sql, *args):
    """Run sql in a savepoint and expect `error`; the outer (rolled-back) transaction stays usable."""
    with pytest.raises(error):
        async with conn.transaction():
            await conn.execute(sql, *args)


async def _statement(conn, learner_id) -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO learn.statement (learner_id, verb, object_type, object_id, body_sha256) "
        "VALUES ($1, 'opened', 'lesson', 'F3-01', $2) RETURNING statement_id", learner_id, SHA)


@pytest.mark.asyncio
async def test_420_registered_with_the_design_tables_and_keys(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '420'") == "420"
    rows = await conn.fetch(
        "SELECT c.relname AS t, c.relrowsecurity AS rls, c.relforcerowsecurity AS force FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'learn' AND c.relkind = 'r'")
    by = {r["t"]: r for r in rows}
    assert set(by) == set(TABLES)
    for t in TENANT_TABLES:
        assert by[t]["rls"] is True and by[t]["force"] is True, t            # the CIS template: ENABLE + FORCE
    assert by["content_review_item"]["rls"] is False                        # no tenant column, no learner data
    pks = {r["t"]: list(r["cols"]) for r in await conn.fetch(
        "SELECT c.conrelid::regclass::text AS t, array_agg(a.attname::text ORDER BY k.ord) AS cols "
        "FROM pg_constraint c CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) "
        "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum "
        "WHERE c.contype = 'p' AND c.connamespace = 'learn'::regnamespace GROUP BY 1")}
    assert pks["learn.lesson_progress"] == ["learner_id", "lesson_id"]
    assert pks["learn.review_card"] == ["learner_id", "item_id"]
    assert pks["learn.learner"] == ["learner_id"]
    fks = {(r["child"], r["parent"]) for r in await conn.fetch(
        "SELECT cn.nspname || '.' || cc.relname AS child, pn.nspname || '.' || pc.relname AS parent "
        "FROM pg_constraint f JOIN pg_class cc ON cc.oid = f.conrelid JOIN pg_namespace cn ON cn.oid = cc.relnamespace "
        "JOIN pg_class pc ON pc.oid = f.confrelid JOIN pg_namespace pn ON pn.oid = pc.relnamespace "
        "WHERE f.contype = 'f' AND f.connamespace = 'learn'::regnamespace")}
    assert ("learn.learner", "shared.admin_users") in fks
    assert ("learn.certificate", "learn.quiz_attempt") in fks
    assert {c for c, p in fks if p == "learn.learner"} == {f"learn.{t}" for t in TENANT_TABLES if t != "learner"}


@pytest.mark.asyncio
async def test_rls_template_and_grants(conn):
    for t in TENANT_TABLES:
        pol = {r["policyname"]: r for r in await conn.fetch(
            "SELECT policyname, roles::text[] AS roles, qual, with_check FROM pg_policies "
            "WHERE schemaname = 'learn' AND tablename = $1", t)}
        assert set(pol) == {f"{t}_cis_app_all", f"{t}_cis_owner_all", "tenant_isolation"}, t
        assert pol[f"{t}_cis_app_all"]["roles"] == ["cis_app"] and pol[f"{t}_cis_app_all"]["qual"] == "true"
        assert pol[f"{t}_cis_owner_all"]["roles"] == ["cis_owner"] and pol[f"{t}_cis_owner_all"]["qual"] == "true"
        iso = pol["tenant_isolation"]
        assert iso["roles"] == ["aa_app_user"] and "NULLIF" in iso["qual"] and "app.tenant_id" in iso["qual"], t
        assert "NULLIF" in iso["with_check"], t
    for t in TABLES:
        rel = f"learn.{t}"
        assert await conn.fetchval("SELECT has_table_privilege('cis_app', $1, 'SELECT, INSERT, UPDATE, DELETE') AS ok",
                                   rel) is True, t
        assert await conn.fetchval("SELECT has_table_privilege('aa_app_user', $1, 'SELECT') AS ok", rel) is False, t
    assert await conn.fetchval("SELECT has_schema_privilege('cis_app', 'learn', 'USAGE') AS ok") is True
    assert await conn.fetchval("SELECT has_schema_privilege('aa_app_user', 'learn', 'USAGE') AS ok") is False
    assert await conn.fetchval(                                            # nothing granted to PUBLIC
        "SELECT count(*) AS n FROM pg_namespace n CROSS JOIN LATERAL aclexplode(n.nspacl) a "
        "WHERE n.nspname = 'learn' AND a.grantee = 0") == 0
    defacl = {r["objtype"]: set(r["privs"]) for r in await conn.fetch(
        "SELECT d.defaclobjtype::text AS objtype, array_agg(a.privilege_type::text) AS privs "
        "FROM pg_default_acl d JOIN pg_namespace n ON n.oid = d.defaclnamespace "
        "CROSS JOIN LATERAL aclexplode(d.defaclacl) a "
        "WHERE n.nspname = 'learn' AND d.defaclrole = 'cis_owner'::regrole AND a.grantee = 'cis_app'::regrole "
        "GROUP BY 1")}
    assert defacl == {"r": {"SELECT", "INSERT", "UPDATE", "DELETE"}, "S": {"USAGE", "SELECT", "UPDATE"},
                      "f": {"EXECUTE"}}


@pytest.mark.asyncio
async def test_learner_kind_rules(conn):
    assert await _learner(conn)
    ins = "INSERT INTO learn.learner (iss, sub, kind, tenant_id) VALUES ($1, $2, $3, $4)"
    iss = "https://cognito-idp.example.invalid/pool"
    await _refused(conn, asyncpg.CheckViolationError, ins, iss, "s1", "staff", uuid.uuid4())   # staff has no tenant
    await _refused(conn, asyncpg.CheckViolationError, ins, iss, "s2", "partner", None)          # partner needs one
    await _refused(conn, asyncpg.CheckViolationError, ins, "http://plain.example.invalid", "s3", "staff", None)
    sub = str(uuid.uuid4())
    await _learner(conn, sub=sub)
    await _refused(conn, asyncpg.UniqueViolationError, ins, iss, sub, "staff", None)


@pytest.mark.asyncio
async def test_statement_is_append_only(conn):
    learner = await _learner(conn)
    sid = await _statement(conn, learner)
    async with conn.transaction():
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with conn.transaction():
                await conn.execute("UPDATE learn.statement SET verb = 'completed' WHERE statement_id = $1", sid)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with conn.transaction():
                await conn.execute("DELETE FROM learn.statement WHERE statement_id = $1", sid)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with conn.transaction():
                await conn.execute("TRUNCATE learn.statement")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with conn.transaction():                                  # the cascade from the learner too
                await conn.execute("DELETE FROM learn.learner WHERE learner_id = $1", learner)
    assert await conn.fetchval("SELECT count(*) AS n FROM learn.statement WHERE statement_id = $1", sid) == 1


@pytest.mark.asyncio
async def test_retention_purge_deletes_the_learner_and_everything_below(conn):
    learner = await _learner(conn)
    await _statement(conn, learner)
    await conn.execute("INSERT INTO learn.enrollment (learner_id, path_id) VALUES ($1, 'fundamentals')", learner)
    await conn.execute("INSERT INTO learn.lesson_progress (learner_id, lesson_id, module_id, module_version) "
                       "VALUES ($1, 'F3-01', 'F3', '0.1.0')", learner)
    attempt = await conn.fetchval(
        "INSERT INTO learn.quiz_attempt (learner_id, scope, target_id, item_ids, module_versions, bundle_sha, "
        "pass_mark_pct) VALUES ($1, 'final', 'fundamentals', ARRAY['F3-Q1'], '{\"F3\": \"0.1.0\"}', 'abc', 80) "
        "RETURNING attempt_id", learner)
    await conn.execute("INSERT INTO learn.certificate (learner_id, path_id, path_title, code_sha256, module_versions, "
                       "final_attempt_id) VALUES ($1, 'fundamentals', 'Fundamentals', $2, '{}', $3)",
                       learner, SHA, attempt)
    await conn.execute("SET LOCAL learn.retention_purge = 'on'")
    await conn.execute("DELETE FROM learn.learner WHERE learner_id = $1", learner)
    for t in TENANT_TABLES:
        assert await conn.fetchval(f"SELECT count(*) AS n FROM learn.{t} WHERE learner_id = $1", learner) == 0, t


@pytest.mark.asyncio
async def test_one_open_attempt_and_one_certificate_per_path(conn):
    learner = await _learner(conn)
    sql = ("INSERT INTO learn.quiz_attempt (learner_id, scope, target_id, item_ids, module_versions, bundle_sha, "
           "pass_mark_pct) VALUES ($1, 'module', 'F3', ARRAY['F3-Q1', 'F3-Q2'], '{\"F3\": \"0.1.0\"}', 'abc', 80)")
    await conn.execute(sql, learner)
    await _refused(conn, asyncpg.UniqueViolationError, sql, learner)
    await _refused(conn, asyncpg.CheckViolationError,                        # submitted needs its grade
                   "UPDATE learn.quiz_attempt SET status = 'submitted' WHERE learner_id = $1", learner)
    cert = ("INSERT INTO learn.certificate (learner_id, path_id, path_title, code_sha256, module_versions) "
            "VALUES ($1, 'fundamentals', 'Fundamentals', $2, '{}')")
    await conn.execute(cert, learner, SHA)
    await _refused(conn, asyncpg.UniqueViolationError, cert, learner, "b" * 64)
    await _refused(conn, asyncpg.CheckViolationError,                        # a revoke names its administrator
                   "UPDATE learn.certificate SET status = 'revoked', revoked_at = now() WHERE learner_id = $1", learner)


@pytest.mark.asyncio
async def test_one_open_review_item_per_source_hash(conn):
    sql = ("INSERT INTO learn.content_review_item (module_id, lesson_id, source_ref, pinned_sha, current_sha, "
           "bundle_sha, reason) VALUES ('F3', 'F3-01', 'adr:0001#decision', '0123456789abcdef', "
           "'fedcba9876543210', 'abc', 'source_changed')")
    await conn.execute(sql)
    await _refused(conn, asyncpg.UniqueViolationError, sql)


@pytest.mark.asyncio
async def test_settings_are_the_compiled_defaults(conn):
    rows = {r["key"]: r for r in await conn.fetch(
        "SELECT key, default_value, enforced, area, label, shared.setting_guide_complete(guide) AS guided "
        "FROM shared.setting_definition WHERE key LIKE 'cis.learn.%'")}
    assert set(rows) == set(SETTINGS)
    for key, default in SETTINGS.items():
        assert json.loads(rows[key]["default_value"]) == default == settings.definition(key).default_value, key
        assert rows[key]["enforced"] is False and settings.definition(key).enforced is False, key
        assert rows[key]["area"] == "learn" and rows[key]["guided"] is True, key
        assert rows[key]["label"] == settings.definition(key).label, key


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    body = open(M420, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    policies = await conn.fetchval("SELECT count(*) AS n FROM pg_policies WHERE schemaname = 'learn'")
    await conn.execute(body)
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before
    assert await conn.fetchval("SELECT count(*) AS n FROM pg_policies WHERE schemaname = 'learn'") == policies
