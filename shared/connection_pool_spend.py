"""AA-1143 (tech lead decision G3, CIS 455 / 456) — the month spend the CIS remainder of an LLM connection pays for.

A connection's budget is its total and one fixed-USD share per app; CIS takes the remainder, which the budget rows'
trigger writes to ``shared.llm_provider_connection.monthly_budget_usd`` (what ``cost_guard.connection_budgets``
enforces). An app of the CIS ledger with its own share (TripPlanner, once it has a row) pays its spend from that
share, so only what it spent ABOVE its share counts here; every app without a share of its own (CIS, rows without
app) counts whole: the rule of ``catalog.v_llm_connection_cap_v2.cis_pool_spent_usd``. Before CIS 455 (no view) the
caller's whole-ledger query runs, as before.
"""
from __future__ import annotations

POOL_SPEND_SQL = """
    WITH spent AS (
        SELECT account, coalesce(app, 'cis') AS app, coalesce(sum(cost_usd), 0) AS usd, count(*)::int AS calls
          FROM shared.llm_call_log
         WHERE account = ANY($1::text[])
           AND created_at >= date_trunc('month', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc'
         GROUP BY account, coalesce(app, 'cis')
    )
    SELECT s.account,
           sum(CASE WHEN b.app IS NULL THEN s.usd ELSE greatest(s.usd - b.budget_usd, 0) END) AS spent,
           sum(s.calls)::int AS calls
      FROM spent s
      LEFT JOIN shared.llm_connection_budget_current b ON b.connection_key = s.account AND b.app = s.app
     GROUP BY s.account
"""


async def rows(conn, connection_keys: list[str], legacy_sql: str) -> list:
    """Rows (account, spent, calls) of this UTC month per connection; ``legacy_sql`` (the whole ledger) before CIS
    455. Any other read error is raised as is."""
    try:
        return await conn.fetch(POOL_SPEND_SQL, connection_keys)
    except Exception as exc:  # noqa: BLE001 — UndefinedTableError before CIS 455: the whole ledger, as before
        if type(exc).__name__ != "UndefinedTableError":
            raise
        return await conn.fetch(legacy_sql, connection_keys)
