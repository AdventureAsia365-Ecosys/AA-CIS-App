"""AA-1027 E3 (root plan S263) — migration 341: catalog.v_llm_gateway_config_v1 gains route_deadline_s and
deployment_routing (non-secret connection facts, the deployment circuit and the connection circuit), applied the way DB
Migrate applies a cis/ file since X1 S22.

conftest skips 323 and 341 in the replay (catalog recipe, 245's owners). This module builds on
test_aa860_migration_246's module fixture (Dev after 244 + 245, db_migrator with rl-002's memberships), applies 323 then
341 as db_migrator under the live-owner switch (errors NOT swallowed), 341 twice. Teardown drops the view and revokes
both files' column grants, so later modules see the replay state.

  * the migration: registered once; the view keeps 323's columns in order and appends exactly two; catalog_owner reads
    only the listed columns of llm_provider_connection / llm_provider_health (never account_id, role_name,
    external_id, secret_name, credential_env, base_url, last_error); tripplanner still cannot read those tables;
  * deployment_routing (as tripplanner, rolled back): one object per deployment in the order of `deployments`, the
    connection facts, the deployment's circuit, and the connection circuit of breaker.connection_health: tripped by
    access_denied on 2 distinct models, not by 1, tripped by one quota_exhausted row, cleared by a later answered call
    on the connection;
  * route_deadline_s comes from the route row.
Invented stage, model and connection names only.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa642_migration_323 as t323  # noqa: E402
import test_aa860_migration_245 as t245  # noqa: E402
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401  (module fixture: Dev after 244 + 245)
from test_aa860_migration_246 import db_migrator_login  # noqa: E402,F401  (module fixture: db_migrator, 246)

from shared.aa_llm import contract  # noqa: E402

HERE = os.path.dirname(__file__)
M341 = os.path.join(HERE, "..", "..", "api", "migrations", "341_llm_gateway_view_routing_health.sql")
APPENDED = ("route_deadline_s", "deployment_routing")
GRANTED_341 = {
    "llm_role_config": {"route_deadline_s"},
    "llm_provider_connection": {"connection_key", "kind", "auth_mode", "account_id_env", "region", "data_scope",
                                "served_roles", "enabled", "access_state"},
    "llm_provider_health": {"connection_key", "model_key", "last_ok_at", "last_error_at", "last_error_class",
                            "consecutive_failures", "circuit_open_until", "probe_until"},
}
ROUTING_KEYS = {"connection_key", "in_registry", "kind", "auth_mode", "account_id_env", "region", "data_scope",
                "served_roles", "connection_enabled", "access_state", "circuit_open_until", "probe_until",
                "last_error_class", "consecutive_failures", "connection_circuit_class",
                "connection_circuit_open_until"}


def undo_341(a):
    t323.undo_323(a)                                         # drops the view, 323's grants and registration
    for table, cols in GRANTED_341.items():
        a.execute(f"REVOKE SELECT ({', '.join(sorted(cols))}) ON shared.{table} FROM catalog_owner")
    a.execute("DELETE FROM shared.schema_versions WHERE version = '341'")


@pytest.fixture(scope="module", autouse=True)
def applied_341(db_migrator_login):  # noqa: F811
    admin = t245._connect()
    admin.autocommit = True
    a = admin.cursor()
    try:
        undo_341(a)
        t323._apply(t323.MIGRATION)
        t323._apply(M341)
        t323._apply(M341)                                    # idempotent re-run
        yield
    finally:
        undo_341(a)
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


def _json(value):
    return value if isinstance(value, (list, dict)) else json.loads(value)


def test_registered_once(cur):
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '341'")
    assert cur.fetchone()[0] == 1


def test_columns_are_323_then_the_two_appended(cur):
    cur.execute("""SELECT a.attname FROM pg_attribute a WHERE a.attrelid = %s::regclass AND a.attnum > 0
                    AND NOT a.attisdropped ORDER BY a.attnum""", (contract.GATEWAY_CONFIG_VIEW,))
    assert tuple(r[0] for r in cur.fetchall()) == contract.GATEWAY_CONFIG_COLUMNS + APPENDED


def test_owner_barrier_and_consumers(cur):
    view = contract.GATEWAY_CONFIG_VIEW
    cur.execute("SELECT pg_get_userbyid(relowner), reloptions FROM pg_class WHERE oid = %s::regclass", (view,))
    owner, opts = cur.fetchone()
    assert owner == "catalog_owner" and "security_barrier=true" in (opts or [])
    cur.execute("SELECT has_table_privilege('tripplanner', %s, 'SELECT')", (view,))
    assert cur.fetchone()[0] is True
    for role in ("booking", "aa_app_user", "cis_app"):
        cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT')", (role, view))
        assert cur.fetchone()[0] is False, role
    cur.execute("SELECT has_schema_privilege('catalog_owner', 'catalog', 'CREATE')")
    assert cur.fetchone()[0] is False


def test_catalog_owner_reads_only_the_listed_columns(cur):
    want = {t: set(c) for t, c in t323.GRANTED.items()}
    for table, cols in GRANTED_341.items():
        want.setdefault(table, set()).update(cols)
    for table, cols in want.items():
        cur.execute("SELECT has_table_privilege('catalog_owner', %s, 'SELECT')", (f"shared.{table}",))
        assert cur.fetchone()[0] is False, f"table-wide SELECT on {table}"
        cur.execute("""SELECT a.attname FROM pg_attribute a
                        WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
                          AND has_column_privilege('catalog_owner', a.attrelid, a.attnum, 'SELECT')""",
                    (f"shared.{table}",))
        assert {r[0] for r in cur.fetchall()} == cols, table


def test_tripplanner_still_cannot_read_connections_or_health(cur):
    for table in ("shared.llm_provider_connection", "shared.llm_provider_health"):
        cur.execute("SELECT has_any_column_privilege('tripplanner', %s, 'SELECT')", (table,))
        assert cur.fetchone()[0] is False, table


def _seed(cur):
    cur.execute("""INSERT INTO shared.llm_provider_connection
                       (connection_key, kind, auth_mode, label, region, data_scope, served_roles, enabled,
                        created_at, updated_at, updated_by)
                   VALUES ('zz-blocked', 'mock', 'none', 'zz blocked', NULL, 'aws_only', ARRAY['writer', 'embed'],
                           true, now(), now(), 'test'),
                          ('zz-onemodel', 'mock', 'none', 'zz one model', NULL, 'aws_only', NULL, true, now(), now(),
                           'test'),
                          ('zz-broke', 'mock', 'none', 'zz broke', NULL, 'vendor_direct', NULL, false, now(), now(),
                           'test'),
                          ('zz-recovered', 'bedrock', 'task_role', 'zz recovered', 'us-west-2', 'aws_only',
                           ARRAY['embed'], true, now(), now(), 'test')""")
    for key in ("zz-writer", "zz-other", "zz-third"):
        cur.execute("""INSERT INTO shared.llm_model_catalog
                           (model_key, label, vendor, provider, api_style, bedrock_profile_ids, callable_via,
                            supports_temperature, price_in_per_mtok, price_out_per_mtok, enabled, updated_at,
                            updated_by, capabilities)
                       VALUES (%s, %s, 'anthropic', 'mock', 'converse', '{}'::jsonb, ARRAY['llm_client'], false,
                               1.0, 5.0, true, now(), 'test', '{}'::jsonb)""", (key, key))
    for conn, prio in (("zz-recovered", 40), ("zz-broke", 30), ("zz-onemodel", 20), ("zz-blocked", 10)):
        cur.execute("""INSERT INTO shared.llm_model_deployment
                           (model_key, connection_key, wire_id, priority, enabled, batch_capable, request_defaults,
                            origin, created_at, updated_at, updated_by)
                       VALUES ('zz-writer', %s, %s, %s, true, false, '{}'::jsonb, 'seed', now(), now(), 'test')""",
                    (conn, f"wire-{conn}", prio))
    h = """INSERT INTO shared.llm_provider_health
               (connection_key, model_key, last_ok_at, last_error_at, last_error_class, last_error,
                consecutive_failures, circuit_open_until, probe_until)
           VALUES (%s, %s, now() - %s::interval, now() - %s::interval, %s, 'zz provider text', %s,
                   now() + %s::interval, NULL)"""
    # zz-blocked: access_denied on two models -> connection circuit (access_denied), open until the later one.
    cur.execute(h, ("zz-blocked", "zz-writer", "2 days", "1 minute", "access_denied", 1, "4 minutes"))
    cur.execute(h, ("zz-blocked", "zz-other", "2 days", "2 minutes", "access_denied", 1, "9 minutes"))
    # zz-onemodel: access_denied on one model only -> the deployment circuit, never the connection's.
    cur.execute(h, ("zz-onemodel", "zz-writer", "2 days", "1 minute", "access_denied", 1, "4 minutes"))
    # zz-broke: one quota_exhausted row (another model) trips the connection alone.
    cur.execute(h, ("zz-broke", "zz-other", "2 days", "1 minute", "quota_exhausted", 1, "-1 minute"))
    # zz-recovered: two access_denied rows, but another model answered after them -> not tripped.
    cur.execute(h, ("zz-recovered", "zz-writer", "1 day", "10 minutes", "access_denied", 1, "4 minutes"))
    cur.execute(h, ("zz-recovered", "zz-other", "1 day", "10 minutes", "access_denied", 1, "4 minutes"))
    cur.execute("""INSERT INTO shared.llm_provider_health (connection_key, model_key, last_ok_at)
                   VALUES ('zz-recovered', 'zz-third', now() - interval '1 minute')""")
    cur.execute("""INSERT INTO shared.llm_role_config
                       (stage, role, provider, model_id, is_active, updated_at, updated_by, fallback_model_ids,
                        shadow_sample_pct, route_deadline_s)
                   VALUES ('tp_zz_compose', 'writer', 'claude', 'zz-writer', true, now(), 'test', '{}', 0, 17)""")


def test_deployment_routing_as_tripplanner(cur):
    _seed(cur)
    cur.execute("SET LOCAL ROLE tripplanner")
    rows = _rows(cur, "SELECT deployments, deployment_routing, route_deadline_s, "
                      "(SELECT now()) AS now_at FROM catalog.v_llm_gateway_config_v1 WHERE stage = %s",
                 "tp_zz_compose")
    assert len(rows) == 1
    row = rows[0]
    assert row["route_deadline_s"] == 17
    deployments, routing = _json(row["deployments"]), _json(row["deployment_routing"])
    order = ["zz-blocked", "zz-onemodel", "zz-broke", "zz-recovered"]
    assert [d["connection_key"] for d in deployments] == order
    assert [d["connection_key"] for d in routing] == order
    for item in routing:
        assert set(item) == ROUTING_KEYS, item["connection_key"]
        assert "zz provider text" not in json.dumps(item)
    by = {d["connection_key"]: d for d in routing}

    blocked = by["zz-blocked"]
    assert (blocked["kind"], blocked["data_scope"], blocked["served_roles"], blocked["connection_enabled"]) == (
        "mock", "aws_only", ["writer", "embed"], True)
    assert blocked["in_registry"] is True and blocked["access_state"] == "unknown"
    assert blocked["last_error_class"] == "access_denied" and blocked["circuit_open_until"] is not None
    assert blocked["connection_circuit_class"] == "access_denied"
    cur.execute("SELECT %s::timestamptz > now() + interval '8 minutes' AS later",
                (blocked["connection_circuit_open_until"],))
    assert cur.fetchone()[0] is True                         # the newest open period of the two models

    one = by["zz-onemodel"]
    assert one["last_error_class"] == "access_denied" and one["connection_circuit_class"] is None
    assert one["served_roles"] is None

    broke = by["zz-broke"]
    assert (broke["connection_circuit_class"], broke["data_scope"], broke["connection_enabled"]) == (
        "quota_exhausted", "vendor_direct", False)
    assert broke["circuit_open_until"] is None               # zz-writer has no health row on it

    rec = by["zz-recovered"]
    assert (rec["kind"], rec["auth_mode"], rec["region"]) == ("bedrock", "task_role", "us-west-2")
    assert rec["last_error_class"] == "access_denied" and rec["connection_circuit_class"] is None
    assert rec["account_id_env"] is None


def test_contract_select_still_runs(cur):
    _seed(cur)
    cur.execute("SET LOCAL ROLE tripplanner")
    rows = _rows(cur, contract.GATEWAY_CONFIG_SQL.replace("$1", "%s"), "tp_zz_compose")
    assert [r["model_key"] for r in rows] == ["zz-writer"]
