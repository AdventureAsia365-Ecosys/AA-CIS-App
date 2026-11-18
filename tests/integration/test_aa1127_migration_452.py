"""AA-1127 (S284, UI audit batch S1) — migration 452 against real Postgres: one ledger for tour rewrites.

shared.v_tenant_monthly_usage.tours_rewritten (GET /v1/billing, the admin tenant list, Tenant 360, the overage) reads the
rewrite quota counter shared.tenant_rewrite_usage for the current UTC month, the row GET /v1/quota reads and
trigger_rewrite increments. A tenant's own inline edit (a tenant_edit version) is no rewrite and is not billed.

The CI test DB has no gold_aa_internal.tenant_tour_versions (452 skips there, as 233 / 439): every test runs the file
inside a transaction with a stand-in for that table, and rolls back. Invented names only.
"""
import os
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

M452 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                    "452_aa1127_usage_view_rewrite_ledger.sql")
COLUMNS = ("tenant_id,tenant_name,plan_tier,slug,tours_quota_monthly,api_calls_quota_monthly,price_usd_monthly,"
           "overage_rate_usd_per_tour,billing_month,tours_rewritten,api_calls_used,quota_tours_pct,quota_calls_pct,"
           "tours_overage,overage_usd,llm_cost_usd,plan_key,billing_currency,price_vnd_monthly,"
           "overage_rate_usd_per_1k_calls")


def _body():
    with open(M452, encoding="utf-8") as f:
        return "\n".join(line for line in f.read().splitlines() if line.strip() not in ("BEGIN;", "COMMIT;"))


@pytest.fixture
def cur():
    c = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    c.autocommit = False
    k = c.cursor()
    try:
        k.execute("SELECT to_regclass('gold_aa_internal.tenant_tour_versions')")
        if k.fetchone()[0] is None:
            k.execute("CREATE SCHEMA IF NOT EXISTS gold_aa_internal; "
                      "CREATE TABLE gold_aa_internal.tenant_tour_versions (id uuid PRIMARY KEY DEFAULT "
                      "gen_random_uuid(), tenant_id uuid NOT NULL, edit_source text NOT NULL DEFAULT 'ai_generated', "
                      "created_at timestamptz NOT NULL DEFAULT now())")
        k.execute("DELETE FROM shared.schema_versions WHERE version = '452'")
        k.execute(_body())
        k.execute(_body())          # idempotent
        yield k
    finally:
        c.rollback()
        c.close()


def _tenant(cur):
    tid = str(uuid.uuid4())
    cur.execute("INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week) "
                "VALUES (%s, 'Zz Juniper Trails', %s, 'starter', 3)", (tid, f"zz-juniper-{tid[:8]}"))
    return tid


def _quota(cur, tid, n):
    cur.execute("INSERT INTO shared.tenant_entitlement_override (tenant_id, feature_key, limit_value, reason, "
                "granted_by) VALUES (%s, 'content.rewrite', %s, 'zz small rewrite quota', 'test')", (tid, n))


def _month_key():
    """The key GET /v1/quota and trigger_rewrite use (api/routers/v1_tours.py)."""
    from api.routers.v1_tours import _current_year_month_and_reset
    return _current_year_month_and_reset()[0]


def _counter(cur, tid, n, month=None):
    cur.execute("INSERT INTO shared.tenant_rewrite_usage (tenant_id, year_month, rewrite_count) VALUES (%s, %s, %s)",
                (tid, month or _month_key(), n))


def _usage(cur, tid):
    cur.execute("SELECT tours_quota_monthly AS q, tours_rewritten AS r, tours_overage AS o, quota_tours_pct AS pct "
                "FROM shared.v_tenant_monthly_usage WHERE tenant_id = %s", (tid,))
    return cur.fetchone()


def test_columns_and_registration(cur):
    cur.execute("SELECT string_agg(column_name, ',' ORDER BY ordinal_position) AS c FROM information_schema.columns "
                "WHERE table_schema = 'shared' AND table_name = 'v_tenant_monthly_usage'")
    assert cur.fetchone()[0] == COLUMNS
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '452'")
    assert cur.fetchone()[0] == 1


def test_tours_rewritten_is_the_quota_counter(cur):
    tid = _tenant(cur)
    _quota(cur, tid, 2)
    _counter(cur, tid, 3)
    _counter(cur, tid, 9, month="2001-01")          # another month never counts
    q, r, o, pct = _usage(cur, tid)
    assert (q, r, o, float(pct)) == (2, 3, 1, 150.0)


def test_a_tenant_edit_is_not_a_rewrite(cur):
    tid = _tenant(cur)
    _quota(cur, tid, 5)
    _counter(cur, tid, 1)
    for source in ("ai_generated", "tenant_edit", "tenant_edit"):
        cur.execute("INSERT INTO gold_aa_internal.tenant_tour_versions (tenant_id, edit_source) VALUES (%s, %s)",
                    (tid, source))
    assert _usage(cur, tid)[1] == 1


def test_no_counter_row_is_zero(cur):
    tid = _tenant(cur)
    _quota(cur, tid, 5)
    q, r, o, pct = _usage(cur, tid)
    assert (q, r, o, float(pct)) == (5, 0, 0, 0.0)


def test_quota_route_and_billing_view_read_one_number(cur):
    """The pin of the shared source: the counter row GET /v1/quota reads (same SQL text) is the view's number."""
    import inspect

    from api.routers import v1_tours
    src = inspect.getsource(v1_tours.get_quota)
    assert "FROM shared.tenant_rewrite_usage" in src and "year_month = $2" in src
    tid = _tenant(cur)
    _counter(cur, tid, 7)
    cur.execute("SELECT rewrite_count FROM shared.tenant_rewrite_usage WHERE tenant_id = %s AND year_month = %s",
                (tid, _month_key()))
    assert cur.fetchone()[0] == _usage(cur, tid)[1] == 7
