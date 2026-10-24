"""AA-835 — /admin/supply-content: the review of the customer content CIS writes for Booking hotels and services.

  GET  /admin/supply-content                                          entities, filter by content_status / type
  GET  /admin/supply-content/status                                   the Booking feed: cursor, lag, last error,
                                                                      draft jobs, LLM cost today
  GET  /admin/supply-content/{entity_id}                              facts (read-only), versions, the open draft,
                                                                      its diff against the published (or last
                                                                      reviewed) version, photos, the Booking path
  POST /admin/supply-content/{entity_id}/versions                     an edit: a new human draft     content.write
  POST /admin/supply-content/{entity_id}/versions/{vid}/publish       approve and publish            review.act
  POST /admin/supply-content/{entity_id}/versions/{vid}/reject        reject a draft                 review.act
  POST /admin/supply-content/{entity_id}/unpublish                    hide it from Booking           review.act
  POST /admin/supply-content/{entity_id}/draft                        queue a new LLM draft (paid)   content.write

Writes need the capability in api/core/staff_authz.py POLICY (content, reviewer and admin hold both; the viewer none,
READ_ONLY_ROLES). Reads are open to every staff role. Publish answers 409 (photo_rights) when a photo of the version
has no usage rights in Booking or is not copied, 409 (outdated) when the facts changed after the version was written.
The rules live in services/supply_content/review.py (also used for ECS-exec evidence runs).
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from api.core.staff_authz import staff_policy
from api.routers.admin import verify_admin_secret
from services.supply_content import review

router = APIRouter(prefix="/admin/supply-content", tags=["admin-supply-content"],
                   dependencies=[Depends(verify_admin_secret), Depends(staff_policy)])

STATUSES = ("draft", "in_review", "published", "stale", "unpublished")


class SupplyPhotoIn(BaseModel):
    media_id: str = Field(..., max_length=64)
    caption: Optional[str] = Field(None, max_length=400)
    alt: Optional[str] = Field(None, max_length=400)


class SupplyContentVersionIn(BaseModel):
    description: str = Field(..., min_length=1, max_length=review.MAX_DESCRIPTION_CHARS)
    highlights: list[str] = Field(default_factory=list, max_length=5)
    good_to_know: list[str] = Field(default_factory=list, max_length=3)
    photos: list[SupplyPhotoIn] = Field(default_factory=list, max_length=review.MAX_LIST)
    based_on_version_id: Optional[UUID] = None


class SupplyReviewNote(BaseModel):
    note: Optional[str] = Field(None, max_length=1000)


def _actor(user_id: Optional[str], role: Optional[str]) -> str:
    return f"staff:{(user_id or role or 'unknown').strip()[:80]}"


def _raise(e: review.ReviewError):
    raise HTTPException(status_code=e.status, detail=e.detail())


@router.get("", summary="AA-835 — supply content entities")
async def list_supply_content(request: Request,
                              status: Optional[str] = Query(None, pattern="^(" + "|".join(STATUSES) + ")$"),
                              entity_type: Optional[str] = Query(None, pattern="^(hotel|service)$"),
                              limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    return await review.list_entities(request.app.state.pool, status=status, entity_type=entity_type, limit=limit,
                                      offset=offset)


@router.get("/status", summary="AA-835 — the Booking supply feed: cursor, lag, last error, draft jobs, cost today")
async def supply_feed_status(request: Request):
    return await review.feed_status(request.app.state.pool)


@router.get("/{entity_id}", summary="AA-835 — one entity: facts, versions, open draft and its diff, photos")
async def get_supply_content(entity_id: UUID, request: Request):
    try:
        return await review.get_detail(request.app.state.pool, str(entity_id))
    except review.ReviewError as e:
        _raise(e)


@router.post("/{entity_id}/versions", status_code=201, summary="AA-835 — an edit (a new human draft)")
async def create_supply_content_version(entity_id: UUID, body: SupplyContentVersionIn, request: Request,
                                        x_admin_user_id: Optional[str] = Header(None),
                                        x_admin_role: Optional[str] = Header(None)):
    data = body.model_dump()
    data["photos"] = [p.model_dump() for p in body.photos]
    data["based_on_version_id"] = str(body.based_on_version_id) if body.based_on_version_id else None
    async with request.app.state.pool.acquire() as conn:
        try:
            return await review.create_human_version(conn, str(entity_id), data,
                                                     actor=_actor(x_admin_user_id, x_admin_role))
        except review.ReviewError as e:
            _raise(e)


@router.post("/{entity_id}/versions/{version_id}/publish", summary="AA-835 — approve and publish a version")
async def publish_supply_content(entity_id: UUID, version_id: UUID, request: Request,
                                 body: Optional[SupplyReviewNote] = None,
                                 x_admin_user_id: Optional[str] = Header(None),
                                 x_admin_role: Optional[str] = Header(None)):
    async with request.app.state.pool.acquire() as conn:
        try:
            return await review.publish(conn, str(entity_id), str(version_id),
                                        actor=_actor(x_admin_user_id, x_admin_role), note=body.note if body else None)
        except review.ReviewError as e:
            _raise(e)


@router.post("/{entity_id}/versions/{version_id}/reject", summary="AA-835 — reject a draft")
async def reject_supply_content(entity_id: UUID, version_id: UUID, request: Request,
                                body: Optional[SupplyReviewNote] = None,
                                x_admin_user_id: Optional[str] = Header(None),
                                x_admin_role: Optional[str] = Header(None)):
    async with request.app.state.pool.acquire() as conn:
        try:
            return await review.reject(conn, str(entity_id), str(version_id),
                                       actor=_actor(x_admin_user_id, x_admin_role), note=body.note if body else None)
        except review.ReviewError as e:
            _raise(e)


@router.post("/{entity_id}/unpublish", summary="AA-835 — hide the content from Booking (rows kept)")
async def unpublish_supply_content(entity_id: UUID, request: Request, x_admin_user_id: Optional[str] = Header(None),
                                   x_admin_role: Optional[str] = Header(None)):
    async with request.app.state.pool.acquire() as conn:
        try:
            return await review.unpublish(conn, str(entity_id), actor=_actor(x_admin_user_id, x_admin_role))
        except review.ReviewError as e:
            _raise(e)


@router.post("/{entity_id}/draft", status_code=202, summary="AA-835 — queue a new LLM draft (paid, capped)")
async def request_supply_content_draft(entity_id: UUID, request: Request,
                                       x_admin_user_id: Optional[str] = Header(None),
                                       x_admin_role: Optional[str] = Header(None)):
    async with request.app.state.pool.acquire() as conn:
        try:
            return await review.request_draft(conn, str(entity_id), actor=_actor(x_admin_user_id, x_admin_role))
        except review.ReviewError as e:
            _raise(e)
