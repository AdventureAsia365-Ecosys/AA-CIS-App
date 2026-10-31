"""AA-825 — rollups written before raw rows expire (DM-7.4). Days and months are UTC.

Invariant: a day (llm_call_daily) or month (tenant_api_usage_monthly) is rolled up exactly once, in
one statement, from its COMPLETE raw rows, before the first raw row of it is deleted:
  - the cutoff passed in is a UTC midnight (runner.cutoff), so every day older than it is whole;
  - for months, the statement covers whole months up to the end of the cutoff's month, which is
    closed long ago (window 90 days > a month);
  - a day/month that already has any rollup row is skipped (NOT EXISTS), because its raw rows may
    already be partly deleted and recomputing would undercount. ON CONFLICT DO NOTHING is only a
    second guard.
Raw rows of a closed day/month never change afterwards (created_at / called_at default now()).
"""
from __future__ import annotations

LLM_DAILY_INSERT_SQL = """
    INSERT INTO shared.llm_call_daily
        (day, app, tenant_id, stage, role, model, provider, account, is_shadow,
         calls, fallback_calls, tokens_in, tokens_out, cost_usd)
    SELECT (l.created_at AT TIME ZONE 'UTC')::date,
           coalesce(l.quality_signal ->> 'app', 'cis'),
           l.tenant_id, l.stage, l.role, l.model, l.provider, l.account,
           l.quality_signal @> '{"shadow": true}'::jsonb,
           count(*),
           count(*) FILTER (WHERE l.fallback_used),
           coalesce(sum(l.tokens_in), 0),
           coalesce(sum(l.tokens_out), 0),
           coalesce(sum(l.cost_usd), 0)
      FROM shared.llm_call_log l
     WHERE l.created_at < $1
       AND NOT EXISTS (SELECT 1 FROM shared.llm_call_daily d
                        WHERE d.day = (l.created_at AT TIME ZONE 'UTC')::date)
     GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9
    ON CONFLICT ON CONSTRAINT llm_call_daily_key DO NOTHING
"""

# AA-1002 (S6, migration 312): the same rollup plus the cache split and the count of rows priced by estimate (a model
# no price source knew). Used only when both tables have the 312 columns (``_s6_columns_ready``), so the retention
# job keeps working before the migration is applied.
LLM_DAILY_INSERT_S6_SQL = """
    INSERT INTO shared.llm_call_daily
        (day, app, tenant_id, stage, role, model, provider, account, is_shadow,
         calls, fallback_calls, tokens_in, tokens_out, cost_usd,
         cache_read_tokens, cache_write_tokens, estimate_calls)
    SELECT (l.created_at AT TIME ZONE 'UTC')::date,
           coalesce(l.quality_signal ->> 'app', 'cis'),
           l.tenant_id, l.stage, l.role, l.model, l.provider, l.account,
           l.quality_signal @> '{"shadow": true}'::jsonb,
           count(*),
           count(*) FILTER (WHERE l.fallback_used),
           coalesce(sum(l.tokens_in), 0),
           coalesce(sum(l.tokens_out), 0),
           coalesce(sum(l.cost_usd), 0),
           coalesce(sum(l.cache_read_tokens), 0),
           coalesce(sum(l.cache_write_tokens), 0),
           count(*) FILTER (WHERE l.pricing_mode LIKE 'estimate%')
      FROM shared.llm_call_log l
     WHERE l.created_at < $1
       AND NOT EXISTS (SELECT 1 FROM shared.llm_call_daily d
                        WHERE d.day = (l.created_at AT TIME ZONE 'UTC')::date)
     GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9
    ON CONFLICT ON CONSTRAINT llm_call_daily_key DO NOTHING
"""

# AA-804 (ledger v2, migration 319): the S6 rollup with app and is_shadow read from their real columns first (rows
# before 319 and TripPlanner's keep them in quality_signal: coalesce, no backfill), plus the outcome counts and the
# latency sum / count. calls stays every row of the group (refused / error attempts included, at 0 USD). Used only
# when both tables have the 319 columns (``_v2_columns_ready``).
LLM_DAILY_INSERT_V2_SQL = """
    INSERT INTO shared.llm_call_daily
        (day, app, tenant_id, stage, role, model, provider, account, is_shadow,
         calls, fallback_calls, tokens_in, tokens_out, cost_usd,
         cache_read_tokens, cache_write_tokens, estimate_calls,
         refused_calls, error_calls, latency_ms_sum, latency_ms_count)
    SELECT (l.created_at AT TIME ZONE 'UTC')::date,
           coalesce(l.app, l.quality_signal ->> 'app', 'cis'),
           l.tenant_id, l.stage, l.role, l.model, l.provider, l.account,
           coalesce(l.is_shadow, l.quality_signal @> '{"shadow": true}'::jsonb),
           count(*),
           count(*) FILTER (WHERE l.fallback_used),
           coalesce(sum(l.tokens_in), 0),
           coalesce(sum(l.tokens_out), 0),
           coalesce(sum(l.cost_usd), 0),
           coalesce(sum(l.cache_read_tokens), 0),
           coalesce(sum(l.cache_write_tokens), 0),
           count(*) FILTER (WHERE l.pricing_mode LIKE 'estimate%'),
           count(*) FILTER (WHERE l.status = 'refused'),
           count(*) FILTER (WHERE l.status = 'error'),
           coalesce(sum(l.latency_ms), 0),
           count(l.latency_ms)
      FROM shared.llm_call_log l
     WHERE l.created_at < $1
       AND NOT EXISTS (SELECT 1 FROM shared.llm_call_daily d
                        WHERE d.day = (l.created_at AT TIME ZONE 'UTC')::date)
     GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9
    ON CONFLICT ON CONSTRAINT llm_call_daily_key DO NOTHING
"""

V2_COLUMNS_SQL = """
    SELECT count(*) FROM information_schema.columns
     WHERE table_schema = 'shared'
       AND ((table_name = 'llm_call_log' AND column_name IN ('app', 'is_shadow', 'status', 'latency_ms'))
         OR (table_name = 'llm_call_daily'
             AND column_name IN ('refused_calls', 'error_calls', 'latency_ms_sum', 'latency_ms_count')))
"""


async def _v2_columns_ready(conn) -> bool:
    return int(await conn.fetchval(V2_COLUMNS_SQL) or 0) == 8


S6_COLUMNS_SQL = """
    SELECT count(*) FROM information_schema.columns
     WHERE table_schema = 'shared'
       AND ((table_name = 'llm_call_log' AND column_name IN ('cache_read_tokens', 'cache_write_tokens', 'pricing_mode'))
         OR (table_name = 'llm_call_daily'
             AND column_name IN ('cache_read_tokens', 'cache_write_tokens', 'estimate_calls')))
"""


async def _s6_columns_ready(conn) -> bool:
    return int(await conn.fetchval(S6_COLUMNS_SQL) or 0) == 6


LLM_DAILY_PENDING_SQL = """
    SELECT count(DISTINCT (l.created_at AT TIME ZONE 'UTC')::date)
      FROM shared.llm_call_log l
     WHERE l.created_at < $1
       AND NOT EXISTS (SELECT 1 FROM shared.llm_call_daily d
                        WHERE d.day = (l.created_at AT TIME ZONE 'UTC')::date)
"""

# $1 = cutoff; months are rolled whole, up to the end (exclusive) of the cutoff's UTC month.
_MONTH_END = "((date_trunc('month', $1::timestamptz AT TIME ZONE 'UTC') + interval '1 month') AT TIME ZONE 'UTC')"

TENANT_USAGE_MONTHLY_INSERT_SQL = f"""
    INSERT INTO shared.tenant_api_usage_monthly
        (month, tenant_id, actor_type, calls, ok_calls, rate_limited_calls, server_error_calls,
         response_ms_sum, response_ms_count)
    SELECT date_trunc('month', u.called_at AT TIME ZONE 'UTC')::date,
           u.tenant_id, u.actor_type,
           count(*),
           count(*) FILTER (WHERE u.status_code < 400),
           count(*) FILTER (WHERE u.status_code = 429),
           count(*) FILTER (WHERE u.status_code >= 500),
           coalesce(sum(u.response_ms), 0),
           count(u.response_ms)
      FROM shared.tenant_api_usage u
     WHERE u.called_at < {_MONTH_END}
       AND {_MONTH_END} <= now()
       AND NOT EXISTS (SELECT 1 FROM shared.tenant_api_usage_monthly m
                        WHERE m.month = date_trunc('month', u.called_at AT TIME ZONE 'UTC')::date)
     GROUP BY 1, 2, 3
    ON CONFLICT ON CONSTRAINT tenant_api_usage_monthly_key DO NOTHING
"""

TENANT_USAGE_MONTHLY_PENDING_SQL = f"""
    SELECT count(DISTINCT date_trunc('month', u.called_at AT TIME ZONE 'UTC'))
      FROM shared.tenant_api_usage u
     WHERE u.called_at < {_MONTH_END}
       AND NOT EXISTS (SELECT 1 FROM shared.tenant_api_usage_monthly m
                        WHERE m.month = date_trunc('month', u.called_at AT TIME ZONE 'UTC')::date)
"""

_SQL = {
    "llm_call_daily": (LLM_DAILY_INSERT_SQL, LLM_DAILY_PENDING_SQL),
    "tenant_api_usage_monthly": (TENANT_USAGE_MONTHLY_INSERT_SQL, TENANT_USAGE_MONTHLY_PENDING_SQL),
}


async def pending(conn, rollup: str, cutoff) -> int:
    """Days / months that a live run would roll up first (reported by a DRY RUN)."""
    return int(await conn.fetchval(_SQL[rollup][1], cutoff) or 0)


async def write(conn, rollup: str, cutoff) -> int:
    """Writes the rollup rows for every not-yet-rolled day / month older than the cutoff, in one
    statement (one transaction). Returns the number of rollup rows inserted."""
    async with conn.transaction():
        await conn.execute("SET LOCAL statement_timeout = '300s'")
        sql = _SQL[rollup][0]
        if rollup == "llm_call_daily" and await _v2_columns_ready(conn):
            sql = LLM_DAILY_INSERT_V2_SQL
        elif rollup == "llm_call_daily" and await _s6_columns_ready(conn):
            sql = LLM_DAILY_INSERT_S6_SQL
        status = await conn.execute(sql, cutoff)
    return int(status.split()[-1])
