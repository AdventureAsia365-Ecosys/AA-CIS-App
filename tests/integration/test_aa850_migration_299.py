"""AA-850 — migration 299: drop the four empty legacy ACP v1 schemas (acp_gold_output, acp_silver_s2, acp_silver_s3,
acp_silver_s4).

conftest replays 299 as committed. x1_244_ci_shape recreates the four for the 244 / 245 tests (they grant on them, as on
Dev before 299) and x1_244_undo drops them again, so outside those modules the test DB has none of them. Checked:
  * the replay really applied it (conftest swallows migration errors, lesson AA-741): 299 registered, none of the four
    exists, `retired` and the seven CIS runtime schemas stay;
  * the file re-runs as a no-op;
  * from the pre-299 state (the four, empty, with 244's cis_app USAGE and the 244 / 245 default privileges) it drops
    all four, and their pg_default_acl rows go with them; as their owner cis_owner too (the DB Migrate run), with a
    default-privilege row FOR ROLE another role in a schema;
  * an absent schema is skipped (only some of the four present);
  * the guard refuses a schema that holds a table, view, sequence, function, enum or domain, names it, and drops none;
  * the file is a destructive migration for DB Migrate (first line), so the runner takes the pre-apply snapshot, and
    its only DROP statements are the four DROP SCHEMA … RESTRICT (no CASCADE).
Every test works in a transaction that is rolled back.
"""
import os
import re
import sys

import psycopg2
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import X1_CIS_SCHEMAS, X1_LEGACY_EMPTY_SCHEMAS, _get_conn  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                         "299_aa850_drop_empty_schemas.sql")
RUNTIME_SCHEMAS = tuple(s for s in X1_CIS_SCHEMAS if s not in X1_LEGACY_EMPTY_SCHEMAS)


def _raw() -> str:
    with open(MIGRATION) as f:
        return f.read()


def _body(sql: str) -> str:
    """The file without its own BEGIN/COMMIT, so a test can run it in a transaction it rolls back."""
    return "\n".join(line for line in sql.splitlines() if line.strip() not in ("BEGIN;", "COMMIT;"))


def _drops_only(sql: str) -> str:
    """The guard and the four DROP SCHEMA statements, without the registry row (a role without INSERT on
    shared.schema_versions can run it)."""
    return _body(sql).split("INSERT INTO shared.schema_versions")[0]


@pytest.fixture
def cur():
    conn = _get_conn()
    conn.autocommit = False
    c = conn.cursor()
    try:
        yield c
    finally:
        conn.rollback()
        conn.close()


def _present(c, schemas=X1_LEGACY_EMPTY_SCHEMAS):
    c.execute("SELECT nspname::text FROM pg_namespace WHERE nspname = ANY(%s) ORDER BY 1", (list(schemas),))
    return [r[0] for r in c.fetchall()]


def _pre_299(c, schemas=X1_LEGACY_EMPTY_SCHEMAS, owner=None):
    """The four as on Dev before 299 (in the test's transaction): empty, cis_app USAGE (244), default privileges for
    cis_app FOR ROLE cis_owner (245) and FOR ROLE aa_cis_admin (244); 299 unregistered."""
    for s in schemas:
        c.execute(f"CREATE SCHEMA IF NOT EXISTS {s}" + (f" AUTHORIZATION {owner}" if owner else ""))
        c.execute(f"GRANT USAGE ON SCHEMA {s} TO cis_app")
        for role in ("cis_owner", "aa_cis_admin"):
            c.execute(f"ALTER DEFAULT PRIVILEGES FOR ROLE {role} IN SCHEMA {s} "
                      "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO cis_app")
    c.execute("DELETE FROM shared.schema_versions WHERE version = '299'")


def _default_acl_rows(c, schemas=X1_LEGACY_EMPTY_SCHEMAS):
    c.execute("SELECT count(*) FROM pg_default_acl d JOIN pg_namespace n ON n.oid = d.defaclnamespace "
              "WHERE n.nspname = ANY(%s)", (list(schemas),))
    return c.fetchone()[0]


def _registered(c):
    c.execute("SELECT EXISTS (SELECT 1 FROM shared.schema_versions WHERE version = '299')")
    return c.fetchone()[0]


def test_the_replay_applied_299(cur):
    assert _registered(cur), "299 is not registered: the replay swallowed an error in the file"
    assert _present(cur) == []
    assert _present(cur, RUNTIME_SCHEMAS + ("retired",)) == sorted(RUNTIME_SCHEMAS + ("retired",))


def test_a_rerun_is_a_no_op(cur):
    cur.execute(_body(_raw()))
    assert _present(cur) == [] and _registered(cur)


def test_drops_the_four_and_their_default_privileges(cur):
    _pre_299(cur)
    assert _present(cur) == sorted(X1_LEGACY_EMPTY_SCHEMAS) and _default_acl_rows(cur) == 8
    cur.execute(_body(_raw()))
    assert _present(cur) == []
    assert _default_acl_rows(cur) == 0
    assert _registered(cur)
    assert _present(cur, RUNTIME_SCHEMAS + ("retired",)) == sorted(RUNTIME_SCHEMAS + ("retired",))


def test_their_owner_drops_them_despite_another_roles_default_privileges(cur):
    """DB Migrate runs CIS files as cis_owner, the owner of the four since 245; Dev still has 244's default
    privileges FOR ROLE aa_cis_admin in them, which the owner cannot alter but which go with the schema."""
    _pre_299(cur, owner="cis_owner")
    cur.execute("SET LOCAL ROLE cis_owner")
    cur.execute(_drops_only(_raw()))
    cur.execute("RESET ROLE")
    assert _present(cur) == [] and _default_acl_rows(cur) == 0


def test_an_absent_schema_is_skipped(cur):
    _pre_299(cur, schemas=("acp_silver_s2", "acp_gold_output"))
    cur.execute(_body(_raw()))
    assert _present(cur) == [] and _registered(cur)


@pytest.mark.parametrize("schema, ddl, needle", [
    ("acp_silver_s4", "CREATE TABLE acp_silver_s4.social_content (id int)", "relation social_content"),
    ("acp_silver_s2", "CREATE VIEW acp_silver_s2.v AS SELECT 1 AS x", "relation v"),
    ("acp_silver_s3", "CREATE SEQUENCE acp_silver_s3.seq", "relation seq"),
    ("acp_gold_output", "CREATE FUNCTION acp_gold_output.f() RETURNS int LANGUAGE sql AS 'SELECT 1'", "function f"),
    ("acp_silver_s3", "CREATE TYPE acp_silver_s3.mood AS ENUM ('a')", "type mood"),
    ("acp_silver_s2", "CREATE DOMAIN acp_silver_s2.pos AS int CHECK (VALUE > 0)", "type pos"),
])
def test_the_guard_refuses_a_schema_that_is_not_empty(cur, schema, ddl, needle):
    _pre_299(cur)
    cur.execute(ddl)
    cur.execute("SAVEPOINT aa850")
    with pytest.raises(psycopg2.Error) as ei:
        cur.execute(_body(_raw()))
    cur.execute("ROLLBACK TO SAVEPOINT aa850")
    msg = str(ei.value)
    assert f"migration 299: schema {schema} is not empty" in msg and needle in msg, msg
    assert _present(cur) == sorted(X1_LEGACY_EMPTY_SCHEMAS)
    assert not _registered(cur)


def test_the_file_contract():
    raw = _raw()
    assert raw.splitlines()[0] == "-- migrate: destructive"
    code = "\n".join(line.split("--", 1)[0] for line in raw.splitlines())
    assert "CASCADE" not in code.upper()
    drops = re.findall(r"^\s*(DROP\b.*?);", code, flags=re.MULTILINE | re.IGNORECASE)
    assert [re.sub(r"\s+", " ", d) for d in drops] == [
        f"DROP SCHEMA IF EXISTS {s} RESTRICT" for s in X1_LEGACY_EMPTY_SCHEMAS]
    assert not re.search(r"\b(TRUNCATE|DELETE|ALTER)\b", code, flags=re.IGNORECASE)
    assert "VALUES ('299'" in code and "ON CONFLICT (version) DO NOTHING" in code
