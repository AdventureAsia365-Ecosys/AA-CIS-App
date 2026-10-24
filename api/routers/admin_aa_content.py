"""AA-977 (CRM-B5) — Assets › AA content (CRM): the editor API for AA-authored content (prep packs, starter templates,
campaign blocks) that the partner CRM reads over catalog.http.v1 ``/catalog/v1/aa-content`` once published.

  GET    /admin/aa-content?kind=&locale=&status=&country=&limit=&offset=   list (server-side filters and paging)
  POST   /admin/aa-content                                  create {kind, locale, group_key, title, body_markdown?,
                                                            applies_to?} -> item + draft v1
  GET    /admin/aa-content/groups/{group_key}               every locale of one group (EN and VI side by side)
  GET    /admin/aa-content/{content_id}                     item + every version (newest first)
  GET    /admin/aa-content/{content_id}/versions/{version}  one version (draft or published), for the preview
  PUT    /admin/aa-content/{content_id}/draft               save the draft {title, body_markdown, applies_to,
                                                            expected_updated_at}; starts draft n+1 after a publish
  DELETE /admin/aa-content/{content_id}/draft?expected_updated_at=   discard the draft
  POST   /admin/aa-content/{content_id}/publish             {expected_updated_at}: draft -> published
  POST   /admin/aa-content/{content_id}/retire              {expected_updated_at}: hidden from the served list
  POST   /admin/aa-content/{content_id}/restore             {expected_updated_at}: shown again

Who may call (api/core/staff_authz.py POLICY): every read is open to every staff role (content.read; the viewer
reads); create / save / discard a draft need ``content.write`` (the content team); publish, retire and restore need
``master.admin`` (admin only, the tier of the Master Content lifecycle), because they change what agencies receive.
The viewer is refused on every write (READ_ONLY_ROLES). The staff-audit middleware logs every non-GET call.

``expected_updated_at`` is the item's ``updated_at`` as the editor last read it: a write on an item someone else
changed since answers 409 ``stale``. Errors: ``{detail, code[, field]}`` — 422 a field rule
(``visa_needs_official_link`` at publish for a prep pack whose visa section has no https link), 409 ``stale`` /
``retired`` / ``no_draft`` / ``duplicate`` / ``already_retired`` / ``not_retired``, 404 unknown item, version or draft.

The router is included without an include prefix (staff_policy reads the route template from scope["route"].path).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from api.core.staff_authz import request_staff_identity, staff_policy
from api.routers.admin import verify_admin_secret
from services.aa_content import store, validate
from services.aa_content.store import ContentConflict, ContentNotFound
from services.aa_content.validate import ContentInvalid

router = APIRouter(prefix="/admin/aa-content", tags=["admin-aa-content"],
                   dependencies=[Depends(verify_admin_secret), Depends(staff_policy)])


class CreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(..., max_length=32)
    locale: str = Field(..., max_length=8)
    group_key: str = Field(..., max_length=80)
    title: str = Field(..., max_length=1000)
    body_markdown: Optional[str] = Field(None, max_length=40000)
    applies_to: Optional[dict[str, Any]] = None


class DraftBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(..., max_length=1000)
    body_markdown: Optional[str] = Field(None, max_length=40000)
    applies_to: Optional[dict[str, Any]] = None
    expected_updated_at: str = Field(..., max_length=64)


class ExpectedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_updated_at: str = Field(..., max_length=64)


def _actor(request: Request) -> str:
    """``admin:<shared.admin_users id>`` of the verified Cognito caller, else of the x-admin-user-id the BFF sends,
    at most 64 characters (CIS 275 CHECK). A label, never a name or an e-mail address."""
    identity = request_staff_identity(request)
    admin_id = getattr(identity, "admin_id", None) if identity is not None else None
    user = admin_id or request.headers.get("x-admin-user-id") or "unknown"
    return f"admin:{user}"[:64]


def _content_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=404, detail="No AA content item has this id") from None


def _expected(value: str) -> datetime:
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise HTTPException(status_code=422, detail="expected_updated_at must be the item's updated_at (RFC 3339)")
    return parsed.astimezone(timezone.utc)


def _refused(exc: Exception) -> JSONResponse:
    if isinstance(exc, ContentInvalid):
        return JSONResponse({"detail": exc.detail, "code": exc.code, "field": exc.field}, status_code=422)
    if isinstance(exc, ContentConflict):
        return JSONResponse({"detail": exc.detail, "code": exc.code}, status_code=409)
    return JSONResponse({"detail": str(exc), "code": "not_found"}, status_code=404)


_REFUSALS = (ContentInvalid, ContentConflict, ContentNotFound)


# Literal paths (groups/…) before the /{content_id} routes of the same method.

@router.get("", summary="AA-977 — AA-authored content, one page (filters kind, locale, status, country)")
async def list_aa_content(request: Request, kind: Optional[str] = None, locale: Optional[str] = None,
                          status: Optional[str] = None, country: Optional[str] = None, limit: int = 25,
                          offset: int = 0):
    try:
        kind = validate.optional_filter(kind, validate.KINDS, "kind")
        locale = validate.optional_filter(locale, validate.LOCALES, "locale")
        status = validate.optional_filter(status, store.STATUSES, "status")
        country = (validate.applies_to({"country_codes": [country]})["country_codes"][0]
                   if country else None)
    except ContentInvalid as exc:
        return _refused(exc)
    if not 1 <= limit <= store.ADMIN_PAGE_MAX or offset < 0:
        raise HTTPException(status_code=422, detail=f"limit must be 1-{store.ADMIN_PAGE_MAX}, offset >= 0")
    async with request.app.state.pool.acquire() as conn:
        return await store.list_items(conn, kind=kind, locale=locale, status=status, country=country,
                                      limit=limit, offset=offset)


@router.post("", status_code=201, summary="AA-977 — create an item (draft v1)")
async def create_aa_content(body: CreateBody, request: Request):
    try:
        async with request.app.state.pool.acquire() as conn:
            return await store.create_item(conn, kind=body.kind, locale=body.locale, group_key=body.group_key,
                                           title=body.title, body_markdown=body.body_markdown,
                                           applies_to=body.applies_to, actor=_actor(request))
    except _REFUSALS as exc:
        return _refused(exc)


@router.get("/groups/{group_key}", summary="AA-977 — every locale of one group (EN and VI side by side)")
async def get_aa_content_group(group_key: str, request: Request):
    try:
        validate.group_key(group_key)
    except ContentInvalid as exc:
        return _refused(exc)
    async with request.app.state.pool.acquire() as conn:
        items = await store.get_group(conn, group_key)
    if not items:
        raise HTTPException(status_code=404, detail="No AA content group has this key")
    return {"group_key": group_key, "items": items}


@router.get("/{content_id}", summary="AA-977 — one item with every version")
async def get_aa_content(content_id: str, request: Request):
    try:
        async with request.app.state.pool.acquire() as conn:
            return await store.get_item(conn, _content_id(content_id))
    except _REFUSALS as exc:
        return _refused(exc)


@router.get("/{content_id}/versions/{version}", summary="AA-977 — one version (draft or published), preview")
async def get_aa_content_version(content_id: str, version: int, request: Request):
    if not 1 <= version <= 2_147_483_647:
        raise HTTPException(status_code=404, detail="No such version")
    try:
        async with request.app.state.pool.acquire() as conn:
            return await store.get_version(conn, _content_id(content_id), version)
    except _REFUSALS as exc:
        return _refused(exc)


@router.put("/{content_id}/draft", summary="AA-977 — save the draft (starts the next version after a publish)")
async def save_aa_content_draft(content_id: str, body: DraftBody, request: Request):
    key, expected = _content_id(content_id), _expected(body.expected_updated_at)
    try:
        async with request.app.state.pool.acquire() as conn:
            return await store.save_draft(conn, key, title=body.title, body_markdown=body.body_markdown,
                                          applies_to=body.applies_to, expected_updated_at=expected,
                                          actor=_actor(request))
    except _REFUSALS as exc:
        return _refused(exc)


@router.delete("/{content_id}/draft", summary="AA-977 — discard the draft")
async def discard_aa_content_draft(content_id: str, expected_updated_at: str, request: Request):
    key, expected = _content_id(content_id), _expected(expected_updated_at)
    try:
        async with request.app.state.pool.acquire() as conn:
            return await store.discard_draft(conn, key, expected_updated_at=expected)
    except _REFUSALS as exc:
        return _refused(exc)


@router.post("/{content_id}/publish", summary="AA-977 — publish the draft (admin)")
async def publish_aa_content(content_id: str, body: ExpectedBody, request: Request):
    key, expected = _content_id(content_id), _expected(body.expected_updated_at)
    try:
        async with request.app.state.pool.acquire() as conn:
            return await store.publish(conn, key, expected_updated_at=expected, actor=_actor(request))
    except _REFUSALS as exc:
        return _refused(exc)


@router.post("/{content_id}/retire", summary="AA-977 — hide the item from the served list (admin)")
async def retire_aa_content(content_id: str, body: ExpectedBody, request: Request):
    key, expected = _content_id(content_id), _expected(body.expected_updated_at)
    try:
        async with request.app.state.pool.acquire() as conn:
            return await store.retire(conn, key, expected_updated_at=expected, actor=_actor(request))
    except _REFUSALS as exc:
        return _refused(exc)


@router.post("/{content_id}/restore", summary="AA-977 — show a retired item again (admin)")
async def restore_aa_content(content_id: str, body: ExpectedBody, request: Request):
    key, expected = _content_id(content_id), _expected(body.expected_updated_at)
    try:
        async with request.app.state.pool.acquire() as conn:
            return await store.restore(conn, key, expected_updated_at=expected)
    except _REFUSALS as exc:
        return _refused(exc)
