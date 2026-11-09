"""AA-1026 D5 — migrations 328 (embedding space registry, in the replay) and 329 (the shadow reset trigger function, the
Titan V2 fallback rows, catalog.v_embedding_space_v1), applied the way DB Migrate applies a cis/ file since X1 S22.

conftest skips 329 in the replay (its catalog recipe needs 245's owners). This module builds on
test_aa860_migration_246's module fixture (Dev after 244 + 245, db_migrator with rl-002's memberships) and applies 329
as db_migrator under the live-owner switch (errors NOT swallowed), twice. Teardown drops what 329 created.

The owner steps of a re-embed (prepare, swap, rollback, drop shadow, the view rebuild) are DDL of the AA-CIS-Infra
run-script db/scripts/aa1026_embedding_shadow.py, tested there against a real PostgreSQL. Here: 329 creates no other
function, and its trigger function behaves as that script installs it (the same CREATE TRIGGER, on an invented real[]
table: the CI Postgres has no pgvector), in one transaction as cis_owner, rolled back.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa860_migration_245 as t245  # noqa: E402
import test_aa860_migration_246 as t246  # noqa: E402
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401  (module fixture: Dev after 244 + 245)
from test_aa860_migration_246 import db_migrator_login  # noqa: E402,F401  (module fixture: db_migrator, 246)

HERE = os.path.dirname(__file__)
M328 = os.path.join(HERE, "..", "..", "api", "migrations", "328_embedding_space_registry.sql")
M329 = os.path.join(HERE, "..", "..", "api", "migrations", "329_embedding_reembed_tools_titan_fallback.sql")
FUNCTIONS = ("_embedding_shadow_reset()",)
COHERE, TITAN = "cohere-embed-v4/1536/v1", "titan-embed-text-v2/1024/v1"


def _apply(path):
    c = t246._migrator()
    c.autocommit = True
    try:
        c.cursor().execute(t246._switched(open(path, encoding="utf-8").read()))     # raises on any error
    finally:
        c.close()


def undo_329(a):
    a.execute("DROP VIEW IF EXISTS catalog.v_embedding_space_v1")
    for fn in FUNCTIONS:
        a.execute(f"DROP FUNCTION IF EXISTS shared.{fn}")
    a.execute("DELETE FROM shared.llm_provider_health WHERE connection_key = 'acc2-usw2'"
              if _has(a, "shared.llm_provider_health", "connection_key") else "SELECT 1")
    a.execute("DELETE FROM shared.llm_model_deployment WHERE model_key = 'titan-embed-text-v2'")
    a.execute("DELETE FROM shared.llm_model_catalog WHERE model_key = 'titan-embed-text-v2'")
    a.execute("DELETE FROM shared.llm_provider_connection WHERE connection_key = 'acc2-usw2'")
    a.execute("DELETE FROM shared.schema_versions WHERE version = '329'")
    a.execute("REVOKE CREATE ON SCHEMA catalog FROM catalog_owner")


def _has(a, table, column):
    a.execute("SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass(%s) AND attname = %s", (table, column))
    return a.fetchone() is not None


@pytest.fixture(scope="module", autouse=True)
def applied_329(db_migrator_login):  # noqa: F811
    admin = t245._connect()
    admin.autocommit = True
    a = admin.cursor()
    try:
        a.execute("SELECT 1 FROM shared.schema_versions WHERE version = '328'")
        if a.fetchone() is None:                             # the replay applies 328; a re-run here is a no-op
            _apply(M328)
        undo_329(a)
        _apply(M329)
        _apply(M329)                                         # idempotent re-run
        yield
    finally:
        undo_329(a)
        admin.close()


@pytest.fixture
def adm():
    """The superuser, autocommit, closed after the test (an open transaction would block the module teardown)."""
    c = t245._connect()
    c.autocommit = True
    try:
        yield c.cursor()
    finally:
        c.close()


def _one(cur, sql, *args):
    cur.execute(sql, args)
    row = cur.fetchone()
    return row[0] if row else None


# ── 328 / 329: registry, seeds, grants ───────────────────────────────────────────────────────────────────────────

def test_registry_seed_and_grants(adm):
    a = adm
    a.execute("SELECT space_key, status, dimension FROM shared.embedding_space ORDER BY space_key")
    assert a.fetchall() == [(COHERE, "active", 1536), (TITAN, "standby", 1024)]
    a.execute("SELECT table_schema || '.' || table_name || '.' || column_name, space_key, swap_group "
              "FROM shared.embedding_column ORDER BY 1")
    rows = a.fetchall()
    assert [r[0] for r in rows] == [
        "acp_contract.atom_embedding.embedding", "acp_contract.question_embedding.embedding",
        "acp_shared.content_piece.content_embedding", "geo.tour_place.embedding",
        "gold_aa_internal.published_tours.content_embedding"]
    assert {r[1] for r in rows} == {COHERE}
    for role in ("cis_app", "catalog_owner"):
        for t in ("shared.embedding_space", "shared.embedding_column"):
            a.execute("SELECT has_table_privilege(%s, %s, 'SELECT') AS ok, has_table_privilege(%s, %s, 'UPDATE') AS w",
                      (role, t, role, t))
            assert a.fetchone() == (True, False), (role, t)


def test_functions_are_not_executable_by_the_app(adm):
    a = adm
    for fn in FUNCTIONS:
        a.execute("SELECT has_function_privilege('cis_app', %s, 'EXECUTE') AS ok", (f"shared.{fn}",))
        assert a.fetchone()[0] is False, fn


def test_fallback_rows_off_and_unrouted(adm):
    a = adm
    a.execute("SELECT kind, auth_mode, region, data_scope, enabled, served_roles, failure_domain "
              "FROM shared.llm_provider_connection WHERE connection_key = 'acc2-usw2'")
    assert a.fetchone() == ("bedrock", "task_role", "us-west-2", "aws_only", False, ["embed"], "bedrock:acc2:aws_only")
    a.execute("SELECT enabled, api_style, vendor, price_in_per_mtok::float FROM shared.llm_model_catalog "
              "WHERE model_key = 'titan-embed-text-v2'")
    assert a.fetchone() == (True, "embed", "amazon", 0.02)
    a.execute("SELECT enabled, wire_id FROM shared.llm_model_deployment WHERE model_key = 'titan-embed-text-v2'")
    assert a.fetchall() == [(True, "amazon.titan-embed-text-v2:0")]      # off through its connection


def test_tripplanner_view_shows_only_its_search_column(adm):
    c = t245._connect()
    try:
        cur = c.cursor()
        cur.execute("SET LOCAL ROLE tripplanner")
        cur.execute("SELECT table_schema, table_name, column_name, space_key, dimension, query_input_type "
                    "FROM catalog.v_embedding_space_v1")
        assert cur.fetchall() == [("geo", "tour_place", "embedding", COHERE, 1536, "search_query")]
    finally:
        c.rollback()
        c.close()
    a = adm
    assert _one(a, "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = 'catalog.v_embedding_space_v1'"
                   "::regclass") == "catalog_owner"
    assert _one(a, "SELECT has_schema_privilege('catalog_owner', 'catalog', 'CREATE')") is False
    assert _one(a, "SELECT has_table_privilege('cis_app', 'catalog.v_embedding_space_v1', 'SELECT')") is False


def test_329_restates_the_328_comments_that_named_owner_functions(adm):
    # 328 is immutable (copied to AA-CIS-Infra) and its COMMENT ON texts named functions 329 never created.
    comments = [
        _one(adm, "SELECT obj_description('shared.embedding_column'::regclass, 'pg_class')"),
        *(_one(adm, "SELECT col_description('shared.embedding_column'::regclass, a.attnum) FROM pg_attribute a "
                    "WHERE a.attrelid = 'shared.embedding_column'::regclass AND a.attname = %s", col)
          for col in ("shadow_column", "previous_column")),
    ]
    for text in comments:
        assert "owner run-script" in text, text
        for gone in ("embedding_swap", "embedding_prepare_shadow", "embedding_drop_previous"):
            assert gone not in text, text


# ── the shadow reset trigger, installed as the Infra run-script installs it ──────────────────────────────────────

def test_329_creates_no_owner_step_function(adm):
    # The first draft had prepare / swap / rollback / drop functions; they switched roles in dynamic SQL (X1 role lint).
    adm.execute("SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'shared' AND p.proname LIKE '%%embedding%%' ORDER BY 1")
    assert [r[0] for r in adm.fetchall()] == ["_embedding_shadow_reset"]
    assert _one(adm, "SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE oid = "
                     "'shared._embedding_shadow_reset()'::regprocedure") == "cis_owner"


@pytest.fixture
def owner_tx():
    """The superuser in one transaction, switched to cis_owner (the role the run-script uses), so a step can also run
    as cis_app (SET LOCAL ROLE); rolled back."""
    c = t245._connect()
    c.autocommit = False
    cur = c.cursor()
    cur.execute("SET LOCAL ROLE cis_owner")
    try:
        yield cur
    finally:
        c.rollback()
        c.close()


def _shadow(cur):
    cur.execute("SELECT id, emb__shadow FROM shared.zz1026_vec ORDER BY id")
    return {r[0]: (list(r[1]) if r[1] is not None else None) for r in cur.fetchall()}


def test_a_rewritten_serving_vector_empties_its_shadow(owner_tx):
    cur = owner_tx
    cur.execute("""
        CREATE TABLE shared.zz1026_vec (id integer PRIMARY KEY, emb real[] NOT NULL, note text);
        INSERT INTO shared.zz1026_vec VALUES (1, '{1,1}', 'first'), (2, '{2,2}', 'second');
        ALTER TABLE shared.zz1026_vec ADD COLUMN emb__shadow real[];
        CREATE TRIGGER emb__shadow_reset BEFORE UPDATE OF emb ON shared.zz1026_vec FOR EACH ROW
            WHEN (NEW.emb IS DISTINCT FROM OLD.emb AND NEW.emb__shadow IS NOT DISTINCT FROM OLD.emb__shadow)
            EXECUTE FUNCTION shared._embedding_shadow_reset('emb__shadow');
        UPDATE shared.zz1026_vec SET emb__shadow = ARRAY[id * 10, id * 10]::real[];
        GRANT SELECT, INSERT, UPDATE ON shared.zz1026_vec TO cis_app;
    """)
    assert _shadow(cur) == {1: [10.0, 10.0], 2: [20.0, 20.0]}                       # the fill is not reset

    # the writers run as cis_app (no EXECUTE on the function: checked when the trigger was created, not when it fires)
    cur.execute("SET LOCAL ROLE cis_app")
    cur.execute("UPDATE shared.zz1026_vec SET emb = '{9,9}' WHERE id = 1")
    cur.execute("UPDATE shared.zz1026_vec SET emb = '{2,2}', note = 'same vector' WHERE id = 2")
    assert _shadow(cur) == {1: None, 2: [20.0, 20.0]}
    cur.execute("INSERT INTO shared.zz1026_vec (id, emb) VALUES (2, '{8,8}') "
                "ON CONFLICT (id) DO UPDATE SET emb = EXCLUDED.emb")
    assert _shadow(cur) == {1: None, 2: None}
    cur.execute("SELECT id, emb, note FROM shared.zz1026_vec ORDER BY id")
    assert [(r[0], list(r[1]), r[2]) for r in cur.fetchall()] == [(1, [9.0, 9.0], "first"),
                                                                   (2, [8.0, 8.0], "same vector")]
