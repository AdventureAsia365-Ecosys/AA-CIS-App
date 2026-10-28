"""AA-853 (S251 D2) — migration 281: codify the CIS schema state Dev has but no migration makes.

The CI DB is built from the files alone, so it is in the OLD shape 281 converges (raw_tours varchar columns,
membership_plans numeric(10,4), 058's NOT NULL flags, 044 / 048's index names, the tables AA-598 / AA-603 dropped by
hand on Dev, no hand-made columns). conftest skips 281; each test runs in ONE transaction on the RDS-like state of
test_aa860_migration_245 (module fixture), applies 281 as cis_owner (DB Migrate's live-owner switch: the file itself
switches to catalog_owner for the catalog views, the X1 S22 recipe) and is rolled back.

Checks: every column type, nullability, index and CHECK ends as on Dev; the hand-dropped tables are gone; every view
that depended on a changed column is back with its owner, columns, options and privileges, and returns the same rows;
catalog_owner keeps no CREATE on schema catalog; registered once. A second apply is a no-op (no view or table is
recreated or rewritten). Refusals: a materialized view on a changed column, a non-empty table Dev dropped. The v1
schemas: dropped when cis_owner may (RESTRICT, with their enums and cross-schema foreign keys), left (NOTICE) when they
are another role's, refused when something outside them depends on them or they hold an object 281 does not drop.
The two raw_tours CHECKs are put in Dev's form even when no column type is pending.
"""
import os
import re
import sys

import psycopg2
import psycopg2.extensions
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa860_migration_245 as t245  # noqa: E402
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401  (module fixture: Dev after 244 + 245)
from _rls_fixture_helpers import undo_294_for_reapply  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "281_schema_drift_reconcile.sql")
CHANGED = {("silver_aa_internal.raw_tours", c): "text"
           for c in ("country", "duration", "group_size", "period", "price_raw", "sku", "tour_id_external")}
CHANGED.update({("shared.membership_plans", c): "numeric(8,4)"
                for c in ("overage_rate_usd_per_tour", "overage_rate_usd_per_1k_calls")})
DROPPED = ("acp_shared.unknown_ledger", "acp_shared.content_metric_snapshot", "acp_deliver.pieces",
           "acp_deliver.packets", "acp_shared.acp_v2_slots", "acp_shared.acp_v2_runs")
DEV_CHECKS = {
    "chk_raw_tours_country":
        "CHECK (((country IS NULL) OR (country = ANY (ARRAY['Bhutan'::text, 'Cambodia'::text, 'China'::text, "
        "'India'::text, 'Indonesia'::text, 'Japan'::text, 'Kyrgyzstan'::text, 'Laos'::text, 'Malaysia'::text, "
        "'Mongolia'::text, 'Myanmar'::text, 'Nepal'::text, 'Pakistan'::text, 'Philippines'::text, "
        "'South Korea'::text, 'Sri Lanka'::text, 'Taiwan'::text, 'Thailand'::text, 'Uzbekistan'::text, "
        "'Vietnam'::text]))))",
    "raw_tours_review_status_check":
        "CHECK (((review_status)::text = ANY (ARRAY[('pending_review'::character varying)::text, "
        "('reviewed'::character varying)::text, ('approved'::character varying)::text, "
        "('rejected'::character varying)::text])))",
}
VIEWS_SQL = """
WITH RECURSIVE v(oid) AS (
    SELECT DISTINCT r.ev_class FROM pg_depend d
      JOIN pg_rewrite r ON r.oid = d.objid AND d.classid = 'pg_rewrite'::regclass
      JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid
     WHERE (d.refobjid = 'silver_aa_internal.raw_tours'::regclass AND a.attname = ANY(%s))
        OR (d.refobjid = 'shared.membership_plans'::regclass AND a.attname LIKE 'overage_rate_usd_per%%')
    UNION
    SELECT r.ev_class FROM v JOIN pg_depend d ON d.refobjid = v.oid AND d.classid = 'pg_rewrite'::regclass
      JOIN pg_rewrite r ON r.oid = d.objid AND r.ev_class <> v.oid
)
SELECT c.oid::regclass::text, c.oid, pg_get_userbyid(c.relowner), coalesce(c.reloptions, '{}'),
       (SELECT array_agg(attname::text ORDER BY attnum) FROM pg_attribute WHERE attrelid = c.oid AND attnum > 0),
       (SELECT coalesce(array_agg(format('%%s=%%s', grantee::regrole, privilege_type) ORDER BY 1), '{}')
          FROM aclexplode(c.relacl) WHERE grantee <> c.relowner AND grantee <> 0)
  FROM v JOIN pg_class c ON c.oid = v.oid ORDER BY 1
"""


def _body():
    sql = open(MIGRATION, encoding="utf-8").read()
    assert sql.count("\nBEGIN;\n") == 1 and sql.rstrip().endswith("COMMIT;")
    return re.sub(r"\nCOMMIT;\s*$", "\n", sql.replace("\nBEGIN;\n", "\n", 1))


def _apply(c):
    c.execute("SET LOCAL ROLE cis_owner")          # DB Migrate's live-owner switch (XD8)
    c.execute(_body())
    c.execute("RESET ROLE")


def _type(c, rel, col):
    c.execute("""SELECT format_type(atttypid, atttypmod), attnotnull FROM pg_attribute
                  WHERE attrelid = %s::regclass AND attname = %s AND attnum > 0 AND NOT attisdropped""", (rel, col))
    return c.fetchone()


def _views(c):
    c.execute(VIEWS_SQL, (["country", "duration", "group_size", "period", "price_raw", "sku", "tour_id_external"],))
    return c.fetchall()


def _rows(c, views):
    out = {}
    for name, *_ in views:
        c.execute(f"SELECT md5(coalesce(string_agg(r::text, '|' ORDER BY r::text), '')), count(*) FROM {name} r")
        out[name] = c.fetchone()
    return out


class _Cur(psycopg2.extensions.cursor):
    """Carries the state before 281."""


@pytest.fixture
def conn(rds_like_245):  # noqa: F811
    c = t245._connect()
    c.autocommit = False
    try:
        yield c
    finally:
        c.rollback()
        c.close()


# 223 (the plans table, and the usage view on it) does not apply in the CI DB (it needs tenant_tour_versions, which
# only Dev has). Its table as 223 writes it, and a view with a grant over it, stand in for Dev's numeric drift.
PLANS_STANDIN = """
    CREATE TABLE IF NOT EXISTS shared.membership_plans (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(), plan_name TEXT NOT NULL UNIQUE,
        overage_rate_usd_per_tour NUMERIC(10, 4) NOT NULL DEFAULT 0,
        overage_rate_usd_per_1k_calls NUMERIC(10, 4) NOT NULL DEFAULT 0);
    INSERT INTO shared.membership_plans (plan_name, overage_rate_usd_per_tour) VALUES ('zz281', 4.0)
        ON CONFLICT DO NOTHING;
    CREATE OR REPLACE VIEW shared.zz281_v_plans WITH (security_invoker = true) AS
        SELECT plan_name, overage_rate_usd_per_tour FROM shared.membership_plans;
    COMMENT ON VIEW shared.zz281_v_plans IS 'zz281 stand-in';
    ALTER TABLE shared.membership_plans OWNER TO cis_owner;
    ALTER VIEW shared.zz281_v_plans OWNER TO cis_owner;
    GRANT SELECT ON shared.zz281_v_plans TO cis_app;
"""


def _standins(c):
    # AA-850: 281 recreates the views over the columns it changes, v_pipeline_summary among them; CIS 294 (after 281
    # on Dev) quarantined it and v_batch_stats in `retired`. Back to shared, owned by cis_owner as 245 left them.
    undo_294_for_reapply(c, ("v_batch_stats", "v_pipeline_summary"), owner="cis_owner")
    c.execute("SELECT to_regclass('shared.membership_plans') IS NULL")
    if c.fetchone()[0]:
        c.execute(PLANS_STANDIN)


@pytest.fixture
def cur(conn):
    c = conn.cursor(cursor_factory=_Cur)
    _standins(c)
    c.execute("DELETE FROM shared.schema_versions WHERE version = '281'")
    c.before_views = _views(c)
    c.before_rows = _rows(c, c.before_views)
    _apply(c)
    yield c


def test_the_ci_db_starts_in_the_old_shape(conn):
    c = conn.cursor()
    _standins(c)
    assert _type(c, "silver_aa_internal.raw_tours", "country")[0] == "character varying(100)"
    assert _type(c, "shared.membership_plans", "overage_rate_usd_per_tour")[0] != "numeric(8,4)"
    assert _type(c, "shared.tenants", "is_canary")[1] is True
    assert _views(c), "no view depends on the changed columns: the recreate path is not exercised"


def test_columns_and_nullability_as_on_dev(cur):
    for (rel, col), typ in CHANGED.items():
        assert _type(cur, rel, col)[0] == typ, (rel, col)
    for rel, col, typ in (("shared.pipeline_runs", "llm_model", "text"),
                          ("shared.pipeline_runs", "llm_provider", "text"),
                          ("shared.pipeline_runs", "step_name", "text"),
                          ("shared.tenant_brand_rules", "created_at", "timestamp with time zone"),
                          ("silver_aa_internal.raw_sources", "file_hash", "character varying(64)")):
        assert _type(cur, rel, col) == (typ, False), (rel, col)
    assert _type(cur, "shared.tenants", "is_canary")[1] is False
    assert _type(cur, "shared.tenants", "skip_hitl")[1] is False
    assert _type(cur, "shared.tenant_brand_rules", "brand_name")[1] is True


def test_indexes_checks_and_drops_as_on_dev(cur):
    cur.execute("""SELECT to_regclass('shared.uq_tenant_brand_name_version') IS NOT NULL,
                          to_regclass('silver_aa_internal.idx_generated_content_metadata_brand_rule') IS NULL,
                          pg_get_indexdef('silver_aa_internal.idx_generated_content_metadata_gin'::regclass),
                          pg_get_indexdef('silver_aa_internal.idx_raw_sources_file_hash'::regclass)""")
    renamed, old_gone, gin, file_hash = cur.fetchone()
    assert renamed and old_gone
    assert gin.endswith("USING gin (metadata jsonb_path_ops)")
    assert file_hash.endswith("USING btree (file_hash) WHERE (file_hash IS NOT NULL)")
    for name, dev in DEV_CHECKS.items():
        cur.execute("""SELECT pg_get_constraintdef(oid) FROM pg_constraint
                        WHERE conrelid = 'silver_aa_internal.raw_tours'::regclass AND conname = %s""", (name,))
        row = cur.fetchone()
        if row:                      # chk_raw_tours_country exists where 150 / 197 ran
            assert row[0] == dev, name
    for t in DROPPED:
        cur.execute("SELECT to_regclass(%s)", (t,))
        assert cur.fetchone()[0] is None, t


def test_dependent_views_come_back_the_same(cur):
    after = _views(cur)
    strip = [(n, owner, opts, cols, acl) for n, _oid, owner, opts, cols, acl in after]
    assert strip == [(n, owner, opts, cols, acl) for n, _oid, owner, opts, cols, acl in cur.before_views]
    assert {o for _n, o, *_ in strip} <= {"cis_owner", "catalog_owner"}
    assert _rows(cur, after) == cur.before_rows
    cur.execute("SELECT has_schema_privilege('catalog_owner', 'catalog', 'CREATE')")
    assert cur.fetchone()[0] is False


def test_registered_once_and_a_second_apply_is_a_no_op(conn, cur):
    after = {n: oid for n, oid, *_ in _views(cur)}
    cur.execute("SELECT relfilenode FROM pg_class WHERE oid = 'shared.membership_plans'::regclass")
    filenode = cur.fetchone()[0]
    del conn.notices[:]
    _apply(cur)
    assert any("step 5 is a no-op" in n for n in conn.notices), conn.notices
    assert {n: oid for n, oid, *_ in _views(cur)} == after          # no view dropped and recreated
    cur.execute("SELECT relfilenode FROM pg_class WHERE oid = 'shared.membership_plans'::regclass")
    assert cur.fetchone()[0] == filenode                            # no rewrite
    cur.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '281'")
    assert cur.fetchone()[0] == 1


def test_refuses_a_materialized_view_on_a_changed_column(conn):
    c = conn.cursor()
    _standins(c)
    c.execute("CREATE MATERIALIZED VIEW shared.zz281_mv AS SELECT country FROM silver_aa_internal.raw_tours")
    with pytest.raises(psycopg2.Error, match="cannot recreate these dependent views faithfully"):
        _apply(c)


def test_refuses_a_table_dev_dropped_that_has_rows(conn):
    c = conn.cursor()
    _standins(c)
    c.execute("DROP TABLE IF EXISTS acp_shared.unknown_ledger")
    c.execute("CREATE TABLE acp_shared.unknown_ledger (x int)")
    c.execute("ALTER TABLE acp_shared.unknown_ledger OWNER TO cis_owner")
    c.execute("INSERT INTO acp_shared.unknown_ledger DEFAULT VALUES")
    with pytest.raises(psycopg2.Error, match="unknown_ledger has 1 row"):
        _apply(c)


def test_v1_schemas_dropped_only_when_cis_owner_may(conn):
    c = conn.cursor()
    _standins(c)
    c.execute("CREATE SCHEMA IF NOT EXISTS silver AUTHORIZATION cis_owner")
    c.execute("CREATE TABLE silver.zz281_empty (x int)")
    c.execute("ALTER TABLE silver.zz281_empty OWNER TO cis_owner")
    c.execute("CREATE SCHEMA IF NOT EXISTS gold")                   # the superuser's, like the master's on Dev
    del conn.notices[:]
    _apply(c)
    c.execute("SELECT to_regnamespace('silver') IS NULL, to_regnamespace('gold') IS NOT NULL")
    assert c.fetchone() == (True, True)
    assert any("schema gold" in n and "left as is" in n for n in conn.notices), conn.notices


def _v1_with_enum_and_fk(c):
    c.execute("CREATE SCHEMA IF NOT EXISTS bronze AUTHORIZATION cis_owner")
    c.execute("CREATE SCHEMA IF NOT EXISTS silver AUTHORIZATION cis_owner")
    c.execute("SET LOCAL ROLE cis_owner")
    c.execute("CREATE TYPE silver.zz281_status AS ENUM ('a', 'b')")
    c.execute("CREATE TABLE bronze.zz281_src (id serial PRIMARY KEY)")
    c.execute("CREATE TABLE silver.zz281_ver (id int REFERENCES bronze.zz281_src (id), s silver.zz281_status)")
    c.execute("RESET ROLE")


def test_v1_schemas_with_enums_and_cross_schema_keys_are_dropped_without_cascade(conn):
    c = conn.cursor()
    _standins(c)
    _v1_with_enum_and_fk(c)
    del conn.notices[:]
    _apply(c)
    c.execute("SELECT to_regnamespace('bronze') IS NULL, to_regnamespace('silver') IS NULL")
    assert c.fetchone() == (True, True)
    assert any("dropped schema(s)" in n for n in conn.notices), conn.notices


def test_v1_schemas_refused_when_something_outside_depends_on_them(conn):
    c = conn.cursor()
    _standins(c)
    _v1_with_enum_and_fk(c)
    c.execute("CREATE VIEW shared.zz281_on_v1 AS SELECT id FROM bronze.zz281_src")
    c.execute("ALTER VIEW shared.zz281_on_v1 OWNER TO cis_owner")
    with pytest.raises(psycopg2.Error, match=r"objects outside the v1 schemas .* depend on them: .*zz281_on_v1"):
        _apply(c)


def test_v1_schemas_refused_when_an_outside_column_uses_their_type(conn):
    c = conn.cursor()
    _standins(c)
    _v1_with_enum_and_fk(c)
    c.execute("CREATE TABLE shared.zz281_uses_enum (s silver.zz281_status)")
    with pytest.raises(psycopg2.Error, match="objects outside the v1 schemas"):
        _apply(c)


def test_v1_schema_with_an_object_281_does_not_drop_fails_instead_of_sweeping_it(conn):
    c = conn.cursor()
    _standins(c)
    c.execute("CREATE SCHEMA IF NOT EXISTS ops AUTHORIZATION cis_owner")
    c.execute("CREATE FUNCTION ops.zz281_f() RETURNS int LANGUAGE sql AS 'SELECT 1'")
    c.execute("ALTER FUNCTION ops.zz281_f() OWNER TO cis_owner")
    with pytest.raises(psycopg2.Error, match="cannot drop schema ops because other objects depend on it"):
        _apply(c)


def test_checks_put_in_devs_form_without_a_pending_type(conn, cur):
    # the columns are text now (first apply); a CHECK in another form with the same values still converges
    cur.execute("""ALTER TABLE silver_aa_internal.raw_tours DROP CONSTRAINT IF EXISTS raw_tours_review_status_check,
                   ADD CONSTRAINT raw_tours_review_status_check
                   CHECK (review_status::text IN ('pending_review', 'reviewed', 'approved', 'rejected'))""")
    cur.execute("""SELECT pg_get_constraintdef(oid) FROM pg_constraint
                    WHERE conrelid = 'silver_aa_internal.raw_tours'::regclass
                      AND conname = 'raw_tours_review_status_check'""")
    assert cur.fetchone()[0] != DEV_CHECKS["raw_tours_review_status_check"]
    del conn.notices[:]
    _apply(cur)
    assert any("step 5 is a no-op" in n for n in conn.notices), conn.notices
    assert any("raw_tours_review_status_check in Dev's form" in n for n in conn.notices), conn.notices
    cur.execute("""SELECT pg_get_constraintdef(oid) FROM pg_constraint
                    WHERE conrelid = 'silver_aa_internal.raw_tours'::regclass
                      AND conname = 'raw_tours_review_status_check'""")
    assert cur.fetchone()[0] == DEV_CHECKS["raw_tours_review_status_check"]
