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
        status = await conn.execute(_SQL[rollup][0], cutoff)
    return int(status.split()[-1])
