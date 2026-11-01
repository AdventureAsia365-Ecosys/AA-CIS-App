"""AA-1017 / AA-1016 / AA-900 — migrations 320 (registry v2: families, stage x model params, deployment api_style,
the truncated health class, route_deadline_s) and 321 (shared.tenant_ai_budget, shared.prompt_version) against real
Postgres.

conftest applies every migration, 320 and 321 included. Each test re-runs both files (idempotent, errors not
swallowed) and checks the tables, CHECKs, FKs, grants and the backfill. One test takes 320 back to the pre-320 shape and
applies it again: the route snapshot (the S262 plan's Q0 hash over role config, deployments, connections, catalog and
aliases, explicit columns) is the same before and after, so 320 changes no route.
"""
import os
import sys

import psycopg2
import psycopg2.errors
import psycopg2.extras
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from _acc2_routes import candidates_off_routes  # noqa: E402
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER, TENANT_ID  # noqa: E402

MIGRATIONS = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")
M320 = os.path.join(MIGRATIONS, "320_llm_registry_v2_families_params.sql")
M321 = os.path.join(MIGRATIONS, "321_tenant_ai_budget_prompt_registry.sql")
# AA-1017 (324): the five acc2 candidate models are catalogued under the 320 families (vendor amazon / meta / moonshot),
# which the pre-320 vendor CHECK refuses. The fixture sets them aside for the test and applies 324 again afterwards.
M324 = os.path.join(MIGRATIONS, "324_llm_acc2_candidate_models.sql")
# AA-1024: 334 fills the route_deadline_s that a 320 re-run from the pre-320 shape leaves NULL, and 337 records the
# Llama 4 capabilities on the rows 324 re-creates; the teardown applies both again so later modules see the full chain.
M334 = os.path.join(MIGRATIONS, "334_llm_route_deadlines.sql")
M337 = os.path.join(MIGRATIONS, "337_llm_llama4_structured_prompt_mode.sql")
FAMILIES = {"anthropic", "openai", "cohere", "typesafe", "amazon", "meta", "moonshot"}
OLD_HEALTH_CLASSES = "('access_denied', 'quota_exhausted', 'throttled', 'credential', 'other')"
OLD_VENDORS = "('anthropic', 'openai', 'cohere', 'typesafe')"

# The S262 plan's Q0 (route snapshot hash), explicit columns: a new column on these tables never changes it.
Q0 = """
SELECT md5(string_agg(t, E'\\n' ORDER BY t)) AS route_hash, count(*) AS n FROM (
  SELECT format('rc|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s', stage, role, provider, model_id,
         coalesce(account_route,''), fallback_model_ids, coalesce(shadow_model_id,''),
         shadow_sample_pct, coalesce(allowed_scopes::text,''), is_active) AS t
    FROM shared.llm_role_config
  UNION ALL SELECT format('dp|%s|%s|%s|%s|%s', model_key, connection_key, wire_id, priority, enabled)
    FROM shared.llm_model_deployment
  UNION ALL SELECT format('cn|%s|%s|%s|%s', connection_key, kind, enabled, data_scope)
    FROM shared.llm_provider_connection
  UNION ALL SELECT format('ct|%s|%s|%s|%s', model_key, enabled, api_style, bedrock_profile_ids::text)
    FROM shared.llm_model_catalog
  UNION ALL SELECT format('al|%s', a::text) FROM shared.llm_model_alias a
) s
"""


def _run(c, path):
    c.execute(open(path, encoding="utf-8").read())


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    _run(c, M320)
    _run(c, M321)
    _set_324_aside(c)
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        c.execute("DELETE FROM shared.stage_model_params WHERE stage LIKE 'zz_aa1017%'")
        c.execute("DELETE FROM shared.prompt_version WHERE stage LIKE 'zz_aa1017%'")
        c.execute("DELETE FROM shared.tenant_ai_budget WHERE updated_by = 'zz-aa1017'")
        _run(c, M320)
        _run(c, M321)
        with candidates_off_routes(c):          # AA-1026 I6: 331 / 332 routes name 324's models; 324's guard refuses
            _run(c, M324)
        _run(c, M334)
        _run(c, M337)
        conn.close()


def _set_324_aside(c):
    """Migration 324's rows out (they need the 320 families); the fixture applies 324 again at teardown."""
    c.execute("DELETE FROM shared.llm_model_deployment WHERE updated_by = 'migration-324-aa1017'")
    # 333 (AA-1026) re-signs the llama4-scout row when it records the model's forced tool choice, 337 (AA-1024) both
    # Llama 4 rows when it records their structured output mode
    c.execute("DELETE FROM shared.llm_model_catalog "
              "WHERE updated_by IN ('migration-324-aa1017', 'migration-333-aa1026', 'migration-337-aa1024')")
    c.execute("DELETE FROM shared.schema_versions WHERE version = '324'")


def _columns(c, table: str) -> set:
    c.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = 'shared' AND table_name = %s",
              (table,))
    return {r["column_name"] for r in c.fetchall()}


def _refused(c, sql: str, args=(), error=psycopg2.errors.CheckViolation):
    c.execute("BEGIN")
    with pytest.raises(error):
        c.execute(sql, args)
    c.execute("ROLLBACK")


def _undo_320(c):
    """The pre-320 shape (what 304 / 309 / 137 left), so 320 can be applied from it."""
    c.execute("DROP TABLE IF EXISTS shared.stage_model_params")
    c.execute("ALTER TABLE shared.llm_model_catalog DROP CONSTRAINT IF EXISTS llm_model_catalog_family_fk")
    c.execute("ALTER TABLE shared.llm_model_catalog DROP COLUMN IF EXISTS family")
    c.execute("DROP TABLE IF EXISTS shared.llm_model_family")
    c.execute("ALTER TABLE shared.llm_model_catalog DROP CONSTRAINT IF EXISTS llm_model_catalog_vendor_check")
    c.execute(f"ALTER TABLE shared.llm_model_catalog ADD CONSTRAINT llm_model_catalog_vendor_check "
              f"CHECK (vendor IN {OLD_VENDORS})")
    c.execute("ALTER TABLE shared.llm_model_deployment DROP COLUMN IF EXISTS api_style")
    c.execute("ALTER TABLE shared.llm_provider_health DROP CONSTRAINT IF EXISTS llm_provider_health_class_check")
    c.execute(f"ALTER TABLE shared.llm_provider_health ADD CONSTRAINT llm_provider_health_class_check "
              f"CHECK (last_error_class IS NULL OR last_error_class IN {OLD_HEALTH_CLASSES})")
    c.execute("ALTER TABLE shared.llm_role_config DROP COLUMN IF EXISTS route_deadline_s")
    c.execute("DELETE FROM shared.schema_versions WHERE version = '320'")


# ── 320 ───────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_320_applies_from_the_pre_320_shape_twice_and_changes_no_route(cur):
    cur.execute(Q0)
    before = cur.fetchone()
    _undo_320(cur)
    cur.execute(Q0)
    assert cur.fetchone() == before
    _run(cur, M320)
    _run(cur, M320)
    cur.execute(Q0)
    assert cur.fetchone() == before
    cur.execute("SELECT 1 FROM shared.schema_versions WHERE version = '320'")
    assert cur.fetchone()


def test_families_and_the_catalog_backfill(cur):
    cur.execute("SELECT family_key FROM shared.llm_model_family")
    assert {r["family_key"] for r in cur.fetchall()} >= FAMILIES
    cur.execute("SELECT model_key, vendor, family FROM shared.llm_model_catalog")
    rows = cur.fetchall()
    assert rows, "conftest seeds catalog rows"
    for r in rows:
        assert r["family"] == r["vendor"], r
    cur.execute("SELECT convalidated FROM pg_constraint WHERE conname = 'llm_model_catalog_family_fk'")
    assert cur.fetchone()["convalidated"] is True


def test_a_new_maker_needs_no_check_change_but_a_known_family(cur):
    insert = ("INSERT INTO shared.llm_model_catalog (model_key, label, vendor, family, provider, api_style, "
              "bedrock_profile_ids, callable_via, supports_temperature, enabled, updated_at, updated_by) VALUES "
              "('zz-aa1017-nova', 'x', 'amazon', %s, 'bedrock', 'converse', '{}', '{llm_client}', true, false, "
              "now(), 'test')")
    cur.execute("BEGIN")
    cur.execute(insert, ("amazon",))                      # the vendor CHECK is gone, the family exists
    cur.execute("ROLLBACK")
    _refused(cur, insert, ("nobody",), psycopg2.errors.ForeignKeyViolation)
    cur.execute("SELECT 1 FROM pg_constraint WHERE conname = 'llm_model_catalog_vendor_check'")
    assert cur.fetchone() is None


def test_stage_model_params_is_empty_and_checked(cur):
    assert _columns(cur, "stage_model_params") == {
        "stage", "model_key", "max_tokens", "min_output_tokens", "temperature", "effort", "reasoning_mode", "notes",
        "created_at", "updated_at", "updated_by"}
    cur.execute("SELECT count(*) AS n FROM shared.stage_model_params WHERE stage NOT LIKE 'zz_aa1017%'")
    assert cur.fetchone()["n"] == 0
    ins = ("INSERT INTO shared.stage_model_params (stage, model_key, max_tokens, min_output_tokens, temperature, "
           "effort, reasoning_mode, updated_by) VALUES (%s, %s, %s, %s, %s, %s, %s, 'test')")
    cur.execute("BEGIN")
    cur.execute(ins, ("zz_aa1017_ok", "sonnet-5", 8000, 2000, 0.2, "high", "adaptive"))
    cur.execute("ROLLBACK")
    for bad in (("zz_aa1017_a", "sonnet-5", 100, 200, None, None, None),       # min above max
                ("zz_aa1017_b", "sonnet-5", None, None, 3, None, None),         # temperature above 2
                ("zz_aa1017_c", "sonnet-5", None, None, None, "extreme", None),  # unknown effort
                ("zz_aa1017_d", "sonnet-5", None, None, None, None, "always"),   # unknown reasoning mode
                ("ZZ bad", "sonnet-5", None, None, None, None, None)):           # stage name
        _refused(cur, ins, bad)
    _refused(cur, ins, ("zz_aa1017_e", "no-such-model", None, None, None, None, None),
             psycopg2.errors.ForeignKeyViolation)


def test_deployment_api_style_is_null_and_checked(cur):
    cur.execute("SELECT count(*) AS n FROM shared.llm_model_deployment WHERE api_style IS NOT NULL")
    assert cur.fetchone()["n"] == 0
    _refused(cur, "UPDATE shared.llm_model_deployment SET api_style = 'smoke_signals'")
    cur.execute("BEGIN")
    cur.execute("UPDATE shared.llm_model_deployment SET api_style = 'converse' WHERE model_key = 'sonnet-5'")
    cur.execute("ROLLBACK")


def test_health_takes_truncated(cur):
    cur.execute("SELECT 1 FROM shared.llm_model_catalog WHERE model_key = 'sonnet-5'")
    if cur.fetchone() is None:
        pytest.skip("catalog row sonnet-5 is not in this DB")
    cur.execute("BEGIN")
    cur.execute("INSERT INTO shared.llm_provider_health (connection_key, model_key, last_error_class) "
                "VALUES ('acc3', 'sonnet-5', 'truncated') ON CONFLICT (connection_key, model_key) "
                "DO UPDATE SET last_error_class = 'truncated'")
    cur.execute("ROLLBACK")
    _refused(cur, "INSERT INTO shared.llm_provider_health (connection_key, model_key, last_error_class) "
                  "VALUES ('acc3', 'sonnet-5', 'nonsense')")


def test_route_deadline_is_bounded_and_set_by_334_only(cur):
    # 320 adds the column with no value; 334 (AA-1024 A3) gives every row the deadline of its role
    # (tests/integration/test_aa1024_migrations_334_337.py), so no row is NULL after the full chain.
    cur.execute("SELECT count(*) AS n FROM shared.llm_role_config WHERE route_deadline_s IS NULL")
    assert cur.fetchone()["n"] == 0
    _refused(cur, "UPDATE shared.llm_role_config SET route_deadline_s = 0")
    cur.execute("SELECT privilege_type FROM information_schema.column_privileges WHERE table_schema = 'shared' "
                "AND table_name = 'llm_role_config' AND column_name = 'route_deadline_s' AND grantee = 'cis_app'")
    assert cur.fetchall() == []                            # migration-only


_OWNER_DEFAULT = "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO cis_app, aa_app_user"


def _table_privs(c, table: str, role: str) -> set:
    c.execute("SELECT privilege_type FROM information_schema.role_table_grants "
              "WHERE table_schema = 'shared' AND table_name = %s AND grantee = %s", (table, role))
    return {r["privilege_type"] for r in c.fetchall()}


def test_320_321_grants_hold_under_the_owner_default_privileges(cur):
    """On Dev DB Migrate creates the tables as cis_owner, whose default privileges in shared (CIS 245) give cis_app
    SELECT / INSERT / UPDATE / DELETE on every new table. The test user stands in for cis_owner: the same default
    privileges FOR ROLE CURRENT_USER, the four tables created again, and the files' REVOKE ALL must leave only the
    intended grants."""
    cur.execute("SELECT count(*) AS n FROM pg_default_acl d JOIN pg_namespace n ON n.oid = d.defaclnamespace "
                "WHERE n.nspname = 'shared' AND d.defaclrole = (SELECT oid FROM pg_roles WHERE rolname = current_user) "
                "AND d.defaclobjtype = 'r'")
    assert cur.fetchone()["n"] == 0, "the test user already has default privileges in shared"
    cur.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA shared {_OWNER_DEFAULT}")
    try:
        cur.execute("DROP TABLE shared.prompt_version, shared.tenant_ai_budget")
        _undo_320(cur)
        cur.execute("CREATE TABLE shared.zz_aa1017_default_acl (x int)")      # the default really applies here
        assert _table_privs(cur, "zz_aa1017_default_acl", "cis_app") == {"SELECT", "INSERT", "UPDATE", "DELETE"}
        _run(cur, M320)
        _run(cur, M321)
    finally:
        cur.execute("DROP TABLE IF EXISTS shared.zz_aa1017_default_acl")
        cur.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA shared "
                    f"{_OWNER_DEFAULT.replace('GRANT', 'REVOKE').replace(' TO ', ' FROM ')}")
    for table, cis in (("llm_model_family", {"SELECT"}), ("stage_model_params", {"SELECT"}),
                       ("prompt_version", {"SELECT"}),
                       ("tenant_ai_budget", {"SELECT", "INSERT", "UPDATE", "DELETE"})):
        assert _table_privs(cur, table, "cis_app") == cis, table
        assert _table_privs(cur, table, "aa_app_user") == set(), table
        assert _table_privs(cur, table, "PUBLIC") == set(), table


def test_320_grants(cur):
    for table in ("llm_model_family", "stage_model_params"):
        for role, privs in (("cis_app", {"SELECT"}), ("tripplanner", set()), ("aa_app_user", set())):
            cur.execute("SELECT privilege_type FROM information_schema.role_table_grants "
                        "WHERE table_schema = 'shared' AND table_name = %s AND grantee = %s", (table, role))
            assert {r["privilege_type"] for r in cur.fetchall()} == privs, (table, role)


# ── 321 ───────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_321_tables_have_no_rows_and_no_prompt_text(cur):
    assert _columns(cur, "tenant_ai_budget") == {
        "tenant_id", "monthly_usd", "alert_pct", "approval_pct", "hard_stop", "notes", "created_at", "updated_at",
        "updated_by"}
    assert _columns(cur, "prompt_version") == {
        "stage", "version", "prompt_sha256", "source_path", "notes", "created_at", "created_by"}
    for table in ("tenant_ai_budget", "prompt_version"):
        cur.execute(f"SELECT count(*) AS n FROM shared.{table}")
        assert cur.fetchone()["n"] == 0, table
    cur.execute("SELECT 1 FROM shared.schema_versions WHERE version = '321'")
    assert cur.fetchone()


def test_tenant_ai_budget_checks_and_tenant_fk(cur):
    ins = ("INSERT INTO shared.tenant_ai_budget (tenant_id, monthly_usd, alert_pct, approval_pct, hard_stop, "
           "updated_by) VALUES (%s, %s, %s, %s, %s, 'zz-aa1017')")
    cur.execute("BEGIN")
    cur.execute(ins, (TENANT_ID, 25, 100, 150, True))
    cur.execute("SELECT alert_pct AS a, approval_pct AS p FROM shared.tenant_ai_budget WHERE tenant_id = %s",
                (TENANT_ID,))
    assert dict(cur.fetchone()) == {"a": 100, "p": 150}
    cur.execute("ROLLBACK")
    for bad in ((TENANT_ID, -1, 100, 150, False), (TENANT_ID, 10, 0, 150, False),
                (TENANT_ID, 10, 150, 100, False)):              # approval below the alert
        _refused(cur, ins, bad)
    _refused(cur, ins, ("11111111-2222-3333-4444-555555555555", 10, 100, 150, False),
             psycopg2.errors.ForeignKeyViolation)
    cur.execute("SELECT confdeltype FROM pg_constraint WHERE conrelid = 'shared.tenant_ai_budget'::regclass "
                "AND contype = 'f'")
    assert cur.fetchone()["confdeltype"] == "c"             # erased with the tenant


def test_prompt_version_checks(cur):
    ins = ("INSERT INTO shared.prompt_version (stage, version, prompt_sha256, source_path, created_by) "
           "VALUES (%s, %s, %s, %s, 'test')")
    sha = "a" * 64
    cur.execute("BEGIN")
    cur.execute(ins, ("zz_aa1017_w", "v1", sha, "services/example/prompts/writer.md"))
    with pytest.raises(psycopg2.errors.UniqueViolation):     # one version per prompt hash and stage
        cur.execute(ins, ("zz_aa1017_w", "v2", sha, "services/example/prompts/writer.md"))
    cur.execute("ROLLBACK")
    for bad in (("zz_aa1017_w", "v1", "not-a-sha", "a.md"),
                ("zz_aa1017_w", "v1", sha, "../secrets.md"),
                ("zz_aa1017_w", "v 1", sha, "a.md")):
        _refused(cur, ins, bad)


def test_321_grants(cur):
    for table, cis in (("tenant_ai_budget", {"SELECT", "INSERT", "UPDATE", "DELETE"}), ("prompt_version", {"SELECT"})):
        for role, privs in (("cis_app", cis), ("tripplanner", set()), ("aa_app_user", set())):
            cur.execute("SELECT privilege_type FROM information_schema.role_table_grants "
                        "WHERE table_schema = 'shared' AND table_name = %s AND grantee = %s", (table, role))
            assert {r["privilege_type"] for r in cur.fetchall()} == privs, (table, role)
