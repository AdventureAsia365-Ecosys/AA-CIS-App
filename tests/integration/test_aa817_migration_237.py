"""AA-817 — migration 237: shared.tenant_integrations on the migration-229 RLS template.

conftest applies 237 as the superuser with every other migration. Each test here re-applies the file
body in a transaction that is rolled back. Checked:
  * shape: RLS on, no FORCE, exactly one policy tenant_isolation, FOR ALL TO aa_app_user, NULLIF on
    app.tenant_id in USING and WITH CHECK;
  * grants: aa_app_user has SELECT and DELETE (the disconnect route), no INSERT/UPDATE/TRUNCATE;
  * the owner still reads every row under a foreign tenant (main pool unchanged);
  * re-run is idempotent;
  * the guards refuse a FORCEd table, a leftover extra policy, another grantee (it would read 0 rows
    once RLS is on) and a non-owner runner;
  * applied as an RDS-like runner (owner, NOSUPERUSER, NOBYPASSRLS, CREATEROLE, not a member of
    aa_app_user), the way DB Migrate applies it on Dev: guard 3 runs through the transaction-scoped
    membership, deletes nothing, and the membership is gone afterwards.
The read/delete isolation itself (WHERE-less queries under own / forged / '' GUC) is in
test_aa817_rls_tenant_isolation.py (TENANT_TABLES).
"""
import os
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                         "237_tenant_integrations_rls.sql")
TABLE = "shared.tenant_integrations"
TENANT_ROLE = "aa_app_user"
RUNNER = "zz237_rds_runner"   # stands in for aa_cis_admin on RDS


def _body() -> str:
    """The file without its own BEGIN/COMMIT, so a test can run it in a transaction it rolls back."""
    with open(MIGRATION) as f:
        return "\n".join(line for line in f.read().splitlines() if line.strip() not in ("BEGIN;", "COMMIT;"))


def _connect():
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)


@pytest.fixture
def cur():
    """A rolled-back transaction with 237 (re-)applied."""
    conn = _connect()
    conn.autocommit = False
    c = conn.cursor()
    try:
        c.execute(_body())
        yield c
    finally:
        conn.rollback()
        conn.close()


def _tenant(c):
    tid = str(uuid.uuid4())
    c.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES (%s, %s, %s, 3)",
              (tid, f"AA-817 237 {tid[:8]}", f"aa817i-{tid[:8]}"))
    return tid


@pytest.fixture
def seeded(cur):
    """Tenant a with two integrations (the busiest tenant guard 3 checks), tenant b with one."""
    a, b = _tenant(cur), _tenant(cur)
    for tid, kind in ((a, "wordpress"), (a, "facebook"), (b, "wordpress")):
        cur.execute(f"INSERT INTO {TABLE} (tenant_id, integration_type, config, secret_key) "
                    "VALUES (%s, %s, '{}'::jsonb, %s)", (tid, kind, f"acp/cms/{tid}"))
    return a, b


def _count_ours(c, a, b):
    c.execute(f"SELECT count(*) FROM {TABLE} WHERE tenant_id::text = ANY(%s)", ([a, b],))
    return c.fetchone()[0]


def _can_set_tenant_role(c) -> bool:
    c.execute("SAVEPOINT aa237set")
    try:
        c.execute(f"SET LOCAL ROLE {TENANT_ROLE}")
        return True
    except psycopg2.errors.InsufficientPrivilege:
        return False
    finally:
        c.execute("ROLLBACK TO SAVEPOINT aa237set")


def _as_rds_like_runner(c, own_table=True):
    """Hand the schema and tables 237 touches to a NOSUPERUSER, NOBYPASSRLS, CREATEROLE role that may not
    SET ROLE aa_app_user (as aa_cis_admin on RDS), then become it (SET LOCAL SESSION AUTHORIZATION: SET
    ROLE permission is checked against the session user). Rolled back with the test transaction."""
    c.execute("SELECT current_setting('server_version_num')::int")
    pg16 = c.fetchone()[0] >= 160000
    c.execute(f"CREATE ROLE {RUNNER} NOSUPERUSER NOBYPASSRLS CREATEROLE INHERIT")
    if pg16:
        # PG16+: CREATEROLE grants only roles it holds ADMIN on (RDS PG15: any non-superuser role).
        # ADMIN without SET does not allow SET ROLE, so guard 3 still takes its GRANT path.
        c.execute(f"GRANT {TENANT_ROLE} TO {RUNNER} WITH ADMIN TRUE, INHERIT FALSE, SET FALSE")
    c.execute(f"ALTER SCHEMA shared OWNER TO {RUNNER}")
    c.execute(f"ALTER TABLE shared.schema_versions OWNER TO {RUNNER}")
    if own_table:
        c.execute(f"ALTER TABLE {TABLE} OWNER TO {RUNNER}")
    c.execute(f"SET LOCAL SESSION AUTHORIZATION {RUNNER}")


def _policies(c):
    c.execute("SELECT policyname, roles::text[], qual, with_check, cmd FROM pg_policies "
              "WHERE schemaname = 'shared' AND tablename = 'tenant_integrations'")
    return {r[0]: r[1:] for r in c.fetchall()}


# ── shape ─────────────────────────────────────────────────────────────────────────────────────────

def test_applied_by_conftest():
    """237 really applied to the shared test DB (conftest swallows migration errors)."""
    conn = _connect()
    try:
        c = conn.cursor()
        c.execute("SELECT 1 FROM shared.schema_versions WHERE version = '237'")
        assert c.fetchone(), "237 did not apply in the test DB"
        assert set(_policies(c)) == {"tenant_isolation"}
    finally:
        conn.rollback()
        conn.close()


def test_policy_template(cur):
    cur.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = %s::regclass", (TABLE,))
    assert cur.fetchone() == (True, False), "expected ENABLE without FORCE (D2)"
    pols = _policies(cur)
    assert set(pols) == {"tenant_isolation"}
    roles, qual, with_check, cmd = pols["tenant_isolation"]
    assert roles == [TENANT_ROLE] and cmd == "ALL"
    for expr in (qual, with_check):
        assert expr and "nullif" in expr.lower() and "app.tenant_id" in expr, expr
        assert "current_tenant" not in expr


def test_tenant_role_grants(cur):
    privs = {}
    for priv in ("SELECT", "DELETE", "INSERT", "UPDATE", "TRUNCATE"):
        cur.execute("SELECT has_table_privilege(%s, %s, %s)", (TENANT_ROLE, TABLE, priv))
        privs[priv] = cur.fetchone()[0]
    assert privs == {"SELECT": True, "DELETE": True, "INSERT": False, "UPDATE": False, "TRUNCATE": False}
    cur.execute("SELECT has_schema_privilege(%s, 'shared', 'USAGE')", (TENANT_ROLE,))
    assert cur.fetchone()[0]


def test_owner_reads_every_row_under_foreign_tenant(cur, seeded):
    """The main pool (owner) is unchanged: exempt, every row under a forged GUC."""
    a, b = seeded
    cur.execute("SELECT set_config('app.tenant_id', %s, true)", (str(uuid.uuid4()),))
    assert _count_ours(cur, a, b) == 3


def test_rerun_is_idempotent(cur, seeded):
    a, b = seeded
    cur.execute(_body())
    cur.execute(_body())
    assert set(_policies(cur)) == {"tenant_isolation"}
    assert _count_ours(cur, a, b) == 3, "the guard's WHERE-less DELETE must not remove a row"


# ── guards ────────────────────────────────────────────────────────────────────────────────────────

def test_guard_refuses_force(cur):
    cur.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    with pytest.raises(psycopg2.errors.RaiseException, match="FORCE ROW LEVEL SECURITY"):
        cur.execute(_body())


def test_guard_refuses_unknown_policy(cur):
    """A live-only permissive policy would OR into the tenant's view."""
    cur.execute(f"CREATE POLICY zz237_extra ON {TABLE} USING (true)")
    with pytest.raises(psycopg2.errors.RaiseException, match="has policies"):
        cur.execute(_body())


def test_guard_refuses_other_grantee(cur):
    """Another reader with a grant would read 0 rows once RLS is on (the policy is TO aa_app_user only)."""
    cur.execute("CREATE ROLE zz237_reader")
    cur.execute(f"GRANT SELECT ON {TABLE} TO zz237_reader")
    with pytest.raises(psycopg2.errors.RaiseException, match="zz237_reader hold privileges"):
        cur.execute(_body())


def test_guard_refuses_truncate(cur):
    cur.execute(f"GRANT TRUNCATE ON {TABLE} TO {TENANT_ROLE}")
    with pytest.raises(psycopg2.errors.RaiseException, match="TRUNCATE"):
        cur.execute(_body())


# ── as an RDS-like runner (how DB Migrate applies it on Dev) ─────────────────────────────────────

def test_applies_as_rds_like_owner(cur, seeded):
    """Owner, NOSUPERUSER, NOBYPASSRLS, CREATEROLE, unable to SET ROLE aa_app_user, like aa_cis_admin:
    guards 1-5 run for real (guard 3 through the transaction-scoped membership, own-tenant count on
    tenant a), nothing is deleted, and the membership is gone afterwards."""
    a, b = seeded
    cur.execute("DELETE FROM shared.schema_versions WHERE version = '237'")
    # back to the pre-237 state on Dev: RLS off, no policy, SELECT only
    cur.execute(f"DROP POLICY tenant_isolation ON {TABLE}")
    cur.execute(f"ALTER TABLE {TABLE} DISABLE ROW LEVEL SECURITY")
    cur.execute(f"REVOKE DELETE ON {TABLE} FROM {TENANT_ROLE}")
    _as_rds_like_runner(cur)
    assert not _can_set_tenant_role(cur), "fixture: the runner must not be able to SET ROLE aa_app_user"
    cur.execute(_body())                       # raises on any guard failure
    assert not _can_set_tenant_role(cur), "the transaction-scoped membership was not revoked"
    cur.execute("SELECT 1 FROM shared.schema_versions WHERE version = '237'")
    assert cur.fetchone()
    assert _count_ours(cur, a, b) == 3, "the guard deleted rows"


def test_rds_like_non_owner_is_refused(cur):
    """A runner that does not own the table would be filtered by ENABLE alone: guard 1."""
    _as_rds_like_runner(cur, own_table=False)
    with pytest.raises(psycopg2.errors.RaiseException, match="neither owns shared.tenant_integrations"):
        cur.execute(_body())
