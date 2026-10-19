"""AA-860 (X1 S16) — what the CIS runtime can do once it logs in as cis_app (X1 S18), checked by logging in AS
cis_app (CI-only password from conftest), plus the standing check every later migration must keep green.

Dev order (S242 amendment, S243): on Dev, 244 is applied after 247/248/249, so its GRANT … ON ALL statements
cover their objects. The conftest replay skips 244 (its parity policies would trip the exact-policy guards that
the 229/237/239/241 tests re-apply); this module applies it after the full replay, the Dev order, and removes it
at teardown. It runs the file up to its registration (grants, default privileges, policies) but not its guards:
244's exact RLS-set guard is a one-time check at apply time (tested in test_aa860_migration_244) and would refuse
any later, correctly-policied RLS table here.

Standing check (plan S15): every RLS table (ENABLE-only or FORCE) in the 11 CIS runtime schemas has a
permissive FOR ALL policy TO cis_app with USING (true) WITH CHECK (true). A later migration that adds an RLS
table must add that policy in the same file (or a decided, tenant-aware one), or the runtime reads 0 rows from
it after S18. owners.yaml rls_enable_only_allowlist is empty: no exception.
"""
import os
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import (  # noqa: E402
    CIS_APP_CI_PASSWORD, DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER, TENANT_ID, X1_CI_ONLY_LEGACY_RLS,
    X1_CIS_SCHEMAS, x1_244_ci_shape, x1_244_ci_unshape, x1_244_undo,
)

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "244_x1_cis_app_runtime.sql")
OTHER_TENANT = "00000000-0000-0000-0000-000000000099"
SEED_RUNS = (str(uuid.uuid4()), str(uuid.uuid4()))
SEED_FACTS = tuple(f"zz860_{uuid.uuid4().hex[:10]}" for _ in range(3))


def _body_without_guards():
    """244 from its first statement to its registration (grants, default privileges, measurement, policies),
    in one transaction; the guards after the registration are left out (see the module docstring)."""
    sql = open(MIGRATION, encoding="utf-8").read()
    start = sql.index("-- ── Guard 0")
    end = sql.index("-- ── 4. Register")
    return "BEGIN;\n" + sql[start:end] + "\nCOMMIT;\n"


def _connect(user=DB_USER, password=DB_PASS):
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=user, password=password)


@pytest.fixture(scope="module", autouse=True)
def dev_order_244():
    admin = _connect()
    admin.autocommit = True
    a = admin.cursor()
    created = []
    try:
        x1_244_ci_shape(a)
        a.execute(_body_without_guards())                          # after the full replay = the Dev order
        a.execute("INSERT INTO shared.schema_versions (version, applied_at, description) "
                  "VALUES ('244', now(), 'test_x1_cis_app_privileges') ON CONFLICT (version) DO NOTHING")
        x1_244_ci_unshape(a)
        for schema, table in (("booking", "customers"), ("tripplanner", "sessions")):
            a.execute("SELECT to_regnamespace(%s) IS NULL", (schema,))
            if a.fetchone()[0]:
                a.execute(f"CREATE SCHEMA {schema}; CREATE TABLE {schema}.{table} (id uuid PRIMARY KEY)")
                created.append(schema)
        for run_id, tenant in zip(SEED_RUNS, (TENANT_ID, OTHER_TENANT)):
            a.execute("""INSERT INTO shared.pipeline_runs (id, tenant_id, batch_id, status, tours_total, tours_passed,
                             tours_hitl, tours_failed, started_at)
                         VALUES (%s, %s, %s, 'completed', 0, 0, 0, 0, now())""", (run_id, tenant, run_id))
        for fact_id, tenant in zip(SEED_FACTS, (TENANT_ID, OTHER_TENANT, None)):
            a.execute("INSERT INTO acp_shared.facts (fact_id, scope, tenant_id, title, body, provenance) "
                      "VALUES (%s, %s, %s, 't', 'b', 'aa860-test')",
                      (fact_id, "tenant" if tenant else "platform", tenant))
        yield
    finally:
        a.execute("DELETE FROM shared.pipeline_runs WHERE id = ANY(%s::uuid[])", (list(SEED_RUNS),))
        a.execute("DELETE FROM acp_shared.facts WHERE fact_id = ANY(%s)", (list(SEED_FACTS),))
        for schema in created:
            a.execute(f"DROP SCHEMA {schema} CASCADE")
        x1_244_undo(a)                                             # conftest skips 244: leave the replay state
        admin.close()


@pytest.fixture
def app():
    """A cursor logged in AS cis_app, in a transaction rolled back after the test."""
    c = _connect("cis_app", CIS_APP_CI_PASSWORD)
    try:
        yield c.cursor()
    finally:
        c.rollback()
        c.close()


@pytest.fixture
def su():
    c = _connect()
    try:
        yield c.cursor()
    finally:
        c.rollback()
        c.close()


def _relations(su, kinds):
    su.execute("""SELECT c.oid::regclass::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname = ANY(%s) AND c.relkind = ANY(%s) ORDER BY 1""", (list(X1_CIS_SCHEMAS), list(kinds)))
    return [r[0] for r in su.fetchall()]


def _rls_tables(su):
    """RLS tables of the 11 schemas, minus the CI-only legacy tables that do not exist on Dev."""
    su.execute("""SELECT c.oid::regclass::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname = ANY(%s) AND c.relrowsecurity ORDER BY 1""", (list(X1_CIS_SCHEMAS),))
    return [r[0] for r in su.fetchall() if r[0] not in X1_CI_ONLY_LEGACY_RLS]


def test_logged_in_as_cis_app(app):
    app.execute("SELECT session_user, current_user")
    assert app.fetchone() == ("cis_app", "cis_app")


def test_every_cis_relation_is_readable(app, su):
    refused = []
    for rel in _relations(su, ("r", "p", "v", "m")):
        app.execute("SAVEPOINT r")
        try:
            app.execute(f"SELECT 1 FROM {rel} LIMIT 0")
        except psycopg2.errors.InsufficientPrivilege:
            refused.append(rel)
        app.execute("ROLLBACK TO SAVEPOINT r")
    assert refused == []


def test_standing_every_rls_table_has_a_cis_app_parity_policy(su):
    su.execute("""SELECT c.oid::regclass::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname = ANY(%s) AND c.relrowsecurity
                     AND NOT EXISTS (SELECT 1 FROM pg_policy p
                                      WHERE p.polrelid = c.oid AND p.polpermissive AND p.polcmd = '*'
                                        AND 'cis_app'::regrole::oid = ANY (p.polroles)
                                        AND pg_get_expr(p.polqual, p.polrelid) = 'true'
                                        AND pg_get_expr(p.polwithcheck, p.polrelid) = 'true')
                   ORDER BY 1""", (list(X1_CIS_SCHEMAS),))
    missing = [r[0] for r in su.fetchall() if r[0] not in X1_CI_ONLY_LEGACY_RLS]   # CI-only, absent on Dev
    assert missing == [], ("RLS table(s) without a permissive FOR ALL USING (true) WITH CHECK (true) policy TO "
                           f"cis_app: {missing}. After X1 S18 the CIS runtime reads 0 rows there.")


def _count(cur, table, guc):
    cur.execute("SELECT set_config('app.tenant_id', %s, true)", (guc,))
    cur.execute(f"SELECT count(*) FROM {table}")
    return cur.fetchone()[0]


def test_rls_tables_parity_without_and_under_a_forged_guc(app, su):
    """cis_app reads what the platform runtime reads today (the superuser here, the master on Dev): every
    tenant's rows, whatever app.tenant_id says."""
    forged = str(uuid.uuid4())
    diff = {}
    for table in _rls_tables(su):
        for guc in ("", forged):
            expected, got = _count(su, table, guc), _count(app, table, guc)
            if got != expected:
                diff[f"{table} guc={guc or 'none'}"] = (got, expected)
    assert diff == {}
    assert _count(app, "shared.pipeline_runs", forged) >= 2            # seeded FORCE rows, two tenants
    assert _count(app, "acp_shared.facts", forged) >= 3                # seeded ENABLE-only rows, two tenants + platform


def test_cis_app_writes(app):
    app.execute("INSERT INTO acp_shared.facts (fact_id, scope, tenant_id, title, body, provenance) "
                "VALUES (%s, 'tenant', %s, 't', 'b', 'aa860-test')", (f"zz860_w_{uuid.uuid4().hex[:8]}", OTHER_TENANT))
    app.execute("UPDATE shared.pipeline_runs SET status = status WHERE id = %s", (SEED_RUNS[1],))
    assert app.rowcount == 1
    app.execute("INSERT INTO shared.job (kind, payload) VALUES ('zz860_privilege_check', '{}'::jsonb) RETURNING id")
    job_id = app.fetchone()[0]
    app.execute("UPDATE shared.job SET status = status WHERE id = %s", (job_id,))
    app.execute("DELETE FROM shared.job WHERE id = %s", (job_id,))
    assert app.rowcount == 1


@pytest.mark.parametrize("sql", [
    "INSERT INTO shared.schema_versions (version, applied_at, description) VALUES ('zz860', now(), 'x')",
    "DELETE FROM shared.schema_versions WHERE version = '244'",
    "UPDATE shared.schema_versions SET description = description WHERE version = '244'",
    "SELECT 1 FROM booking.customers",
    "SELECT 1 FROM tripplanner.sessions",
    "CREATE TABLE shared.zz860_ddl (id int)",
])
def test_cis_app_refused(app, sql):
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        app.execute(sql)


def test_cis_app_reads_the_registry(app):
    app.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '244'")
    assert app.fetchone()[0] == 1
