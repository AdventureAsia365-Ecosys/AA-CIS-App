"""AA-712 — place pipeline admin API (behind the admin BFF, admin secret).

  POST /admin/places/backfill  {country?, limit ≤ 50, only_stale, dry_run, force}
       dry_run (default true): the tours it would take + expected calls and cost, no call made.
       real run: one place_extract job per tour (background kind, cap 1). 409 while a wave runs
       (s1_rewrite / a3_atomize / recompute queued or running) or a previous backfill batch still
       has queued jobs; 409 when the expected geocoder cost exceeds the per-backfill cap ($50).

AA-847 (content-validity gate, ADR 0007 decision 10, design §5):
  GET  /admin/places/validity?country=&check=&reason=&page=&page_size=
       the deterministic report (services/places/validity.py): counts per check and reason, catalog-wide and per
       country, and one page of findings; the last builds' counts (geo.graph_build.stats.validity) as a trend.
  GET  /admin/places/coverage
       per country: live tours, built (geo.tour_build ok / partial), in the current graph, with a stop on every
       extracted day — the gate for removing the TripPlanner places fallback.
  POST /admin/places/validity/fix {check, country?, source_id?, dry_run = true, undo?, fingerprint?}
       services/places/validity_fix.py; apply writes an audit row with the prior values, undo restores them.

Every response that shows coordinates carries the Geoapify / OpenStreetMap attribution (ADR 0007).
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from api.core.staff_authz import staff_policy
from pydantic import BaseModel, Field

from api.routers.admin import verify_admin_secret

ATTRIBUTION = {"geoapify": "Powered by Geoapify", "geoapify_url": "https://www.geoapify.com/",
               "osm": "© OpenStreetMap contributors", "osm_url": "https://www.openstreetmap.org/copyright"}
VALIDITY_CACHE_S = 60.0
_validity_cache: dict = {}

logger = structlog.get_logger()

# AA-882 (GĐ3): staff_policy narrows every route listed in api/core/staff_authz.py POLICY by role.
router = APIRouter(prefix="/admin/places", tags=["admin-places"], dependencies=[Depends(staff_policy)])


class BackfillRequest(BaseModel):
    country: Optional[str] = None
    limit: int = Field(20, ge=1, le=50)
    only_stale: bool = True
    dry_run: bool = True
    force: bool = False


@router.post("/backfill", summary="AA-712 — backfill place_extract for ≤ 50 live tours (dry run by default)")
async def backfill(body: BackfillRequest, request: Request, x_admin_secret: str = Header(None),
                   x_admin_user_id: Optional[str] = Header(None)):
    verify_admin_secret(x_admin_secret)
    from services.places import backfill as bf
    from services.places.enqueue import enqueue_place_extract
    from services.places.geocoder import get_geocoder

    pool = request.app.state.pool
    geocoder = get_geocoder()
    async with pool.acquire() as conn:
        busy = await bf.wave_busy(conn)
        tours = await bf.plan(conn, country=body.country, limit=body.limit, only_stale=body.only_stale)
        est = await bf.estimate(conn, tours, geocoder.cost_per_request, geocoder.calls_external)
    out = {"dry_run": body.dry_run, "country": body.country, "only_stale": body.only_stale,
           "geocoder": geocoder.name, "busy": busy, "estimate": est,
           "tours": [{"tour_id": t["tour_id"], "source_id": t["source_id"], "country": t.get("country"),
                      "name": t.get("aa_name"), "built": t.get("build") is not None} for t in tours]}
    if body.dry_run:
        out["would_refuse"] = bool(busy) or est["geocoder_cost_usd"] > bf.GEOCODER_BACKFILL_CAP_USD
        return out
    if busy:
        raise HTTPException(status_code=409, detail={"error": "a wave or a previous backfill batch is running",
                                                     "busy": busy})
    if est["geocoder_cost_usd"] > bf.GEOCODER_BACKFILL_CAP_USD:
        raise HTTPException(status_code=409, detail={"error": "expected geocoder cost above the backfill cap",
                                                     "cap_usd": bf.GEOCODER_BACKFILL_CAP_USD, "estimate": est})
    if not tours:
        return {**out, "enqueued": 0, "jobs": []}
    batch = uuid.uuid4().hex[:8]
    created_by = f"backfill:admin:{x_admin_user_id or 'unknown'}"
    jobs = []
    for t in tours:
        job_id, created = await enqueue_place_extract(
            pool, tour_id=t["tour_id"], version_id=t.get("version_id"), reason=f"backfill:{batch}",
            created_by=created_by, idempotency_key=f"place_extract:backfill:{batch}:{t['tour_id']}",
            force=body.force)
        jobs.append({"tour_id": t["tour_id"], "job_id": job_id, "created": created})
    logger.info("places_backfill_enqueued", batch=batch, tours=len(jobs), country=body.country,
                expected_cost_usd=est["expected_cost_usd"])
    return {**out, "batch": batch, "enqueued": sum(1 for j in jobs if j["created"]), "jobs": jobs}


# ── AA-847: content-validity report, coverage, fixes ────────────────────────────────────────────────────────────────

TREND_SQL = """
    SELECT build_id::text AS build_id, built_at, stats -> 'validity' AS validity, is_current
      FROM geo.graph_build ORDER BY built_at DESC LIMIT 5
"""

COVERAGE_SQL = """
    WITH live AS (
        SELECT DISTINCT COALESCE(rt.source_group_id, rt.tour_id)::text AS source_id, rt.country
          FROM gold_aa_internal.published_tours pt
          JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
         WHERE pt.tenant_id = $1::uuid
           AND pt.master_status = 'active' AND pt.deleted_at IS NULL
           AND rt.source_status = 'active' AND rt.deleted_at IS NULL
    ), days AS (
        SELECT source_id, count(*) AS n FROM geo.tour_day GROUP BY source_id
    ), stops AS (
        SELECT s.source_id, count(DISTINCT s.day_index) AS n
          FROM geo.tour_stop s JOIN geo.graph_build b ON b.build_id = s.build_id AND b.is_current
         GROUP BY s.source_id
    )
    SELECT coalesce(l.country, '-') AS country,
           count(*) AS live,
           count(*) FILTER (WHERE tb.status IN ('ok', 'partial')) AS built,
           count(*) FILTER (WHERE tb.status = 'partial') AS partial,
           count(*) FILTER (WHERE st.n > 0) AS in_graph,
           count(*) FILTER (WHERE st.n > 0 AND st.n >= d.n) AS stop_every_day,
           coalesce(round(sum(tb.cost_usd), 4), 0) AS build_cost_usd
      FROM live l
      LEFT JOIN geo.tour_build tb ON tb.source_id = l.source_id
      LEFT JOIN days d ON d.source_id = l.source_id
      LEFT JOIN stops st ON st.source_id = l.source_id
     GROUP BY 1 ORDER BY 1
"""


async def _validity(pool, country: Optional[str]):
    import time

    from services.places import validity
    hit = _validity_cache.get(country)
    if hit and time.monotonic() - hit[0] < VALIDITY_CACHE_S:
        return hit[1], hit[2]
    async with pool.acquire() as conn:
        summary, items = await validity.report(conn, country)
    _validity_cache[country] = (time.monotonic(), summary, items)
    return summary, items


@router.get("/validity", summary="AA-847 — content-validity report of the live catalog's places (no model call)")
async def validity_report(request: Request, country: Optional[str] = None, check: Optional[str] = None,
                          reason: Optional[str] = None, page: int = 1, page_size: int = 50,
                          x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    from services.places import validity
    if check is not None and check not in validity.CHECKS:
        raise HTTPException(status_code=422, detail=f"check must be one of {validity.CHECKS}")
    if reason is not None and not any(reason in r for r in validity.REASONS.values()):
        raise HTTPException(status_code=422, detail="unknown reason")
    page_size = max(1, min(page_size, 200))
    summary, items = await _validity(request.app.state.pool, country or None)
    async with request.app.state.pool.acquire() as conn:
        trend = [{"build_id": r["build_id"], "built_at": r["built_at"].isoformat() if r["built_at"] else None,
                  "is_current": r["is_current"],
                  "validity": (json.loads(r["validity"]) if isinstance(r["validity"], str) else r["validity"])}
                 for r in await conn.fetch(TREND_SQL)]
    return {"country": country, "checks": list(validity.CHECKS), "reasons": validity.REASONS,
            "summary": summary, "trend": trend, "attribution": ATTRIBUTION,
            **validity.page(items, check=check, reason=reason, page_no=page, page_size=page_size)}


@router.get("/coverage", summary="AA-847 — place pipeline coverage per country (live / built / graph / stop per day)")
async def coverage(request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    from services.places.validity import MASTER_TENANT_ID
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", MASTER_TENANT_ID)
            rows = [dict(r) for r in await conn.fetch(COVERAGE_SQL, MASTER_TENANT_ID)]
    for r in rows:
        r["build_cost_usd"] = float(r["build_cost_usd"] or 0)
    totals = {k: sum(r[k] for r in rows) for k in ("live", "built", "partial", "in_graph", "stop_every_day")}
    totals["build_cost_usd"] = round(sum(r["build_cost_usd"] for r in rows), 4)
    return {"countries": rows, "totals": totals}


class ValidityFixRequest(BaseModel):
    check: Optional[str] = None
    country: Optional[str] = None
    source_id: Optional[str] = None
    dry_run: bool = True
    undo: Optional[str] = None
    fingerprint: Optional[str] = None     # the dry run's; apply refuses (409) when the plan changed since


@router.post("/validity/fix", summary="AA-847 — fix content-validity findings (dry run by default; audit + undo)")
async def validity_fix(body: ValidityFixRequest, request: Request, x_admin_secret: str = Header(None),
                       x_admin_user_id: Optional[str] = Header(None)):
    verify_admin_secret(x_admin_secret)
    from services.places import validity_fix as vf
    actor = f"admin:{x_admin_user_id or 'unknown'}"
    pool = request.app.state.pool
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                if body.undo:
                    try:
                        uuid.UUID(body.undo)
                    except ValueError:
                        raise HTTPException(status_code=422, detail="undo must be an audit row id (uuid)")
                    out = await vf.undo(conn, body.undo, actor=actor, dry_run=body.dry_run)
                else:
                    if body.check not in vf.CHECKS:
                        raise HTTPException(status_code=422, detail=f"check must be one of {vf.CHECKS}")
                    fix = await vf.plan(conn, body.check, country=body.country or None,
                                        source_id=body.source_id or None)
                    if not body.dry_run:
                        vf.check_fingerprint(fix, body.fingerprint)
                    out = {**fix, "dry_run": True} if body.dry_run else {**await vf.apply(conn, fix, actor=actor),
                                                                         "dry_run": False}
    except vf.FixError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    if not body.dry_run:
        _validity_cache.clear()
        logger.info("places_validity_fix", check=out.get("check"), applied=out.get("applied", out.get("restored")),
                    undo=body.undo, actor=actor)
    return out
