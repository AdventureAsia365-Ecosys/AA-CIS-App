"""
api/routers/v1_route_hub.py — AA-510: Route/Hub read endpoints + route_pick.

`/v1/*` tenant-JWT-only router, same convention as `v1_tours.py`/`v1_planning.py` (reuses
`get_tenant` unchanged, no staff/admin path — ADR-2026-038 §0.2, tenant self-service).

Route/Hub themselves have no write endpoint here — they are entirely derived
(`services/acp_contract/route_detection.py::run_route_detection()`, fired in the background
right after T5 ranking, `v1_tours.py::_run_ranking_pipeline()`). This router only exposes what
was last derived, plus the one real write in this layer: a tenant PICKING a Route, which
snapshots it into a route_pick (ADR 0024 — no live FK, ever, back into `route.route_id`).

Was `/v1/subjects` + `acp_contract.subject` at AA-510; renamed to `/v1/route-picks` +
`acp_contract.route_pick` at AA-511 STEP0 (migration 132) — `acp_shared.subject` is a different,
unrelated concept (the AA-511 Slate proposal) and the two names collided. The old
`/v1/subjects` path had no real frontend consumer yet, so this is a deliberate breaking rename,
not a versioned/compatibility change (see docs/implementation-notes/AA-510.md).

GET  /v1/routes             — this tenant's current Routes, ordered by score (best first)
GET  /v1/hubs                — this tenant's Hubs (persist across rebuilds, never deleted)
POST /v1/route-picks         — snapshot one Route into a route_pick (`{"route_id": "..."}` body)
GET  /v1/route-picks         — this tenant's route_picks, newest first
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from api.routers.v1_tours import get_tenant

router = APIRouter(prefix="/v1", tags=["Route/Hub"])


def get_pool(request: Request):
    return request.app.state.pool


async def _tenant_tour_ids(tenant_id: str, conn) -> list:
    """AA-545 — Route/Hub are platform-wide now (no `tenant_id` column); scope this router's
    reads to tours the tenant has actually picked, same join `services/acp_shared/slate.py`'s
    own `_tenant_tour_ids()` uses."""
    # INTENTIONAL: the JOIN into published_tours below reads a shared reference pool (100%
    # sentinel tenant_id=aa_internal), not per-tenant data — the real per-tenant boundary is
    # already drawn by tenant_tour_versions.tenant_id above it. KHÔNG BAO GIỜ chuyển sang RLS
    # pool thật cho query này — xem AA-578.
    rows = await conn.fetch("""
        SELECT pt.tour_id
        FROM gold_aa_internal.tenant_tour_versions ttv
        JOIN gold_aa_internal.published_tours pt ON pt.id = ttv.published_tour_id
        WHERE ttv.tenant_id = $1::uuid
    """, tenant_id)
    return [r["tour_id"] for r in rows]


def _route_row_to_dict(row) -> dict:
    segment_ids = row["ordered_segment_ids"]
    if isinstance(segment_ids, str):
        segment_ids = json.loads(segment_ids)
    return {
        "route_id": row["route_id"],
        "tour_id": str(row["tour_id"]),
        "hub_id": str(row["hub_id"]) if row["hub_id"] else None,
        "hub_name": row["hub_name"],
        "ordered_segment_ids": segment_ids,
        "first_day": row["first_day"],
        "last_day": row["last_day"],
        "score": float(row["score"]) if row["score"] is not None else None,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


@router.get("/routes")
async def list_routes(request: Request, tenant=Depends(get_tenant)):
    """AA-532: `superseded_at IS NULL` — a tenant only ever picks the CURRENT version of a Route
    identity (tour_id, first_day, last_day); an older version stays in the table (never deleted,
    only superseded) so any Subject already pointing at it keeps resolving, but it must not be
    offered here as if it were still pick-able.

    AA-545 — `route` no longer stores `score`/`tenant_id`; this endpoint applies the same
    read-time `AVG(total_rank)`-by-market computation `services/acp_shared/slate.py::
    _fetch_route_candidates()` already does (0 real frontend traffic on this router, confirmed
    before touching it — fixed anyway so the next real call doesn't 500 on a dropped column)."""
    tenant_id = tenant["sub"]
    pool = get_pool(request)
    async with pool.acquire() as conn:
        tour_ids = await _tenant_tour_ids(tenant_id, conn)
        if not tour_ids:
            return {"routes": []}
        from services.acp_shared.slate import _tenant_market_codes
        markets = await _tenant_market_codes(tenant_id, conn)
        rows = await conn.fetch("""
            WITH per_market AS (
                SELECT r.route_id, r.tour_id, r.hub_id, r.hub_name, r.ordered_segment_ids,
                       r.first_day, r.last_day, r.created_at, ar.market,
                       AVG(ar.total_rank) AS avg_rank
                FROM acp_contract.route r
                JOIN acp_contract.atom_ranking ar
                    ON ar.tour_id = r.tour_id
                   AND ar.segment_id = ANY (
                           SELECT jsonb_array_elements_text(r.ordered_segment_ids)
                       )
                   AND ar.excluded_reason IS NULL
                   AND ar.market = ANY($2::text[])
                   AND ar.superseded_at IS NULL  -- AA-734: current ranking row only
                WHERE r.superseded_at IS NULL AND r.tour_id = ANY($1::uuid[])
                GROUP BY r.route_id, r.tour_id, r.hub_id, r.hub_name, r.ordered_segment_ids,
                         r.first_day, r.last_day, r.created_at, ar.market
            )
            SELECT DISTINCT ON (route_id)
                   route_id, tour_id, hub_id, hub_name, ordered_segment_ids,
                   first_day, last_day, created_at, avg_rank AS score
            FROM per_market
            ORDER BY route_id, avg_rank ASC
        """, tour_ids, markets)
    routes = sorted(
        (_route_row_to_dict(r) for r in rows),
        key=lambda r: (r["score"] if r["score"] is not None else float("inf"), r["route_id"]),
    )
    return {"routes": routes}


@router.get("/hubs")
async def list_hubs(request: Request, tenant=Depends(get_tenant)):
    tenant_id = tenant["sub"]
    pool = get_pool(request)
    async with pool.acquire() as conn:
        tour_ids = await _tenant_tour_ids(tenant_id, conn)
        if not tour_ids:
            return {"hubs": []}
        rows = await conn.fetch("""
            SELECT h.hub_id, h.hub_name, h.created_at, h.updated_at,
                   COUNT(r.route_id) AS route_count,
                   array_agg(DISTINCT r.tour_id::text) FILTER (WHERE r.route_id IS NOT NULL)
                       AS tour_ids
            FROM acp_contract.hub h
            -- AA-532: only a Route's CURRENT version counts toward route_count/tour_ids — a
            -- superseded row staying in the table (never deleted) must not double-count or keep
            -- a Hub looking like it still covers a tour whose Route moved on. AA-545: scoped to
            -- this tenant's OWN picked tours (hub itself is platform-wide, no tenant_id left).
            LEFT JOIN acp_contract.route r
                ON r.hub_id = h.hub_id AND r.superseded_at IS NULL
               AND r.tour_id = ANY($1::uuid[])
            GROUP BY h.hub_id, h.hub_name, h.created_at, h.updated_at
            HAVING COUNT(r.route_id) > 0
            ORDER BY h.updated_at DESC
        """, tour_ids)
    return {"hubs": [
        {
            "hub_id": str(r["hub_id"]),
            "hub_name": r["hub_name"],
            "created_at": r["created_at"].isoformat(),
            "updated_at": r["updated_at"].isoformat(),
            "route_count": r["route_count"],
            "tour_ids": r["tour_ids"] or [],
        }
        for r in rows
    ]}


class CreateRoutePickRequest(BaseModel):
    route_id: str


@router.post("/route-picks")
async def pick_route(
    body: CreateRoutePickRequest, request: Request, tenant=Depends(get_tenant),
):
    tenant_id = tenant["sub"]
    pool = get_pool(request)
    from services.acp_contract.route_detection import create_route_pick

    result = await create_route_pick(
        tenant_id, body.route_id, pool, selected_by=f"tenant:{tenant_id}",
    )
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="Route no longer available — it may have been rebuilt. Refresh and pick again.",
        )
    return result


@router.get("/route-picks")
async def list_route_picks(request: Request, tenant=Depends(get_tenant)):
    tenant_id = tenant["sub"]
    pool = get_pool(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT route_pick_id, hub_name, route_snapshot, selected_at, selected_by
            FROM acp_contract.route_pick
            WHERE tenant_id = $1::uuid
            ORDER BY selected_at DESC
        """, tenant_id)
    route_picks = []
    for r in rows:
        snapshot = r["route_snapshot"]
        if isinstance(snapshot, str):
            snapshot = json.loads(snapshot)
        route_picks.append({
            "route_pick_id": str(r["route_pick_id"]),
            "hub_name": r["hub_name"],
            "route_snapshot": snapshot,
            "selected_at": r["selected_at"].isoformat(),
            "selected_by": r["selected_by"],
        })
    return {"route_picks": route_picks}


__all__ = ["router"]
