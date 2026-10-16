"""AA-817 — migration 239: master read for the tenant role, NULLIF on the legacy FORCE-RLS policies, no
tenant-role writes on tables without RLS.

conftest applies 239 as the superuser with every other migration. Each test here re-applies the file
body in a transaction that is rolled back. Checked:
  * shape: tenant_isolation is the NULLIF form on all eight legacy tables (TO public, FOR ALL, FORCE
    kept); aa_app_user_aa_master_read is FOR SELECT TO aa_app_user on the AA master tenant on
    published_tours / raw_tours / seo_context; each policy set is exact;
  * grants: SELECT on the three master-read tables, no write on angle_gate_option / tenant_atom_state /
    year_plan (SELECT kept);
  * re-run is idempotent, also with rows of several tenants in every table (guard 4's behavioural
    counts and WHERE-less UPDATE run on real rows, Dev's grants mirrored);
  * the runner (main pool) still reads every row under a foreign tenant;
  * the guards refuse: an unknown policy, a drifted tenant_isolation, a member of aa_app_user, RLS
    already on a REVOKE table, acp_output_rules readable by the tenant role, and a runner that RLS
    filters (an owner without bypass on a FORCEd table);
  * applied as an RDS-like runner (owner, NOSUPERUSER, CREATEROLE, not a member of aa_app_user; with
    BYPASSRLS standing in for RDS's observed rds_superuser bypass on the FORCEd tables, AA-817 critique
    A4) from the pre-239 state: guard 4 runs through the transaction-scoped membership, which is gone
    afterwards.
The read/write isolation per table (WHERE-less queries under own / forged / '' / master GUC) is in
test_aa817_rls_tenant_isolation.py (LEGACY_TABLES, NO_RLS_NO_WRITE_TABLES, OUTPUT_RULES).
"""
import os
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from test_aa817_rls_tenant_isolation import (  # noqa: E402
    DEV_LEGACY_GRANTS, LEGACY_TABLES, MASTER_TENANT, NO_RLS_NO_WRITE_TABLES, OUTPUT_RULES_STANDIN_DDL,
    _seed_legacy,
)

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                         "239_tenant_master_read_nullif.sql")
TENANT_ROLE = "aa_app_user"
RUNNER = "zz239_rds_runner"   # stands in for aa_cis_admin on RDS
MASTER_READ = sorted(t for t, master in LEGACY_TABLES.items() if master)
NULLIF_QUAL = "(tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)"
OLD_QUAL = "tenant_id = current_setting('app.tenant_id', true)::uuid"


def _body() -> str:
    """The file without its own BEGIN/COMMIT, so a test can run it in a transaction it rolls back."""
    with open(MIGRATION) as f:
        return "\n".join(line for line in f.read().splitlines() if line.strip() not in ("BEGIN;", "COMMIT;"))


def _connect():
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)


@pytest.fixture
def cur():
    """A rolled-back transaction with Dev's legacy grants mirrored and 239 (re-)applied."""
    conn = _connect()
    conn.autocommit = False
    c = conn.cursor()
    try:
        for grant in DEV_LEGACY_GRANTS:
            c.execute(grant)
        c.execute(_body())
        yield c
    finally:
        conn.rollback()
        conn.close()


def _tenant(c):
    tid = str(uuid.uuid4())
    c.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES (%s, %s, %s, 3)",
              (tid, f"AA-817 239 {tid[:8]}", f"aa817m-{tid[:8]}"))
    return tid


@pytest.fixture
def seeded(cur):
    """Tenant a with two rows per legacy table (the busiest non-master tenant guard 4 checks), tenant b
    with one, the master tenant with one."""
    a, b = _tenant(cur), _tenant(cur)
    for tid in (a, a, b, MASTER_TENANT):
        _seed_legacy(cur, tid)
    return a, b


def _policies(c, table):
    schema, name = table.split(".")
    c.execute("SELECT policyname, roles::text[], cmd, qual, with_check FROM pg_policies "
              "WHERE schemaname = %s AND tablename = %s", (schema, name))
    return {r[0]: r[1:] for r in c.fetchall()}


def _count_ours(c, table, tenants):
    c.execute(f"SELECT count(*) FROM {table} WHERE tenant_id::text = ANY(%s)", (list(tenants),))
    return c.fetchone()[0]


def _can_set_tenant_role(c) -> bool:
    c.execute("SAVEPOINT aa239set")
    try:
        c.execute(f"SET LOCAL ROLE {TENANT_ROLE}")
        return True
    except psycopg2.errors.InsufficientPrivilege:
        return False
    finally:
        c.execute("ROLLBACK TO SAVEPOINT aa239set")


def _to_pre_239(c):
    """Back to Dev's state before 239: no master-read policy, the 004/006/008 USING on the six tables,
    INSERT/UPDATE on the three tables without RLS (098/147), version not registered."""
    c.execute("DELETE FROM shared.schema_versions WHERE version = '239'")
    for table in MASTER_READ:
        c.execute(f"DROP POLICY aa_app_user_aa_master_read ON {table}")
    for table in sorted(set(LEGACY_TABLES) - {"gold_aa_internal.published_tours", "silver_aa_internal.raw_tours"}):
        c.execute(f"ALTER POLICY tenant_isolation ON {table} USING ({OLD_QUAL})")
    c.execute(f"GRANT INSERT, UPDATE ON {', '.join(NO_RLS_NO_WRITE_TABLES)} TO {TENANT_ROLE}")


def _as_rds_like_runner(c, bypass=True):
    """Hand the schemas and tables 239 touches to a NOSUPERUSER, CREATEROLE role that may not SET ROLE
    aa_app_user (as aa_cis_admin on RDS), then become it. bypass=True gives it BYPASSRLS, standing in for
    the bypass RDS's rds_superuser shows on the FORCEd tables (critique A4); bypass=False is a plain owner,
    which FORCE filters. Rolled back with the test transaction."""
    c.execute("SELECT current_setting('server_version_num')::int")
    pg16 = c.fetchone()[0] >= 160000
    c.execute(f"CREATE ROLE {RUNNER} NOSUPERUSER {'BYPASSRLS' if bypass else 'NOBYPASSRLS'} CREATEROLE INHERIT")
    if pg16:
        # PG16+: CREATEROLE grants only roles it holds ADMIN on (RDS PG15: any non-superuser role).
        # ADMIN without SET does not allow SET ROLE, so guard 4 still takes its GRANT path.
        c.execute(f"GRANT {TENANT_ROLE} TO {RUNNER} WITH ADMIN TRUE, INHERIT FALSE, SET FALSE")
    for schema in ("gold_aa_internal", "silver_aa_internal", "shared", "acp_shared"):
        c.execute(f"ALTER SCHEMA {schema} OWNER TO {RUNNER}")
    for table in sorted(LEGACY_TABLES) + list(NO_RLS_NO_WRITE_TABLES) + ["shared.schema_versions"]:
        c.execute(f"ALTER TABLE {table} OWNER TO {RUNNER}")
    c.execute(f"SET LOCAL SESSION AUTHORIZATION {RUNNER}")


# ── shape ─────────────────────────────────────────────────────────────────────────────────────────

def test_applied_by_conftest():
    """239 really applied to the shared test DB (conftest swallows migration errors)."""
    conn = _connect()
    try:
        c = conn.cursor()
        c.execute("SELECT 1 FROM shared.schema_versions WHERE version = '239'")
        assert c.fetchone(), "239 did not apply in the test DB"
        for table in MASTER_READ:
            assert "aa_app_user_aa_master_read" in _policies(c, table), table
    finally:
        conn.rollback()
        conn.close()


@pytest.mark.parametrize("table", sorted(LEGACY_TABLES))
def test_tenant_isolation_is_the_nullif_form(cur, table):
    roles, cmd, qual, with_check = _policies(cur, table)["tenant_isolation"]
    assert roles == ["public"] and cmd == "ALL"
    assert qual == NULLIF_QUAL and with_check in (None, NULLIF_QUAL)
    cur.execute("SELECT relrowsecurity FROM pg_class WHERE oid = %s::regclass", (table,))
    assert cur.fetchone()[0]


@pytest.mark.parametrize("table", MASTER_READ)
def test_master_read_policy(cur, table):
    pols = _policies(cur, table)
    expected = {"aa_app_user_aa_master_read", "tenant_isolation"}
    if table != "silver_aa_internal.seo_context":
        expected.add("catalog_owner_aa_master_read")
    assert set(pols) == expected
    roles, cmd, qual, with_check = pols["aa_app_user_aa_master_read"]
    assert roles == [TENANT_ROLE] and cmd == "SELECT" and with_check is None
    assert qual == f"(tenant_id = '{MASTER_TENANT}'::uuid)"


def test_grants(cur):
    for table in MASTER_READ:
        cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT')", (TENANT_ROLE, table))
        assert cur.fetchone()[0], table
    for table in sorted(LEGACY_TABLES):
        cur.execute("SELECT has_table_privilege(%s, %s, 'DELETE, TRUNCATE')", (TENANT_ROLE, table))
        assert not cur.fetchone()[0], table
    for table in NO_RLS_NO_WRITE_TABLES:
        cur.execute("SELECT has_table_privilege(%s, %s, 'INSERT, UPDATE, DELETE, TRUNCATE')", (TENANT_ROLE, table))
        assert not cur.fetchone()[0], table


def test_owner_reads_every_row_under_foreign_tenant(cur, seeded):
    """The main pool is unchanged: every row under a forged GUC, and under ''."""
    a, b = seeded
    for guc in (str(uuid.uuid4()), ""):
        cur.execute("SELECT set_config('app.tenant_id', %s, true)", (guc,))
        for table in sorted(LEGACY_TABLES):
            assert _count_ours(cur, table, (a, b)) == 3, (table, guc)


def test_rerun_with_rows_is_idempotent(cur, seeded):
    """Guard 4 counts, per table, the master rows and the busiest non-master tenant (a), and runs its
    WHERE-less UPDATE on pipeline_runs (Dev grant): nothing may change."""
    a, b = seeded
    cur.execute(_body())
    cur.execute(_body())
    for table in MASTER_READ:
        assert set(_policies(cur, table)) >= {"aa_app_user_aa_master_read", "tenant_isolation"}
    for table in sorted(LEGACY_TABLES):
        assert _count_ours(cur, table, (a, b)) == 3, table


# ── guards ────────────────────────────────────────────────────────────────────────────────────────

def test_guard_refuses_unknown_policy(cur):
    """A live-only permissive policy would OR into the tenant's view."""
    cur.execute("CREATE POLICY zz239_extra ON silver_aa_internal.generated_content USING (true)")
    with pytest.raises(psycopg2.errors.RaiseException, match="has policies"):
        cur.execute(_body())


def test_guard_refuses_drifted_tenant_isolation(cur):
    cur.execute("ALTER POLICY tenant_isolation ON gold_aa_internal.content_exports USING (true)")
    with pytest.raises(psycopg2.errors.RaiseException, match="drift"):
        cur.execute(_body())


def test_guard_refuses_a_member_of_the_tenant_role(cur):
    """The new policy would apply to every session of a member too."""
    cur.execute("CREATE ROLE zz239_member")
    cur.execute(f"GRANT {TENANT_ROLE} TO zz239_member")
    with pytest.raises(psycopg2.errors.RaiseException, match="members of aa_app_user"):
        cur.execute(_body())


def test_guard_refuses_rls_on_a_revoke_table(cur):
    cur.execute("ALTER TABLE acp_shared.year_plan ENABLE ROW LEVEL SECURITY")
    with pytest.raises(psycopg2.errors.RaiseException, match="already has ROW LEVEL SECURITY"):
        cur.execute(_body())


def test_guard_refuses_output_rules_readable_by_tenant_role(cur):
    cur.execute(OUTPUT_RULES_STANDIN_DDL)
    cur.execute(f"GRANT SELECT ON acp_shared.acp_output_rules TO {TENANT_ROLE}")
    with pytest.raises(psycopg2.errors.RaiseException, match="no longer deny-all"):
        cur.execute(_body())


def test_guard_refuses_a_runner_filtered_by_rls(cur):
    """An owner without bypass on a FORCEd table reads through tenant_isolation: '' turning from an error
    into no rows would then change the main pool. Not the Dev case (critique A4), so refuse."""
    _as_rds_like_runner(cur, bypass=False)
    with pytest.raises(psycopg2.errors.RaiseException, match="RLS is active for the runner"):
        cur.execute(_body())


# ── as an RDS-like runner (how DB Migrate applies it on Dev) ─────────────────────────────────────

def test_applies_as_rds_like_runner(cur, seeded):
    a, b = seeded
    _to_pre_239(cur)
    _as_rds_like_runner(cur)
    assert not _can_set_tenant_role(cur), "fixture: the runner must not be able to SET ROLE aa_app_user"
    cur.execute(_body())                       # raises on any guard failure
    assert not _can_set_tenant_role(cur), "the transaction-scoped membership was not revoked"
    cur.execute("SELECT 1 FROM shared.schema_versions WHERE version = '239'")
    assert cur.fetchone()
    for table in sorted(LEGACY_TABLES):
        assert _count_ours(cur, table, (a, b)) == 3, table
    for table in NO_RLS_NO_WRITE_TABLES:
        cur.execute("SELECT has_table_privilege(%s, %s, 'INSERT, UPDATE')", (TENANT_ROLE, table))
        assert not cur.fetchone()[0], table
