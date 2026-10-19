"""AA-860 (X1 step S22) — migration 246 (probe: DB Migrate logs in as db_migrator and runs cis/ files as cis_owner)
and the S22 catalog-view recipe, applied the way DB Migrate applies a cis/ file from S22 on.

From S22 the DB Migrate login is db_migrator (Infra rl-002: LOGIN, NOCREATEROLE, INHERIT member of bk_owner,
catalog_owner, cis_app, cis_owner, pg_read_all_stats, tp_owner). Before each cis/ file the runner reads the owner of
schema shared (cis_owner since 245) and runs the file under `SET LOCAL ROLE cis_owner` inside the file's own
transaction (AA-CIS-Infra db/run_migration.py, plan XD8); `_apply_as_migrator` reproduces that. The module builds on
test_aa860_migration_245's module fixture (Dev after 244 + 245, an RDS-like master, undone at teardown), then gives
db_migrator rl-002's memberships and a CI-only password, applies 246 and one catalog view through the recipe, and
gives everything back at teardown.

The catalog recipe (CONTEXT.md, plan S22): after 245 cis_owner owns schema catalog and catalog_owner only has USAGE
on it, so a catalog.* view change is
    GRANT CREATE ON SCHEMA catalog TO catalog_owner; SET LOCAL ROLE catalog_owner; CREATE OR REPLACE VIEW catalog.…;
    SET LOCAL ROLE cis_owner; REVOKE CREATE ON SCHEMA catalog FROM catalog_owner;
never RESET ROLE (it returns to db_migrator, and what it creates then is db_migrator's).
"""
import os
import re
import sys

import psycopg2
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa860_migration_245 as t245  # noqa: E402
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401  (module fixture: Dev state after 244 + 245)

MIGRATOR = "db_migrator"
MIGRATOR_CI_PASSWORD = "db-migrator-ci-only-not-a-secret"
# Infra rl-002 grants (AA-CIS-Infra db/owners.yaml migrator_member_of).
MIGRATOR_MEMBER_OF = ("bk_owner", "catalog_owner", "cis_app", "cis_owner", "pg_read_all_stats", "tp_owner")
HERE = os.path.dirname(__file__)
MIGRATION = os.path.join(HERE, "..", "..", "api", "migrations", "246_x1_set_role_probe.sql")
RECIPE_VIEW = "catalog.zz246_v_recipe"
RECIPE_KEY = "zz246-recipe"
# A later cis/ file that adds (and, applied again, replaces) a catalog view with the S22 recipe. Passes the
# AA-CIS-Infra role lint as a cis/ file above the head (checked with run_migration.py --inspect, AA-860-S22.md).
RECIPE = f"""-- Migration (test): a catalog view through the X1 S22 catalog recipe.
BEGIN;
GRANT CREATE ON SCHEMA catalog TO catalog_owner;
SET LOCAL ROLE catalog_owner;
CREATE OR REPLACE VIEW {RECIPE_VIEW} WITH (security_barrier = true) AS
    SELECT t.source_system, t.source_id FROM catalog.v_tour t;
SET LOCAL ROLE cis_owner;
REVOKE CREATE ON SCHEMA catalog FROM catalog_owner;
INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('{RECIPE_KEY}', now(), 'test: catalog view through the S22 recipe') ON CONFLICT (version) DO NOTHING;
COMMIT;
"""
BEGIN_RE = re.compile(r"^BEGIN;[ \t]*$", re.M)


def _sql(path=MIGRATION):
    return open(path, encoding="utf-8").read()


def _switched(sql, owner="cis_owner"):
    """The file as DB Migrate runs it under the live-owner switch: SET LOCAL ROLE <owner> right after its BEGIN."""
    assert len(BEGIN_RE.findall(sql)) == 1, "one BEGIN; line, like every switched file"
    return BEGIN_RE.sub(f"BEGIN;\nSET LOCAL ROLE {owner};", sql, count=1)


def _migrator():
    return t245._connect(MIGRATOR, MIGRATOR_CI_PASSWORD)


def _apply_as_migrator(sql, switch=True):
    c = _migrator()
    c.autocommit = True
    try:
        c.cursor().execute(_switched(sql) if switch else sql)        # raises on any error
    finally:
        c.close()


def _apply_expecting(match, sql, switch=True, login="migrator"):
    c = _migrator() if login == "migrator" else t245._connect(t245.RUNNER)
    c.autocommit = True
    try:
        k = c.cursor()
        with pytest.raises(psycopg2.errors.RaiseException, match=match):
            k.execute(_switched(sql) if switch else sql)
        k.execute("ROLLBACK")
    finally:
        c.close()


def _admin(sql, params=None):
    a = t245._connect()
    a.autocommit = True
    try:
        a.cursor().execute(sql, params)
    finally:
        a.close()


def _drop_recipe(a):
    a.execute(f"DROP VIEW IF EXISTS {RECIPE_VIEW}")
    a.execute("DELETE FROM shared.schema_versions WHERE version = ANY(%s)", (["246", RECIPE_KEY],))
    a.execute("REVOKE CREATE ON SCHEMA catalog FROM catalog_owner")


@pytest.fixture(scope="module", autouse=True)
def db_migrator_login(rds_like_245):  # noqa: F811  (the imported fixture, requested by name)
    admin = t245._connect()
    admin.autocommit = True
    a = admin.cursor()
    try:
        _drop_recipe(a)
        a.execute(f"ALTER ROLE {MIGRATOR} LOGIN PASSWORD %s", (MIGRATOR_CI_PASSWORD,))
        a.execute(f"GRANT {', '.join(MIGRATOR_MEMBER_OF)} TO {MIGRATOR}")
        _apply_as_migrator(_sql())
        _apply_as_migrator(_sql())                          # idempotent re-run (ON CONFLICT DO NOTHING)
        _apply_as_migrator(RECIPE)                          # creates the view …
        _apply_as_migrator(RECIPE)                          # … and replaces it (CREATE OR REPLACE as its owner)
        yield
    finally:
        _drop_recipe(a)
        a.execute(f"REVOKE {', '.join(MIGRATOR_MEMBER_OF)} FROM {MIGRATOR}")
        a.execute(f"ALTER ROLE {MIGRATOR} PASSWORD NULL")
        admin.close()


@pytest.fixture
def cur():
    c = t245._connect()
    c.autocommit = False
    try:
        yield c.cursor()
    finally:
        c.rollback()
        c.close()


# ── 246 ─────────────────────────────────────────────────────────────────────────────────────────────

def test_246_registered_once(cur):
    cur.execute("SELECT count(*), max(description) FROM shared.schema_versions WHERE version = '246'")
    n, description = cur.fetchone()
    assert n == 1
    assert "logged in as db_migrator, ran as cis_owner" in description


def test_246_refuses_the_master_login():
    """Applied before the S22 Terraform switch (DB Migrate still logs in as the master, switched to cis_owner)."""
    _apply_expecting(r"migration 246: expected the DB Migrate login db_migrator \(X1 S22\), logged in as aa_cis_admin",
                     _sql(), login="master")


def test_246_refuses_db_migrator_without_the_switch():
    _apply_expecting(r"migration 246: expected to run as cis_owner .*running as db_migrator", _sql(), switch=False)


def test_246_refuses_a_migrator_outside_catalog_owner():
    _admin(f"REVOKE catalog_owner FROM {MIGRATOR}")
    try:
        _apply_expecting(r"migration 246: db_migrator is not a member of catalog_owner", _sql())
    finally:
        _admin(f"GRANT catalog_owner TO {MIGRATOR}")


def test_246_is_one_transaction_block_and_registers_last():
    """The shape DB Migrate's switch needs (one BEGIN … COMMIT); assert-only, no role statement of its own."""
    code = "\n".join(line for line in _sql().splitlines() if not line.lstrip().startswith("--")).strip()
    assert re.findall(r"^(BEGIN|COMMIT);$", code, re.M) == ["BEGIN", "COMMIT"]
    assert code.startswith("BEGIN;") and code.endswith("COMMIT;")
    assert "SET LOCAL ROLE" not in code and "RESET ROLE" not in code
    assert code.index("END $$;") < code.index("INSERT INTO shared.schema_versions")
    assert re.search(r"ON CONFLICT \(version\) DO NOTHING;\s*COMMIT;$", code)


# ── the catalog recipe ──────────────────────────────────────────────────────────────────────────────

def test_recipe_view_belongs_to_catalog_owner_and_create_is_revoked(cur):
    cur.execute(f"SELECT pg_get_userbyid(relowner), reloptions FROM pg_class WHERE oid = '{RECIPE_VIEW}'::regclass")
    assert cur.fetchone() == ("catalog_owner", ["security_barrier=true"])
    cur.execute("SELECT pg_get_userbyid(nspowner), has_schema_privilege('catalog_owner', 'catalog', 'CREATE'), "
                "has_schema_privilege('catalog_owner', 'catalog', 'USAGE') FROM pg_namespace WHERE nspname = 'catalog'")
    assert cur.fetchone() == ("cis_owner", False, True)
    cur.execute("SELECT count(*) FROM shared.schema_versions WHERE version = %s", (RECIPE_KEY,))
    assert cur.fetchone()[0] == 1                           # registered as cis_owner, after the way back


def test_db_migrator_owns_nothing(cur):
    """What the runner's ownership post-check enforces before COMMIT: nothing is left owned by the login role."""
    cur.execute("""
        SELECT kind, name FROM (
            SELECT 'schema' AS kind, nspname::text AS name, nspowner AS owner FROM pg_namespace
            UNION ALL SELECT 'relation', relnamespace::regnamespace || '.' || relname, relowner FROM pg_class
            UNION ALL SELECT 'routine', pronamespace::regnamespace || '.' || proname, proowner FROM pg_proc
            UNION ALL SELECT 'type', typnamespace::regnamespace || '.' || typname, typowner FROM pg_type
            UNION ALL SELECT 'default privileges', coalesce(defaclnamespace::regnamespace::text, '*'), defaclrole
              FROM pg_default_acl) o
         WHERE owner = %s::regrole""", (MIGRATOR,))
    assert cur.fetchall() == []


def test_recipe_view_reads_through_its_owner():
    c = _migrator()
    try:
        k = c.cursor()
        k.execute(f"SELECT count(*) FROM {RECIPE_VIEW}")
        n = k.fetchone()[0]
        k.execute("SELECT count(*) FROM catalog.v_tour")
        assert n == k.fetchone()[0]
    finally:
        c.rollback()
        c.close()


@pytest.mark.parametrize("name,steps,error", [
    # cis_owner owns schema catalog but not the views: "must be owner of view".
    ("as cis_owner", [f"CREATE OR REPLACE VIEW {RECIPE_VIEW} WITH (security_barrier = true) AS "
                      "SELECT t.source_system, t.source_id FROM catalog.v_tour t"],
     psycopg2.errors.InsufficientPrivilege),
    # catalog_owner without the GRANT CREATE: "permission denied for schema catalog".
    ("as catalog_owner without CREATE", ["SET LOCAL ROLE catalog_owner",
                                         "CREATE VIEW catalog.zz246_v_new AS SELECT 1 AS x"],
     psycopg2.errors.InsufficientPrivilege),
])
def test_recipe_steps_are_all_needed(name, steps, error):
    c = _migrator()
    try:
        k = c.cursor()
        k.execute("SET LOCAL ROLE cis_owner")               # the runner's switch
        with pytest.raises(error):
            for step in steps:
                k.execute(step)
    finally:
        c.rollback()
        c.close()


def test_reset_role_would_leave_an_object_owned_by_db_migrator():
    """Why the recipe goes back with SET LOCAL ROLE cis_owner: RESET ROLE returns to the login role, which inherits
    CREATE on every CIS schema through cis_owner. The AA-CIS-Infra lint refuses RESET ROLE and the runner's
    post-check refuses such an object before COMMIT; here the transaction is rolled back."""
    c = _migrator()
    try:
        k = c.cursor()
        k.execute("SET LOCAL ROLE cis_owner")
        k.execute("RESET ROLE")
        k.execute("CREATE TABLE shared.zz246_after_reset (id int)")
        k.execute("SELECT current_user::text, pg_get_userbyid(relowner) FROM pg_class "
                  "WHERE oid = 'shared.zz246_after_reset'::regclass")
        assert k.fetchone() == (MIGRATOR, MIGRATOR)
    finally:
        c.rollback()
        c.close()


# ── what the schema dump and the probe read as db_migrator ──────────────────────────────────────────

def test_db_migrator_sees_other_sessions_state():
    """pg_read_all_stats (rl-002): the dump's Connections table and the probe's session cells are not blank."""
    other = t245._connect()
    try:
        other.cursor().execute("SELECT 1")
        c = _migrator()
        try:
            k = c.cursor()
            k.execute("SELECT count(*) FROM pg_stat_activity WHERE pid = %s AND state IS NOT NULL",
                      (other.get_backend_pid(),))
            assert k.fetchone()[0] == 1
        finally:
            c.rollback()
            c.close()
    finally:
        other.close()


def test_db_migrator_reads_the_registry_and_the_job_table():
    """DB Migrate's idle check and registry reads run as the login role (before the switch)."""
    c = _migrator()
    try:
        k = c.cursor()
        k.execute("SELECT count(*) FROM shared.schema_versions WHERE version IN ('245', '246')")
        assert k.fetchone()[0] == 2
        k.execute("SELECT count(*) FROM shared.job WHERE status IN ('queued', 'running')")
        assert k.fetchone()[0] >= 0
    finally:
        c.rollback()
        c.close()
