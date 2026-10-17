"""AA-712 — place pipeline admin API (behind the admin BFF, admin secret).

  POST /admin/places/backfill  {country?, limit ≤ 50, only_stale, dry_run, force}
       dry_run (default true): the tours it would take + expected calls and cost, no call made.
       real run: one place_extract job per tour (background kind, cap 1). 409 while a wave runs
       (s1_rewrite / a3_atomize / recompute queued or running) or a previous backfill batch still
       has queued jobs; 409 when the expected geocoder cost exceeds the per-backfill cap ($50).

The /admin/places page (coverage, pending / suspect names, fix action) is a later slice (design §7.5).
"""
from __future__ import annotations

import uuid
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from api.core.staff_authz import staff_policy
from pydantic import BaseModel, Field

from api.routers.admin import verify_admin_secret

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
