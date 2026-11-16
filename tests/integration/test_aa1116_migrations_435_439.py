"""AA-1116 (S282, option B pricing) — migrations 435, 436, 437 and 439 against real Postgres.

The conftest replay applies 435 / 436 / 437 / 439 like any file (438 is the catalog recipe: its own module). This module
re-applies each file (re-runnable, errors NOT swallowed), then every test runs in a transaction it rolls back:

  * 435: the new CHECKs (VND prices >= 0, seats_min 1-500, billing currency USD | VND, founding dates both or
    neither, agreed price and currency both or neither, billing interval month | year), no retail add-on product or
    unit price unit (S282 decision 14), a pilot without an end date, no shared.tenant_addon table,
    shared.v_setting_billing_effective (default, then the approved value in force; billing keys only);
  * 436: the nineteen keys at the option B values, maker-checker, enforced, guides present;
  * 437: the AI budget = content x the trial share while trialing + the crm / tripplanner increments (each x the trial
    share while that subscription trials); an override stays absolute; an AA-1016 row without an amount follows the
    plan; tenant_entitlement_sync_due re-syncs a changed trial share; the materialisation equals the resolver;
  * 439: the usage view's columns (233's order + four appended) and its numbers from shared.plan and the resolved
    quotas (built on a stand-in tenant_tour_versions inside the rolled-back transaction, as the 233 test does).
Invented names only.
"""
import json
import os
import sys
import uuid

import psycopg2
import psycopg2.errors
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

MIG = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")
M435 = os.path.join(MIG, "435_aa1116_pricing_option_b_schema.sql")
M436 = os.path.join(MIG, "436_aa1116_billing_policy_settings.sql")
M437 = os.path.join(MIG, "437_aa1116_resolver_trial_ai_budget.sql")
M439 = os.path.join(MIG, "439_aa1116_usage_view_reads_shared_plan.sql")
REASON = "zz test: a reason of at least twenty characters"
OPTION_B = {
    "cis.billing.trial_days_default": 14, "cis.billing.trial_days_max": 60, "cis.billing.trial_ai_budget_pct": 50,
    "cis.billing.pilot_free": True, "cis.tenant.pilot_max_weeks": 8, "cis.billing.ai_alert_pct_default": 80,
    "cis.billing.ai_approval_pct_default": 100, "cis.billing.ai_hard_stop_default": True,
    "cis.billing.annual_paid_months": 10, "cis.billing.founding_discount_pct": 30, "cis.billing.founding_months": 12,
    "cis.billing.founding_max_agencies": 10,
    "cis.billing.founding_eligible_plans": ["content.growth", "content.business", "crm.*", "tripplanner.*"],
    "cis.billing.combo_discount_pct": 10, "cis.billing.combo_products": ["content", "crm", "tripplanner"],
    "cis.billing.fx_vnd_per_usd": 26000, "cis.billing.vnd_price_review_months": 3,
    "cis.billing.price_lock_months": 12, "cis.billing.price_review_months": 12,
}


def _connect():
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)


def _body(path):
    with open(path, encoding="utf-8") as f:
        return "\n".join(line for line in f.read().splitlines() if line.strip() not in ("BEGIN;", "COMMIT;"))


@pytest.fixture(scope="module", autouse=True)
def reapplied():
    c = _connect()
    c.autocommit = True
    try:
        for path in (M435, M436, M437, M439):
            with open(path, encoding="utf-8") as f:
                c.cursor().execute(f.read())       # the replay applied them once: a re-run is a no-op
    finally:
        c.close()
    yield


@pytest.fixture
def cur():
    c = _connect()
    c.autocommit = False
    try:
        yield c.cursor()
    finally:
        c.rollback()
        c.close()


def _tenant(cur, tier="starter", **cols):
    tid = str(uuid.uuid4())
    names = ["tenant_id", "name", "slug", "plan_tier", "posts_per_week", *cols]
    vals = [tid, "Zz Marigold Paths", f"zz-marigold-{tid[:8]}", tier, 3, *cols.values()]
    cur.execute(f"INSERT INTO shared.tenants ({', '.join(names)}) VALUES ({', '.join(['%s'] * len(vals))})", vals)
    return tid


def _feature(cur, plan_key, feature_key, limit):
    cur.execute("INSERT INTO shared.plan_feature (plan_key, feature_key, limit_value, updated_by) "
                "VALUES (%s, %s, %s, 'test:1116') ON CONFLICT (plan_key, feature_key) "
                "DO UPDATE SET limit_value = EXCLUDED.limit_value", (plan_key, feature_key, limit))


def _sub(cur, tid, product, plan_key, status="active", trial_days=None):
    cur.execute("INSERT INTO shared.tenant_subscription (tenant_id, product, plan_key, status, trial_ends_at, "
                "updated_by) VALUES (%s, %s, %s, %s, CASE WHEN %s::int IS NULL THEN NULL "
                "ELSE now() + make_interval(days => %s::int) END, 'test:1116')",
                (tid, product, plan_key, status, trial_days, trial_days))


def _ai(cur, tid):
    cur.execute("SELECT limit_value AS v FROM shared.tenant_entitlement "
                "WHERE tenant_id = %s AND feature_key = 'ai.budget_usd_monthly'", (tid,))
    row = cur.fetchone()
    return None if row is None else (None if row[0] is None else float(row[0]))


def _resolved(cur, tid, key):
    cur.execute("SELECT limit_value AS v, source, plan_key FROM shared.v_tenant_entitlement_resolved "
                "WHERE tenant_id = %s AND feature_key = %s", (tid, key))
    return cur.fetchone()


def _approve(cur, key, value):
    cur.execute("SELECT coalesce(max(version), 0) + 1 AS v FROM shared.setting_value "
                "WHERE key = %s AND tenant_id IS NULL",
                (key,))
    version = cur.fetchone()[0]
    cur.execute("INSERT INTO shared.setting_value (key, value, version, effective_from, proposed_by, reason) "
                "VALUES (%s, %s::jsonb, %s, now(), 'admin:zz-a', %s) RETURNING id", (key, json.dumps(value), version,
                                                                                    REASON))
    vid = cur.fetchone()[0]
    cur.execute("UPDATE shared.setting_value SET status = 'approved', decided_by = 'admin:zz-b', "
                "decided_at = greatest(now(), proposed_at) WHERE id = %s", (vid,))


# ── registration ─────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("version", ["435", "436", "437", "439"])
def test_registered_once(cur, version):
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = %s", (version,))
    assert cur.fetchone()[0] == 1


# ── 435 ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_vnd_list_price_and_seat_minimum(cur):
    cur.execute("UPDATE shared.plan SET price_vnd_monthly = 494000, seats_min = 3 WHERE plan_key = 'crm.core' "
                "RETURNING price_vnd_monthly, seats_min")
    assert cur.fetchone() == (494000, 3)


def test_no_retail_addon_storage(cur):
    # S282 decision 14: no addon product, no unit price unit, no shared.tenant_addon table
    cur.execute("SELECT to_regclass('shared.tenant_addon') IS NULL AS gone")
    assert cur.fetchone()[0] is True


@pytest.mark.parametrize("sql", [
    "UPDATE shared.plan SET seats_min = 0 WHERE plan_key = 'crm.core'",
    "UPDATE shared.plan SET seats_min = 501 WHERE plan_key = 'crm.core'",
    "UPDATE shared.plan SET price_vnd_monthly = -1 WHERE plan_key = 'crm.core'",
    "UPDATE shared.plan SET price_floor_usd_monthly = -1 WHERE plan_key = 'content.enterprise'",
    "UPDATE shared.plan SET overage_usd_per_tour = -0.5 WHERE plan_key = 'content.starter'",
    "UPDATE shared.plan SET price_unit = 'month' WHERE plan_key = 'crm.core'",
    "INSERT INTO shared.plan (plan_key, product, name, price_unit, updated_by) "
    "VALUES ('bundle.zz', 'bundle', 'Zz', 'unit', 'test')",
    "INSERT INTO shared.plan (plan_key, product, name, price_unit, updated_by) "
    "VALUES ('addon.zz_lantern', 'addon', 'Zz', 'tenant', 'test')",
    "INSERT INTO shared.plan (plan_key, product, name, price_unit, updated_by) "
    "VALUES ('crm.zz_lantern', 'crm', 'Zz', 'unit', 'test')",
])
def test_plan_checks_refuse(cur, sql):
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(sql)


@pytest.mark.parametrize("cols", [
    {"billing_currency": "EUR"},
    {"founding_offer_from": "2027-03-01"},
    {"founding_offer_from": "2027-03-01", "founding_offer_until": "2027-02-01"},
])
def test_tenant_billing_terms_refuse(cur, cols):
    with pytest.raises(psycopg2.errors.CheckViolation):
        _tenant(cur, **cols)


def test_tenant_defaults_and_a_pilot_until_go_live(cur):
    tid = _tenant(cur, is_pilot=True, kind="agency")
    cur.execute("SELECT billing_currency, pilot_ends_at FROM shared.tenants WHERE tenant_id = %s", (tid,))
    assert cur.fetchone() == ("USD", None)
    with pytest.raises(psycopg2.errors.CheckViolation):     # a non-pilot still has no end date
        cur.execute("UPDATE shared.tenants SET is_pilot = false, pilot_ends_at = now() + interval '1 week' "
                    "WHERE tenant_id = %s", (tid,))


@pytest.mark.parametrize("sets", [
    "billing_interval = 'week'",
    "agreed_price_monthly = 500",
    "agreed_currency = 'VND'",
    "agreed_price_monthly = 500, agreed_currency = 'EUR'",
    "agreed_until = '2028-01-01'",
    "agreed_price_monthly = -1, agreed_currency = 'USD'",
])
def test_subscription_terms_refuse(cur, sets):
    tid = _tenant(cur)
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(f"UPDATE shared.tenant_subscription SET {sets} WHERE tenant_id = %s", (tid,))


def test_subscription_terms_accept_a_locked_price(cur):
    tid = _tenant(cur)
    cur.execute("UPDATE shared.tenant_subscription SET billing_interval = 'year', agreed_price_monthly = 349, "
                "agreed_currency = 'USD', agreed_until = current_date + 365 WHERE tenant_id = %s "
                "RETURNING billing_interval", (tid,))
    assert cur.fetchone()[0] == "year"


def test_a_crm_plan_still_cannot_list_a_tripplanner_flag(cur):
    with pytest.raises(psycopg2.errors.CheckViolation):
        _feature(cur, "crm.core", "tp.custom_domain", None)


def _effective(cur, key):
    cur.execute("SELECT value #>> '{}' AS v FROM shared.v_setting_billing_effective WHERE key = %s", (key,))
    row = cur.fetchone()
    return None if row is None else row[0]


def test_setting_view_default_then_approved(cur):
    assert _effective(cur, "cis.billing.trial_ai_budget_pct") == "50"
    _approve(cur, "cis.billing.trial_ai_budget_pct", 40)
    assert _effective(cur, "cis.billing.trial_ai_budget_pct") == "40"


def test_setting_view_serves_billing_keys_only(cur):
    cur.execute("SELECT count(*) AS n FROM shared.v_setting_billing_effective WHERE key NOT LIKE 'cis.billing.%%'")
    assert cur.fetchone()[0] == 0
    assert _effective(cur, "cis.settings.two_person_approval") is None
    for role, ok in (("cis_app", True), ("catalog_owner", True), ("booking", False), ("tripplanner", False)):
        cur.execute("SELECT has_table_privilege(%s, 'shared.v_setting_billing_effective', 'SELECT') AS ok", (role,))
        assert cur.fetchone()[0] is ok, role


# ── 436 ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_option_b_keys_seeded(cur):
    cur.execute("SELECT key, default_value, approval, enforced, guide IS NOT NULL AS g FROM shared.setting_definition "
                "WHERE key = ANY(%s)", (list(OPTION_B),))
    rows = {r[0]: r[1:] for r in cur.fetchall()}
    assert set(rows) == set(OPTION_B)
    for key, want in OPTION_B.items():
        assert rows[key] == (want, "maker_checker", True, True), key


# ── 437 ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_ai_budget_content_plus_tripplanner_increment(cur):
    _feature(cur, "tripplanner.hosted", "ai.budget_addon_usd_monthly", 10)
    tid = _tenant(cur, tier="growth")
    assert _ai(cur, tid) == 40
    _sub(cur, tid, "tripplanner", "tripplanner.hosted")
    assert _ai(cur, tid) == 50
    assert _resolved(cur, tid, "ai.budget_addon_usd_monthly") is None


def test_trial_share_applies_to_each_trialing_part(cur):
    _feature(cur, "tripplanner.hosted", "ai.budget_addon_usd_monthly", 10)
    tid = _tenant(cur, tier="growth")
    cur.execute("UPDATE shared.tenant_subscription SET status = 'trialing', trial_ends_at = now() + interval '14 days' "
                "WHERE tenant_id = %s", (tid,))
    assert _ai(cur, tid) == 20                      # 40 x 50 %
    _sub(cur, tid, "tripplanner", "tripplanner.hosted", status="trialing", trial_days=14)
    assert _ai(cur, tid) == 25                      # 20 + 10 x 50 %
    cur.execute("UPDATE shared.tenant_subscription SET status = 'active', trial_ends_at = NULL "
                "WHERE tenant_id = %s AND product = 'content'", (tid,))
    assert _ai(cur, tid) == 45                      # 40 + 5


def test_changed_trial_share_reaches_the_materialisation_through_sync_due(cur):
    tid = _tenant(cur, tier="growth")
    cur.execute("UPDATE shared.tenant_subscription SET status = 'trialing', trial_ends_at = now() + interval '14 days' "
                "WHERE tenant_id = %s", (tid,))
    assert _ai(cur, tid) == 20
    _approve(cur, "cis.billing.trial_ai_budget_pct", 25)
    assert _ai(cur, tid) == 20                      # materialised until the hourly re-sync
    cur.execute("SELECT shared.tenant_entitlement_sync_due() AS n")
    assert _ai(cur, tid) == 10


def test_override_stays_absolute(cur):
    _feature(cur, "tripplanner.hosted", "ai.budget_addon_usd_monthly", 10)
    tid = _tenant(cur)
    _sub(cur, tid, "tripplanner", "tripplanner.hosted")
    assert _ai(cur, tid) == 25
    cur.execute("INSERT INTO shared.tenant_entitlement_override (tenant_id, feature_key, limit_value, reason, "
                "granted_by) VALUES (%s, 'ai.budget_usd_monthly', 7, 'zz contract cap', 'test')", (tid,))
    assert _ai(cur, tid) == 7


def test_ai_budget_row_without_amount_follows_the_plan(cur):
    tid = _tenant(cur)
    cur.execute("INSERT INTO shared.tenant_ai_budget (tenant_id, monthly_usd, alert_pct, approval_pct, hard_stop, "
                "updated_by) VALUES (%s, NULL, 80, 100, true, 'test')", (tid,))
    assert _ai(cur, tid) == 15
    cur.execute("UPDATE shared.tenant_ai_budget SET monthly_usd = 3 WHERE tenant_id = %s", (tid,))
    assert _ai(cur, tid) == 3


def test_ai_budget_row_without_amount_is_unlimited_for_internal(cur):
    tid = _tenant(cur, tier="internal", kind="internal")
    cur.execute("INSERT INTO shared.tenant_ai_budget (tenant_id, monthly_usd, alert_pct, approval_pct, hard_stop, "
                "updated_by) VALUES (%s, NULL, 80, 100, true, 'test')", (tid,))
    cur.execute("SELECT source, limit_value AS v FROM shared.tenant_entitlement "
                "WHERE tenant_id = %s AND feature_key = 'ai.budget_usd_monthly'", (tid,))
    assert cur.fetchone() == ("override", None)


def test_materialisation_equals_the_resolver(cur):
    _feature(cur, "tripplanner.hosted", "ai.budget_addon_usd_monthly", 10)
    _feature(cur, "crm.core", "ai.budget_addon_usd_monthly", 4)
    tid = _tenant(cur, tier="business")
    _sub(cur, tid, "tripplanner", "tripplanner.hosted", status="trialing", trial_days=14)
    _sub(cur, tid, "crm", "crm.core")
    cur.execute("SELECT count(*) AS n FROM shared.v_tenant_entitlement_resolved r "
                "FULL JOIN (SELECT * FROM shared.tenant_entitlement WHERE tenant_id = %s) e "
                "ON e.feature_key = r.feature_key AND e.tenant_id = r.tenant_id "
                "WHERE (r.tenant_id = %s OR e.tenant_id = %s) AND (r.feature_key IS NULL OR e.feature_key IS NULL "
                "OR r.limit_value IS DISTINCT FROM e.limit_value OR r.source IS DISTINCT FROM e.source)",
                (tid, tid, tid))
    assert cur.fetchone()[0] == 0
    assert _ai(cur, tid) == 89                      # 80 + 10 x 50 % + 4


# ── 439 ──────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def usage_cur(cur):
    cur.execute("SELECT to_regclass('gold_aa_internal.tenant_tour_versions')")
    if cur.fetchone()[0] is None:
        cur.execute("CREATE SCHEMA IF NOT EXISTS gold_aa_internal; "
                    "CREATE TABLE gold_aa_internal.tenant_tour_versions (id uuid PRIMARY KEY DEFAULT "
                    "gen_random_uuid(), tenant_id uuid NOT NULL, created_at timestamptz NOT NULL DEFAULT now())")
    cur.execute("DELETE FROM shared.schema_versions WHERE version = '439'")
    cur.execute(_body(M439))
    cur.execute(_body(M439))
    yield cur


def test_usage_view_columns(usage_cur):
    usage_cur.execute("SELECT string_agg(column_name, ',' ORDER BY ordinal_position) AS c FROM "
                      "information_schema.columns WHERE table_schema = 'shared' "
                      "AND table_name = 'v_tenant_monthly_usage'")
    assert usage_cur.fetchone()[0] == (
        "tenant_id,tenant_name,plan_tier,slug,tours_quota_monthly,api_calls_quota_monthly,price_usd_monthly,"
        "overage_rate_usd_per_tour,billing_month,tours_rewritten,api_calls_used,quota_tours_pct,quota_calls_pct,"
        "tours_overage,overage_usd,llm_cost_usd,plan_key,billing_currency,price_vnd_monthly,"
        "overage_rate_usd_per_1k_calls")


def test_usage_view_reads_shared_plan_and_the_resolved_quota(usage_cur):
    cur = usage_cur
    cur.execute("UPDATE shared.plan SET price_usd_monthly = 149, price_vnd_monthly = 3790000, "
                "overage_usd_per_tour = 4 WHERE plan_key = 'content.starter'")
    tid = _tenant(cur, billing_currency="VND")
    cur.execute("INSERT INTO shared.tenant_entitlement_override (tenant_id, feature_key, limit_value, reason, "
                "granted_by) VALUES (%s, 'content.rewrite', 2, 'zz small quota', 'test')", (tid,))
    for _ in range(3):
        cur.execute("INSERT INTO gold_aa_internal.tenant_tour_versions (tenant_id) VALUES (%s)", (tid,))
    cur.execute("SELECT price_usd_monthly AS p, tours_quota_monthly AS q, tours_rewritten AS r, tours_overage AS o, "
                "overage_usd AS u, plan_key, billing_currency AS c, price_vnd_monthly AS v, api_calls_quota_monthly "
                "AS a FROM shared.v_tenant_monthly_usage WHERE tenant_id = %s", (tid,))
    p, q, r, o, u, plan_key, c, v, a = cur.fetchone()
    assert (float(p), q, r, o, float(u), plan_key, c, int(v), a) == (
        149.0, 2, 3, 1, 4.0, "content.starter", "VND", 3790000, 5000)


def test_usage_view_no_quota_means_no_overage(usage_cur):
    cur = usage_cur
    tid = _tenant(cur, tier="internal", kind="internal")
    cur.execute("INSERT INTO gold_aa_internal.tenant_tour_versions (tenant_id) VALUES (%s)", (tid,))
    cur.execute("SELECT tours_quota_monthly AS q, tours_overage AS o, quota_tours_pct AS pct "
                "FROM shared.v_tenant_monthly_usage WHERE tenant_id = %s", (tid,))
    assert cur.fetchone() == (None, 0, 0)
