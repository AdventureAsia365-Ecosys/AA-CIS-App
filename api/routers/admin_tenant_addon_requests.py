"""AA-1036 R3 (root ADR 0019 d7; clarifications TL-7) — Tenant 360 › Subscription › Add-on requests.

- ``GET  /admin/tenants/{tenant_id}/addon-requests``                           the tenant's requests, newest first,
                                                                               an approved one with its ``setup``
                                                                               (in_progress / done / failed)
- ``GET  /admin/tenants/{tenant_id}/addon-requests/{request_id}``              one request in the provision job shape:
                                                                               what the approve drawer polls (the
                                                                               account manager holds no tenant.read,
                                                                               so not GET /tenants/provision/{job_id})
- ``POST /admin/tenants/{tenant_id}/addon-requests/{request_id}/approve``      {plan_key, seats?, note?} -> 202 the
                                                                               tenant_provision job (the saga attaches
                                                                               the plan; the UI waits for it like
                                                                               "Add an add-on") + the request
- ``POST /admin/tenants/{tenant_id}/addon-requests/{request_id}/decline``      {reason} -> 200 the request

The agency asks from the agency app (``v1_account_addons.py``); the staff bell gets a ``tenant.addon.requested`` row.
Reads: the Subscription tab's permission (tenant.read, or the account manager's own book); decisions (clarifications
TL-7 step 4: the account manager approves): admin (``tenant.write``) or the tenant's account manager
(``tenant.book.write``: customer_success, kept to its own book by ``require_tenant_scope``), re-checked here with
``_require_tenant_book_write``. Every ``{tenant_id}`` route is scoped by ``require_tenant_scope``, so a request id of
another tenant answers 404 ``request_not_found``. Rules: ``services/tenant_account/addon_requests.py``.
503 while CIS 374 is not applied. Included before ``admin.router``.
"""
from __future__ import annotations

import uuid
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from api.core.staff_authz import staff_policy
from api.core.tenant_scope import TENANT_SCOPED
from api.routers.admin import _admin_actor, _require_tenant_book_write, verify_admin_secret
from services.tenant_account import addon_requests as ar
from services.tenant_account import provision as pv
from services.tenant_account.plans import PlanError

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(staff_policy)])

UNAVAILABLE = "Add-on requests need migration 374 (shared.tenant_addon_request)."


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_key: str = Field(..., pattern=r"^[a-z]+\.[a-z][a-z0-9_]*$", max_length=64)
    seats: Optional[int] = Field(None, ge=1, le=pv.SEATS_MAX)
    note: Optional[str] = Field(None, max_length=ar.NOTE_MAX)


class DeclineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(..., min_length=1, max_length=ar.NOTE_MAX)


def _refuse(e: Exception) -> HTTPException:
    if isinstance(e, PlanError):
        return HTTPException(status_code=e.status, detail={"detail": e.code, "message": e.message},
                             headers={"X-Error-Code": e.code})
    assert isinstance(e, pv.ProvisionError)  # noqa: S101
    return HTTPException(status_code=422, detail={"detail": e.code, "message": e.message},
                         headers={"X-Error-Code": e.code})


def _staff_id(x_admin_user_id: Optional[str]) -> Optional[str]:
    return x_admin_user_id[:40] if isinstance(x_admin_user_id, str) and x_admin_user_id else None


def _job_shape(tenant_id: uuid.UUID, row: dict, job_id: Optional[str], status: Optional[str],
               error: Optional[str]) -> dict:
    """The provision job shape (lib/tenantPlans.ts runSaga) + the request. ``status`` ``unknown`` when the job row is
    gone (retention)."""
    return {"job_id": job_id, "created": True, "status": status or "unknown",
            "request_key": f"{ar.REQUEST_KEY_PREFIX}{row['request_id']}", "tenant_id": str(tenant_id), "steps": [],
            "result": None, "error": (error or "")[:500] or None, "request": row}


@router.get("/tenants/{tenant_id}/addon-requests", summary="A tenant's add-on requests (newest first)",
            dependencies=TENANT_SCOPED)
async def list_addon_requests(tenant_id: uuid.UUID, request: Request, x_admin_secret: str = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    try:
        async with request.app.state.pool.acquire() as conn:
            items = await ar.staff_list(conn, str(tenant_id))
    except asyncpg.UndefinedTableError:
        raise HTTPException(status_code=503, detail=UNAVAILABLE) from None
    return {"items": items, "pending": sum(1 for r in items if r["status"] == "pending")}


@router.post("/tenants/{tenant_id}/addon-requests/{request_id}/approve", status_code=202,
             summary="Approve an add-on request: attach the plan through the tenant_provision saga (audited)",
             dependencies=TENANT_SCOPED)
async def approve_addon_request(
    tenant_id: uuid.UUID, request_id: uuid.UUID, body: ApproveIn, request: Request,
    x_admin_secret: str = Header(None), x_admin_user_id: Optional[str] = Header(None),
    x_admin_role: Optional[str] = Header(None),
) -> dict:
    verify_admin_secret(x_admin_secret)
    _require_tenant_book_write(x_admin_role)
    try:
        async with request.app.state.pool.acquire() as conn:
            row, job_id = await ar.approve(conn, str(tenant_id), str(request_id), plan_key=body.plan_key,
                                           seats=body.seats, note=body.note, actor=_admin_actor(x_admin_user_id),
                                           staff_id=_staff_id(x_admin_user_id))
            job = await conn.fetchrow("SELECT status, error FROM shared.job WHERE id = $1::uuid", job_id)
    except asyncpg.UndefinedTableError:
        raise HTTPException(status_code=503, detail=UNAVAILABLE) from None
    except (PlanError, pv.ProvisionError) as e:
        raise _refuse(e) from None
    # The provision job shape + the request; the drawer polls GET …/addon-requests/{request_id} (same shape).
    return _job_shape(tenant_id, row, job_id, job["status"] if job else "queued", job["error"] if job else None)


@router.get("/tenants/{tenant_id}/addon-requests/{request_id}",
            summary="One add-on request with its provision job (the approve drawer polls it)",
            dependencies=TENANT_SCOPED)
async def get_addon_request(tenant_id: uuid.UUID, request_id: uuid.UUID, request: Request,
                            x_admin_secret: str = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    try:
        async with request.app.state.pool.acquire() as conn:
            row, status, error = await ar.staff_one(conn, str(tenant_id), str(request_id))
    except asyncpg.UndefinedTableError:
        raise HTTPException(status_code=503, detail=UNAVAILABLE) from None
    except PlanError as e:
        raise _refuse(e) from None
    return _job_shape(tenant_id, row, row["provision_job_id"], status, error)


@router.post("/tenants/{tenant_id}/addon-requests/{request_id}/decline",
             summary="Decline an add-on request with a reason the tenant sees (audited)",
             dependencies=TENANT_SCOPED)
async def decline_addon_request(
    tenant_id: uuid.UUID, request_id: uuid.UUID, body: DeclineIn, request: Request,
    x_admin_secret: str = Header(None), x_admin_user_id: Optional[str] = Header(None),
    x_admin_role: Optional[str] = Header(None),
) -> dict:
    verify_admin_secret(x_admin_secret)
    _require_tenant_book_write(x_admin_role)
    try:
        async with request.app.state.pool.acquire() as conn:
            return await ar.decline(conn, str(tenant_id), str(request_id), reason=body.reason,
                                    actor=_admin_actor(x_admin_user_id))
    except asyncpg.UndefinedTableError:
        raise HTTPException(status_code=503, detail=UNAVAILABLE) from None
    except PlanError as e:
        raise _refuse(e) from None
