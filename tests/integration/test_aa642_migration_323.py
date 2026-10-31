"""AA-642 S8 (root ADR 0009 decision 13) — migration 323: the TripPlanner gateway contract views
catalog.v_llm_gateway_config_v1 and catalog.v_llm_app_spend_today_v1, applied the way DB Migrate applies a cis/ file
since X1 S22.

conftest skips 323 in the replay (its catalog recipe needs 245's owners). This module builds on
test_aa860_migration_246's module fixture (Dev after 244 + 245, db_migrator with rl-002's memberships) and applies 323
as db_migrator under the live-owner switch (errors NOT swallowed), twice. Teardown drops what 323 created and revokes
its column grants, so later modules see the replay state.

  * the migration: registered once; both views catalog_owner's and security_barrier, readable by tripplanner only;
    catalog_owner reads only the listed columns of the four shared.llm_* tables (no table-wide SELECT, nothing on
    llm_provider_connection); CREATE on schema catalog revoked; the columns are the ones shared/aa_llm/contract.py
    names;
  * the gateway view (as tripplanner, rolled back): TripPlanner stages only, one row per model of the chain in order,
    a key missing from the catalog kept with in_catalog false, deployments ordered by priority with their overrides;
  * the spend view (as tripplanner, rolled back): today's tripplanner rows only (the v2 app column or the pre-v2
    quality_signal app), unpriced and failed calls counted, other apps and yesterday left out;
  * the contract SELECTs of shared/aa_llm/contract.py run on both views.
Invented stage, model and connection names only.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa860_migration_245 as t245  # noqa: E402
import test_aa860_migration_246 as t246  # noqa: E402
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401  (module fixture: Dev after 244 + 245)
from test_aa860_migration_246 import db_migrator_login  # noqa: E402,F401  (module fixture: db_migrator, 246)

from shared.aa_llm import contract  # noqa: E402

HERE = os.path.dirname(__file__)
MIGRATION = os.path.join(HERE, "..", "..", "api", "migrations", "323_llm_contract_views.sql")
VIEWS = (contract.GATEWAY_CONFIG_VIEW, contract.APP_SPEND_VIEW)
GRANTED = {
    "llm_role_config": {"stage", "role", "model_id", "fallback_model_ids", "account_route", "is_active", "updated_at",
                        "shadow_model_id", "shadow_sample_pct", "allowed_scopes"},
    "llm_model_catalog": {"model_key", "vendor", "provider", "api_style", "bedrock_profile_ids", "wire_model",
                          "max_output_tokens", "enabled", "price_in_per_mtok", "price_out_per_mtok",
                          "price_cache_read_per_mtok", "price_cache_write_per_mtok"},
    "llm_model_deployment": {"model_key", "connection_key", "wire_id", "priority", "enabled", "price_in_override",
                             "price_out_override", "price_cache_read_override", "price_cache_write_override",
                             "price_cache_write_1h_override"},
    "llm_call_log": {"created_at", "stage", "cost_usd", "status", "app", "quality_signal"},
}


def _apply(path):
    c = t246._migrator()
    c.autocommit = True
    try:
        c.cursor().execute(t246._switched(open(path, encoding="utf-8").read()))     # raises on any error
    finally:
        c.close()


def undo_323(a):
    for view in VIEWS:
        a.execute(f"DROP VIEW IF EXISTS {view}")
    for table, cols in GRANTED.items():
        a.execute(f"REVOKE SELECT ({', '.join(sorted(cols))}) ON shared.{table} FROM catalog_owner")
    a.execute("DELETE FROM shared.schema_versions WHERE version = '323'")
    a.execute("REVOKE CREATE ON SCHEMA catalog FROM catalog_owner")


@pytest.fixture(scope="module", autouse=True)
def applied_323(db_migrator_login):  # noqa: F811
    admin = t245._connect()
    admin.autocommit = True
    a = admin.cursor()
    try:
        undo_323(a)
        _apply(MIGRATION)
        _apply(MIGRATION)                                    # idempotent re-run
        yield
    finally:
        undo_323(a)
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


def _rows(cur, sql, *args):
    cur.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def test_registered_once(cur):
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '323'")
    assert cur.fetchone()[0] == 1


def test_views_owner_barrier_and_exact_grant(cur):
    for view in VIEWS:
        cur.execute("SELECT pg_get_userbyid(relowner), reloptions FROM pg_class WHERE oid = %s::regclass", (view,))
        owner, opts = cur.fetchone()
        assert owner == "catalog_owner" and "security_barrier=true" in (opts or []), view
        cur.execute("SELECT has_table_privilege('tripplanner', %s, 'SELECT')", (view,))
        assert cur.fetchone()[0] is True, view
        for role in ("booking", "aa_app_user", "cis_app"):
            cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT')", (role, view))
            assert cur.fetchone()[0] is False, (role, view)
    cur.execute("SELECT has_schema_privilege('catalog_owner', 'catalog', 'CREATE')")
    assert cur.fetchone()[0] is False


def test_catalog_owner_reads_only_the_listed_columns(cur):
    for table, cols in GRANTED.items():
        cur.execute("SELECT has_table_privilege('catalog_owner', %s, 'SELECT')", (f"shared.{table}",))
        assert cur.fetchone()[0] is False, f"table-wide SELECT on {table}"
        cur.execute("""SELECT a.attname FROM pg_attribute a
                        WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
                          AND has_column_privilege('catalog_owner', a.attrelid, a.attnum, 'SELECT')""",
                    (f"shared.{table}",))
        assert {r[0] for r in cur.fetchall()} == cols, table
    cur.execute("SELECT has_any_column_privilege('catalog_owner', 'shared.llm_provider_connection', 'SELECT')")
    assert cur.fetchone()[0] is False


def test_columns_are_the_contract(cur):
    for view, want in ((contract.GATEWAY_CONFIG_VIEW, contract.GATEWAY_CONFIG_COLUMNS),
                       (contract.APP_SPEND_VIEW, contract.APP_SPEND_COLUMNS)):
        cur.execute("""SELECT a.attname FROM pg_attribute a WHERE a.attrelid = %s::regclass AND a.attnum > 0
                        AND NOT a.attisdropped ORDER BY a.attnum""", (view,))
        assert tuple(r[0] for r in cur.fetchall()) == want, view


def _seed_gateway(cur):
    cur.execute("""INSERT INTO shared.llm_provider_connection
                       (connection_key, kind, auth_mode, label, data_scope, enabled, created_at, updated_at, updated_by)
                   VALUES ('zz-conn-a', 'mock', 'none', 'zz a', 'aws_only', false, now(), now(), 'test'),
                          ('zz-conn-b', 'mock', 'none', 'zz b', 'aws_only', false, now(), now(), 'test')""")
    for key, pin in (("zz-writer", 2.0), ("zz-writer-b", 1.0), ("zz-other", 1.0)):
        cur.execute("""INSERT INTO shared.llm_model_catalog
                           (model_key, label, vendor, provider, api_style, bedrock_profile_ids, callable_via,
                            supports_temperature, price_in_per_mtok, price_out_per_mtok, enabled, updated_at,
                            updated_by, capabilities)
                       VALUES (%s, %s, 'anthropic', 'mock', 'converse', '{"acc3": "zz.profile"}'::jsonb,
                               ARRAY['llm_client'], false, %s, %s, true, now(), 'test', '{}'::jsonb)""",
                    (key, key, pin, pin * 5))
    for conn, prio, override in (("zz-conn-b", 20, None), ("zz-conn-a", 10, 1.5)):
        cur.execute("""INSERT INTO shared.llm_model_deployment
                           (model_key, connection_key, wire_id, priority, enabled, price_in_override, batch_capable,
                            request_defaults, origin, created_at, updated_at, updated_by)
                       VALUES ('zz-writer', %s, %s, %s, true, %s, false, '{}'::jsonb, 'seed', now(), now(), 'test')""",
                    (conn, f"wire-{conn}", prio, override))
    for stage, model, fallbacks in (("tp_zz_compose", "zz-writer", ["zz-missing", "zz-writer-b"]),
                                    ("zz_not_tripplanner", "zz-other", [])):
        cur.execute("""INSERT INTO shared.llm_role_config
                           (stage, role, provider, model_id, is_active, updated_at, updated_by, fallback_model_ids,
                            shadow_sample_pct)
                       VALUES (%s, 'writer', 'claude', %s, true, now(), 'test', %s, 0)""", (stage, model, fallbacks))


def test_gateway_view_rows_as_tripplanner(cur):
    _seed_gateway(cur)
    cur.execute("SET LOCAL ROLE tripplanner")
    rows = _rows(cur, contract.GATEWAY_CONFIG_SQL.replace("$1", "%s"), "tp_zz_compose")
    assert [(r["position"], r["model_key"], r["in_catalog"]) for r in rows] == [
        (0, "zz-writer", True), (1, "zz-missing", False), (2, "zz-writer-b", True)]
    first = rows[0]
    assert first["role"] == "writer" and first["api_style"] == "converse" and first["model_enabled"] is True
    assert float(first["price_in_per_mtok"]) == 2.0 and first["bedrock_profile_ids"] == {"acc3": "zz.profile"}
    deployments = first["deployments"] if isinstance(first["deployments"], list) else json.loads(first["deployments"])
    assert [d["connection_key"] for d in deployments] == ["zz-conn-a", "zz-conn-b"]
    assert float(deployments[0]["price_in_override"]) == 1.5 and deployments[1]["price_in_override"] is None
    assert rows[1]["vendor"] is None and rows[1]["deployments"] in ([], "[]")
    cur.execute("SELECT count(*) AS n FROM catalog.v_llm_gateway_config_v1 WHERE stage = 'zz_not_tripplanner'")
    assert cur.fetchone()[0] == 0


def test_tripplanner_cannot_read_the_tables_behind_the_new_views(cur):
    # Deployments, connections and other apps' spend stay behind the views (the grandfathered SELECTs of CIS 173 on
    # llm_role_config / llm_model_catalog are untouched by 323, removed later).
    for table in ("shared.llm_model_deployment", "shared.llm_provider_connection"):
        cur.execute("SELECT has_table_privilege('tripplanner', %s, 'SELECT')", (table,))
        assert cur.fetchone()[0] is False, table
    cur.execute("SELECT has_table_privilege('tripplanner', 'shared.llm_call_log', 'SELECT')")
    assert cur.fetchone()[0] is False


def test_spend_view_counts_today_tripplanner_only(cur):
    ins = """INSERT INTO shared.llm_call_log (stage, role, model, cost_usd, quality_signal, app, status, created_at)
             VALUES (%s, 'embed', 'zz-embed', %s, %s::jsonb, %s, %s, now() - %s::interval)"""
    rows = [
        ("tp_zz_search", 0.001, '{"app": "tripplanner"}', "tripplanner", "ok", "0 s"),
        ("tp_zz_search", 0.002, '{"app": "tripplanner"}', None, None, "0 s"),                  # pre-v2: app in signal
        ("tp_zz_search", None, '{"app": "tripplanner"}', "tripplanner", "ok", "0 s"),          # unpriced
        ("tp_zz_search", 0.0, '{"app": "tripplanner", "failed": true}', None, None, "0 s"),    # pre-v2 failed attempt
        ("tp_zz_compose", 0.0, '{}', "tripplanner", "refused", "0 s"),                         # v2 refused attempt
        ("tp_zz_search", 5.0, '{"app": "cis"}', "cis", "ok", "0 s"),                           # another app
        ("tp_zz_search", 9.0, '{"app": "tripplanner"}', "tripplanner", "ok", "1 day"),         # before today (UTC)
    ]
    for row in rows:
        cur.execute(ins, row)
    cur.execute("SET LOCAL ROLE tripplanner")
    got = {r["stage"]: r for r in _rows(cur, "SELECT day, stage, calls, unpriced_calls, failed_calls, cost_usd "
                                             "FROM catalog.v_llm_app_spend_today_v1 WHERE stage LIKE 'tp_zz%%'")}
    assert set(got) == {"tp_zz_search", "tp_zz_compose"}
    search = got["tp_zz_search"]
    assert (search["calls"], search["unpriced_calls"], search["failed_calls"]) == (4, 1, 1)
    assert float(search["cost_usd"]) == pytest.approx(0.003)
    assert (got["tp_zz_compose"]["calls"], got["tp_zz_compose"]["failed_calls"]) == (1, 1)
    cur.execute(contract.APP_SPEND_SQL.replace("$1", "%s"), (["tp_zz_search", "tp_zz_compose"],))
    assert float(cur.fetchone()[0]) == pytest.approx(0.003)
