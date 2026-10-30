"""AA-1002 (gateway slice S1) — migration 304 (connection registry, model deployments, widened CHECKs) against real
Postgres.

conftest applies every migration, 304 included. These tests run the file again directly (errors NOT swallowed): from
the pre-304 state, twice (idempotent), then check the CHECKs, the grants, and that the backfill describes the paths
today's code takes (the first enabled deployment of every catalog model is the account / wire id the gateway calls).
Every test leaves the post-304 state behind.
"""
import os
import sys

import psycopg2
import psycopg2.extras
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from shared.llm_client import bedrock_satellite  # noqa: E402
from shared.llm_client.catalog import LEGACY_KEYS, _row_to_model  # noqa: E402
from shared.llm_client.connections import check_base_url, check_secret_name  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                         "304_llm_connection_deployment.sql")
OLD_CHECKS = {
    ("llm_model_catalog", "llm_model_catalog_provider_check"): "CHECK (provider IN ('bedrock', 'openai', 'typesafe'))",
    ("llm_model_catalog", "llm_model_catalog_api_style_check"):
        "CHECK (api_style IN ('anthropic_native', 'converse', 'openai_chat', 'embed', 'decide'))",
    ("spend_budget", "spend_budget_provider_check"):
        "CHECK (provider IN ('dfs', 'bedrock', 'openai', 'jev', 'geocoder'))",
}
LEGACY_ACC3 = {"haiku": bedrock_satellite._PROFILE_ID_HAIKU, "sonnet": bedrock_satellite._PROFILE_ID_SONNET}


def _run(c):
    c.execute(open(MIGRATION, encoding="utf-8").read())


# AA-1002 S4: 309 (shared.llm_provider_health, FK to the connection) and 310 (the admin edit grant) build on 304's
# tables; _undo drops them first and the fixture puts them back, so later modules see the post-310 state.
LATER = [os.path.join(os.path.dirname(MIGRATION), f) for f in
         ("309_llm_provider_health.sql", "310_llm_connection_admin_edit_grant.sql")]


def _run_later(c):
    for path in LATER:
        c.execute(open(path, encoding="utf-8").read())


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    _run(c)                               # other modules may have deleted catalog rows (cascade): back to post-304
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        c.execute("DELETE FROM shared.llm_model_deployment WHERE model_key LIKE 'zz-aa1002%'")
        c.execute("DELETE FROM shared.llm_model_catalog WHERE model_key LIKE 'zz-aa1002%'")
        c.execute("DELETE FROM shared.llm_provider_connection WHERE connection_key LIKE 'zz-aa1002%'")
        c.execute("DELETE FROM shared.spend_budget WHERE scope = 'job:zz_aa1002'")
        _run(c)
        _run_later(c)
        conn.close()


def _undo(c):
    """The pre-304 state: no new tables, no capabilities column, the 181 / 169 / 226 CHECKs, 304 unregistered."""
    c.execute("DROP TABLE IF EXISTS shared.llm_provider_health")          # 309: references the connection table
    c.execute("DELETE FROM shared.schema_versions WHERE version IN ('309', '310')")
    c.execute("DROP TABLE IF EXISTS shared.llm_model_deployment")
    c.execute("DROP TABLE IF EXISTS shared.llm_provider_connection")
    c.execute("ALTER TABLE shared.llm_model_catalog DROP COLUMN IF EXISTS capabilities")
    for (table, name), check in OLD_CHECKS.items():
        c.execute(f"ALTER TABLE shared.{table} DROP CONSTRAINT IF EXISTS {name}")
        c.execute(f"ALTER TABLE shared.{table} ADD CONSTRAINT {name} {check}")
    c.execute("DELETE FROM shared.schema_versions WHERE version = '304'")


def _profiles(c):
    c.execute("SELECT model_key, bedrock_profile_ids FROM shared.llm_model_catalog ORDER BY model_key")
    return [(r["model_key"], r["bedrock_profile_ids"]) for r in c.fetchall()]


def _deployments(c):
    # the rows 304 writes: 306 (AA-1002 S2) adds anthropic-direct seeds that a 304 re-run does not recreate
    c.execute("SELECT model_key, connection_key, wire_id, priority, enabled, origin "
              "FROM shared.llm_model_deployment WHERE updated_by <> 'migration-306-aa1002' "
              "ORDER BY model_key, priority, connection_key")
    return [tuple(r.values()) for r in c.fetchall()]


def _insert_conn(c, **kw):
    row = dict(connection_key="zz-aa1002-x", kind="anthropic", auth_mode="api_key", label="t",
               data_scope="vendor_direct", enabled=False, updated_by="test")
    row.update(kw)
    cols = ", ".join(row)
    c.execute(f"INSERT INTO shared.llm_provider_connection ({cols}) VALUES ({', '.join(['%s'] * len(row))})",
              list(row.values()))


# ── the file ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_applies_from_the_pre_304_state_twice_and_keeps_bedrock_profile_ids(cur):
    before_profiles = _profiles(cur)
    before = _deployments(cur)
    _undo(cur)
    _run(cur)
    first = _deployments(cur)
    _run(cur)                                       # second run: no change, no error
    assert _deployments(cur) == first == before
    assert _profiles(cur) == before_profiles        # TripPlanner's column untouched
    cur.execute("SELECT 1 FROM shared.schema_versions WHERE version = '304'")
    assert cur.fetchone()


def test_rerun_keeps_an_admin_edit(cur):
    cur.execute("UPDATE shared.llm_provider_connection SET label = 'edited', updated_by = 'admin' "
                "WHERE connection_key = 'acc2'")
    cur.execute("UPDATE shared.llm_model_deployment SET priority = 5, origin = 'admin' "
                "WHERE model_key = 'cohere-embed-v4' AND connection_key = 'acc2'")
    _run(cur)
    cur.execute("SELECT label FROM shared.llm_provider_connection WHERE connection_key = 'acc2'")
    assert cur.fetchone()["label"] == "edited"
    cur.execute("SELECT priority FROM shared.llm_model_deployment "
                "WHERE model_key = 'cohere-embed-v4' AND connection_key = 'acc2'")
    assert cur.fetchone()["priority"] == 5
    cur.execute("UPDATE shared.llm_provider_connection SET label = 'Bedrock, main account (task role)', "
                "updated_by = 'migration-304-aa1002' WHERE connection_key = 'acc2'")
    cur.execute("UPDATE shared.llm_model_deployment SET priority = 20, origin = 'bedrock_profile' "
                "WHERE model_key = 'cohere-embed-v4' AND connection_key = 'acc2'")


# ── seeds and backfill = today's paths ─────────────────────────────────────────────────────────────────────────────

def test_seeded_connections(cur):
    cur.execute("SELECT * FROM shared.llm_provider_connection ORDER BY connection_key")
    rows = {r["connection_key"]: r for r in cur.fetchall()}
    assert set(rows) >= {"acc3", "acc2", "openai-direct", "anthropic-direct", "bedrock-llm-org"}
    assert {k for k, r in rows.items() if r["enabled"]} == {"acc3", "acc2", "openai-direct"}
    acc3 = rows["acc3"]
    assert (acc3["role_name"], acc3["external_id"], acc3["region"], acc3["account_id"], acc3["account_id_env"]) == (
        "AA3-Bedrock-Invoker", bedrock_satellite.ACC3_EXTERNAL_ID, bedrock_satellite.ACC3_REGION, None,
        "BEDROCK_ACCT_PRIMARY")
    assert rows["openai-direct"]["credential_env"] == "OPENAI_API_KEY"
    for r in rows.values():
        check_secret_name(r["secret_name"])
        check_base_url(r["kind"], r["base_url"])


def test_first_enabled_deployment_is_the_path_the_gateway_takes_today(cur):
    cur.execute("SELECT * FROM shared.llm_model_catalog ORDER BY model_key")
    models = [_row_to_model(r) for r in cur.fetchall()]
    cur.execute("SELECT model_key, connection_key, wire_id FROM shared.llm_model_deployment "
                "WHERE enabled ORDER BY model_key, priority, connection_key")
    first: dict = {}
    for r in cur.fetchall():
        first.setdefault(r["model_key"], (r["connection_key"], r["wire_id"]))
    checked = 0
    for m in models:
        if m.provider == "typesafe":
            assert m.model_key not in first                  # Jev stays outside the registry in S1
            continue
        if m.model_key in LEGACY_KEYS and m.provider == "bedrock":
            expected = ("acc3", LEGACY_ACC3[m.model_key])    # _legacy_chain -> invoke_claude constants
        elif m.provider == "openai":
            expected = ("openai-direct", m.wire_model or m.model_key)   # _call_openai
        else:
            account = m.account_for(None)                     # _call_catalog_model / embed
            expected = (account, m.bedrock_profile_ids[account]) if account else None
        assert first.get(m.model_key) == expected, m.model_key
        checked += 1
    assert checked >= 10


def test_acc2_deployments_are_enabled_only_where_the_code_calls_acc2(cur):
    cur.execute("SELECT d.model_key, d.enabled, m.api_style FROM shared.llm_model_deployment d "
                "JOIN shared.llm_model_catalog m USING (model_key) WHERE d.connection_key = 'acc2'")
    for r in cur.fetchall():
        assert r["enabled"] == (r["api_style"] == "embed"), r["model_key"]


def test_deleting_a_model_deletes_its_deployments(cur):
    cur.execute("BEGIN")
    cur.execute("DELETE FROM shared.llm_model_catalog WHERE model_key = 'sonnet-5'")
    cur.execute("SELECT count(*) AS n FROM shared.llm_model_deployment WHERE model_key = 'sonnet-5'")
    assert cur.fetchone()["n"] == 0
    cur.execute("ROLLBACK")


# ── CHECKs ─────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fields", [
    dict(secret_name="aa-cis/dev/rds"),
    dict(secret_name="aa-llm/dev/../rds"),
    dict(base_url="http://api.anthropic.com"),
    dict(base_url="https://user@api.anthropic.com"),
    dict(kind="bedrock", auth_mode="task_role", region="us-west-1", data_scope="aws_only",
         base_url="https://bedrock.example.com"),
    dict(kind="vertex"),
    dict(kind="openai", auth_mode="assume_role"),
    dict(credential_env="AWS_SECRET_ACCESS_KEY"),
    dict(account_id_env="HOME"),
    dict(account_id="12345"),
    dict(enabled=True),                                               # api_key with no secret / env
    dict(kind="bedrock", auth_mode="assume_role", data_scope="aws_only", role_name="r", external_id="e",
         region="us-west-1", enabled=True),                           # no account
    dict(kind="bedrock", auth_mode="task_role", data_scope="aws_only", region="us-west-1",
         secret_name="aa-llm/dev/x"),                                 # task role holds no credential
    dict(connection_key="ZZ bad key"),
    dict(timeout_s=0),
    dict(data_scope="anywhere"),
])
def test_connection_checks_refuse(cur, fields):
    cur.execute("BEGIN")
    with pytest.raises(psycopg2.errors.CheckViolation):
        _insert_conn(cur, **fields)
    cur.execute("ROLLBACK")


def test_connection_checks_accept_a_complete_row(cur):
    _insert_conn(cur, secret_name="aa-llm/dev/zz-aa1002", base_url="https://api.anthropic.com", enabled=True)
    _insert_conn(cur, connection_key="zz-aa1002-b", kind="bedrock", auth_mode="assume_role", data_scope="aws_only",
                 account_id="123456789012", role_name="AA-LLM-Invoker", external_id="e", region="us-east-1",
                 enabled=True)


def test_widened_checks_take_the_new_values(cur):
    cur.execute("BEGIN")
    cur.execute("INSERT INTO shared.llm_model_catalog (model_key, label, vendor, provider, api_style, "
                "bedrock_profile_ids, callable_via, supports_temperature, enabled, updated_at, updated_by) VALUES "
                "('zz-aa1002-m', 'x', 'anthropic', 'anthropic', 'anthropic_messages', '{}', '{llm_client}', false, "
                "false, now(), 'test'), "
                "('zz-aa1002-r', 'x', 'openai', 'azure_openai', 'openai_responses', '{}', '{llm_client}', false, "
                "false, now(), 'test')")
    cur.execute("INSERT INTO shared.llm_model_deployment (model_key, connection_key, wire_id, origin, updated_by) "
                "VALUES ('zz-aa1002-m', 'anthropic-direct', 'claude-sonnet-4-6', 'admin', 'test')")
    cur.execute("INSERT INTO shared.spend_budget (provider, scope, per_run_usd, hard_stop, alert_pct, updated_by) "
                "VALUES ('anthropic', 'job:zz_aa1002', 1, true, 80, 'test')")
    cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("UPDATE shared.llm_model_catalog SET capabilities = '[]'::jsonb WHERE model_key = 'haiku'")
    cur.execute("ROLLBACK")


# ── grants ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_cis_app_reads_only_and_the_tenant_pool_has_nothing(cur):
    for table in ("llm_provider_connection", "llm_model_deployment"):
        for role, expected in (("cis_app", {"SELECT"}), ("aa_app_user", set()), ("catalog_owner", set())):
            cur.execute("SELECT privilege_type FROM information_schema.role_table_grants "
                        "WHERE table_schema = 'shared' AND table_name = %s AND grantee = %s", (table, role))
            assert {r["privilege_type"] for r in cur.fetchall()} == expected, (table, role)
