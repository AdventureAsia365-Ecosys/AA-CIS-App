"""AA-1127 (C1, S283 UI audit) — Tenant 360's Tours and Pipeline lists (GET /admin/tenants/{id}/details).

The Tours tab read "Tours (25)" (the page length) while the header said 892, with no pager; "Pipeline (20)" was the
query's LIMIT, not a count. The tenant branch now pages its versions on the server (page / page_size, like the internal
catalog), lists every version the total counts (LEFT JOIN), and the summary carries the runs window the UI labels
"latest N".
"""
from __future__ import annotations

# How many of the newest pipeline runs Tenant 360 lists (a display window, not a count).
PIPELINE_RUNS_SHOWN = 20

# $1 tenant, $2 page size, $3 offset; newest first, the same rows `SELECT COUNT(*) ... WHERE tenant_id = $1` counts.
TENANT_VERSIONS_PAGE_SQL = """
    SELECT ttv.id, NULL::uuid AS tour_id, pt.aa_name, rt.country,
           ttv.quality_score, ttv.version_number, ttv.status,
           'active'::text AS master_status, ttv.created_at
    FROM gold_aa_internal.tenant_tour_versions ttv
    LEFT JOIN gold_aa_internal.published_tours pt ON pt.id = ttv.published_tour_id
    LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
    WHERE ttv.tenant_id = $1
    ORDER BY ttv.created_at DESC, ttv.id
    LIMIT $2 OFFSET $3
"""


# AA-702 / AA-718: a tenant's real LLM spend from shared.llm_call_log (same source as External Spend), all time and in
# the rolling 30-day window [$3, $4). Internal work is logged both as NULL and as the aa_internal UUID, so the internal
# tenant ($2 true) also counts the rows without a tenant. AA-838 (S291, C4.3 W48 finding 3): two disjoint branches
# (tenant_id = $1 / tenant_id IS NULL, the second skipped unless $2) instead of `tenant_id = $1 OR ($2 AND tenant_id IS
# NULL)`, whose OR kept the (tenant_id, created_at) index out and scanned the ledger (~13,000 blocks a call). Same
# rows, same sum (a NULL-tenant row never equals $1).
TENANT_COST_TOTAL_SQL = """
    SELECT COALESCE(SUM(c.usd), 0) FROM (
        SELECT SUM(cost_usd) AS usd FROM shared.llm_call_log WHERE tenant_id = $1
        UNION ALL
        SELECT SUM(cost_usd) FROM shared.llm_call_log WHERE $2 AND tenant_id IS NULL
    ) c
"""
TENANT_COST_WINDOW_SQL = """
    SELECT COALESCE(SUM(c.usd), 0) FROM (
        SELECT SUM(cost_usd) AS usd FROM shared.llm_call_log
         WHERE tenant_id = $1 AND created_at >= $3 AND created_at < $4
        UNION ALL
        SELECT SUM(cost_usd) FROM shared.llm_call_log
         WHERE $2 AND tenant_id IS NULL AND created_at >= $3 AND created_at < $4
    ) c
"""
