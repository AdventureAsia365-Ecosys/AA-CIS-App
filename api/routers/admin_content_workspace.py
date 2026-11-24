"""AA-1148 (S290) — /admin/content-workspace: content staff create and edit tours by hand, tied to an AA supplier of
AA-Booking, and link the old tours whose supplier is only the sheet's text.

  GET  /admin/content-workspace/options                       countries, the limits in force, the tour types and the
                                                              one-click included / not included items (settings an
                                                              administrator edits), whether the Booking supplier list
                                                              can be read, two-person publishing
  GET  /admin/content-workspace/suppliers                     the supplier picker (Booking view supply.supplier.v1)
  GET  /admin/content-workspace/tours                         the hand-made tours (state, supplier, days)
  POST /admin/content-workspace/tours                         create a hand-made tour + its first wording draft
                                                                                               content.write
  GET  /admin/content-workspace/tours/{tour_id}               facts, supplier, required list, state, from price
  PUT  /admin/content-workspace/tours/{tour_id}/facts         save the facts (autosave before the first publish;
                                                              a reason once published)          content.write
  GET  /admin/content-workspace/supplier-links                the live tours not linked to a supplier yet, with the
                                                              suggested supplier when the sheet's text names one
  POST /admin/content-workspace/supplier-links                link tours to one supplier        content.write
  POST /admin/content-workspace/supplier-links/apply-suggestions
                                                              link the suggested matches (DRY RUN first)
                                                                                               content.publish

The wording of a hand-made tour is a draft version like every other tour's (AA-1146): PUT /admin/tours/{id}/draft,
the re-validation, then POST /admin/tours/{id}/draft/publish, which for a hand-made tour also requires the complete
required list (services/content_workspace/store.publish_check, 409 ``required_missing``). Writes need the capability in
api/core/staff_authz.py POLICY; reads are content.read. Logic: services/content_workspace/.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from api.core.staff_authz import staff_policy
from api.routers.admin import verify_admin_secret
from api.routers.admin_pipeline._publish import request_actor
from api.services import content_publish_gate as publish_gate
from services.content_workspace import facts as F
from services.content_workspace import policy
from services.content_workspace import store
from services.content_workspace import suppliers as S

router = APIRouter(prefix="/admin/content-workspace", tags=["admin-content-workspace"],
                   dependencies=[Depends(verify_admin_secret), Depends(staff_policy)])


class FactsSave(F.FactsIn):
    expected_saved_at: Optional[str] = Field(None, max_length=64)
    reason: Optional[str] = Field(None, max_length=500)


class LinkBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)   # the reason's 3 characters do not count spaces
    tour_ids: list[str] = Field(..., min_length=1, max_length=500)
    supplier_id: str = Field(..., max_length=36)
    reason: str = Field(..., min_length=3, max_length=500)


class ApplyBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    dry_run: bool = True
    reason: str = Field(..., min_length=3, max_length=500)


def _raise(e: store.WorkspaceError):
    raise HTTPException(status_code=e.status, detail=e.detail(), headers={"X-Error-Code": e.code})


@router.get("/options", summary="Countries, limits, option lists and switches of the content workspace")
async def options(request: Request):
    from shared import countries

    pool = request.app.state.pool
    sup = await S.list_all(pool)
    async with pool.acquire() as conn:
        two_person = await publish_gate.switch_on(conn)
    return {"countries": sorted(({"iso2": c.iso2, "name": c.name} for c in countries.all_countries()),
                                key=lambda c: c["name"]),
            "limits": (await policy.limits()).as_dict(), **(await policy.form_options()).as_dict(),
            "suppliers_available": sup is not None, "two_person_publish": two_person}


@router.get("/suppliers", summary="AA suppliers of Booking for the picker")
async def list_suppliers(request: Request, q: str = Query("", max_length=100),
                         country: Optional[str] = Query(None, pattern="^[A-Za-z]{2}$"),
                         include_inactive: bool = False, limit: int = Query(50, ge=1, le=200)):
    rows = await S.list_all(request.app.state.pool)
    if rows is None:
        return {"available": False, "items": [], "total": 0}
    ranked = S.rank(rows, query=q, country=country, include_inactive=include_inactive)
    return {"available": True, "items": ranked[:limit], "total": len(ranked)}


@router.get("/tours", summary="Hand-made tours")
async def list_tours(request: Request, q: str = Query("", max_length=100), limit: int = Query(50, ge=1, le=200),
                     offset: int = Query(0, ge=0)):
    return await store.list_manual(request.app.state.pool, q=q, limit=limit, offset=offset)


@router.post("/tours", status_code=201, summary="Create a hand-made tour")
async def create_tour(request: Request, body: F.FactsIn):
    try:
        return await store.create_tour(request.app.state.pool, body, actor=request_actor(request))
    except store.WorkspaceError as e:
        _raise(e)


@router.get("/supplier-links", summary="Live tours not linked to an AA supplier yet")
async def supplier_links(request: Request, country: Optional[str] = Query(None, pattern="^[A-Za-z]{2}$"),
                         q: str = Query("", max_length=100), only_suggested: bool = False,
                         limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    return await store.unlinked_report(request.app.state.pool, country=country, q=q, only_suggested=only_suggested,
                                       limit=limit, offset=offset)


@router.post("/supplier-links/apply-suggestions", summary="Link the suggested supplier matches (DRY RUN first)")
async def apply_suggestions(request: Request, body: ApplyBody):
    try:
        return await store.apply_suggestions(request.app.state.pool, dry_run=body.dry_run, reason=body.reason.strip(),
                                             actor=request_actor(request))
    except store.WorkspaceError as e:
        _raise(e)


@router.post("/supplier-links", summary="Link tours to one AA supplier")
async def link_tours(request: Request, body: LinkBody):
    try:
        return await store.link_tours(request.app.state.pool, body.tour_ids, body.supplier_id,
                                      reason=body.reason.strip(), actor=request_actor(request))
    except store.WorkspaceError as e:
        _raise(e)


@router.get("/tours/{tour_id}", summary="A tour in the content workspace")
async def get_tour(tour_id: str, request: Request):
    _uuid(tour_id)
    try:
        return await store.read_tour(request.app.state.pool, tour_id)
    except store.WorkspaceError as e:
        _raise(e)


@router.put("/tours/{tour_id}/facts", summary="Save the facts of a hand-made tour")
async def save_facts(tour_id: str, request: Request, body: FactsSave):
    _uuid(tour_id)
    form = F.FactsIn(**body.model_dump(exclude={"expected_saved_at", "reason"}))
    try:
        return await store.save_facts(request.app.state.pool, tour_id, form,
                                      expected_saved_at=body.expected_saved_at, reason=body.reason,
                                      actor=request_actor(request))
    except store.WorkspaceError as e:
        _raise(e)


def _uuid(value: str) -> None:
    import uuid

    try:
        uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=404, detail="Tour not found")
