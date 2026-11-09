"""AA-1038 R5 slice 3 (S272) — migration 390: the lead route of a TripPlanner site (shared.tenant_tripplanner_site),
four columns appended to catalog.v_tenant_tripplanner_v1 (crm_core, lead_channel and its target) and the new view
catalog.v_tenant_ai_budget_v1 (the tenant's AI budget and its month spend), applied the way DB Migrate applies a cis/
file since X1 S22.

conftest skips 379 and 390 in the replay (catalog recipe, 245's owners). This module builds on the 379 module's
fixtures (Dev after 244 + 245, db_migrator), applies 379 then 390 as db_migrator (errors NOT swallowed), 390 twice.
Teardown drops what 390 added and then undoes 379, so later modules see the replay state.

  * registered once; both views' exact columns, owner catalog_owner, security_barrier, tripplanner only;
    catalog_owner reads only the granted columns; tripplanner holds nothing on the base tables;
  * the lead route (as tripplanner): crm when crm.core is live (whatever the site says), else the site's webhook /
    email with its target, else none; the target is only served for its own channel; the CHECKs refuse a channel
    without its target, a non-https URL and a malformed address;
  * the AI budget (as tripplanner): the plan's included spend, an override, a disabled override (0 USD), no key
    (source none), and the month's spend of the tenant only (another tenant's rows, last month's rows and shadow rows
    left out);
  * the SELECTs AA-TripPlanner-Web runs (backend/assembly/leads.py ROUTE_SQL, backend/shared/tenant_budget.py
    BUDGET_SQL) answer as tripplanner.
Invented names only.
"""
import json
import os
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa642_migration_323 as t323  # noqa: E402
import test_aa860_migration_245 as t245  # noqa: E402
import test_aa1038_migration_379 as t379  # noqa: E402
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401  (module fixture: Dev after 244 + 245)
from test_aa860_migration_246 import db_migrator_login  # noqa: E402,F401  (module fixture: db_migrator, 246)

HERE = os.path.dirname(__file__)
M390 = os.path.join(HERE, "..", "..", "api", "migrations", "390_tenant_tripplanner_lead_route_ai_budget.sql")
VIEW = "catalog.v_tenant_tripplanner_v1"
BUDGET_VIEW = "catalog.v_tenant_ai_budget_v1"
SITE = "shared.tenant_tripplanner_site"
DEMO = t379.DEMO
# Copied verbatim into AA-TripPlanner-Web backend/tests/test_aa1038_leads_pg.py (CIS_390_COLUMNS / CIS_390_BUDGET).
COLUMNS = t379.COLUMNS + ", crm_core boolean, lead_channel text, lead_webhook_url text, lead_email text"
BUDGET_COLUMNS = ("tenant_id uuid, budget_source text, monthly_usd numeric, month_spent_usd numeric(14,6), "
                  "month_calls integer")
# The SELECTs of AA-TripPlanner-Web (column names, never *).
TP_ROUTE_SQL = (f"SELECT tenant_id, slug, name, kind, served, in_service, crm_core, lead_channel, lead_webhook_url, "
                f"lead_email FROM {VIEW} WHERE tenant_id = %s")
TP_BUDGET_SQL = (f"SELECT tenant_id, budget_source, monthly_usd, month_spent_usd, month_calls FROM {BUDGET_VIEW} "
                 f"WHERE tenant_id = %s")
SITE_COLUMNS = ("lead_channel", "lead_webhook_url", "lead_email")
OVERRIDE_COLUMNS = ("tenant_id", "feature_key", "enabled", "expires_at")
LEDGER_COLUMNS = ("tenant_id", "is_shadow")


def undo_390(a):
    a.execute(f"DROP VIEW IF EXISTS {BUDGET_VIEW}")
    a.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (SITE,))
    if a.fetchone()[0]:
        a.execute(f"REVOKE SELECT ({', '.join(SITE_COLUMNS)}) ON {SITE} FROM catalog_owner")
    a.execute(f"REVOKE SELECT ({', '.join(OVERRIDE_COLUMNS)}) ON shared.tenant_entitlement_override FROM catalog_owner")
    a.execute(f"REVOKE SELECT ({', '.join(LEDGER_COLUMNS)}) ON shared.llm_call_log FROM catalog_owner")
    a.execute("DELETE FROM shared.schema_versions WHERE version = '390'")
    # The appended view columns and the site columns go with 379's undo (DROP VIEW, DROP TABLE).


@pytest.fixture(scope="module", autouse=True)
def applied_390(db_migrator_login):  # noqa: F811
    admin = t245._connect()
    admin.autocommit = True
    a = admin.cursor()
    try:
        undo_390(a)
        t379.undo_379(a)
        t323._apply(t379.M379)
        t323._apply(M390)
        t323._apply(M390)                                     # idempotent re-run
        yield
    finally:
        undo_390(a)
        t379.undo_379(a)
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


_rows = t379._rows


def _tenant(cur, name, **kw):
    return t379._tenant(cur, name, **kw)


def _site(cur, tid, **cols):
    cols.setdefault("updated_by", "test:390")
    names = ", ".join(cols)
    marks = ", ".join(["%s"] * len(cols))
    cur.execute(f"INSERT INTO {SITE} (tenant_id, {names}) VALUES (%s, {marks})", (tid, *cols.values()))


def _subscribe(cur, tid, product, plan_key):
    cur.execute("INSERT INTO shared.tenant_subscription (tenant_id, product, plan_key, status, updated_by) "
                "VALUES (%s, %s, %s, 'active', 'test:390')", (tid, product, plan_key))


def _ledger(cur, tid, cost, *, shadow=False, months_ago=0):
    cur.execute("INSERT INTO shared.llm_call_log (tenant_id, stage, role, model, quality_signal, cost_usd, app, "
                "is_shadow, created_at) VALUES (%s, 'tp_compose', 'writer', 'synthetic-model', '{}'::jsonb, %s, "
                "'tripplanner', %s, "
                "now() - make_interval(months => %s))", (tid, cost, shadow, months_ago))


def test_registered_once(cur):
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '390'")
    assert cur.fetchone()[0] == 1


@pytest.mark.parametrize("view,expected", [(VIEW, COLUMNS), (BUDGET_VIEW, BUDGET_COLUMNS)])
def test_columns_owner_barrier_and_consumers(cur, view, expected):
    cur.execute("""SELECT string_agg(a.attname || ' ' || format_type(a.atttypid, a.atttypmod), ', ' ORDER BY a.attnum)
                     AS cols FROM pg_attribute a WHERE a.attrelid = %s::regclass AND a.attnum > 0
                      AND NOT a.attisdropped""", (view,))
    assert cur.fetchone()[0] == expected
    cur.execute("SELECT pg_get_userbyid(relowner) AS o, reloptions AS r FROM pg_class WHERE oid = %s::regclass",
                (view,))
    owner, opts = cur.fetchone()
    assert owner == "catalog_owner" and "security_barrier=true" in (opts or [])
    cur.execute("SELECT has_table_privilege('tripplanner', %s, 'SELECT') AS ok", (view,))
    assert cur.fetchone()[0] is True
    for role in ("booking", "aa_app_user", "cis_app", "crm_app"):
        cur.execute("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s) AS ok", (role,))
        if not cur.fetchone()[0]:
            continue
        cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT') AS ok", (role, view))
        assert cur.fetchone()[0] is False, role


def test_catalog_owner_and_tripplanner_privileges(cur):
    cur.execute("""SELECT string_agg(a.attname, ',' ORDER BY a.attname) AS cols FROM pg_attribute a
                    WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
                      AND has_column_privilege('catalog_owner', a.attrelid, a.attnum, 'SELECT')""", (SITE,))
    assert cur.fetchone()[0] == "lead_channel,lead_email,lead_webhook_url,locale,tenant_id,theme"
    for table in (SITE, "shared.tenant_entitlement_override", "shared.llm_call_log"):
        cur.execute("SELECT has_table_privilege('catalog_owner', %s, 'SELECT') AS ok", (table,))
        assert cur.fetchone()[0] is False, table
        cur.execute("SELECT has_any_column_privilege('tripplanner', %s, 'SELECT') AS ok", (table,))
        assert cur.fetchone()[0] is False, table
    cur.execute("SELECT has_schema_privilege('catalog_owner', 'catalog', 'CREATE') AS ok")
    assert cur.fetchone()[0] is False


def test_demo_tenant_has_no_lead_route(cur):
    cur.execute("SET LOCAL ROLE tripplanner")
    row = _rows(cur, TP_ROUTE_SQL, DEMO)[0]
    assert row["lead_channel"] == "none" and row["crm_core"] is False
    assert row["lead_webhook_url"] is None and row["lead_email"] is None


def test_lead_route_resolution(cur):
    crm, _ = _tenant(cur, "Quillmere Voyages synthetic")
    _subscribe(cur, crm, "crm", "crm.core")
    _site(cur, crm, lead_channel="webhook", lead_webhook_url="https://quillmere.example.invalid/leads")
    hook, _ = _tenant(cur, "Larchfield Expeditions synthetic")
    _site(cur, hook, lead_channel="webhook", lead_webhook_url="https://larchfield.example.invalid/hooks/tp")
    mail, _ = _tenant(cur, "Ottervale Journeys synthetic")
    _site(cur, mail, lead_channel="email", lead_email="desk@ottervale.example.invalid",
          lead_webhook_url="https://ottervale.example.invalid/unused")
    bare, _ = _tenant(cur, "Saltmarsh Routes synthetic")
    cur.execute("SET LOCAL ROLE tripplanner")
    by = {t: _rows(cur, TP_ROUTE_SQL, t)[0] for t in (crm, hook, mail, bare)}
    assert by[crm]["crm_core"] is True and by[crm]["lead_channel"] == "crm"
    assert by[crm]["lead_webhook_url"] is None and by[crm]["lead_email"] is None
    assert by[hook]["lead_channel"] == "webhook"
    assert by[hook]["lead_webhook_url"] == "https://larchfield.example.invalid/hooks/tp"
    assert by[mail]["lead_channel"] == "email" and by[mail]["lead_email"] == "desk@ottervale.example.invalid"
    assert by[mail]["lead_webhook_url"] is None                # only the resolved channel's target is served
    assert by[bare]["lead_channel"] == "none" and by[bare]["crm_core"] is False


@pytest.mark.parametrize("cols", [
    {"lead_channel": "webhook"},
    {"lead_channel": "email"},
    {"lead_channel": "sms"},
    {"lead_channel": "webhook", "lead_webhook_url": "http://agency.example.invalid/leads"},
    {"lead_channel": "webhook", "lead_webhook_url": "https://agency.example.invalid/a b"},
    {"lead_channel": "email", "lead_email": "not-an-address"},
    {"lead_channel": "webhook", "lead_webhook_url": "https://agency.example.invalid/" + "x" * 500},
])
def test_route_checks_refuse_bad_values(cur, cols):
    tid, _ = _tenant(cur, "Pebblewick Tours synthetic")
    with pytest.raises(psycopg2.errors.CheckViolation):
        _site(cur, tid, **cols)


def test_ai_budget_sources(cur):
    # A new tenant gets the content subscription of its plan tier from the CIS triggers (355 bridge): starter here.
    plan, _ = _tenant(cur, "Fernhollow Trails synthetic")
    override, _ = _tenant(cur, "Moorgate Passage synthetic")
    cur.execute("INSERT INTO shared.tenant_entitlement_override (tenant_id, feature_key, limit_value, enabled, reason, "
                "granted_by) VALUES (%s, 'ai.budget_usd_monthly', 12.5, true, 'synthetic test', 'test:390')",
                (override,))
    disabled, _ = _tenant(cur, "Bramblecourt Travel synthetic")
    cur.execute("INSERT INTO shared.tenant_entitlement_override (tenant_id, feature_key, enabled, reason, granted_by) "
                "VALUES (%s, 'ai.budget_usd_monthly', false, 'synthetic test', 'test:390')", (disabled,))
    # No content plan at all (a state the CIS guards normally prevent; the triggers are off for this one statement of
    # the superuser test login): no platform key resolves, so the view says 'none' and TripPlanner fails closed.
    nokey, _ = _tenant(cur, "Inkwell Coast synthetic")
    cur.execute("SET LOCAL session_replication_role = replica")
    cur.execute("DELETE FROM shared.tenant_subscription WHERE tenant_id = %s AND product = 'content'", (nokey,))
    cur.execute("SET LOCAL session_replication_role = DEFAULT")
    cur.execute("SET LOCAL ROLE tripplanner")
    by = {t: _rows(cur, TP_BUDGET_SQL, t)[0] for t in (plan, override, disabled, nokey)}
    assert by[plan]["budget_source"] == "entitlement" and float(by[plan]["monthly_usd"]) == 15.0
    assert by[override]["budget_source"] == "entitlement" and float(by[override]["monthly_usd"]) == 12.5
    assert by[disabled]["budget_source"] == "disabled" and float(by[disabled]["monthly_usd"]) == 0.0
    assert by[nokey]["budget_source"] == "none" and by[nokey]["monthly_usd"] is None


def test_month_spend_is_the_tenants_own_this_month(cur):
    a, _ = _tenant(cur, "Larchfield Expeditions synthetic")
    b, _ = _tenant(cur, "Ottervale Journeys synthetic")
    _ledger(cur, a, 1.25)
    _ledger(cur, a, 0.75)
    _ledger(cur, a, 5.0, shadow=True)        # AA's own A/B cost: not the tenant's
    _ledger(cur, a, 9.0, months_ago=2)       # an earlier month
    _ledger(cur, b, 3.0)                     # another tenant
    cur.execute("SET LOCAL ROLE tripplanner")
    ra, rb = _rows(cur, TP_BUDGET_SQL, a)[0], _rows(cur, TP_BUDGET_SQL, b)[0]
    assert float(ra["month_spent_usd"]) == pytest.approx(2.0) and ra["month_calls"] == 2
    assert float(rb["month_spent_usd"]) == pytest.approx(3.0) and rb["month_calls"] == 1


def test_internal_and_non_tripplanner_tenants_have_no_rows(cur):
    internal, _ = _tenant(cur, "Inner Desk synthetic", kind="internal", tier="internal")
    content_only, _ = _tenant(cur, "Brightwater Studio synthetic", tripplanner=False)
    cur.execute("SET LOCAL ROLE tripplanner")
    for tid in (internal, content_only):
        assert _rows(cur, TP_BUDGET_SQL, tid) == []
        assert _rows(cur, TP_ROUTE_SQL, tid) == []


def test_379_reads_still_answer(cur):
    tid, slug = _tenant(cur, "Quillmere Voyages synthetic")
    _site(cur, tid, theme=json.dumps({"accent": "#1a2b3c"}))
    cur.execute("SET LOCAL ROLE tripplanner")
    row = _rows(cur, t379.TP_TENANT_SQL, slug)[0]
    assert row["served"] is True and row["theme"] == {"accent": "#1a2b3c"}
    assert str(uuid.UUID(str(row["tenant_id"]))) == tid
