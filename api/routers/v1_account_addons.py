"""
/v1/account add-ons — AA-1036 R3 (root ADR 0019 d7): the agency app's add-on list and its "Request add-on" action.

  GET  /v1/account/add-ons            per add-on (crm, tripplanner): active | requested | available, the pending
                                      request and the last decision (a decline shows its reason; an approval its
                                      setup: in_progress | done | failed); a "view as tenant" session reads it too
  POST /v1/account/add-on-requests    {"product": "crm" | "tripplanner", "note": optional, <= 500 characters}
                                      -> 201 the new request, 200 the request already pending (no second one);
                                      409 already_subscribed / tenant_not_in_service, 429 too_many_requests

Tenant-JWT-gated (``get_tenant``; a staff token is 403). The tenant id comes ONLY from the verified token. The POST is a
``/v1/account`` write, so the tenant role middleware lets only the Content studio owner make it (editor and viewer get
403 ``studio_role_forbidden``; a read-only "view as tenant" session too). Runs on the main pool (cis_app): CIS 374
grants the table to cis_app only, like the members directory. A new request writes an audit row and a staff bell row;
the decision is made in Tenant 360 › Subscription (``admin_tenant_addon_requests.py``). Rules:
``services/tenant_account/addon_requests.py``. 503 while CIS 374 is not applied.
"""
from __future__ import annotations

import uuid
from typing import Literal, Optional

import asyncpg
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from api.routers.v1_tours import get_tenant
from services.tenant_account import addon_requests as ar
from services.tenant_account.plans import PlanError

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/account", tags=["tenant-account"])

UNAVAILABLE = "Add-on requests are not available yet."


class AddOnRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: Literal["crm", "tripplanner"]
    note: Optional[str] = Field(None, max_length=ar.NOTE_MAX)


class AddOnRequestOut(BaseModel):
    request_id: str
    product: Literal["crm", "tripplanner"]
    status: Literal["pending", "approved", "declined"]
    note: Optional[str] = None
    requested_at: str
    decided_at: Optional[str] = None
    decision_note: Optional[str] = None
    # An approved request's setup (the attach job): in_progress, done, or failed (the add-on is not on; Adventure Asia
    # finishes it). None when not approved or no longer known.
    setup: Optional[Literal["in_progress", "done", "failed"]] = None


class AddOnState(BaseModel):
    product: Literal["crm", "tripplanner"]
    state: Literal["active", "requested", "available"]
    subscription_status: Optional[str] = None
    request: Optional[AddOnRequestOut] = None
    last_decision: Optional[AddOnRequestOut] = None


class AddOnList(BaseModel):
    in_service: bool
    add_ons: list[AddOnState]


class AddOnRequestCreated(BaseModel):
    created: bool
    request: AddOnRequestOut


def _tenant_id(tenant: dict) -> str:
    sub = tenant.get("sub")
    if not isinstance(sub, str) or not sub:
        raise HTTPException(status_code=403, detail="Tenant token required")
    try:
        return str(uuid.UUID(sub))
    except ValueError:
        raise HTTPException(status_code=403, detail="Tenant token required")


def _actor(tenant: dict, tenant_id: str) -> str:
    """The person who asked: their Content studio membership (``mid``), never an e-mail; else the tenant."""
    mid = tenant.get("mid")
    try:
        return f"membership:{uuid.UUID(str(mid))}" if mid else f"tenant:{tenant_id}"
    except ValueError:
        return f"tenant:{tenant_id}"


def _refuse(e: PlanError) -> HTTPException:
    return HTTPException(status_code=e.status, detail={"detail": e.code, "message": e.message},
                         headers={"X-Error-Code": e.code})


@router.get("/add-ons", response_model=AddOnList, summary="The agency's add-ons: active, requested or available")
async def list_add_ons(request: Request, tenant=Depends(get_tenant)):
    tenant_id = _tenant_id(tenant)
    try:
        async with request.app.state.pool.acquire() as conn:
            return await ar.tenant_view(conn, tenant_id)
    except asyncpg.UndefinedTableError as exc:
        logger.error("tenant_add_ons_unavailable", reason=type(exc).__name__)
        raise HTTPException(status_code=503, detail=UNAVAILABLE) from exc
    except PlanError as e:
        raise _refuse(e) from None


@router.post("/add-on-requests", response_model=AddOnRequestCreated, status_code=201,
             responses={200: {"model": AddOnRequestCreated, "description": "A request for this add-on is open"}},
             summary="Ask Adventure Asia for an add-on (Content studio owner)")
async def request_add_on(body: AddOnRequestIn, request: Request, tenant=Depends(get_tenant)):
    tenant_id = _tenant_id(tenant)
    try:
        async with request.app.state.pool.acquire() as conn:
            row, created = await ar.create_request(conn, tenant_id, body.product, body.note,
                                                   actor=_actor(tenant, tenant_id))
    except asyncpg.UndefinedTableError as exc:
        logger.error("tenant_add_on_request_unavailable", reason=type(exc).__name__)
        raise HTTPException(status_code=503, detail=UNAVAILABLE) from exc
    except PlanError as e:
        logger.info("tenant_add_on_request_refused", tenant_id=tenant_id, product=body.product, code=e.code)
        raise _refuse(e) from None
    logger.info("tenant_add_on_requested", tenant_id=tenant_id, product=body.product, created=created)
    return JSONResponse(status_code=201 if created else 200, content={"created": created, "request": row})
