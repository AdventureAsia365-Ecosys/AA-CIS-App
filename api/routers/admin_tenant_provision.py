"""AA-1028 R1 slice 2 (root ADR 0019 d1) — one-action tenant provisioning: the ``tenant_provision`` saga on
``shared.job`` (``services/jobs/tenant_provision_job.py``, rules in ``services/tenant_account/provision.py``).

- ``POST /admin/tenants/provision``: a new tenant (name, slug, tier, posts per week, kind agency / demo) with its
  content subscription and optional add-ons (crm, tripplanner), as one job. 202 with the job. ``trial_days`` (slice
  3) starts the account in trial (the hourly ``tenant_account_tick`` ends it). On Dev the default kind is ``demo``;
  an ``agency`` only as the one pilot (``pilot_weeks``, root ADR 0019 d16; ``services/tenant_account/dev_guard.py``).
- ``POST /admin/tenants/{tenant_id}/provision``: add-ons for an existing tenant (or a new plan / seats for one).
- ``POST /admin/tenants/{tenant_id}/provision/cancel``: cancel add-ons (slice 3, the revocation path).
- ``GET /admin/tenants/provision/{job_id}``: the job's status, steps and result.

Idempotent per ``request_key`` (the caller's, 8-64 characters): the same key twice returns the first job
(``created`` false). Admin role only for the writes (``tenant.write`` in staff_authz POLICY, re-checked here like
the AA-797 tenant writes). There are no tenant API keys (AA-1036 R3 slice 3, root ADR 0019 d6).
``POST /admin/tenants`` (the synchronous create) stays; the CIS 355 bridge trigger keeps its content subscription in
step.

This router is included before ``admin.router`` so ``/tenants/provision`` is never read as a tenant id.
"""
from __future__ import annotations

import json
import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from api.core.staff_authz import staff_policy
from api.core.tenant_scope import TENANT_SCOPED
from api.routers.admin import _require_tenant_admin, verify_admin_secret
from services.jobs import tenant_provision_job
from services.tenant_account import provision as pv

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(staff_policy)])

REQUEST_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,63}$"


class AddOnIn(BaseModel):
    product: Literal["crm", "tripplanner"]
    plan_key: str = Field(..., pattern=r"^[a-z]+\.[a-z][a-z0-9_]*$", max_length=64)
    seats: Optional[int] = Field(None, ge=1, le=pv.SEATS_MAX)


class ProvisionCreateIn(BaseModel):
    request_key: str = Field(..., pattern=REQUEST_KEY_PATTERN,
                             description="The caller's key for this request; the same key twice is one job")
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., pattern=r"^[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?$")
    plan_tier: Literal["starter", "growth", "business"]
    posts_per_week: int = Field(..., ge=1, le=14)
    kind: Optional[Literal["agency", "demo"]] = Field(None, description="Default demo on Dev, agency elsewhere")
    trial_days: Optional[int] = Field(None, ge=1, le=pv.TRIAL_DAYS_MAX,
                                      description="The account starts in trial for this many days (the content "
                                                  "subscription and the add-ons of this request trialing)")
    pilot_weeks: Optional[int] = Field(None, ge=1, le=8, description="Dev only: the one pilot agency for this "
                                                                     "many weeks (root ADR 0019 d16)")
    add_ons: list[AddOnIn] = Field(default_factory=list, max_length=2)


class ProvisionAttachIn(BaseModel):
    request_key: str = Field(..., pattern=REQUEST_KEY_PATTERN)
    add_ons: list[AddOnIn] = Field(..., min_length=1, max_length=2)


class ProvisionCancelIn(BaseModel):
    request_key: str = Field(..., pattern=REQUEST_KEY_PATTERN)
    products: list[Literal["crm", "tripplanner"]] = Field(..., min_length=1, max_length=2)


class ProvisionJobOut(BaseModel):
    job_id: str
    created: bool
    status: str
    request_key: str
    tenant_id: Optional[str] = None
    steps: list[str] = Field(default_factory=list)
    result: Optional[dict] = None
    error: Optional[str] = None


def _actor_id(x_admin_user_id: Optional[str]) -> Optional[str]:
    return x_admin_user_id[:40] if isinstance(x_admin_user_id, str) and x_admin_user_id else None


async def _enqueue(request: Request, payload: dict) -> ProvisionJobOut:
    try:
        req = pv.parse(payload)
    except pv.ProvisionError as e:
        raise HTTPException(status_code=422, detail={"detail": e.code, "message": e.message}) from None
    job_id, created = await tenant_provision_job.enqueue_request(request.app.state.pool, req)
    out = await _job(request, job_id)
    out.created = created
    return out


def _json(v):
    return json.loads(v) if isinstance(v, str) else v


async def _job(request: Request, job_id: str) -> ProvisionJobOut:
    row = await request.app.state.pool.fetchrow(
        "SELECT id::text AS id, status, payload, progress, result, error FROM shared.job "
        "WHERE id = $1::uuid AND kind = $2", job_id, pv.KIND)
    if row is None:
        raise HTTPException(status_code=404, detail="No tenant provisioning job with this id")
    payload, progress, result = _json(row["payload"]) or {}, _json(row["progress"]) or {}, _json(row["result"])
    tenant_id = (result or {}).get("tenant_id") or progress.get("tenant_id") or payload.get("tenant_id")
    return ProvisionJobOut(job_id=row["id"], created=False, status=row["status"],
                           request_key=str(payload.get("request_key") or ""), tenant_id=tenant_id,
                           steps=list((result or {}).get("steps") or progress.get("steps") or []), result=result,
                           error=(row["error"] or "")[:500] or None)


@router.post("/tenants/provision", status_code=202, response_model=ProvisionJobOut,
             summary="Provision a tenant: account, content subscription and add-ons in one job")
async def provision_tenant(
    body: ProvisionCreateIn,
    request: Request,
    x_admin_secret: str = Header(None),
    x_admin_user_id: Optional[str] = Header(None),
    x_admin_role: Optional[str] = Header(None),
):
    verify_admin_secret(x_admin_secret)
    _require_tenant_admin(x_admin_role)
    payload = {**body.model_dump(), "mode": "create", "add_ons": [a.model_dump() for a in body.add_ons],
               "requested_by": _actor_id(x_admin_user_id)}
    return await _enqueue(request, payload)


@router.post("/tenants/{tenant_id}/provision", status_code=202, response_model=ProvisionJobOut,
             summary="Add or change add-on subscriptions of a tenant in one job",
             dependencies=TENANT_SCOPED)
async def provision_add_ons(
    tenant_id: uuid.UUID,
    body: ProvisionAttachIn,
    request: Request,
    x_admin_secret: str = Header(None),
    x_admin_user_id: Optional[str] = Header(None),
    x_admin_role: Optional[str] = Header(None),
):
    verify_admin_secret(x_admin_secret)
    _require_tenant_admin(x_admin_role)
    payload = {"request_key": body.request_key, "mode": "attach", "tenant_id": str(tenant_id),
               "add_ons": [a.model_dump() for a in body.add_ons], "requested_by": _actor_id(x_admin_user_id)}
    return await _enqueue(request, payload)


@router.post("/tenants/{tenant_id}/provision/cancel", status_code=202, response_model=ProvisionJobOut,
             summary="Cancel add-on subscriptions of a tenant in one job",
             dependencies=TENANT_SCOPED)
async def provision_cancel(
    tenant_id: uuid.UUID,
    body: ProvisionCancelIn,
    request: Request,
    x_admin_secret: str = Header(None),
    x_admin_user_id: Optional[str] = Header(None),
    x_admin_role: Optional[str] = Header(None),
):
    verify_admin_secret(x_admin_secret)
    _require_tenant_admin(x_admin_role)
    payload = {"request_key": body.request_key, "mode": "detach", "tenant_id": str(tenant_id),
               "products": list(body.products), "requested_by": _actor_id(x_admin_user_id)}
    return await _enqueue(request, payload)


@router.get("/tenants/provision/{job_id}", response_model=ProvisionJobOut, summary="One tenant provisioning job")
async def get_provision_job(job_id: uuid.UUID, request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    return await _job(request, str(job_id))
