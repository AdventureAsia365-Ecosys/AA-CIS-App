"""
api/routers/v1_planning.py — AA-448: T7 Content Planning (tenant self-service quarter plan).

Per ADR-2026-038 §0.2 (tenant self-service — AA does not gate tenant content at any T0-T11
step): this is a `/v1/*` tenant-JWT-only router, same convention as `v1_tours.py`/
`v1_marketplace.py`/`v1_competitors.py` (reuses `get_tenant` unchanged, no staff/admin path).

AA-578 (2026-09-11) — `POST /quarter-plan/preview`, `GET`+`POST /quarter-plan`, `GET /slot-grid`,
`GET /slot-suggestions` (and their backing `_compute_plan()`/`get_slot_grid()`/
`get_slot_suggestions()`) REMOVED here. Confirmed dead: their only frontend caller (the old
Quarter Plan Preview/Finalize/History UI + `SlotPickerPanel.tsx`) was torn out at AA-519/AA-522,
and CloudWatch showed 0 real traffic on any of these routes since — see AA-578's Linear comments
for the full grep+CloudWatch evidence. This REVERSES AA-511's own "giữ code cũ, chỉ ngưng dùng,
không xoá" call for this specific neighborhood — a deliberate, confirmed-dead exception, not a
blanket policy change; `services/acp_planning/quarter.py`'s underlying functions
(`compute_quarter_plan()`, `save_quarter_plan_version()`, `approve_quarter_plan_version()`,
`fetch_approved_quarter_plan()`) are NOT deleted — still live, used by
`services/acp_planning/trip_reallocation.py`'s `suggest_trip_reallocation()`/
`confirm_trip_reallocation()` (the real, live quarterly-reallocation panel below), which reuses
the SAME Gate-B-Option-A finalize path the deleted `POST /quarter-plan` used to expose directly.

markets/channels/capacity_posts_per_week are NEVER client-supplied for anything in this file
(unlike the retired admin `CreateQuarterPlanRequest`) — read fresh from
`fetch_tenant_planning_config()` (services/acp_planning/tenant_config.py), the tenant's own
configured values.

AA-603 (21/09/2026) — the manual metric-entry + atom-weight rollup feedback loop
(`POST /metrics`, `POST /metrics/rollup`, backed by the deleted `services/acp_shared/
content_metrics.py`) was REMOVED: it read the dead N7/N8 `acp_deliver.pieces`/`acp_v2_slots`
tables (never the live T-series `acp_shared.content_piece`), never ran against real data
(0 rows, every `tour_atoms.weight` still 1.0), and its FE was already torn out at AA-519.
`suggest_trip_reallocation()`/`confirm_trip_reallocation()` (the live quarterly-reallocation
panel below) are KEPT — they no longer receive a feedback-adjusted weight, so
`compute_quarter_plan()`'s scoring is now purely runway/richness/distinctiveness/DFS.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from api.routers.v1_tours import get_tenant
from services.acp_planning.tenant_config import TenantNotFoundError, fetch_tenant_planning_config
from services.acp_planning.trip_reallocation import confirm_trip_reallocation, suggest_trip_reallocation
from services.acp_shared.slate import (SubjectNotEligibleError, SubjectNotFoundError,
                                        cut_subject, fetch_slate, pick_subject, propose_slate)

router = APIRouter(prefix="/v1/planning", tags=["tenant-planning"])

# AA-511 — the Slate (Weekly Slots' replacement, docs/claude_audit/
# AA-511-step0-slate-investigation.md). A SEPARATE router in this same file, under a bare `/v1`
# prefix rather than `/v1/planning`, so the paths match the build prompt's own literal spec
# (`GET /v1/slate`, `POST /v1/subjects/{id}/pick`) exactly.
slate_router = APIRouter(prefix="/v1", tags=["slate"])


@slate_router.get("/slate", summary="AA-511 — this tenant's Slate: every Subject that has cleared a Channel's Bar")
async def get_slate(request: Request, tenant=Depends(get_tenant)):
    tenant_id = UUID(tenant["sub"])
    pool = request.app.state.pool
    await propose_slate(tenant_id, pool)
    config = await _resolve_config(tenant_id, pool)
    channels = await fetch_slate(tenant_id, pool)
    return {
        "channels": channels,
        "posts_per_week": config.capacity_posts_per_week,
    }


@slate_router.post("/subjects/{subject_id}/pick", summary="AA-511 — pick a Subject, create its T8 angle_gate_request")
async def post_pick_subject(
    subject_id: UUID, request: Request, tenant=Depends(get_tenant),
):
    tenant_id = UUID(tenant["sub"])
    pool = request.app.state.pool
    try:
        result = await pick_subject(
            tenant_id, subject_id, pool, selected_by=f"tenant:{tenant_id}",
        )
    except SubjectNotFoundError:
        raise HTTPException(status_code=404, detail=f"No subject {subject_id} for this tenant")
    except SubjectNotEligibleError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return result


@slate_router.post(
    "/subjects/{subject_id}/cut",
    summary="AA-554 mục H.2 — mark a Subject 'cut'; wired to the tenant-facing \"Cut\" button in "
            "AA-556 (SlateTab.tsx's SubjectRow)",
)
async def post_cut_subject(
    subject_id: UUID, request: Request, tenant=Depends(get_tenant),
):
    tenant_id = UUID(tenant["sub"])
    pool = request.app.state.pool
    try:
        result = await cut_subject(tenant_id, subject_id, pool)
    except SubjectNotFoundError:
        raise HTTPException(status_code=404, detail=f"No subject {subject_id} for this tenant")
    except SubjectNotEligibleError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return result


async def _resolve_config(tenant_id: UUID, pool):
    try:
        return await fetch_tenant_planning_config(tenant_id, pool)
    except TenantNotFoundError:
        # Should not happen for a tenant with a valid JWT (the token itself only exists because
        # shared.tenants had a row at login time) — 404, not 500, if it somehow does.
        raise HTTPException(status_code=404, detail="Unknown tenant")


# ---------------------------------------------------------------- trip reallocation

@router.get("/trip-reallocation/suggest", summary="Trip-reallocation suggestion for a quarter")
async def get_trip_reallocation_suggestion(
    request: Request, tenant=Depends(get_tenant),
    year: int = Query(...), quarter: int = Query(...),
):
    tenant_id = UUID(tenant["sub"])
    pool = request.app.state.pool
    return await suggest_trip_reallocation(tenant_id, year, quarter, pool)


class TripReallocationConfirmRequest(BaseModel):
    year: int
    quarter: int
    accept: bool


@router.post("/trip-reallocation/confirm", summary="Accept or reject a trip-reallocation suggestion")
async def post_trip_reallocation_confirm(
    body: TripReallocationConfirmRequest, request: Request, tenant=Depends(get_tenant),
):
    tenant_id = UUID(tenant["sub"])
    pool = request.app.state.pool
    return await confirm_trip_reallocation(
        pool, tenant_id, body.year, body.quarter, body.accept, actor=f"tenant:{tenant_id}",
    )


__all__ = ["router", "slate_router"]
