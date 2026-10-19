"""AA-860 (X1 step S21) — migration 245 (CIS schemas and objects to cis_owner, cis_owner policies on the FORCE
tables, default privileges FOR ROLE cis_owner) applied the way DB Migrate applies it on Dev.

Lesson S236 / test_aa860_migration_244: conftest applies migrations as a SUPERUSER, which skips every privilege
check and is never filtered by RLS, and it skips 244 and 245. On Dev, 245 runs as the RDS master aa_cis_admin: a plain
role that owns every CIS object, is a member of cis_app (rl-001) and of cis_owner (rl-002), reads the FORCE RLS
tables unfiltered (rds_superuser; BYPASSRLS here, as in the 244 test) and reads the catalog_owner views through
pg_read_all_data (rds_superuser). cis_owner is a plain NOLOGIN role with CREATE on the database (rl-002). The module
fixture:
  * brings the CI DB to Dev's RLS shape (conftest.x1_244_ci_shape) and builds the cross-app stand-ins;
  * turns aa_cis_admin into that master and hands it the 12 CIS schemas and everything in the 11 runtime schemas
    (relations, routines, enum types); the catalog.* views stay catalog_owner's;
  * creates, as the master, objects Dev has before 245 (a view over a FORCE table, a standalone sequence, an enum,
    a function), applies 244, then 245 twice (idempotent as the master, errors NOT swallowed);
  * creates objects as cis_owner the way DB Migrate runs every later cis/ file (session user = the master,
    SET ROLE cis_owner, plan XD8): the default privileges must reach cis_app;
and gives everything back at teardown (conftest.x1_245_undo, x1_244_undo).
"""
import collections
import os
import re
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import (  # noqa: E402
    CIS_APP_CI_PASSWORD, DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER, TENANT_ID, X1_CIS_SCHEMAS, X1_FORCE_RLS,
    x1_244_ci_shape, x1_244_ci_unshape, x1_244_undo, x1_245_undo,
)

RUNNER = "aa_cis_admin"
HERE = os.path.dirname(__file__)
MIGRATION_244 = os.path.join(HERE, "..", "..", "api", "migrations", "244_x1_cis_app_runtime.sql")
MIGRATION = os.path.join(HERE, "..", "..", "api", "migrations", "245_x1_cis_owner.sql")
ALL_SCHEMAS = tuple(sorted(X1_CIS_SCHEMAS + ("catalog",)))
OTHER_TENANT = "00000000-0000-0000-0000-000000000099"
SEED_RUNS = (str(uuid.uuid4()), str(uuid.uuid4()))
PARITY_TABLES = X1_FORCE_RLS + ("gold_aa_internal.tenant_tour_versions",)
REL_KIND_DDL = {"r": "TABLE", "p": "TABLE", "v": "VIEW", "m": "MATERIALIZED VIEW", "S": "SEQUENCE",
                "f": "FOREIGN TABLE", "c": "TYPE"}
ZZ_DROP = ("DROP VIEW IF EXISTS shared.zz245_v_runs; DROP TABLE IF EXISTS acp_shared.zz245_post, "
           "acp_shared.zz245_new_force; "
           "DROP SEQUENCE IF EXISTS shared.zz245_pre_seq, acp_shared.zz245_third_owner; "
           "DROP TYPE IF EXISTS acp_shared.zz245_pre_enum; "
           "DROP FUNCTION IF EXISTS acp_shared.zz245_pre_fn(), geo.zz245_post_fn(), acp_shared.zz245_definer()")


def _connect(user=DB_USER, password=DB_PASS):
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=user, password=password)


def _sql(path=MIGRATION):
    return open(path, encoding="utf-8").read()


def _apply_as_runner(sql=None, as_cis_owner=False):
    """Runs sql (default: 245) logged in as the master; as_cis_owner = DB Migrate's live-owner switch (XD8)."""
    runner = _connect(RUNNER)
    runner.autocommit = True
    runner.notices = collections.deque()               # unbounded: 245 logs one NOTICE per object it moves
    try:
        cur = runner.cursor()
        if as_cis_owner:
            cur.execute("SET ROLE cis_owner")
        cur.execute(sql or _sql())                    # raises on any error
        return list(runner.notices)
    finally:
        runner.close()


def _hand_to_runner(a):
    """Dev before 245: the master owns the 12 CIS schemas and every object of the 11 runtime schemas (schema
    reference 19/10); the catalog.* views belong to catalog_owner (CIS 225, already so in the replay)."""
    for s in ALL_SCHEMAS:
        a.execute(f"ALTER SCHEMA {s} OWNER TO {RUNNER}")
    a.execute("""
        SELECT c.relkind, quote_ident(n.nspname) || '.' || quote_ident(c.relname)
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = ANY(%s) AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f', 'c')
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
    a.execute("""SELECT quote_ident(n.nspname) || '.' || quote_ident(t.typname), t.typtype
                   FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
                  WHERE n.nspname = ANY(%s) AND t.typtype IN ('e', 'd', 'r')
                    AND pg_get_userbyid(t.typowner) <> %s""", (list(X1_CIS_SCHEMAS), RUNNER))
    for name, typtype in a.fetchall():
        a.execute(f"ALTER {'DOMAIN' if typtype == 'd' else 'TYPE'} {name} OWNER TO {RUNNER}")


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


def _rl002(a, grant=True):
    """What the Infra role bootstrap rl-002 (X1 S20) gives on Dev, plus the master's rds_superuser reads."""
    if grant:
        a.execute(f"GRANT CREATE ON DATABASE {DB_NAME} TO cis_owner")
        a.execute(f"GRANT cis_app, cis_owner, pg_read_all_data TO {RUNNER}")
    else:
        a.execute(f"REVOKE cis_app, cis_owner, pg_read_all_data FROM {RUNNER}")


@pytest.fixture(scope="module", autouse=True)
def rds_like_245():
    admin = _connect()
    admin.autocommit = True
    a = admin.cursor()
    created_schemas = []
    try:
        a.execute(ZZ_DROP)
        x1_245_undo(a)
        x1_244_undo(a)
        x1_244_ci_shape(a)
        created_schemas = _cross_app_standins(a)
        _seed(a)
        a.execute(f"ALTER ROLE {RUNNER} LOGIN PASSWORD %s NOSUPERUSER BYPASSRLS INHERIT", (DB_PASS,))
        _rl002(a)
        _hand_to_runner(a)
        # Dev before 245: a view over a FORCE table (like v_active_tour_atoms over published_tours), a standalone
        # sequence, an enum and a function, all the master's.
        _apply_as_runner("""
            CREATE VIEW shared.zz245_v_runs AS SELECT id, tenant_id FROM shared.pipeline_runs;
            CREATE SEQUENCE shared.zz245_pre_seq;
            CREATE TYPE acp_shared.zz245_pre_enum AS ENUM ('a', 'b');
            CREATE FUNCTION acp_shared.zz245_pre_fn() RETURNS int LANGUAGE sql AS 'SELECT 1';
        """)
        _apply_as_runner(_sql(MIGRATION_244))                 # Dev order: 244 (after 247-250), then 245
        _apply_as_runner()
        _apply_as_runner()                                     # idempotent re-run as the master
        # Every later cis/ file: DB Migrate logs in as the master (db_migrator from S22) and SET ROLE cis_owner.
        _apply_as_runner("""
            CREATE TABLE acp_shared.zz245_post (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, v text);
            CREATE FUNCTION geo.zz245_post_fn() RETURNS int LANGUAGE sql AS 'SELECT 2';
        """, as_cis_owner=True)
        yield
    finally:
        a.execute(ZZ_DROP)
        a.execute("DELETE FROM shared.pipeline_runs WHERE id = ANY(%s::uuid[])", (list(SEED_RUNS),))
        x1_245_undo(a)
        a.execute(f"REASSIGN OWNED BY {RUNNER} TO CURRENT_USER")
        _rl002(a, grant=False)
        a.execute(f"ALTER ROLE {RUNNER} NOLOGIN NOBYPASSRLS PASSWORD NULL")
        x1_244_undo(a)                                         # conftest skips 244 and 245: leave the replay state
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
    cur.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '245'")
    assert cur.fetchone()[0] == 1


def test_every_cis_object_belongs_to_cis_owner(cur):
    cur.execute("""
        WITH s AS (SELECT oid, nspname, nspowner FROM pg_namespace WHERE nspname = ANY(%(schemas)s))
        SELECT kind, name, owner FROM (
            SELECT 'schema' AS kind, nspname AS name, pg_get_userbyid(nspowner) AS owner, false AS cat FROM s
            UNION ALL
            SELECT 'relation', s.nspname || '.' || c.relname, pg_get_userbyid(c.relowner), s.nspname = 'catalog'
              FROM pg_class c JOIN s ON s.oid = c.relnamespace WHERE c.relkind NOT IN ('i', 'I')
            UNION ALL
            SELECT 'routine', s.nspname || '.' || p.proname, pg_get_userbyid(p.proowner), false
              FROM pg_proc p JOIN s ON s.oid = p.pronamespace
            UNION ALL
            SELECT 'type', s.nspname || '.' || t.typname, pg_get_userbyid(t.typowner), false
              FROM pg_type t JOIN s ON s.oid = t.typnamespace WHERE t.typrelid = 0 AND t.typcategory <> 'A') o
         WHERE owner <> CASE WHEN cat THEN 'catalog_owner' ELSE 'cis_owner' END
         ORDER BY 1, 2""", {"schemas": list(ALL_SCHEMAS)})
    assert cur.fetchall() == []
    rel = "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = '{}'::regclass"
    for name, sql in (("view", rel.format("shared.zz245_v_runs")),
                      ("sequence", rel.format("shared.zz245_pre_seq")),
                      ("enum", "SELECT pg_get_userbyid(typowner) FROM pg_type "
                               "WHERE oid = 'acp_shared.zz245_pre_enum'::regtype"),
                      ("function", "SELECT pg_get_userbyid(proowner) FROM pg_proc "
                                   "WHERE oid = 'acp_shared.zz245_pre_fn()'::regprocedure")):
        cur.execute(sql)
        assert cur.fetchone()[0] == "cis_owner", name


def test_catalog_views_stay_with_catalog_owner(cur):
    cur.execute("SELECT DISTINCT pg_get_userbyid(c.relowner) FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'catalog' AND c.relkind = 'v'")
    assert [r[0] for r in cur.fetchall()] == ["catalog_owner"]
    cur.execute("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'catalog'")
    assert cur.fetchone()[0] == "cis_owner"
    cur.execute("SELECT pg_has_role(%s, 'catalog_owner', 'MEMBER'), "
                "pg_has_role('cis_owner', 'catalog_owner', 'MEMBER')", (RUNNER,))
    assert cur.fetchone() == (False, False)


def test_default_privileges_are_for_cis_owner_only(cur):
    cur.execute("""SELECT pg_get_userbyid(d.defaclrole), n.nspname, d.defaclobjtype::text,
                          array_agg(x.privilege_type ORDER BY x.privilege_type), x.grantee::regrole::text
                     FROM pg_default_acl d JOIN pg_namespace n ON n.oid = d.defaclnamespace,
                          LATERAL aclexplode(d.defaclacl) x
                    WHERE n.nspname = ANY(%s)
                    GROUP BY 1, 2, 3, 5""", (list(ALL_SCHEMAS),))
    expected = []
    for s in X1_CIS_SCHEMAS:
        expected += [("cis_owner", s, "S", ["SELECT", "UPDATE", "USAGE"], "cis_app"),
                     ("cis_owner", s, "f", ["EXECUTE"], "cis_app"),
                     ("cis_owner", s, "r", ["DELETE", "INSERT", "SELECT", "UPDATE"], "cis_app")]
    assert sorted(cur.fetchall()) == sorted(expected)     # order-free (collation, lesson S243)


def test_cis_owner_policy_on_each_force_table_only(cur):
    cur.execute("""SELECT schemaname || '.' || tablename, policyname, permissive, roles::text[], cmd, qual, with_check
                     FROM pg_policies WHERE 'cis_owner' = ANY(roles) ORDER BY 1""")
    rows = cur.fetchall()
    assert [r[0] for r in rows] == sorted(X1_FORCE_RLS)
    for table, name, permissive, roles, cmd, qual, with_check in rows:
        assert (name, permissive, roles, cmd, qual, with_check) == (
            f"{table.split('.')[1]}_cis_owner_all", "PERMISSIVE", ["cis_owner"], "ALL", "true", "true"), table


def test_cis_owner_is_a_plain_owner_role(cur):
    cur.execute("SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb FROM pg_roles "
                "WHERE rolname = 'cis_owner'")
    assert cur.fetchone() == (False, False, False, False, False)
    cur.execute("SELECT count(*) FROM pg_auth_members WHERE member = 'cis_owner'::regrole")
    assert cur.fetchone()[0] == 0


def test_consumer_and_owner_grants_carried_over(cur):
    cur.execute("""SELECT has_schema_privilege('bk_owner', 'shared', 'USAGE'),
                          has_table_privilege('bk_owner', 'shared.schema_versions', 'INSERT'),
                          has_table_privilege('tp_owner', 'shared.schema_versions', 'SELECT'),
                          has_table_privilege('catalog_owner', 'gold_aa_internal.published_tours', 'SELECT'),
                          has_table_privilege('cis_app', 'shared.zz245_v_runs', 'SELECT'),
                          has_table_privilege('cis_app', 'shared.schema_versions', 'INSERT')""")
    assert cur.fetchone() == (True, True, True, True, True, False)


# ── behaviour ───────────────────────────────────────────────────────────────────────────────────────

def _counts_as(role_switch, table):
    """(no GUC, forged GUC) counts of table, logged in as the master, optionally after SET ROLE role_switch."""
    c = _connect(RUNNER)
    try:
        k = c.cursor()
        if role_switch:
            k.execute(f"SET ROLE {role_switch}")
        forged = str(uuid.uuid4())
        return _count(k, table), _count(k, table, forged)
    finally:
        c.rollback()
        c.close()


@pytest.mark.parametrize("table", PARITY_TABLES)
def test_cis_owner_reads_what_the_master_reads(table):
    """Every later cis/ file runs as cis_owner (XD8): it must see every tenant's rows, like the master today."""
    assert _counts_as("cis_owner", table) == _counts_as(None, table)


def test_cis_app_parity_still_holds():
    c = _connect("cis_app", CIS_APP_CI_PASSWORD)
    try:
        k = c.cursor()
        for table in PARITY_TABLES:
            assert (_count(k, table), _count(k, table, str(uuid.uuid4()))) == _counts_as(None, table), table
    finally:
        c.rollback()
        c.close()


def test_view_owned_by_cis_owner_reads_every_tenant(cur):
    """A view reads its base tables with its OWNER's rights: shared.zz245_v_runs (like v_active_tour_atoms over
    published_tours) now runs as cis_owner and must still return both tenants' rows to the runtime."""
    cur.execute("SELECT count(*) FROM shared.pipeline_runs")
    total = cur.fetchone()[0]
    c = _connect("cis_app", CIS_APP_CI_PASSWORD)
    try:
        k = c.cursor()
        assert _count(k, "shared.zz245_v_runs") == _count(k, "shared.zz245_v_runs", str(uuid.uuid4())) == total >= 2
    finally:
        c.rollback()
        c.close()


def test_data_migration_as_cis_owner_sees_every_tenant():
    c = _connect(RUNNER)
    try:
        k = c.cursor()
        k.execute("SET ROLE cis_owner")
        k.execute("UPDATE shared.pipeline_runs SET status = status WHERE id = ANY(%s::uuid[])", (list(SEED_RUNS),))
        assert k.rowcount == 2
        k.execute("INSERT INTO shared.schema_versions (version, applied_at, description) "
                  "VALUES ('zz245-key', now(), 'x') ON CONFLICT (version) DO NOTHING")   # the registry owner
        assert k.rowcount == 1
    finally:
        c.rollback()
        c.close()


def test_objects_a_later_cis_file_creates_reach_cis_app(cur):
    cur.execute("""SELECT pg_get_userbyid(c.relowner),
                          has_table_privilege('cis_app', c.oid, 'SELECT, INSERT, UPDATE, DELETE'),
                          has_sequence_privilege('cis_app', pg_get_serial_sequence('acp_shared.zz245_post', 'id'),
                                                 'USAGE'),
                          has_function_privilege('cis_app', 'geo.zz245_post_fn()', 'EXECUTE')
                     FROM pg_class c WHERE c.oid = 'acp_shared.zz245_post'::regclass""")
    assert cur.fetchone() == ("cis_owner", True, True, True)


@pytest.mark.parametrize("role,sql", [
    ("cis_app", "INSERT INTO shared.schema_versions (version, applied_at, description) VALUES ('zz245', now(), 'x')"),
    ("cis_app", "DELETE FROM shared.schema_versions WHERE version = '245'"),
    ("cis_app", "SELECT 1 FROM booking.customers"),
    ("cis_app", "SELECT 1 FROM tripplanner.sessions"),
    ("cis_app", "CREATE TABLE shared.zz245_ddl (id int)"),
    ("cis_owner", "SELECT 1 FROM booking.customers"),
    ("cis_owner", "SELECT 1 FROM tripplanner.sessions"),
])
def test_refused(role, sql):
    if role == "cis_app":
        c = _connect("cis_app", CIS_APP_CI_PASSWORD)
    else:
        c = _connect(RUNNER)
        c.cursor().execute("SET ROLE cis_owner")
    try:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            c.cursor().execute(sql)
    finally:
        c.rollback()
        c.close()


# ── the guards ──────────────────────────────────────────────────────────────────────────────────────

def _apply_expecting(match, sql=None, as_cis_owner=False):
    runner = _connect(RUNNER)
    runner.autocommit = True
    try:
        k = runner.cursor()
        if as_cis_owner:
            k.execute("SET ROLE cis_owner")
        with pytest.raises(psycopg2.errors.RaiseException, match=match):
            k.execute(sql or _sql())
        k.execute("ROLLBACK")
    finally:
        runner.close()


def _admin(sql, params=None):
    admin = _connect()
    admin.autocommit = True
    try:
        admin.cursor().execute(sql, params)
    finally:
        admin.close()


def _cut(sql, start, end):
    """sql without the part from the line starting with `start` up to (not including) the one with `end`."""
    i, j = sql.index(start), sql.index(end)
    assert i < j
    return sql[:i] + sql[j:]


def test_guard_parity_refuses_a_transfer_without_the_cis_owner_policies():
    """Without <table>_cis_owner_all (and without the shape guard that would name them first), the views and
    every later cis/ file would read 0 rows of the FORCE tables: the behavioural parity block refuses."""
    sql = _cut(_sql(), "CREATE POLICY content_exports_cis_owner_all", "-- ── 6. Default privileges")
    sql = _cut(sql,
               "    SELECT string_agg(n.nspname || '.' || c.relname, ', ' ORDER BY n.nspname || '.' || c.relname) "
               "INTO bad",
               "    SELECT string_agg(pg_catalog.format('%I.%I %I', n.nspname, c.relname, p.polname), ', ') INTO bad")
    assert "CREATE POLICY" not in sql
    _apply_expecting(r"migration 245: parity: (view \S+ returns 0 row|cis_owner reads 0 row)", sql)


def test_guard_refuses_a_third_owner():
    """Step 4 moves only the master's objects; anything owned by another role in a CIS schema fails the file."""
    _admin("CREATE SEQUENCE acp_shared.zz245_third_owner; "
           "ALTER SEQUENCE acp_shared.zz245_third_owner OWNER TO tripplanner")
    try:
        _apply_expecting(r"migration 245: objects with the wrong owner .*acp_shared\.zz245_third_owner \(tripplanner\)")
    finally:
        _admin("DROP SEQUENCE acp_shared.zz245_third_owner")


def test_guard_refuses_a_security_definer_routine():
    _admin("CREATE FUNCTION acp_shared.zz245_definer() RETURNS int LANGUAGE sql SECURITY DEFINER AS 'SELECT 1'; "
           f"ALTER FUNCTION acp_shared.zz245_definer() OWNER TO {RUNNER}")
    try:
        _apply_expecting(r"migration 245: SECURITY DEFINER routine\(s\) acp_shared\.zz245_definer")
    finally:
        _admin("DROP FUNCTION acp_shared.zz245_definer()")


def test_guard_refuses_a_new_force_table():
    _admin("CREATE TABLE acp_shared.zz245_new_force (id int); "
           "ALTER TABLE acp_shared.zz245_new_force ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY; "
           f"ALTER TABLE acp_shared.zz245_new_force OWNER TO {RUNNER}")
    try:
        _apply_expecting(r"migration 245: FORCE RLS tables are .*acp_shared\.zz245_new_force")
    finally:
        _admin("DROP TABLE acp_shared.zz245_new_force")


def test_guard_refuses_another_member_of_cis_owner():
    _admin("GRANT cis_owner TO booking")
    try:
        _apply_expecting(r"migration 245: only aa_cis_admin and db_migrator may be members of cis_owner, "
                         r"found also booking")
    finally:
        _admin("REVOKE cis_owner FROM booking")


def test_guard_refuses_a_master_outside_cis_owner():
    _admin(f"REVOKE cis_owner FROM {RUNNER}")
    try:
        _apply_expecting(r"migration 245: aa_cis_admin is not a member of cis_owner")
    finally:
        _admin(f"GRANT cis_owner TO {RUNNER}")


def test_guard_refuses_a_run_after_the_transfer():
    """After 245 DB Migrate would run this file as cis_owner (XD8): guard 0 refuses, the undo is a roles/ file."""
    _apply_expecting(r"migration 245: runs only as the RDS master aa_cis_admin before the transfer "
                     r"\(current_user cis_owner", as_cis_owner=True)


def test_view_counts_skipped_with_a_notice_when_the_master_cannot_read_a_view():
    """Without pg_read_all_data the master cannot read the catalog_owner views: their count parity is skipped
    with a NOTICE (the file still applies); everything else is checked as before."""
    _admin(f"REVOKE pg_read_all_data FROM {RUNNER}")
    try:
        notices = _apply_as_runner()
    finally:
        _admin(f"GRANT pg_read_all_data TO {RUNNER}")
    skipped = [n for n in notices if "is not readable by aa_cis_admin" in n]
    assert any(re.search(r"catalog\.v_tour is not readable", n) for n in skipped)
    assert not any("shared.zz245_v_runs" in n for n in skipped)
