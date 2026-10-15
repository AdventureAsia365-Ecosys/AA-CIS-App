"""AA-817 — migration 229: the tenant-pool RLS template on tenant_tour_versions, angle_gate_request,
content_piece, publish_log and facts.

conftest applies 229 as the superuser against a stand-in of tenant_tour_versions (the CI DB skips
migration 001, which creates it) and drops the stand-in again, so other tests are unaffected. Each test
here recreates the stand-in and re-applies the file body in a transaction that is rolled back.
Checked:
  * shape: RLS on, no FORCE, exactly the expected policies, all TO aa_app_user, NULLIF on app.tenant_id
    in USING and WITH CHECK, no app.current_tenant;
  * tenant_tour_versions is isolated for aa_app_user (reads and writes), and aa_app_user has the
    grants the stage-2 routes need;
  * facts: platform rows are readable by every tenant and writable by none;
  * re-run: the live-only drift policies (tenant_tour_versions_isolation, output_rules_rls) are
    dropped, a 007b-style default privilege no longer grants new tables to aa_app_user, and the
    file is idempotent;
  * the guards refuse a FORCEd table and a leftover extra policy;
  * applied as an RDS-like runner (owner, NOSUPERUSER, NOBYPASSRLS, CREATEROLE, not a member of
    aa_app_user), the way DB Migrate applies it on Dev: guard 3 runs through the
    transaction-scoped membership and the membership is gone afterwards.
"""
import os
import sys
import uuid
from contextlib import contextmanager

import psycopg2
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER, TTV_STANDIN_DDL  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                         "229_tenant_rls_policies.sql")
TENANT_ROLE = "aa_app_user"
RUNNER = "zz817_rds_runner"   # stands in for aa_cis_admin on RDS
TABLES = ("acp_shared.angle_gate_request", "acp_shared.content_piece", "acp_shared.publish_log",
          "acp_shared.facts", "gold_aa_internal.tenant_tour_versions")
EXPECTED_POLICIES = {t: {"tenant_isolation"} for t in TABLES}
EXPECTED_POLICIES["acp_shared.facts"] = {"tenant_isolation", "facts_platform_read"}


def _body() -> str:
    """The file without its own BEGIN/COMMIT, so a test can run it in a transaction it rolls back."""
    with open(MIGRATION) as f:
        return "\n".join(line for line in f.read().splitlines() if line.strip() not in ("BEGIN;", "COMMIT;"))


def _connect():
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)


@pytest.fixture
def cur():
    """A rolled-back transaction with the tenant_tour_versions stand-in and 229 (re-)applied."""
    conn = _connect()
    conn.autocommit = False
    c = conn.cursor()
    try:
        c.execute(TTV_STANDIN_DDL)
        c.execute(_body())
        yield c
    finally:
        conn.rollback()
        conn.close()


def _tenant(c):
    tid = str(uuid.uuid4())
    c.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES (%s, %s, %s, 3)",
              (tid, f"AA-817 229 {tid[:8]}", f"aa817m-{tid[:8]}"))
    return tid


def _fact(c, tid=None):
    fid = f"fact_{uuid.uuid4().hex[:10]}"
    c.execute("INSERT INTO acp_shared.facts (fact_id, scope, tenant_id, title, body, provenance) "
              "VALUES (%s, %s, %s, 't', 'b', 'aa817-229-test')", (fid, "tenant" if tid else "platform", tid))
    return fid


def _version(c, tid):
    c.execute("INSERT INTO gold_aa_internal.tenant_tour_versions (tenant_id, published_tour_id, edit_source) "
              "VALUES (%s, %s, 'ai_generated') RETURNING id", (tid, str(uuid.uuid4())))
    return str(c.fetchone()[0])


@pytest.fixture
def seeded(cur):
    a, b = _tenant(cur), _tenant(cur)
    for tid in (a, b):
        _version(cur, tid)
        _fact(cur, tid)
    _fact(cur)                       # one platform fact
    return a, b


@contextmanager
def _as_tenant(c, guc):
    """The block runs as the tenant role with app.tenant_id = guc, in a savepoint rolled back after."""
    c.execute("SAVEPOINT aa817m")
    try:
        c.execute(f"SET LOCAL ROLE {TENANT_ROLE}")
        c.execute("SELECT set_config('app.tenant_id', %s, true)", (guc,))
        yield c
    finally:
        c.execute("ROLLBACK TO SAVEPOINT aa817m")


def _refused(c, sql, params=()):
    with pytest.raises(psycopg2.errors.InsufficientPrivilege, match="row-level security"):
        c.execute(sql, params)


def _can_set_tenant_role(c) -> bool:
    c.execute("SAVEPOINT aa817set")
    try:
        c.execute(f"SET LOCAL ROLE {TENANT_ROLE}")
        return True
    except psycopg2.errors.InsufficientPrivilege:
        return False
    finally:
        c.execute("ROLLBACK TO SAVEPOINT aa817set")


def _as_rds_like_runner(c, skip=()):
    """Hand the schemas and tables 229 touches to a NOSUPERUSER, NOBYPASSRLS, CREATEROLE role that may
    not SET ROLE aa_app_user (as aa_cis_admin on RDS), then become it. SET LOCAL SESSION AUTHORIZATION,
    not SET ROLE: SET ROLE permission is checked against the session user, which would still be the
    test superuser. Rolled back with the test transaction."""
    c.execute("SELECT current_setting('server_version_num')::int")
    pg16 = c.fetchone()[0] >= 160000
    c.execute(f"CREATE ROLE {RUNNER} NOSUPERUSER NOBYPASSRLS CREATEROLE INHERIT")
    if pg16:
        # PG16+: CREATEROLE grants only roles it holds ADMIN on (RDS PG15: any non-superuser role).
        # ADMIN without SET does not allow SET ROLE, so guard 3 still takes its GRANT path.
        c.execute(f"GRANT {TENANT_ROLE} TO {RUNNER} WITH ADMIN TRUE, INHERIT FALSE, SET FALSE")
    for schema in ("acp_shared", "gold_aa_internal", "shared", "silver_aa_internal"):
        c.execute(f"ALTER SCHEMA {schema} OWNER TO {RUNNER}")
    for table in TABLES + ("shared.schema_versions",):
        if table not in skip:
            c.execute(f"ALTER TABLE {table} OWNER TO {RUNNER}")
    c.execute(f"SET LOCAL SESSION AUTHORIZATION {RUNNER}")


def _policies(c, table):
    c.execute("SELECT policyname, roles::text[], qual, with_check, cmd FROM pg_policies "
              "WHERE schemaname || '.' || tablename = %s", (table,))
    return {r[0]: r[1:] for r in c.fetchall()}


# ── shape ─────────────────────────────────────────────────────────────────────────────────────────

def test_applied_by_conftest():
    """229 really applied to the shared test DB (conftest swallows migration errors), so every other
    integration test runs against the 229 policies of the acp_shared tables."""
    conn = _connect()
    try:
        c = conn.cursor()
        c.execute("SELECT 1 FROM shared.schema_versions WHERE version = '229'")
        assert c.fetchone(), "229 did not apply in the test DB"
        for table in TABLES[:4]:
            assert set(_policies(c, table)) == EXPECTED_POLICIES[table], table
    finally:
        conn.rollback()
        conn.close()


@pytest.mark.parametrize("table", TABLES)
def test_policy_template(cur, table):
    cur.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = %s::regclass", (table,))
    assert cur.fetchone() == (True, False), f"{table}: expected ENABLE without FORCE (D2)"
    pols = _policies(cur, table)
    assert set(pols) == EXPECTED_POLICIES[table]
    for name, (roles, qual, with_check, cmd) in pols.items():
        assert roles == [TENANT_ROLE], (table, name, roles)
        assert "current_tenant" not in (qual or "") + (with_check or "")
        if name == "facts_platform_read":
            assert cmd == "SELECT" and with_check is None
            continue
        assert cmd == "ALL"
        for expr in (qual, with_check):
            assert expr and "nullif" in expr.lower() and "app.tenant_id" in expr, (table, name, expr)


def test_tenant_role_grants(cur):
    for priv in ("SELECT", "INSERT", "UPDATE"):
        cur.execute("SELECT has_table_privilege(%s, 'gold_aa_internal.tenant_tour_versions', %s)",
                    (TENANT_ROLE, priv))
        assert cur.fetchone()[0], priv
    cur.execute("SELECT has_table_privilege(%s, 'gold_aa_internal.tenant_tour_versions', 'DELETE')",
                (TENANT_ROLE,))
    assert not cur.fetchone()[0], "no DELETE: tenants do not delete versions (offboard runs on the main pool)"


# ── behaviour ─────────────────────────────────────────────────────────────────────────────────────

def test_tenant_tour_versions_isolated(cur, seeded):
    a, b = seeded
    for guc, expected in ((a, {a}), (b, {b}), (str(uuid.uuid4()), set()), ("", set())):
        with _as_tenant(cur, guc) as c:
            c.execute("SELECT DISTINCT tenant_id::text FROM gold_aa_internal.tenant_tour_versions")  # no WHERE
            assert {r[0] for r in c.fetchall()} == expected, guc
    with _as_tenant(cur, a) as c:
        c.execute("UPDATE gold_aa_internal.tenant_tour_versions SET status = status")   # no WHERE
        assert c.rowcount == 1
    with _as_tenant(cur, a) as c:
        _refused(c, "UPDATE gold_aa_internal.tenant_tour_versions SET tenant_id = %s", (b,))
    with _as_tenant(cur, a) as c:
        _refused(c, "INSERT INTO gold_aa_internal.tenant_tour_versions (tenant_id, edit_source) "
                    "VALUES (%s, 'ai_generated')", (b,))
    with _as_tenant(cur, a) as c:   # the stage-2 rewrite INSERT for the own tenant goes through
        c.execute("INSERT INTO gold_aa_internal.tenant_tour_versions (tenant_id, edit_source) "
                  "VALUES (%s, 'ai_generated')", (a,))
        assert c.rowcount == 1


def test_facts_platform_rows_read_only(cur, seeded):
    a, b = seeded
    with _as_tenant(cur, a) as c:
        c.execute("SELECT scope, tenant_id::text FROM acp_shared.facts WHERE provenance = 'aa817-229-test'")
        assert {r for r in c.fetchall()} == {("tenant", a), ("platform", None)}
    with _as_tenant(cur, a) as c:
        c.execute("UPDATE acp_shared.facts SET body = 'x' WHERE scope = 'platform'")
        assert c.rowcount == 0, "tenant role updated platform facts"
    with _as_tenant(cur, a) as c:
        c.execute("UPDATE acp_shared.facts SET body = 'x' WHERE provenance = 'aa817-229-test'")
        assert c.rowcount == 1, "own tenant fact only"
    with _as_tenant(cur, a) as c:
        _refused(c, "INSERT INTO acp_shared.facts (fact_id, scope, tenant_id, title, body, provenance) "
                    "VALUES (%s, 'platform', NULL, 't', 'b', 'p')", (f"fact_{uuid.uuid4().hex[:10]}",))
    with _as_tenant(cur, a) as c:
        _refused(c, "INSERT INTO acp_shared.facts (fact_id, scope, tenant_id, title, body, provenance) "
                    "VALUES (%s, 'tenant', %s, 't', 'b', 'p')", (f"fact_{uuid.uuid4().hex[:10]}", b))
    with _as_tenant(cur, a) as c:
        c.execute("INSERT INTO acp_shared.facts (fact_id, scope, tenant_id, title, body, provenance) "
                  "VALUES (%s, 'tenant', %s, 't', 'b', 'p')", (f"fact_{uuid.uuid4().hex[:10]}", a))
        assert c.rowcount == 1
    for guc in (str(uuid.uuid4()), ""):
        with _as_tenant(cur, guc) as c:
            c.execute("SELECT DISTINCT scope FROM acp_shared.facts WHERE provenance = 'aa817-229-test'")
            assert {r[0] for r in c.fetchall()} == {"platform"}, guc


# ── re-run: drift, default privileges, idempotence ──────────────────────────────────────────────

def test_rerun_drops_drift_and_default_privilege(cur, seeded):
    # the live-only drift: text-compare policy TO public on tenant_tour_versions, and output_rules_rls
    # on the other GUC name (acp_output_rules is not created by any repo migration: stand-in here)
    cur.execute("CREATE POLICY tenant_tour_versions_isolation ON gold_aa_internal.tenant_tour_versions "
                "USING ((tenant_id)::text = current_setting('app.tenant_id', true))")
    made_rules = False
    cur.execute("SELECT to_regclass('acp_shared.acp_output_rules') IS NULL")
    if cur.fetchone()[0]:
        cur.execute("CREATE TABLE acp_shared.acp_output_rules (rule_id uuid PRIMARY KEY, tenant_id varchar(50))")
        cur.execute("ALTER TABLE acp_shared.acp_output_rules ENABLE ROW LEVEL SECURITY")
        made_rules = True
    cur.execute("DROP POLICY IF EXISTS output_rules_rls ON acp_shared.acp_output_rules")
    cur.execute("CREATE POLICY output_rules_rls ON acp_shared.acp_output_rules USING "
                "(tenant_id IS NULL OR (tenant_id)::text = current_setting('app.current_tenant', true))")
    # 007b's default privilege, granted by the role that applies migrations
    for schema in ("shared", "silver_aa_internal", "gold_aa_internal"):
        cur.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT SELECT ON TABLES TO {TENANT_ROLE}")
    cur.execute("CREATE TABLE shared.zz817_before (id int)")
    cur.execute("SELECT has_table_privilege(%s, 'shared.zz817_before', 'SELECT')", (TENANT_ROLE,))
    assert cur.fetchone()[0], "fixture: the default privilege should apply before 229"

    cur.execute(_body())
    cur.execute(_body())   # idempotent

    assert "tenant_tour_versions_isolation" not in _policies(cur, "gold_aa_internal.tenant_tour_versions")
    assert _policies(cur, "acp_shared.acp_output_rules") == {}
    cur.execute("SELECT relrowsecurity FROM pg_class WHERE oid = 'acp_shared.acp_output_rules'::regclass")
    assert cur.fetchone()[0], "acp_output_rules keeps RLS on with no policy: non-owners are denied"
    for schema in ("shared", "silver_aa_internal", "gold_aa_internal"):
        cur.execute(f"CREATE TABLE {schema}.zz817_after (id int)")
        cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT')", (TENANT_ROLE, f"{schema}.zz817_after"))
        assert not cur.fetchone()[0], f"{schema}: new tables are still granted to {TENANT_ROLE}"
    cur.execute("SELECT has_table_privilege(%s, 'shared.zz817_before', 'SELECT')", (TENANT_ROLE,))
    assert cur.fetchone()[0], "existing grants are unchanged (REVOKE on defaults affects new tables only)"
    for table in TABLES:
        assert set(_policies(cur, table)) == EXPECTED_POLICIES[table], table
    if made_rules:
        cur.execute("DROP TABLE acp_shared.acp_output_rules")


def test_guard_refuses_force(cur):
    cur.execute("ALTER TABLE acp_shared.content_piece FORCE ROW LEVEL SECURITY")
    with pytest.raises(psycopg2.errors.RaiseException, match="FORCE ROW LEVEL SECURITY"):
        cur.execute(_body())


def test_guard_refuses_unknown_policy(cur):
    """A live-only permissive policy the file does not know about would OR into the tenant's view."""
    cur.execute("CREATE POLICY zz817_extra ON acp_shared.content_piece USING (true)")
    with pytest.raises(psycopg2.errors.RaiseException, match="has policies"):
        cur.execute(_body())


# ── as an RDS-like runner (how DB Migrate applies it on Dev) ─────────────────────────────────────

def test_applies_as_rds_like_owner(cur, seeded):
    """Owner, NOSUPERUSER, NOBYPASSRLS, CREATEROLE and unable to SET ROLE aa_app_user, like
    aa_cis_admin: guards 1-5 run for real (guard 3 through the transaction-scoped membership), and the
    membership is gone afterwards."""
    cur.execute("DELETE FROM shared.schema_versions WHERE version = '229'")
    _as_rds_like_runner(cur)
    assert not _can_set_tenant_role(cur), "fixture: the runner must not be able to SET ROLE aa_app_user"
    cur.execute(_body())                       # raises on any guard failure
    assert not _can_set_tenant_role(cur), "the transaction-scoped membership was not revoked"
    cur.execute("SELECT 1 FROM shared.schema_versions WHERE version = '229'")
    assert cur.fetchone()


def test_rds_like_owner_force_is_refused(cur):
    """With FORCE the owner matches no policy (all are TO aa_app_user) and would read 0 rows: guard 1."""
    cur.execute("ALTER TABLE gold_aa_internal.tenant_tour_versions FORCE ROW LEVEL SECURITY")
    _as_rds_like_runner(cur)
    with pytest.raises(psycopg2.errors.RaiseException, match="FORCE ROW LEVEL SECURITY"):
        cur.execute(_body())


def test_rds_like_non_owner_is_refused(cur):
    """A runner that does not own a table would be filtered by ENABLE alone: guard 1."""
    cur.execute("CREATE ROLE zz817_other")
    cur.execute("ALTER TABLE acp_shared.publish_log OWNER TO zz817_other")
    _as_rds_like_runner(cur, skip=("acp_shared.publish_log",))
    with pytest.raises(psycopg2.errors.RaiseException, match="neither owns acp_shared.publish_log"):
        cur.execute(_body())
