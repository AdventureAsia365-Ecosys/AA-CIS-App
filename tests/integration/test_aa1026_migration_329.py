"""AA-1026 D5 — migrations 328 (embedding space registry, expand, in the replay) and 329 (re-embed tools, the Titan V2
fallback rows, catalog.v_embedding_space_v1), applied the way DB Migrate applies a cis/ file since X1 S22.

conftest skips 329 in the replay (its catalog recipe needs 245's owners). This module builds on
test_aa860_migration_246's module fixture (Dev after 244 + 245, db_migrator with rl-002's memberships) and applies 329
as db_migrator under the live-owner switch (errors NOT swallowed), twice. Teardown drops what 329 created.

The CI Postgres has no pgvector: the swap tools are exercised on an invented table with real[] columns (the functions
are type-agnostic; prepare takes a column type for exactly this), with a plain view, a nested view and a catalog view
owned by catalog_owner (the case the swap must rebuild as its owner). Everything runs in one transaction as db_migrator
with SET LOCAL ROLE cis_owner (as a run-script would) and is rolled back.
"""
import os
import sys

import psycopg2
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
FUNCTIONS = ("embedding_dependent_views(regclass, smallint[])", "_embedding_shadow_reset()",
             "_embedding_shadow_trigger(regclass, text, boolean)",
             "_embedding_rename_indexes(regclass, text, text, text)",
             "embedding_prepare_shadow(text, text, text, text, text)", "_embedding_switch(text, text, boolean)",
             "embedding_swap(text, boolean)", "embedding_rollback(text, boolean)",
             "embedding_drop_shadow(text, text, text)", "embedding_drop_previous(text)")
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
def mig():
    """db_migrator in one transaction, switched to cis_owner (a run-script); rolled back."""
    c = t246._migrator()
    c.autocommit = False
    cur = c.cursor()
    cur.execute("SET LOCAL ROLE cis_owner")
    try:
        yield cur
    finally:
        c.rollback()
        c.close()


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


def _refused(cur, match, sql, *args):
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.Error, match=match):
        cur.execute(sql, args)
    cur.execute("ROLLBACK TO SAVEPOINT s")


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


# ── the swap tools on an invented real[] table ───────────────────────────────────────────────────────────────────

def _fixture_table(cur):
    cur.execute("""
        CREATE TABLE shared.zz1026_vec (id integer PRIMARY KEY, emb real[] NOT NULL);
        CREATE INDEX zz1026_vec_emb_idx ON shared.zz1026_vec (emb);
        INSERT INTO shared.zz1026_vec VALUES (1, '{1,1}'), (2, '{2,2}');
        CREATE VIEW shared.zz1026_v1 AS SELECT id, emb FROM shared.zz1026_vec;
        CREATE VIEW shared.zz1026_v2 AS SELECT id, emb FROM shared.zz1026_v1;
        COMMENT ON VIEW shared.zz1026_v1 IS 'invented view';
        GRANT SELECT ON shared.zz1026_v1 TO tripplanner;
        GRANT SELECT ON shared.zz1026_vec TO catalog_owner;
        GRANT CREATE ON SCHEMA catalog TO catalog_owner;
        SET LOCAL ROLE catalog_owner;
        CREATE VIEW catalog.zz1026_cv WITH (security_barrier = true) AS SELECT id, emb FROM shared.zz1026_vec;
        GRANT SELECT ON catalog.zz1026_cv TO tripplanner;
        GRANT SELECT ON catalog.zz1026_cv TO cis_owner;
        SET LOCAL ROLE cis_owner;
        REVOKE CREATE ON SCHEMA catalog FROM catalog_owner;
        INSERT INTO shared.embedding_column (table_schema, table_name, column_name, space_key, swap_group,
                                             key_columns, updated_by)
        VALUES ('shared', 'zz1026_vec', 'emb', 'cohere-embed-v4/1536/v1', 'zz1026', ARRAY['id'], 'test');
    """)


def _registry(cur):
    cur.execute("SELECT space_key, shadow_column, shadow_space_key, previous_column, previous_space_key "
                "FROM shared.embedding_column WHERE table_name = 'zz1026_vec'")
    return cur.fetchone()


def _view_values(cur, view):
    cur.execute(f"SELECT emb FROM {view} ORDER BY id")
    return [list(r[0]) if r[0] is not None else None for r in cur.fetchall()]


def test_prepare_fill_swap_rollback_and_drop(mig):
    _fixture_table(mig)
    assert _one(mig, "SELECT shared.embedding_prepare_shadow('shared', 'zz1026_vec', 'emb', %s, 'real[]')",
                TITAN) == "emb__shadow"
    assert _one(mig, "SELECT shared.embedding_prepare_shadow('shared', 'zz1026_vec', 'emb', %s, 'real[]')",
                TITAN) == "emb__shadow"                                       # idempotent: a fill resumes
    assert _registry(mig) == (COHERE, "emb__shadow", TITAN, None, None)
    _refused(mig, "2 row\\(s\\) have a vector but no shadow vector", "SELECT shared.embedding_swap('zz1026')")

    mig.execute("UPDATE shared.zz1026_vec SET emb__shadow = ARRAY[id * 10, id * 10]::real[]")
    mig.execute("CREATE INDEX zz1026_vec_emb_idx__shadow ON shared.zz1026_vec (emb__shadow)")
    assert _one(mig, "SELECT shared.embedding_swap('zz1026')") == 1

    # every view (plain, nested, catalog_owner's catalog view) now reads the new vectors
    for view in ("shared.zz1026_v1", "shared.zz1026_v2", "catalog.zz1026_cv"):
        assert _view_values(mig, view) == [[10.0, 10.0], [20.0, 20.0]], view
    assert _registry(mig) == (TITAN, None, None, "emb__prev", COHERE)
    assert _one(mig, "SELECT status FROM shared.embedding_space WHERE space_key = %s", TITAN) == "active"
    assert _one(mig, "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = 'catalog.zz1026_cv'::regclass") \
        == "catalog_owner"
    assert _one(mig, "SELECT 'security_barrier=true' = ANY(reloptions) FROM pg_class "
                     "WHERE oid = 'catalog.zz1026_cv'::regclass") is True
    assert _one(mig, "SELECT has_table_privilege('tripplanner', 'catalog.zz1026_cv', 'SELECT')") is True
    assert _one(mig, "SELECT has_table_privilege('tripplanner', 'shared.zz1026_v1', 'SELECT')") is True
    assert _one(mig, "SELECT obj_description('shared.zz1026_v1'::regclass, 'pg_class')") == "invented view"
    assert _one(mig, "SELECT has_schema_privilege('catalog_owner', 'catalog', 'CREATE')") is False
    mig.execute("SELECT attname, attnotnull FROM pg_attribute WHERE attrelid = 'shared.zz1026_vec'::regclass "
                "AND attname LIKE 'emb%' ORDER BY attname")
    assert mig.fetchall() == [("emb", True), ("emb__prev", False)]
    mig.execute("SELECT indexrelid::regclass::text FROM pg_index WHERE indrelid = 'shared.zz1026_vec'::regclass "
                "AND NOT indisprimary ORDER BY 1")
    assert [r[0] for r in mig.fetchall()] == ["shared.zz1026_vec_emb_idx", "shared.zz1026_vec_emb_idx__prev"]
    assert _one(mig, "SELECT pg_get_indexdef('shared.zz1026_vec_emb_idx'::regclass)").endswith("(emb)")

    # a writer after the swap fills only the new column; rolling back then needs allow_missing
    mig.execute("INSERT INTO shared.zz1026_vec (id, emb) VALUES (3, '{30,30}')")
    _refused(mig, "1 row\\(s\\) have no vector in emb__prev", "SELECT shared.embedding_rollback('zz1026')")
    mig.execute("DELETE FROM shared.zz1026_vec WHERE id = 3")
    assert _one(mig, "SELECT shared.embedding_rollback('zz1026')") == 1
    for view in ("shared.zz1026_v1", "shared.zz1026_v2", "catalog.zz1026_cv"):
        assert _view_values(mig, view) == [[1.0, 1.0], [2.0, 2.0]], view
    assert _registry(mig) == (COHERE, "emb__shadow", TITAN, None, None)
    mig.execute("SELECT indexrelid::regclass::text FROM pg_index WHERE indrelid = 'shared.zz1026_vec'::regclass "
                "AND NOT indisprimary ORDER BY 1")
    assert [r[0] for r in mig.fetchall()] == ["shared.zz1026_vec_emb_idx", "shared.zz1026_vec_emb_idx__shadow"]

    assert _one(mig, "SELECT shared.embedding_drop_shadow('shared', 'zz1026_vec', 'emb')") == "emb__shadow"
    assert _registry(mig) == (COHERE, None, None, None, None)
    assert _one(mig, "SELECT count(*) FROM pg_attribute WHERE attrelid = 'shared.zz1026_vec'::regclass "
                     "AND attname ~ '^emb__' AND NOT attisdropped") == 0


@pytest.fixture
def owner_tx():
    """The superuser in one transaction, switched to cis_owner, so a step can also run as cis_app (SET LOCAL ROLE);
    rolled back."""
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


def _reset_triggers(cur):
    cur.execute("SELECT tgname FROM pg_trigger WHERE tgrelid = 'shared.zz1026_vec'::regclass AND NOT tgisinternal")
    return [r[0] for r in cur.fetchall()]


def test_a_rewritten_serving_vector_empties_its_shadow_until_the_swap(owner_tx):
    cur = owner_tx
    _fixture_table(cur)
    cur.execute("SELECT shared.embedding_prepare_shadow('shared', 'zz1026_vec', 'emb', %s, 'real[]')", (TITAN,))
    assert _reset_triggers(cur) == ["emb__shadow_reset"]
    cur.execute("UPDATE shared.zz1026_vec SET emb__shadow = ARRAY[id * 10, id * 10]::real[]")   # the fill
    assert _shadow(cur) == {1: [10.0, 10.0], 2: [20.0, 20.0]}

    # the writers run as cis_app: a re-embed of row 1, an unchanged vector on row 2, then an upsert of row 2
    cur.execute("GRANT SELECT, INSERT, UPDATE ON shared.zz1026_vec TO cis_app")
    cur.execute("SET LOCAL ROLE cis_app")
    cur.execute("UPDATE shared.zz1026_vec SET emb = '{9,9}' WHERE id = 1")
    cur.execute("UPDATE shared.zz1026_vec SET emb = '{2,2}' WHERE id = 2")
    assert _shadow(cur) == {1: None, 2: [20.0, 20.0]}
    cur.execute("INSERT INTO shared.zz1026_vec (id, emb) VALUES (2, '{8,8}') "
                "ON CONFLICT (id) DO UPDATE SET emb = EXCLUDED.emb")
    assert _shadow(cur) == {1: None, 2: None}
    cur.execute("SET LOCAL ROLE cis_owner")

    # the swap refuses both rows; a fill catch-up (which sets only the shadow) is not reset, then the swap runs
    _refused(cur, "2 row\\(s\\) have a vector but no shadow vector", "SELECT shared.embedding_swap('zz1026')")
    cur.execute("UPDATE shared.zz1026_vec SET emb__shadow = ARRAY[id * 100, id * 100]::real[]")
    assert _shadow(cur) == {1: [100.0, 100.0], 2: [200.0, 200.0]}
    assert _one(cur, "SELECT shared.embedding_swap('zz1026')") == 1
    assert _reset_triggers(cur) == []
    cur.execute("UPDATE shared.zz1026_vec SET emb = '{3,3}' WHERE id = 1")      # a writer after the swap
    cur.execute("SELECT emb__prev FROM shared.zz1026_vec WHERE id = 1")
    assert list(cur.fetchone()[0]) == [9.0, 9.0]

    # the rollback brings the shadow back, and its trigger; drop_shadow removes both
    cur.execute("SELECT shared.embedding_rollback('zz1026')")
    assert _reset_triggers(cur) == ["emb__shadow_reset"]
    cur.execute("UPDATE shared.zz1026_vec SET emb = '{4,4}' WHERE id = 2")
    assert _shadow(cur)[2] is None and _shadow(cur)[1] == [3.0, 3.0]
    assert _one(cur, "SELECT shared.embedding_drop_shadow('shared', 'zz1026_vec', 'emb')") == "emb__shadow"
    assert _reset_triggers(cur) == []


def test_swap_then_drop_previous(mig):
    _fixture_table(mig)
    mig.execute("SELECT shared.embedding_prepare_shadow('shared', 'zz1026_vec', 'emb', %s, 'real[]')", (TITAN,))
    mig.execute("UPDATE shared.zz1026_vec SET emb__shadow = '{7,7}'")
    mig.execute("SELECT shared.embedding_swap('zz1026')")
    assert _one(mig, "SELECT shared.embedding_drop_previous('zz1026')") == 1
    assert _registry(mig) == (TITAN, None, None, None, None)
    _refused(mig, "keeps no previous column", "SELECT shared.embedding_drop_previous('zz1026')")
    _refused(mig, "nothing to roll back", "SELECT shared.embedding_rollback('zz1026')")


def test_prepare_refusals(mig):
    _fixture_table(mig)
    _refused(mig, "is not in shared.embedding_column",
             "SELECT shared.embedding_prepare_shadow('shared', 'zz1026_vec', 'nope', %s, 'real[]')", TITAN)
    _refused(mig, "already serves",
             "SELECT shared.embedding_prepare_shadow('shared', 'zz1026_vec', 'emb', %s, 'real[]')", COHERE)
    _refused(mig, "unknown or retired",
             "SELECT shared.embedding_prepare_shadow('shared', 'zz1026_vec', 'emb', 'nope/16/v1', 'real[]')")
    _refused(mig, "bad column type",
             "SELECT shared.embedding_prepare_shadow('shared', 'zz1026_vec', 'emb', %s, 'text; select 1')", TITAN)
    _refused(mig, "prepare and fill it first", "SELECT shared.embedding_swap('zz1026')")
    _refused(mig, "no column in swap group", "SELECT shared.embedding_swap('nope')")


def test_swap_allow_missing_leaves_unfilled_rows_empty(mig):
    _fixture_table(mig)
    mig.execute("ALTER TABLE shared.zz1026_vec ALTER COLUMN emb DROP NOT NULL")
    mig.execute("SELECT shared.embedding_prepare_shadow('shared', 'zz1026_vec', 'emb', %s, 'real[]')", (TITAN,))
    mig.execute("UPDATE shared.zz1026_vec SET emb__shadow = '{5,5}' WHERE id = 1")
    assert _one(mig, "SELECT shared.embedding_swap('zz1026', true)") == 1
    assert _view_values(mig, "shared.zz1026_v1") == [[5.0, 5.0], None]
