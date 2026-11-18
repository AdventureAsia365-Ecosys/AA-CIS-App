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
