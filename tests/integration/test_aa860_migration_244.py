"""AA-860 (X1 step S16) — migration 244 (cis_app runtime grants, default privileges, parity policies) applied the
way DB Migrate applies it on Dev.

Lesson S236 / test_aa852_migration_243: conftest applies migrations as a SUPERUSER, which skips every
privilege check and is never filtered by RLS. On Dev the file runs as the RDS master aa_cis_admin: a plain
role that owns every CIS object, is a member of cis_app (rl-001) and reads the FORCE-RLS tables unfiltered
because it is in rds_superuser (AA-817 probe v2). The module fixture therefore:
  * brings the CI DB to Dev's RLS shape (conftest.x1_244_ci_shape; the conftest replay skips 244);
  * turns the CI role aa_cis_admin into that master: LOGIN, NOSUPERUSER, BYPASSRLS (the stand-in for
    rds_superuser's unfiltered reads, as test_aa817_migration_239 does), member of cis_app, owner of the 11
    CIS schemas and everything in them;
  * models the Dev order (S243: 244 is applied AFTER 247/248/249): objects the master created before 244,
    then 244 applied twice (idempotent, errors NOT swallowed), then objects the master creates after it, and
    one object a superuser creates after it (the CI replay order, outside the default privileges);
  * seeds two tenants' rows in a FORCE table and in tenant_tour_versions (ENABLE-only), so parity is
    measured on real rows;
and gives everything back at teardown, 244 included (conftest.x1_244_undo).
"""
import os
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import (  # noqa: E402
    CIS_APP_CI_PASSWORD, DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER, TENANT_ID, X1_CIS_SCHEMAS,
    X1_ENABLE_ONLY_RLS, X1_FORCE_RLS, x1_244_ci_shape, x1_244_ci_unshape, x1_244_undo,
)

RUNNER = "aa_cis_admin"
MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "244_x1_cis_app_runtime.sql")
POLICY_TABLES = X1_ENABLE_ONLY_RLS + X1_FORCE_RLS
OTHER_TENANT = "00000000-0000-0000-0000-000000000099"
SEED_RUNS = (str(uuid.uuid4()), str(uuid.uuid4()))
REL_KIND_DDL = {"r": "TABLE", "p": "TABLE", "v": "VIEW", "m": "MATERIALIZED VIEW", "S": "SEQUENCE",
                "f": "FOREIGN TABLE"}


def _connect(user=DB_USER, password=DB_PASS):
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=user, password=password)


def _sql():
    return open(MIGRATION, encoding="utf-8").read()


def _apply_as_runner(sql=None):
    runner = _connect(RUNNER)
    runner.autocommit = True
    try:
        runner.cursor().execute(sql or _sql())        # raises on any error
    finally:
        runner.close()


def _hand_to_runner(a):
    """Dev: the master owns the 11 CIS schemas and every object in them (schema reference 18/10)."""
    for s in X1_CIS_SCHEMAS:
        a.execute(f"ALTER SCHEMA {s} OWNER TO {RUNNER}")
    a.execute("""
        SELECT c.relkind, c.oid::regclass::text
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = ANY(%s) AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
           AND NOT EXISTS (SELECT 1 FROM pg_depend d          -- sequences owned by a column follow their table
                            WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype IN ('a', 'i'))
           AND pg_get_userbyid(c.relowner) <> %s
         ORDER BY c.relkind DESC""", (list(X1_CIS_SCHEMAS), RUNNER))
    for kind, name in a.fetchall():
        a.execute(f"ALTER {REL_KIND_DDL[kind]} {name} OWNER TO {RUNNER}")
    a.execute("""SELECT p.oid::regprocedure::text, p.prokind
                   FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                  WHERE n.nspname = ANY(%s) AND pg_get_userbyid(p.proowner) <> %s""", (list(X1_CIS_SCHEMAS), RUNNER))
    for fn, kind in a.fetchall():
        a.execute(f"ALTER {'PROCEDURE' if kind == 'p' else 'FUNCTION'} {fn} OWNER TO {RUNNER}")


def _cross_app_standins(a):
    """booking.customers / tripplanner.sessions exist on Dev (bk_owner / tp_owner); the CI DB has neither."""
    created = []
    for schema, table in (("booking", "customers"), ("tripplanner", "sessions")):
        a.execute("SELECT to_regnamespace(%s) IS NULL", (schema,))
        if a.fetchone()[0]:
            a.execute(f"CREATE SCHEMA {schema}; CREATE TABLE {schema}.{table} (id uuid PRIMARY KEY); "
                      f"INSERT INTO {schema}.{table} VALUES (gen_random_uuid())")
            created.append(schema)
    return created


def _seed(a):
    for run_id, tenant in zip(SEED_RUNS, (TENANT_ID, OTHER_TENANT)):
        a.execute("""INSERT INTO shared.pipeline_runs (id, tenant_id, batch_id, status, tours_total, tours_passed,
                         tours_hitl, tours_failed, started_at)
                     VALUES (%s, %s, %s, 'completed', 0, 0, 0, 0, now())""", (run_id, tenant, run_id))
        a.execute("INSERT INTO gold_aa_internal.tenant_tour_versions (tenant_id) VALUES (%s)", (tenant,))


@pytest.fixture(scope="module", autouse=True)
def rds_like_244():
    admin = _connect()
    admin.autocommit = True
    a = admin.cursor()
    created_schemas = []
    try:
        x1_244_undo(a)
        x1_244_ci_shape(a)
        created_schemas = _cross_app_standins(a)
        _seed(a)
        a.execute(f"ALTER ROLE {RUNNER} LOGIN PASSWORD %s NOSUPERUSER BYPASSRLS INHERIT", (DB_PASS,))
        a.execute(f"GRANT cis_app TO {RUNNER}")                 # rl-001
        _hand_to_runner(a)
        # Dev order: what 247/248/249 create before 244 is applied.
        _apply_as_runner("""
            CREATE TABLE acp_shared.zz860_pre (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, v text);
            CREATE SEQUENCE shared.zz860_pre_seq;
            CREATE FUNCTION acp_shared.zz860_pre_fn() RETURNS int LANGUAGE sql AS 'SELECT 1';
            CREATE FUNCTION acp_shared.zz860_pre_trg() RETURNS trigger LANGUAGE plpgsql AS 'BEGIN RETURN NEW; END';
        """)
        _apply_as_runner()
        _apply_as_runner()                                     # idempotent re-run
        # After 244: the master's later objects (default privileges). A superuser's (the CI replay order) is
        # created inside its own test: 244 cannot be re-run while a table the master cannot grant on exists.
        _apply_as_runner("""
            CREATE TABLE acp_shared.zz860_post (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, v text);
            CREATE FUNCTION geo.zz860_post_fn() RETURNS int LANGUAGE sql AS 'SELECT 2';
        """)
        yield
    finally:
        a.execute("DROP TABLE IF EXISTS acp_shared.zz860_pre, acp_shared.zz860_post, acp_shared.zz860_superuser, "
                  "acp_shared.zz860_new_rls; DROP SEQUENCE IF EXISTS shared.zz860_pre_seq; "
                  "DROP FUNCTION IF EXISTS acp_shared.zz860_pre_fn(), acp_shared.zz860_pre_trg(), geo.zz860_post_fn()")
        a.execute("DELETE FROM shared.pipeline_runs WHERE id = ANY(%s::uuid[])", (list(SEED_RUNS),))
        a.execute(f"REASSIGN OWNED BY {RUNNER} TO CURRENT_USER")
        a.execute(f"REVOKE cis_app FROM {RUNNER}")
        a.execute(f"ALTER ROLE {RUNNER} NOLOGIN NOBYPASSRLS PASSWORD NULL")
        x1_244_undo(a)                                         # conftest skips 244: leave the replay state
        x1_244_ci_unshape(a)
        for schema in created_schemas:
            a.execute(f"DROP SCHEMA {schema} CASCADE")
        admin.close()


@pytest.fixture
def cur():
    c = _connect()
    c.autocommit = False
    try:
        yield c.cursor()
    finally:
        c.rollback()
        c.close()


def _count(c, table, guc=None):
    c.execute("SELECT set_config('app.tenant_id', %s, true)", (guc or "",))
    c.execute(f"SELECT count(*) FROM {table}")
    return c.fetchone()[0]


# ── shape ───────────────────────────────────────────────────────────────────────────────────────────

def test_registered_once(cur):
    cur.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '244'")
    assert cur.fetchone()[0] == 1


def test_parity_policy_on_each_of_the_16_rls_tables(cur):
    cur.execute("""SELECT schemaname || '.' || tablename, policyname, permissive, roles::text[], cmd, qual, with_check
                     FROM pg_policies WHERE 'cis_app' = ANY(roles) ORDER BY 1""")
    rows = cur.fetchall()
    assert [r[0] for r in rows] == sorted(POLICY_TABLES)
    for table, name, permissive, roles, cmd, qual, with_check in rows:
        assert (name, permissive, roles, cmd, qual, with_check) == (
            f"{table.split('.')[1]}_cis_app_all", "PERMISSIVE", ["cis_app"], "ALL", "true", "true"), table


def test_cis_app_is_a_plain_runtime_role(cur):
    cur.execute("SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb FROM pg_roles "
                "WHERE rolname = 'cis_app'")
    assert cur.fetchone() == (True, False, False, False, False)
    cur.execute("SELECT count(*) FROM pg_auth_members WHERE member = 'cis_app'::regrole")
    assert cur.fetchone()[0] == 0
    for schema in X1_CIS_SCHEMAS:
        cur.execute("SELECT has_schema_privilege('cis_app', %s, 'USAGE')", (schema,))
        assert cur.fetchone()[0] is True, schema
    for schema in ("booking", "tripplanner", "catalog"):
        cur.execute("SELECT has_schema_privilege('cis_app', %s, 'USAGE')", (schema,))
        assert cur.fetchone()[0] is False, schema


def test_registry_is_read_only_for_cis_app(cur):
    cur.execute("SELECT has_table_privilege('cis_app', 'shared.schema_versions', p) "
                "FROM unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE']) p")
    assert [r[0] for r in cur.fetchall()] == [True, False, False, False, False]


# ── Dev order (S243): 244 after 247/248/249 ─────────────────────────────────────────────────────────

def _table_privs(c, table):
    c.execute("SELECT array_agg(p ORDER BY p) FROM unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']) p "
              "WHERE has_table_privilege('cis_app', %s, p)", (table,))
    return c.fetchone()[0] or []


def test_objects_created_before_244_are_granted(cur):
    """GRANT … ON ALL covers what 247/248/249 created on Dev before 244 is applied."""
    assert _table_privs(cur, "acp_shared.zz860_pre") == ["DELETE", "INSERT", "SELECT", "UPDATE"]
    cur.execute("SELECT has_sequence_privilege('cis_app', pg_get_serial_sequence('acp_shared.zz860_pre', 'id'), "
                "'USAGE'), has_sequence_privilege('cis_app', 'shared.zz860_pre_seq', 'USAGE'), "
                "has_function_privilege('cis_app', 'acp_shared.zz860_pre_fn()', 'EXECUTE')")
    assert cur.fetchone() == (True, True, True)


def test_trigger_functions_get_no_execute_grant(cur):
    cur.execute("""SELECT p.oid::regprocedure::text FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace,
                          LATERAL aclexplode(p.proacl) x
                    WHERE n.nspname = ANY(%s) AND p.prorettype = 'trigger'::regtype
                      AND x.grantee = 'cis_app'::regrole""", (list(X1_CIS_SCHEMAS),))
    assert cur.fetchall() == []
    cur.execute("SELECT count(*) FROM pg_proc WHERE oid = 'acp_shared.zz860_pre_trg()'::regprocedure")
    assert cur.fetchone()[0] == 1                       # the fixture's pre-244 trigger function was there


def test_objects_the_master_creates_after_244_get_default_privileges(cur):
    assert _table_privs(cur, "acp_shared.zz860_post") == ["DELETE", "INSERT", "SELECT", "UPDATE"]
    cur.execute("SELECT has_sequence_privilege('cis_app', pg_get_serial_sequence('acp_shared.zz860_post', 'id'), "
                "'USAGE'), has_function_privilege('cis_app', 'geo.zz860_post_fn()', 'EXECUTE')")
    assert cur.fetchone() == (True, True)


def test_ci_replay_order_is_outside_the_default_privileges(cur):
    """In CI the superuser replays 247+ after 244: default privileges FOR ROLE aa_cis_admin do not reach those
    objects (unlike Dev). This is why test_x1_cis_app_privileges re-applies 244 after the full replay."""
    cur.execute("CREATE TABLE acp_shared.zz860_superuser (id int)")        # rolled back with the test
    assert _table_privs(cur, "acp_shared.zz860_superuser") == []


def test_default_acl_rows_match_owners_yaml(cur):
    cur.execute("""SELECT n.nspname, d.defaclobjtype::text, array_agg(x.privilege_type ORDER BY x.privilege_type)
                     FROM pg_default_acl d JOIN pg_namespace n ON n.oid = d.defaclnamespace,
                          LATERAL aclexplode(d.defaclacl) x
                    WHERE d.defaclrole = %s::regrole AND x.grantee = 'cis_app'::regrole
                    GROUP BY 1, 2 ORDER BY 1, 2""", (RUNNER,))
    expected = []
    for s in sorted(X1_CIS_SCHEMAS):
        expected += [(s, "S", ["SELECT", "UPDATE", "USAGE"]), (s, "f", ["EXECUTE"]),
                     (s, "r", ["DELETE", "INSERT", "SELECT", "UPDATE"])]
    assert cur.fetchall() == expected


# ── behaviour as cis_app ────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("table", X1_FORCE_RLS + ("gold_aa_internal.tenant_tour_versions",))
def test_parity_with_the_master(table):
    """Same row counts for the master (owner, unfiltered) and cis_app (parity policy), without a GUC and under
    a forged foreign tenant."""
    forged = str(uuid.uuid4())
    counts = {}
    for user, password in ((RUNNER, DB_PASS), ("cis_app", CIS_APP_CI_PASSWORD)):
        c = _connect(user, password)
        try:
            k = c.cursor()
            counts[user] = (_count(k, table), _count(k, table, forged))
        finally:
            c.rollback()
            c.close()
    assert counts["cis_app"] == counts[RUNNER]
    if table in ("shared.pipeline_runs", "gold_aa_internal.tenant_tour_versions"):
        assert counts["cis_app"][0] >= 2                # the two seeded tenants are both visible


def test_cis_app_writes_rls_tables_and_jobs():
    c = _connect("cis_app", CIS_APP_CI_PASSWORD)
    try:
        k = c.cursor()
        k.execute("INSERT INTO gold_aa_internal.tenant_tour_versions (tenant_id) VALUES (%s) RETURNING id",
                  (OTHER_TENANT,))                    # ENABLE-only: WITH CHECK (true), no GUC
        assert k.fetchone()
        k.execute("UPDATE shared.pipeline_runs SET status = status WHERE id = %s", (SEED_RUNS[1],))
        assert k.rowcount == 1                          # FORCE: a foreign tenant's row, no GUC
        k.execute("UPDATE shared.job SET status = status WHERE false")
    finally:
        c.rollback()
        c.close()


@pytest.mark.parametrize("sql", [
    "INSERT INTO shared.schema_versions (version, applied_at, description) VALUES ('zz860', now(), 'x')",
    "DELETE FROM shared.schema_versions WHERE version = '244'",
    "SELECT 1 FROM booking.customers",
    "SELECT 1 FROM tripplanner.sessions",
])
def test_cis_app_refused(sql):
    c = _connect("cis_app", CIS_APP_CI_PASSWORD)
    try:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            c.cursor().execute(sql)
    finally:
        c.rollback()
        c.close()


# ── the guards ──────────────────────────────────────────────────────────────────────────────────────

def _apply_expecting(match):
    runner = _connect(RUNNER)
    runner.autocommit = True
    try:
        with pytest.raises(psycopg2.errors.RaiseException, match=match):
            runner.cursor().execute(_sql())
        runner.cursor().execute("ROLLBACK")
    finally:
        runner.close()


def test_guard_parity_refuses_a_filtered_master():
    """The plan's original premise (FORCE filters the master): then cis_app's parity policy would CHANGE what
    the runtime sees, and the guard refuses."""
    admin = _connect()
    admin.autocommit = True
    try:
        admin.cursor().execute(f"ALTER ROLE {RUNNER} NOBYPASSRLS")
        _apply_expecting(r"migration 244: parity: cis_app reads \d+ row\(s\) of \S+ under a foreign app.tenant_id")
    finally:
        admin.cursor().execute(f"ALTER ROLE {RUNNER} BYPASSRLS")
        admin.close()


def test_guard_refuses_a_new_rls_table():
    _apply_as_runner("CREATE TABLE acp_shared.zz860_new_rls (id int); "
                     "ALTER TABLE acp_shared.zz860_new_rls ENABLE ROW LEVEL SECURITY")
    try:
        _apply_expecting(r"migration 244: ENABLE-only RLS tables are .*acp_shared.zz860_new_rls")
    finally:
        _apply_as_runner("DROP TABLE acp_shared.zz860_new_rls")


def test_guard_refuses_a_membership_of_cis_app():
    admin = _connect()
    admin.autocommit = True
    try:
        admin.cursor().execute("GRANT aa_app_user TO cis_app")
        _apply_expecting(r"migration 244: cis_app is a member of aa_app_user")
    finally:
        admin.cursor().execute("REVOKE aa_app_user FROM cis_app")
        admin.close()
