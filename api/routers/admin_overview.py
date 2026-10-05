"""AA-664 — Admin Overview dashboard: one whole-system snapshot in a single endpoint.

`GET /admin/overview` aggregates six sections — pipeline funnel (per country, 7-day deltas),
intelligence (atoms/segments/routes/hubs + research coverage), tenants, jobs, cost vs budget,
and alerts — cached in Redis for 60s. Every widget on the page links to its own detail page;
this endpoint only supplies the numbers.

Reuses existing building blocks rather than re-deriving them:
  - intelligence counts: the same SQL as admin_dashboard.dashboard_summary (platform-wide).
  - jobs: the admin_job_runner.summary SQL over shared.job + queue.worker_health.
  - cost: shared.cost_guard.day_spend + shared.spend_budget (same as admin_budgets.list_budgets),
    the month-to-date llm_call_log roll-up, and the stored DFS balance snapshot.
  - alerts: unread admin-targeted rows in shared.notifications.
Only the per-country funnel (section 1) needs new SQL, because no existing endpoint produces it.
"""
from __future__ import annotations

import json

import structlog
from fastapi import APIRouter, Header, Request

from api.routers.admin import verify_admin_secret
from shared.cost_guard import PROVIDERS, day_spend
from shared.jobs import queue

logger = structlog.get_logger()

router = APIRouter(prefix="/admin/overview", tags=["admin-overview"])

_CACHE_KEY = "admin:overview:v1"
_CACHE_TTL_S = 60

# Master Content is owned by the aa_internal sentinel tenant.
_AA_INTERNAL = "00000000-0000-0000-0000-000000000001"

# ── Section 1: per-country pipeline funnel ──────────────────────────────────────────────────
# raw (active) → S1 generated → in review (pending) → published (master) → atomized, by country,
# each with the count added in the last 7 days (delta). published_tours has no country, so it is
# joined back to raw_tours; atoms are read through v_active_tour_atoms (migration 203).
_FUNNEL_SQL = """
WITH raw_c AS (
    SELECT country,
           count(*) AS raw_active,
           count(*) FILTER (WHERE ingest_at >= now() - interval '7 days') AS raw_7d
    FROM silver_aa_internal.raw_tours
    WHERE source_status = 'active' AND deleted_at IS NULL
    GROUP BY country
),
gen_c AS (
    SELECT rt.country,
           count(DISTINCT gc.tour_id) AS generated,
           count(DISTINCT gc.tour_id) FILTER (WHERE gc.created_at >= now() - interval '7 days') AS generated_7d
    FROM silver_aa_internal.generated_content gc
    JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = gc.tour_id
    GROUP BY rt.country
),
rev_c AS (
    SELECT rt.country, count(DISTINCT rq.tour_id) AS in_review
    FROM silver_aa_internal.review_queue rq
    JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = rq.tour_id
    WHERE rq.review_status = 'pending'
    GROUP BY rt.country
),
pub_c AS (
    SELECT rt.country,
           count(DISTINCT pt.tour_id) AS published,
           count(DISTINCT pt.tour_id) FILTER (WHERE pt.published_at >= now() - interval '7 days') AS published_7d
    FROM gold_aa_internal.published_tours pt
    JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
    WHERE pt.tenant_id = $1::uuid AND pt.master_status <> 'trashed'
    GROUP BY rt.country
),
atom_c AS (
    SELECT rt.country, count(DISTINCT ta.tour_id) AS atomized
    FROM acp_contract.v_active_tour_atoms ta
    JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = ta.tour_id
    GROUP BY rt.country
)
SELECT
    coalesce(raw_c.country, 'Unknown')                 AS country,
    coalesce(raw_c.raw_active, 0)                      AS raw_active,
    coalesce(raw_c.raw_7d, 0)                           AS raw_7d,
    coalesce(gen_c.generated, 0)                        AS generated,
    coalesce(gen_c.generated_7d, 0)                     AS generated_7d,
    coalesce(rev_c.in_review, 0)                        AS in_review,
    coalesce(pub_c.published, 0)                        AS published,
    coalesce(pub_c.published_7d, 0)                     AS published_7d,
    coalesce(atom_c.atomized, 0)                        AS atomized
FROM raw_c
LEFT JOIN gen_c  ON gen_c.country  = raw_c.country
LEFT JOIN rev_c  ON rev_c.country  = raw_c.country
LEFT JOIN pub_c  ON pub_c.country  = raw_c.country
LEFT JOIN atom_c ON atom_c.country = raw_c.country
ORDER BY raw_c.raw_active DESC, raw_c.country
"""

# ── Section 2: intelligence totals (platform-wide — same SQL as dashboard_summary) ──────────
_INTEL_SQL = """
SELECT
    (SELECT count(*) FROM acp_contract.tour_atoms
      WHERE NOT deleted AND NOT is_empty_marker)                          AS atom_count,
    (SELECT count(DISTINCT asm.segment_id)
       FROM acp_contract.atom_segment_member asm
       JOIN acp_contract.tour_atoms ta ON ta.atom_id = asm.atom_id
      WHERE NOT ta.deleted AND NOT ta.is_empty_marker)                    AS segment_count,
    (SELECT count(*) FROM acp_contract.route WHERE superseded_at IS NULL) AS route_count,
    (SELECT count(DISTINCT h.hub_id)
       FROM acp_contract.hub h
       JOIN acp_contract.route r ON r.hub_id = h.hub_id
      WHERE r.superseded_at IS NULL)                                      AS hub_count,
    (SELECT count(*) FROM acp_contract.atom_ranking)                      AS score_count
"""

# Research coverage: distinct places researched vs how many are stale (>30 days old).
_RESEARCH_SQL = """
SELECT
    (SELECT count(DISTINCT canonical_place) FROM acp_contract.segment_research_log)  AS places_researched,
    (SELECT count(DISTINCT canonical_place) FROM acp_contract.segment_research_log
      WHERE researched_at < now() - interval '30 days')                             AS places_stale,
    (SELECT count(*) FROM acp_contract.search_demand)                               AS demand_rows,
    (SELECT max(researched_at) FROM acp_contract.segment_research_log)              AS last_researched
"""

# ── Section 3: tenants ──────────────────────────────────────────────────────────────────────
_TENANTS_SQL = """
SELECT
    (SELECT count(*) FROM shared.tenants WHERE is_active)                                     AS active_tenants,
    (SELECT count(*) FROM gold_aa_internal.tenant_tour_versions
      WHERE created_at >= now() - interval '7 days')                                          AS rewrites_7d,
    (SELECT count(*) FROM acp_shared.content_piece
      WHERE created_at >= now() - interval '7 days')                                          AS pieces_7d,
    (SELECT count(*) FROM acp_shared.publish_log
      WHERE published_at >= now() - interval '7 days' AND status = 'published')               AS published_pieces_7d
"""

# Quota outliers: tenants over their alert threshold this billing month (view already computes %).
_QUOTA_SQL = """
SELECT tenant_name, plan_tier,
       round(coalesce(quota_tours_pct, 0)::numeric, 1)  AS quota_tours_pct,
       round(coalesce(quota_calls_pct, 0)::numeric, 1)  AS quota_calls_pct
FROM shared.v_tenant_monthly_usage
WHERE coalesce(quota_tours_pct, 0) >= 80 OR coalesce(quota_calls_pct, 0) >= 80
ORDER BY greatest(coalesce(quota_tours_pct, 0), coalesce(quota_calls_pct, 0)) DESC
LIMIT 10
"""

# ── Section 5: month-to-date cost per provider/account (llm_call_log + dfs_call_log) ────────
_MTD_LLM_SQL = """
SELECT coalesce(account, 'unknown') AS account,
       coalesce(provider, 'unknown') AS provider,
       coalesce(sum(cost_usd), 0)::float AS cost_usd
FROM shared.llm_call_log
WHERE created_at >= date_trunc('month', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc'
GROUP BY account, provider
ORDER BY cost_usd DESC
"""
_MTD_DFS_SQL = """
SELECT coalesce(sum(cost_usd), 0)::float AS cost_usd
FROM shared.dfs_call_log
WHERE created_at >= date_trunc('month', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc'
"""

# ── Section 6: recent unread admin alerts ───────────────────────────────────────────────────
_ALERTS_SQL = """
SELECT event_type, entity_type, entity_id, payload, created_at
FROM shared.notifications
WHERE target_roles && ARRAY['admin'] AND is_read = FALSE
ORDER BY created_at DESC
LIMIT 20
"""


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


async def _build_overview(pool) -> dict:
    async with pool.acquire() as conn:
        funnel_rows = await conn.fetch(_FUNNEL_SQL, _AA_INTERNAL)
        intel = await conn.fetchrow(_INTEL_SQL)
        research = await conn.fetchrow(_RESEARCH_SQL)
        tenants = await conn.fetchrow(_TENANTS_SQL)
        quota_rows = await conn.fetch(_QUOTA_SQL)
        budget_rows = await conn.fetch(
            "SELECT provider, scope, per_run_usd, per_day_usd, hard_stop, alert_pct "
            "FROM shared.spend_budget ORDER BY provider, scope")
        spent_today = {p: round(await day_spend(conn, p), 4) for p in PROVIDERS}
        mtd_llm = await conn.fetch(_MTD_LLM_SQL)
        mtd_dfs = await conn.fetchval(_MTD_DFS_SQL)
        alert_rows = await conn.fetch(_ALERTS_SQL)

    # jobs: counts per status (last 30d) + live worker/queue health
    job_rows = await pool.fetch(
        "SELECT status, count(*)::int AS n FROM shared.job "
        "WHERE created_at >= now() - interval '30 days' GROUP BY status")
    job_counts: dict[str, int] = {r["status"]: r["n"] for r in job_rows}
    try:
        health = await queue.worker_health(pool)
        workers_alive = len(health.get("workers", []))
        queue_depth = len(health.get("queued", []))
        running_now = len(health.get("running", []))
    except Exception as e:  # worker health must never sink the whole overview
        logger.warning("overview_worker_health_failed", error=str(e)[:200])
        workers_alive = queue_depth = running_now = None

    # DFS balance (stored snapshot, never a live DFS call)
    dfs_balance = None
    try:
        from shared.dfs_client.balance import read_latest_balance
        dfs_balance = await read_latest_balance(pool)
    except Exception as e:
        logger.warning("overview_dfs_balance_failed", error=str(e)[:200])

    # month-to-date per provider (collapse llm accounts into provider totals + keep the split)
    mtd_by_provider: dict[str, float] = {}
    mtd_breakdown = []
    for r in mtd_llm:
        mtd_breakdown.append({"account": r["account"], "provider": r["provider"],
                              "cost_usd": round(r["cost_usd"], 4)})
        mtd_by_provider[r["provider"]] = round(
            mtd_by_provider.get(r["provider"], 0.0) + r["cost_usd"], 4)
    mtd_by_provider["dfs"] = round(float(mtd_dfs or 0.0), 4)

    budgets = []
    for r in budget_rows:
        d = dict(r)
        for k in ("per_run_usd", "per_day_usd"):
            d[k] = float(d[k]) if d[k] is not None else None
        budgets.append(d)

    return {
        "pipeline_funnel": [dict(r) for r in funnel_rows],
        "intelligence": {**dict(intel),
                         "research": {k: _iso(v) for k, v in dict(research).items()}},
        "tenants": {**dict(tenants),
                    "quota_outliers": [dict(r) for r in quota_rows]},
        "jobs": {"counts": job_counts, "workers_alive": workers_alive,
                 "queue_depth": queue_depth, "running_now": running_now},
        "cost": {"spent_today_usd": spent_today,
                 "month_to_date_usd": mtd_by_provider,
                 "month_to_date_breakdown": mtd_breakdown,
                 "budgets": budgets,
                 "dfs_balance": dfs_balance},
        "alerts": [{"event_type": r["event_type"], "entity_type": r["entity_type"],
                    "entity_id": r["entity_id"],
                    "payload": r["payload"], "created_at": _iso(r["created_at"])}
                   for r in alert_rows],
    }


@router.get("", summary="AA-664 — whole-system overview (cached 60s)")
async def get_overview(request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    redis = getattr(request.app.state, "redis", None)
    if redis is not None:
        try:
            cached = await redis.get(_CACHE_KEY)
            if cached:
                data = json.loads(cached)
                data["_cached"] = True
                return data
        except Exception as e:
            logger.warning("overview_cache_read_failed", error=str(e)[:200])

    data = await _build_overview(request.app.state.pool)
    data["_cached"] = False
    if redis is not None:
        try:
            await redis.set(_CACHE_KEY, json.dumps(data, default=str), ex=_CACHE_TTL_S)
        except Exception as e:
            logger.warning("overview_cache_write_failed", error=str(e)[:200])
    return data
