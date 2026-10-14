"""AA-798 — migration 223 against real Postgres: shared.v_tenant_monthly_usage keeps the live
column shape and llm_cost_usd is the tenant's real month-to-date llm_call_log spend.

Lesson S231: conftest swallows migration errors, so a green test can run against a view that
was never created. This test therefore applies 223 itself and asserts the view column exists
before it reads any value.

The CI test DB has no gold_aa_internal.tenant_tour_versions (no repo migration creates it; on Dev
it predates the migration history), so conftest's own run of 223 fails with a warning there. The
test runs the migration body inside one transaction, adds a minimal stand-in for that table only
when it is missing, and rolls everything back at the end, so the shared test DB is left as it was.
"""
import os
import sys
import uuid
from decimal import Decimal

import psycopg2
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                         "223_tenant_usage_real_llm_cost.sql")

LIVE_COLUMNS = [
    "tenant_id", "tenant_name", "plan_tier", "slug", "tours_quota_monthly",
    "api_calls_quota_monthly", "price_usd_monthly", "overage_rate_usd_per_tour", "billing_month",
    "tours_rewritten", "api_calls_used", "quota_tours_pct", "quota_calls_pct", "tours_overage",
    "overage_usd", "llm_cost_usd",
]
STARTER_PLAN_ID = "5e1c9c9d-6517-4c48-9421-beb5c780f044"


def _migration_body() -> str:
    """The file without its own BEGIN/COMMIT, so the test can run it in a transaction it rolls back."""
    with open(MIGRATION) as f:
        lines = f.read().splitlines()
    return "\n".join(line for line in lines if line.strip() not in ("BEGIN;", "COMMIT;"))


@pytest.fixture
def cur():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER,
                            password=DB_PASS)
    conn.autocommit = False
    c = conn.cursor()
    c.execute("SELECT to_regclass('gold_aa_internal.tenant_tour_versions')")
    if c.fetchone()[0] is None:
        c.execute("""
            CREATE SCHEMA IF NOT EXISTS gold_aa_internal;
            CREATE TABLE gold_aa_internal.tenant_tour_versions (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                tenant_id UUID NOT NULL,
                edit_source TEXT NOT NULL DEFAULT 'ai_generated',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")
    body = _migration_body()
    c.execute(body)
    c.execute(body)  # idempotent: a second run must not fail or duplicate the seeded plans
    yield c
    conn.rollback()
    conn.close()


def _tenant(cur, plan_id=STARTER_PLAN_ID) -> str:
    tid = str(uuid.uuid4())
    cur.execute("""
        INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, rate_limit_rpm, is_active,
                                    posts_per_week, plan_id)
        VALUES (%s, 'AA-798 test', %s, 'starter', 60, true, 3, %s)""",
                (tid, f"aa798-{tid[:8]}", plan_id))
    return tid


def _call(cur, tenant_id, cost, when="now()", shadow=False):
    signal = '{"shadow": true, "shadow_of": "x"}' if shadow else '{"heuristic": 1}'
    cur.execute(f"""
        INSERT INTO shared.llm_call_log (tenant_id, stage, role, model, cost_usd, quality_signal,
                                         created_at)
        VALUES (%s, 'adhoc_aa798', 'writer', 'test-model', %s, %s::jsonb, {when})""",
                (tenant_id, cost, signal))


def _usage(cur, tenant_id):
    cur.execute("SELECT * FROM shared.v_tenant_monthly_usage WHERE tenant_id = %s", (tenant_id,))
    names = [d[0] for d in cur.description]
    rows = cur.fetchall()
    assert len(rows) == 1
    return dict(zip(names, rows[0]))


def test_view_keeps_the_live_columns_in_order(cur):
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'shared' AND table_name = 'v_tenant_monthly_usage'
        ORDER BY ordinal_position""")
    cols = [r[0] for r in cur.fetchall()]
    assert "llm_cost_usd" in cols  # lesson S231: the column exists before any value is read
    assert cols == LIVE_COLUMNS


def test_llm_cost_is_month_to_date_llm_call_log_sum(cur):
    cur.execute("SELECT 1 FROM information_schema.columns WHERE table_schema = 'shared' "
                "AND table_name = 'v_tenant_monthly_usage' AND column_name = 'llm_cost_usd'")
    assert cur.fetchone() is not None

    tenant, other = _tenant(cur), _tenant(cur)
    _call(cur, tenant, Decimal("0.500000"))
    _call(cur, tenant, Decimal("0.250000"))
    _call(cur, tenant, Decimal("1.000000"), shadow=True)                          # shadow: excluded
    _call(cur, tenant, Decimal("2.000000"),
          when="date_trunc('month', now()) - interval '1 day'")                  # last month
    _call(cur, other, Decimal("4.000000"))                                        # another tenant
    _call(cur, None, Decimal("8.000000"))                                         # platform call

    row = _usage(cur, tenant)
    assert row["llm_cost_usd"] == Decimal("0.7500")
    assert _usage(cur, other)["llm_cost_usd"] == Decimal("4.0000")


def test_tenant_without_calls_or_plan_reads_zero_cost_and_null_quota(cur):
    tenant = _tenant(cur, plan_id=None)
    row = _usage(cur, tenant)
    assert row["llm_cost_usd"] == Decimal("0")
    assert row["tours_quota_monthly"] is None
    assert row["tours_rewritten"] == 0


def test_plan_quota_comes_from_membership_plans(cur):
    cur.execute("SELECT plan_name FROM shared.membership_plans ORDER BY plan_name")
    names = [r[0] for r in cur.fetchall()]
    for plan in ("business", "enterprise", "growth", "internal", "starter"):
        assert names.count(plan) == 1
    row = _usage(cur, _tenant(cur))
    assert row["tours_quota_monthly"] == 50
    assert row["api_calls_quota_monthly"] == 5000
    assert row["price_usd_monthly"] == Decimal("299.00")
