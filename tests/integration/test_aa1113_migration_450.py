"""AA-1113 (root ADR 0009 decisions 6 and 10) — migrations 449 and 450: the free-text registry column and the
connection cap contract views, applied the way DB Migrate applies a cis/ file since X1 S22.

449 is replayed by conftest like every plain file (shared.llm_role_config.free_text); conftest skips 323, 341, 350 and
450 (catalog recipe, 245's owners). This module builds on test_aa860_migration_246's module fixture (Dev after 244 +
245, db_migrator with rl-002's memberships), applies 323, 341, 350, then 450 twice as db_migrator under the live-owner
switch (errors NOT swallowed). Teardown drops both views and revokes the four files' column grants and crm_app's
schema USAGE, so later modules see the replay state.

  * 449: every tenant stage marked free_text, a pii stage can never be marked (CHECK);
  * 450: registered once; the gateway view keeps 350's columns in order and appends free_text, over_cap_connections;
    the cap view's columns; owners, barriers and the exact consumers (tripplanner both, crm_app the cap view only);
  * the cap view (as crm_app / tripplanner, rolled back): this UTC month's ledger spend per capped connection, the
    policy defaults, over_cap and stops; a connection with no budget has no row; last month's rows do not count;
  * the gateway view (as tripplanner): free_text from the stage row, over_cap_connections lists only the stopped
    connections of the model's own deployments.
Invented stage, model and connection names only.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa1027_migration_341 as t341  # noqa: E402
import test_aa1027_migration_350 as t350  # noqa: E402
import test_aa642_migration_323 as t323  # noqa: E402
import test_aa860_migration_245 as t245  # noqa: E402
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401  (module fixture: Dev after 244 + 245)
from test_aa860_migration_246 import db_migrator_login  # noqa: E402,F401  (module fixture: db_migrator, 246)

from shared.aa_llm import contract  # noqa: E402

HERE = os.path.dirname(__file__)
M449 = os.path.join(HERE, "..", "..", "api", "migrations", "449_aa1113_stage_free_text.sql")
M450 = os.path.join(HERE, "..", "..", "api", "migrations", "450_aa1113_connection_cap_views.sql")
CAP_VIEW = "catalog.v_llm_connection_cap_v1"
APPENDED = ("free_text", "over_cap_connections")
CAP_COLUMNS = ("connection_key", "monthly_budget_usd", "month_start", "month_spent_usd", "month_calls", "alert_pct",
               "hard_stop", "over_cap", "stops")
GRANTED_450 = {
    "llm_role_config": {"free_text"},
    "llm_provider_connection": {"monthly_budget_usd"},
    "llm_connection_cost_policy": {"connection_key", "alert_pct", "hard_stop"},
    "llm_call_log": {"account"},
}


def _role_exists(a, role):
    a.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
    return a.fetchone() is not None


def undo_450(a):
    a.execute("DROP VIEW IF EXISTS catalog.v_llm_gateway_config_v1")
    a.execute(f"DROP VIEW IF EXISTS {CAP_VIEW}")
    t350.undo_350(a)
    for table, cols in GRANTED_450.items():
        a.execute(f"REVOKE SELECT ({', '.join(sorted(cols))}) ON shared.{table} FROM catalog_owner")
    if _role_exists(a, "crm_app"):
        a.execute("REVOKE USAGE ON SCHEMA catalog FROM crm_app")
    a.execute("DELETE FROM shared.schema_versions WHERE version = '450'")


@pytest.fixture(scope="module", autouse=True)
def applied_450(db_migrator_login):  # noqa: F811
    admin = t245._connect()
    admin.autocommit = True
    a = admin.cursor()
    try:
        undo_450(a)
        t323._apply(t323.MIGRATION)
        t323._apply(t341.M341)
        t323._apply(t350.M350)
        t323._apply(M449)                                    # replayed by conftest already: a no-op re-run
        t323._apply(M450)
        t323._apply(M450)                                    # idempotent re-run
        yield
    finally:
        undo_450(a)
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


def _cols(cur, view):
    cur.execute("""SELECT a.attname FROM pg_attribute a WHERE a.attrelid = %s::regclass AND a.attnum > 0
                    AND NOT a.attisdropped ORDER BY a.attnum""", (view,))
    return tuple(r[0] for r in cur.fetchall())


# ── 449 ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_449_registered_and_every_tenant_stage_marked(cur):
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '449'")
    assert cur.fetchone()[0] == 1
    rows = _rows(cur, "SELECT stage, free_text FROM shared.llm_role_config WHERE stage ~ '^t[0-9]+_' "
                      "AND pii_class <> 'pii'")
    assert all(r["free_text"] for r in rows)


def test_449_a_pii_stage_is_never_free_text(cur):
    import psycopg2

    cur.execute("""INSERT INTO shared.llm_role_config
                       (stage, role, provider, model_id, is_active, updated_at, updated_by, fallback_model_ids,
                        shadow_sample_pct, allowed_scopes, pii_class, free_text)
                   VALUES ('zz_pii_stage', 'writer', 'claude', 'zz-writer', true, now(), 'test', '{}', 0,
                           ARRAY['aws_only'], 'pii', false)""")
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("UPDATE shared.llm_role_config SET free_text = true WHERE stage = 'zz_pii_stage'")


# ── 450: shape and grants ────────────────────────────────────────────────────────────────────────────────────────

def test_450_registered_once(cur):
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '450'")
    assert cur.fetchone()[0] == 1


def test_gateway_view_columns_are_350_then_the_two_appended(cur):
    assert _cols(cur, contract.GATEWAY_CONFIG_VIEW) == (
        contract.GATEWAY_CONFIG_COLUMNS + t341.APPENDED + t350.APPENDED + APPENDED)


def test_cap_view_columns(cur):
    assert _cols(cur, CAP_VIEW) == CAP_COLUMNS


def test_owners_barriers_and_consumers(cur):
    for view in (contract.GATEWAY_CONFIG_VIEW, CAP_VIEW):
        cur.execute("SELECT pg_get_userbyid(relowner), reloptions FROM pg_class WHERE oid = %s::regclass", (view,))
        owner, opts = cur.fetchone()
        assert owner == "catalog_owner" and "security_barrier=true" in (opts or []), view
        cur.execute("SELECT has_table_privilege('tripplanner', %s, 'SELECT')", (view,))
        assert cur.fetchone()[0] is True, view
        for role in ("booking", "aa_app_user", "cis_app"):
            cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT')", (role, view))
            assert cur.fetchone()[0] is False, (role, view)
    if _role_exists(cur, "crm_app"):
        cur.execute("SELECT has_table_privilege('crm_app', %s, 'SELECT')", (CAP_VIEW,))
        assert cur.fetchone()[0] is True
        cur.execute("SELECT has_table_privilege('crm_app', %s, 'SELECT')", (contract.GATEWAY_CONFIG_VIEW,))
        assert cur.fetchone()[0] is False
    cur.execute("SELECT has_schema_privilege('catalog_owner', 'catalog', 'CREATE')")
    assert cur.fetchone()[0] is False


def test_catalog_owner_reads_only_the_granted_columns_of_the_new_tables(cur):
    for table in ("llm_connection_cost_policy",):
        cur.execute("""SELECT a.attname FROM pg_attribute a
                        WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
                          AND has_column_privilege('catalog_owner', a.attrelid, a.attnum, 'SELECT')""",
                    (f"shared.{table}",))
        assert {r[0] for r in cur.fetchall()} == GRANTED_450[table], table
    for table in GRANTED_450:
        cur.execute("SELECT has_table_privilege('catalog_owner', %s, 'SELECT')", (f"shared.{table}",))
        assert cur.fetchone()[0] is False, f"table-wide SELECT on {table}"


# ── 450: behaviour ───────────────────────────────────────────────────────────────────────────────────────────────

def _connection(cur, key, budget):
    cur.execute("""INSERT INTO shared.llm_provider_connection
                       (connection_key, kind, auth_mode, label, data_scope, enabled, monthly_budget_usd, updated_by)
                   VALUES (%s, 'mock', 'none', %s, 'aws_only', true, %s, 'test')""", (key, key, budget))


def _spend(cur, account, usd, when="now()"):
    cur.execute(f"""INSERT INTO shared.llm_call_log
                        (stage, role, model, account, cost_usd, status, quality_signal, created_at)
                    VALUES ('adhoc_zz', 'writer', 'zz-writer', %s, %s, 'ok', '{{}}'::jsonb, {when})""", (account, usd))


def _seed(cur):
    _connection(cur, "zz-capped", 1.00)          # spent 1.20 this month: over, hard stop by default -> stops
    _connection(cur, "zz-alert", 1.00)           # spent 1.50, policy alert only -> over_cap, not stops
    _connection(cur, "zz-room", 5.00)            # spent 0.40 this month, 9 last month -> under
    _connection(cur, "zz-nocap", None)           # no budget: no row
    cur.execute("""INSERT INTO shared.llm_connection_cost_policy (connection_key, alert_pct, hard_stop, updated_by)
                   VALUES ('zz-alert', 60, false, 'test')""")
    _spend(cur, "zz-capped", 0.70)
    _spend(cur, "zz-capped", 0.50)
    _spend(cur, "zz-alert", 1.50)
    _spend(cur, "zz-room", 0.40)
    _spend(cur, "zz-room", 9.00, "date_trunc('month', now()) - interval '2 days'")
    _spend(cur, "zz-nocap", 3.00)


def test_cap_view_rows(cur):
    _seed(cur)
    cur.execute("SET LOCAL ROLE tripplanner")
    rows = {r["connection_key"]: r for r in _rows(
        cur, f"SELECT * FROM {CAP_VIEW} WHERE connection_key LIKE 'zz-%%'")}
    assert set(rows) == {"zz-capped", "zz-alert", "zz-room"}
    capped, alert, room = rows["zz-capped"], rows["zz-alert"], rows["zz-room"]
    assert float(capped["month_spent_usd"]) == pytest.approx(1.20) and capped["month_calls"] == 2
    assert (capped["alert_pct"], capped["hard_stop"], capped["over_cap"], capped["stops"]) == (80, True, True, True)
    assert (alert["alert_pct"], alert["hard_stop"], alert["over_cap"], alert["stops"]) == (60, False, True, False)
    assert float(room["month_spent_usd"]) == pytest.approx(0.40)              # last month's 9 USD left out
    assert (room["over_cap"], room["stops"]) == (False, False)


def test_cap_view_as_crm_app(cur):
    if not _role_exists(cur, "crm_app"):
        pytest.skip("no crm_app role on this database")
    _seed(cur)
    cur.execute("SET LOCAL ROLE crm_app")
    rows = _rows(cur, f"SELECT connection_key AS k, stops AS s FROM {CAP_VIEW} "
                      "WHERE connection_key = ANY(%s) ORDER BY 1", ["zz-capped", "zz-room"])
    assert [(r["k"], r["s"]) for r in rows] == [("zz-capped", True), ("zz-room", False)]


def test_gateway_view_free_text_and_over_cap_connections(cur):
    _seed(cur)
    cur.execute("""INSERT INTO shared.llm_model_catalog
                       (model_key, label, vendor, provider, api_style, bedrock_profile_ids, callable_via,
                        supports_temperature, price_in_per_mtok, price_out_per_mtok, enabled, updated_at,
                        updated_by, capabilities)
                   VALUES ('zz-writer', 'zz-writer', 'anthropic', 'mock', 'anthropic_native', '{}'::jsonb,
                           ARRAY['llm_client'], true, 1.0, 5.0, true, now(), 'test', '{}'::jsonb)""")
    for key, prio in (("zz-capped", 1), ("zz-alert", 2), ("zz-room", 3)):
        cur.execute("""INSERT INTO shared.llm_model_deployment
                           (model_key, connection_key, wire_id, priority, enabled, batch_capable, request_defaults,
                            origin, created_at, updated_at, updated_by)
                       VALUES ('zz-writer', %s, 'zz-wire', %s, true, false, '{}'::jsonb, 'seed', now(), now(),
                               'test')""", (key, prio))
    for stage, free in (("tp_zz_chat", True), ("tp_zz_compose", False)):
        cur.execute("""INSERT INTO shared.llm_role_config
                           (stage, role, provider, model_id, is_active, updated_at, updated_by, fallback_model_ids,
                            shadow_sample_pct, free_text)
                       VALUES (%s, 'writer', 'claude', 'zz-writer', true, now(), 'test', '{}', 0, %s)""",
                    (stage, free))
    cur.execute("SET LOCAL ROLE tripplanner")
    rows = {r["stage"]: r for r in _rows(
        cur, "SELECT stage, free_text, over_cap_connections FROM catalog.v_llm_gateway_config_v1 "
             "WHERE stage = ANY(%s)", ["tp_zz_chat", "tp_zz_compose"])}
    assert rows["tp_zz_chat"]["free_text"] is True
    assert rows["tp_zz_compose"]["free_text"] is False
    assert rows["tp_zz_chat"]["over_cap_connections"] == ["zz-capped"]       # zz-alert is alert only, zz-room under


def test_the_350_select_still_runs(cur):
    cur.execute("SET LOCAL ROLE tripplanner")
    cols = contract.GATEWAY_CONFIG_COLUMNS + t341.APPENDED + t350.APPENDED
    cur.execute(f"SELECT {', '.join(cols)} FROM {contract.GATEWAY_CONFIG_VIEW} LIMIT 1")
