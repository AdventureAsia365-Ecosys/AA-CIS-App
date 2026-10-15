"""AA-825 — migration 233 against real Postgres: the sargable shared.v_tenant_monthly_usage returns
exactly what the 223 definition returned, the planner can now use the new
(tenant_id, called_at) index, and the two rollup tables exist with their keys.

Same approach as test_aa798_usage_view.py: the CI test DB has no
gold_aa_internal.tenant_tour_versions (no repo migration creates it), so conftest's own run of 223
and 233 fails there with a warning. The fixture adds a minimal stand-in for that table only when it
is missing, runs the 223 and 233 bodies inside one transaction and rolls everything back, so the
shared test DB is left as it was.
"""
import os
import sys
import uuid

import psycopg2
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

MIGRATIONS = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")
M223 = os.path.join(MIGRATIONS, "223_tenant_usage_real_llm_cost.sql")
M233 = os.path.join(MIGRATIONS, "233_retention_rollups_sargable_usage.sql")

LIVE_COLUMNS = [
    "tenant_id", "tenant_name", "plan_tier", "slug", "tours_quota_monthly",
    "api_calls_quota_monthly", "price_usd_monthly", "overage_rate_usd_per_tour", "billing_month",
    "tours_rewritten", "api_calls_used", "quota_tours_pct", "quota_calls_pct", "tours_overage",
    "overage_usd", "llm_cost_usd",
]
STARTER_PLAN_ID = "5e1c9c9d-6517-4c48-9421-beb5c780f044"


def _body(path: str) -> str:
    """The file without its own BEGIN/COMMIT, so the test can run it in a transaction it rolls back."""
    with open(path) as f:
        lines = f.read().splitlines()
    return "\n".join(line for line in lines if line.strip() not in ("BEGIN;", "COMMIT;"))


def _old_view_sql() -> str:
    """The 223 view, renamed, to compare numbers with the 233 rewrite in the same transaction."""
    body = _body(M223)
    start = body.index("CREATE VIEW shared.v_tenant_monthly_usage AS")
    end = body.index(";", body.index("GROUP BY t.tenant_id", start)) + 1
    return body[start:end].replace("shared.v_tenant_monthly_usage", "shared.v_usage_223_aa825")


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
    c.execute(_body(M223))
    c.execute(_old_view_sql())
    body = _body(M233)
    c.execute(body)
    c.execute(body)  # idempotent: a second run must not fail
    yield c
    conn.rollback()
    conn.close()


def _tenant(cur) -> str:
    tid = str(uuid.uuid4())
    cur.execute("""
        INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, rate_limit_rpm, is_active,
                                    posts_per_week, plan_id)
        VALUES (%s, 'AA-825 test', %s, 'starter', 60, true, 3, %s)""",
                (tid, f"aa825-{tid[:8]}", STARTER_PLAN_ID))
    return tid


def _api(cur, tenant_id, when):
    actor = "tenant" if tenant_id else "admin"
    cur.execute(f"""
        INSERT INTO shared.tenant_api_usage (tenant_id, endpoint, method, status_code, response_ms,
                                             called_at, actor_type)
        VALUES (%s, '/v1/tours', 'GET', 200, 12, {when}, %s)""", (tenant_id, actor))


def _rows(cur, view, tenant_ids):
    cur.execute(f"SELECT * FROM {view} WHERE tenant_id = ANY(%s::uuid[]) ORDER BY tenant_id",
                (tenant_ids,))
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def test_view_keeps_the_live_columns_in_order(cur):
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'shared' AND table_name = 'v_tenant_monthly_usage'
        ORDER BY ordinal_position""")
    assert [r[0] for r in cur.fetchall()] == LIVE_COLUMNS


def test_sargable_view_returns_the_same_numbers_as_223(cur):
    a, b = _tenant(cur), _tenant(cur)
    month = "date_trunc('month', now())"
    for when in (month,                                         # first instant of the month: in
                 f"{month} + interval '3 days'",
                 "now()",
                 f"{month} - interval '1 microsecond'",          # last instant of last month: out
                 f"{month} + interval '1 month'",                # first instant of next month: out
                 f"{month} - interval '40 days'"):
        _api(cur, a, when)
    _api(cur, b, "now()")
    _api(cur, None, "now()")                                     # admin traffic: no tenant
    cur.execute("""
        INSERT INTO gold_aa_internal.tenant_tour_versions (tenant_id, created_at)
        VALUES (%s, date_trunc('month', now())), (%s, now()),
               (%s, date_trunc('month', now()) - interval '1 microsecond')""", (a, a, a))
    cur.execute("""
        INSERT INTO shared.llm_call_log (tenant_id, stage, role, model, cost_usd, quality_signal,
                                         created_at)
        VALUES (%s, 'adhoc_aa825', 'writer', 'm', 0.5, '{}'::jsonb, now()),
               (%s, 'adhoc_aa825', 'writer', 'm', 2.0, '{}'::jsonb,
                date_trunc('month', now()) - interval '1 second')""", (a, a))

    new = _rows(cur, "shared.v_tenant_monthly_usage", [a, b])
    old = _rows(cur, "shared.v_usage_223_aa825", [a, b])
    assert new == old
    by_tenant = {str(r["tenant_id"]): r for r in new}
    assert by_tenant[a]["api_calls_used"] == 3
    assert by_tenant[a]["tours_rewritten"] == 2
    assert by_tenant[b]["api_calls_used"] == 1


def _plan(cur, view):
    cur.execute(f"EXPLAIN SELECT api_calls_used FROM {view}")
    return "\n".join(r[0] for r in cur.fetchall())


def test_api_calls_subquery_uses_the_tenant_called_index(cur):
    # An empty test table makes the planner prefer the narrower single-column tenant_id index, so
    # take it out of the picture (rolled back with the fixture) and force index access.
    cur.execute("DROP INDEX shared.idx_tenant_api_usage_tenant_id")
    cur.execute("SET LOCAL enable_seqscan = off")
    cur.execute("SET LOCAL enable_bitmapscan = off")
    new = _plan(cur, "shared.v_tenant_monthly_usage")
    assert "tenant_api_usage_tenant_called_idx" in new
    # The month bound is an index condition (range on the raw column)...
    cond = [ln for ln in new.splitlines() if "Index Cond" in ln and "called_at" in ln]
    assert cond and "called_at >= date_trunc('month'" in cond[0]
    # ...while 223 could only filter date_trunc('month', called_at) row by row.
    old = _plan(cur, "shared.v_usage_223_aa825")
    assert not [ln for ln in old.splitlines() if "Index Cond" in ln and "called_at" in ln]


def test_retention_indexes_and_rollup_tables_exist(cur):
    cur.execute("""
        SELECT indexname FROM pg_indexes WHERE schemaname = 'shared' AND indexname = ANY(%s)""",
                (["tenant_api_usage_tenant_called_idx", "llm_call_log_created_id_idx",
                  "decision_log_created_id_idx", "job_finished_idx",
                  "llm_call_daily_tenant_day_idx", "tenant_api_usage_monthly_tenant_idx"],))
    assert len(cur.fetchall()) == 6
    cur.execute("SELECT to_regclass('shared.llm_call_daily'), to_regclass('shared.tenant_api_usage_monthly')")
    assert all(cur.fetchone())
    cur.execute("SELECT 1 FROM shared.schema_versions WHERE version = '233'")
    assert cur.fetchone() is not None


def test_rollup_keys_treat_null_tenant_as_one_group(cur):
    cur.execute("""
        INSERT INTO shared.tenant_api_usage_monthly
            (month, tenant_id, actor_type, calls, ok_calls, rate_limited_calls, server_error_calls)
        VALUES ('2026-01-01', NULL, 'admin', 1, 1, 0, 0)""")
    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute("""
            INSERT INTO shared.tenant_api_usage_monthly
                (month, tenant_id, actor_type, calls, ok_calls, rate_limited_calls, server_error_calls)
            VALUES ('2026-01-01', NULL, 'admin', 2, 2, 0, 0)""")


def test_monthly_rollup_rejects_a_mid_month_date(cur):
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("""
            INSERT INTO shared.tenant_api_usage_monthly
                (month, tenant_id, actor_type, calls, ok_calls, rate_limited_calls, server_error_calls)
            VALUES ('2026-01-15', NULL, 'admin', 1, 1, 0, 0)""")


def test_without_tenant_tour_versions_the_view_is_skipped_and_the_rest_applies():
    """The CI test DB has no tenant_tour_versions: 233 must still create the indexes and rollup
    tables there (the retention tests need them) and only skip the view rebuild."""
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER,
                            password=DB_PASS)
    conn.autocommit = False
    c = conn.cursor()
    try:
        c.execute("SELECT to_regclass('gold_aa_internal.tenant_tour_versions')")
        if c.fetchone()[0] is not None:
            pytest.skip("this test DB has tenant_tour_versions")
        c.execute("DROP TABLE IF EXISTS shared.llm_call_daily, shared.tenant_api_usage_monthly")
        c.execute(_body(M233))
        c.execute("SELECT to_regclass('shared.llm_call_daily'), "
                  "to_regclass('shared.tenant_api_usage_monthly')")
        assert all(c.fetchone())
    finally:
        conn.rollback()
        conn.close()
