"""AA-705 — Admin SEO Intelligence: one page that merges every SEO signal the pipeline has.

Replaces the thin Dashboard "SEO Intelligence" tab (3 KPIs + a short list). Five tabs, all fed by
`GET /admin/seo-intelligence`:
  1. Overview  — coverage %, demand by market, PAA count, research spend, data freshness.
  2. Keywords  — every researched keyword with real volume, per market, which tours use it.
  3. Questions — People-Also-Ask questions found per market.
  4. Gaps      — published masters with no keyword that has measured volume / stale / unresearched.
  5. Spend     — DataForSEO cost + cache-hit rate per endpoint (reuses the External Spend rollup).

Data sources, merged (the main reason the old tab looked poor):
  - acp_contract.search_demand       — researched keywords + PAA + volume per market (US/UK/AU/DE/FR/NL).
  - silver_aa_internal.seo_context   — S1 per-tour keyword ideas (`:ideas_v3`).
  - gold_aa_internal.published_tours — seo_keywords_used + master coverage (join raw_tours for country).
  - shared.dfs_call_log              — spend + cache hits (reuses admin_llm_ops SQL).
  - acp_contract.segment_research_log — research freshness (FRESH_FOR = 182 days).

NOTE: search_demand carries only `search_volume` — there is no CPC / competition data anywhere in
the pipeline, so the Keywords tab does not promise those columns.
"""
from __future__ import annotations

import json

import structlog
from fastapi import APIRouter, Header, Query, Request

from api.routers.admin import verify_admin_secret
from api.routers.admin_llm_ops import (
    _DFS_LOCATION_NAMES,
    _DFS_SUMMARY_SQL,
    _DFS_TREE_SQL,
    _resolve_window,
    _window_meta,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/admin/seo-intelligence", tags=["admin-seo"])

_AA_INTERNAL = "00000000-0000-0000-0000-000000000001"
# The research cache's own freshness horizon (services/acp_contract/segment_research.py FRESH_FOR).
_FRESH_DAYS = 182

# ── market codes (seed_builder.DFS_LOCATION_MAP — the 6 targeted buyer markets) ─────────────
_MARKETS = ["US", "UK", "AU", "DE", "FR", "NL"]

# ── Overview ────────────────────────────────────────────────────────────────────────────────
_COVERAGE_SQL = """
SELECT
    (SELECT count(*) FROM gold_aa_internal.published_tours
      WHERE tenant_id = $1::uuid AND master_status <> 'trashed')                      AS total_tours,
    (SELECT count(DISTINCT pt.tour_id)
       FROM gold_aa_internal.published_tours pt
      WHERE pt.tenant_id = $1::uuid AND pt.master_status <> 'trashed'
        AND EXISTS (SELECT 1 FROM silver_aa_internal.seo_context sc WHERE sc.tour_id = pt.tour_id)
    )                                                                                  AS seo_covered
"""

# published masters that have >=1 keyword with measured volume, via seo_context.keyword_ideas.
_WITH_VOLUME_SQL = """
SELECT count(DISTINCT pt.tour_id)
FROM gold_aa_internal.published_tours pt
WHERE pt.tenant_id = $1::uuid AND pt.master_status <> 'trashed'
  AND EXISTS (
      SELECT 1 FROM silver_aa_internal.seo_context sc
      CROSS JOIN LATERAL jsonb_array_elements(
          CASE WHEN jsonb_typeof(sc.keyword_ideas) = 'array' THEN sc.keyword_ideas ELSE '[]'::jsonb END
      ) ki
      WHERE sc.tour_id = pt.tour_id
        AND (ki->>'search_volume') ~ '^[0-9]+$' AND (ki->>'search_volume')::bigint > 0
  )
"""

# demand totals per market
_DEMAND_BY_MARKET_SQL = """
SELECT market,
       count(*)                                    AS keywords,
       count(*) FILTER (WHERE search_volume > 0)   AS keywords_with_volume,
       coalesce(sum(search_volume), 0)::bigint     AS total_volume
FROM acp_contract.search_demand
GROUP BY market
ORDER BY total_volume DESC
"""

_PAA_COUNT_SQL = """
SELECT coalesce(sum(jsonb_array_length(
    CASE WHEN jsonb_typeof(people_also_ask) = 'array' THEN people_also_ask ELSE '[]'::jsonb END)), 0) AS paa_total
FROM acp_contract.search_demand
"""

_FRESHNESS_SQL = """
SELECT
    (SELECT count(DISTINCT canonical_place) FROM acp_contract.segment_research_log)        AS places_researched,
    (SELECT count(DISTINCT canonical_place) FROM acp_contract.segment_research_log
       WHERE researched_at < now() - make_interval(days => $1))                            AS places_expired,
    (SELECT extract(epoch FROM (now() - percentile_cont(0.5) WITHIN GROUP (ORDER BY researched_at)))
            / 86400 FROM acp_contract.segment_research_log)                                AS median_age_days,
    (SELECT max(researched_at) FROM acp_contract.segment_research_log)                     AS last_researched
"""

# ── Keywords tab ──────────────────────────────────────────────────────────────────────────
# researched keywords with real volume + how many published masters use each (seo_keywords_used).
_KEYWORDS_SQL = """
WITH used AS (
    SELECT lower(kw) AS keyword, count(DISTINCT pt.tour_id) AS tours_using
    FROM gold_aa_internal.published_tours pt
    CROSS JOIN LATERAL jsonb_array_elements_text(
        CASE WHEN jsonb_typeof(pt.seo_keywords_used) = 'array' THEN pt.seo_keywords_used ELSE '[]'::jsonb END
    ) kw
    WHERE pt.tenant_id = $1::uuid AND pt.master_status <> 'trashed'
    GROUP BY lower(kw)
)
SELECT sd.keyword, sd.market, sd.search_volume,
       coalesce(u.tours_using, 0) AS tours_using,
       (u.keyword IS NOT NULL)    AS used_in_content
FROM acp_contract.search_demand sd
LEFT JOIN used u ON u.keyword = lower(sd.keyword)
WHERE sd.search_volume IS NOT NULL AND sd.search_volume > 0
ORDER BY sd.search_volume DESC
LIMIT 2000
"""

# ── Questions / PAA tab ─────────────────────────────────────────────────────────────────────
_PAA_SQL = """
SELECT keyword, market, people_also_ask
FROM acp_contract.search_demand
WHERE people_also_ask IS NOT NULL
  AND jsonb_typeof(people_also_ask) = 'array'
  AND jsonb_array_length(people_also_ask) > 0
ORDER BY market, keyword
LIMIT 1500
"""

# ── Gaps tab ─────────────────────────────────────────────────────────────────────────────────
# published masters with NO keyword that has measured volume (via seo_context ideas), by country.
_GAPS_SQL = """
SELECT pt.tour_id, rt.src_name AS name, rt.country,
       EXISTS (SELECT 1 FROM silver_aa_internal.seo_context sc WHERE sc.tour_id = pt.tour_id) AS has_research
FROM gold_aa_internal.published_tours pt
JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
WHERE pt.tenant_id = $1::uuid AND pt.master_status <> 'trashed'
  AND NOT EXISTS (
      SELECT 1 FROM silver_aa_internal.seo_context sc
      CROSS JOIN LATERAL jsonb_array_elements(
          CASE WHEN jsonb_typeof(sc.keyword_ideas) = 'array' THEN sc.keyword_ideas ELSE '[]'::jsonb END
      ) ki
      WHERE sc.tour_id = pt.tour_id
        AND (ki->>'search_volume') ~ '^[0-9]+$' AND (ki->>'search_volume')::bigint > 0
  )
ORDER BY rt.country, rt.src_name
"""


def _jsonb(v):
    """asyncpg returns jsonb as text (no codec registered) — parse to a Python object."""
    if v is None:
        return []
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except (ValueError, TypeError):
        return []


def _paa_items(raw) -> list[str]:
    out = []
    for q in _jsonb(raw):
        if isinstance(q, str):
            out.append(q)
        elif isinstance(q, dict):
            t = q.get("question") or q.get("title")
            if t:
                out.append(t)
    return out


@router.get("", summary="AA-705 — SEO Intelligence (all tabs)")
async def get_seo_intelligence(
    request: Request,
    days: int = Query(30, ge=1, le=365),
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    since, until = _resolve_window(days, None, None)

    async with pool.acquire() as conn:
        cov = await conn.fetchrow(_COVERAGE_SQL, _AA_INTERNAL)
        with_vol = await conn.fetchval(_WITH_VOLUME_SQL, _AA_INTERNAL)
        demand = await conn.fetch(_DEMAND_BY_MARKET_SQL)
        paa_total = await conn.fetchval(_PAA_COUNT_SQL)
        fresh = await conn.fetchrow(_FRESHNESS_SQL, _FRESH_DAYS)
        keywords = await conn.fetch(_KEYWORDS_SQL, _AA_INTERNAL)
        paa_rows = await conn.fetch(_PAA_SQL)
        gaps = await conn.fetch(_GAPS_SQL, _AA_INTERNAL)
        dfs_branches = await conn.fetch(_DFS_TREE_SQL, since, until)
        dfs_summary = await conn.fetchrow(_DFS_SUMMARY_SQL, since, until)

    total_tours = cov["total_tours"] or 0

    # Questions: flatten PAA, de-duplicated, with the markets each appears in.
    q_map: dict[str, set] = {}
    for r in paa_rows:
        for q in _paa_items(r["people_also_ask"]):
            q_map.setdefault(q, set()).add(r["market"])
    questions = [
        {"question": q, "markets": sorted(m)}
        for q, m in sorted(q_map.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ][:500]

    # DFS spend branches (reuse External Spend rollup) + per-call cache-hit rate.
    branches = []
    for r in dfs_branches:
        d = dict(r)
        d["last_call_at"] = d["last_call_at"].isoformat() if d["last_call_at"] else None
        tot = d["call_count"] or 0
        d["cache_hit_rate"] = (d["cache_hit_count"] / tot) if tot else None
        branches.append(d)
    s = dict(dfs_summary) if dfs_summary else {}
    stot = s.get("total_calls") or 0
    s["cache_hit_rate"] = (s.get("cache_hits", 0) / stot) if stot else None

    return {
        "overview": {
            "total_tours": total_tours,
            "seo_covered": cov["seo_covered"] or 0,
            "coverage_pct": round((cov["seo_covered"] or 0) / total_tours * 100, 1) if total_tours else 0,
            "tours_with_volume": with_vol or 0,
            "with_volume_pct": round((with_vol or 0) / total_tours * 100, 1) if total_tours else 0,
            "paa_total": int(paa_total or 0),
            "demand_by_market": [dict(r) for r in demand],
            "freshness": {
                "places_researched": fresh["places_researched"] or 0,
                "places_expired": fresh["places_expired"] or 0,
                "median_age_days": (round(float(fresh["median_age_days"]), 1)
                                    if fresh["median_age_days"] is not None else None),
                "fresh_days": _FRESH_DAYS,
                "last_researched": fresh["last_researched"].isoformat() if fresh["last_researched"] else None,
            },
        },
        "keywords": [dict(r) for r in keywords],
        "questions": questions,
        "gaps": [dict(r) | {"tour_id": str(dict(r)["tour_id"])} for r in gaps],
        "spend": {**_window_meta(days, since, until), "summary": s, "branches": branches},
    }
