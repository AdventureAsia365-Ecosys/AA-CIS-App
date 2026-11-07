"""AA-1028 R1 slice 3 (root ADR 0019 d1 / d2) — Tenant 360 › Subscription.

- ``GET    /admin/tenants/{tenant_id}/subscription``                      subscriptions, resolved entitlements with
                                                                          their source, overrides, add-on plans
- ``PUT    /admin/tenants/{tenant_id}/entitlement-overrides/{feature_key}``  set an override (enabled, limit, reason,
                                                                          expires_at)
- ``DELETE /admin/tenants/{tenant_id}/entitlement-overrides/{feature_key}``  remove it (the plan's value again)
- ``GET    /admin/tenants/{tenant_id}/tripplanner-site/lead-route``         where the tenant's TripPlanner leads go
                                                                          when it has no CRM add-on (CIS 390)
- ``PUT    /admin/tenants/{tenant_id}/tripplanner-site/lead-route``         set it (channel, webhook URL, e-mail;
                                                                          audited). A webhook URL that is not https
                                                                          or names a private / internal host is a
                                                                          422 with the field (AA-1038 follow-up)

Add-ons are attached / changed / cancelled through the ``tenant_provision`` saga (``admin_tenant_provision.py``).
Reads: ``tenant.read``; writes: admin role (``tenant.write``, re-checked here). Rules:
``services/tenant_account/subscription.py``. Included before ``admin.router``.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request
from pydantic import BaseModel, Field

from api.core.staff_authz import staff_policy
from api.core.tenant_scope import TENANT_SCOPED
from api.routers.admin import _admin_actor, _require_tenant_admin, verify_admin_secret
from services.tenant_account import subscription as sub
from services.tenant_account import tripplanner_site as tp_site
from services.tenant_account.plans import PlanError

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(staff_policy)])

FEATURE_KEY = Path(..., max_length=64, pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


class OverrideIn(BaseModel):
    enabled: bool = True
    limit_value: Optional[str | float | int] = Field(None, description="Decimal; null = no limit (or a flag)")
    reason: str = Field(..., min_length=1, max_length=500)
    expires_at: Optional[datetime] = None


class LeadRouteIn(BaseModel):
    lead_channel: Literal["none", "webhook", "email"]
    lead_webhook_url: Optional[str] = Field(None, max_length=4096, description="https URL of the agency; public host")
    lead_email: Optional[str] = Field(None, max_length=1024, description="One e-mail address of the agency")


def _refuse(e: PlanError) -> HTTPException:
    return HTTPException(status_code=e.status, detail={"detail": e.code, "message": e.message},
                         headers={"X-Error-Code": e.code})


@router.get("/tenants/{tenant_id}/subscription", summary="A tenant's subscriptions, entitlements and overrides",
            dependencies=TENANT_SCOPED)
async def get_subscription(tenant_id: uuid.UUID, request: Request, x_admin_secret: str = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    async with request.app.state.pool.acquire() as conn:
        out = await sub.snapshot(conn, str(tenant_id))
    if out is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return out


@router.put("/tenants/{tenant_id}/entitlement-overrides/{feature_key}",
            summary="Set a per-tenant entitlement override (audited)",
            dependencies=TENANT_SCOPED)
async def put_override(tenant_id: uuid.UUID, body: OverrideIn, request: Request, feature_key: str = FEATURE_KEY,
                       x_admin_secret: str = Header(None), x_admin_user_id: Optional[str] = Header(None),
                       x_admin_role: Optional[str] = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    _require_tenant_admin(x_admin_role)
    try:
        async with request.app.state.pool.acquire() as conn:
            return await sub.set_override(conn, str(tenant_id), feature_key, enabled=body.enabled,
                                          limit_value=body.limit_value, reason=body.reason,
                                          expires_at=body.expires_at, actor=_admin_actor(x_admin_user_id))
    except PlanError as e:
        raise _refuse(e) from None


@router.delete("/tenants/{tenant_id}/entitlement-overrides/{feature_key}",
               summary="Remove a per-tenant entitlement override (audited)",
               dependencies=TENANT_SCOPED)
async def delete_override(tenant_id: uuid.UUID, request: Request, feature_key: str = FEATURE_KEY,
                          x_admin_secret: str = Header(None), x_admin_user_id: Optional[str] = Header(None),
                          x_admin_role: Optional[str] = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    _require_tenant_admin(x_admin_role)
    try:
        async with request.app.state.pool.acquire() as conn:
            return await sub.remove_override(conn, str(tenant_id), feature_key, actor=_admin_actor(x_admin_user_id))
    except PlanError as e:
        raise _refuse(e) from None


def _refuse_route(e: tp_site.LeadRouteError) -> HTTPException:
    detail = {"detail": e.code, "message": e.message}
    if e.field:
        detail["field"] = e.field
    return HTTPException(status_code=e.status, detail=detail, headers={"X-Error-Code": e.code})


@router.get("/tenants/{tenant_id}/tripplanner-site/lead-route",
            summary="Where a tenant's TripPlanner leads go when it has no CRM add-on",
            dependencies=TENANT_SCOPED)
async def get_lead_route(tenant_id: uuid.UUID, request: Request, x_admin_secret: str = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    async with request.app.state.pool.acquire() as conn:
        out = await tp_site.lead_route(conn, str(tenant_id))
    if out is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return out


@router.put("/tenants/{tenant_id}/tripplanner-site/lead-route",
            summary="Set the TripPlanner lead route of a tenant (audited; the webhook must be a public https URL)",
            dependencies=TENANT_SCOPED)
async def put_lead_route(tenant_id: uuid.UUID, body: LeadRouteIn, request: Request,
                         x_admin_secret: str = Header(None), x_admin_user_id: Optional[str] = Header(None),
                         x_admin_role: Optional[str] = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    _require_tenant_admin(x_admin_role)
    try:
        async with request.app.state.pool.acquire() as conn:
            return await tp_site.set_lead_route(conn, str(tenant_id), channel=body.lead_channel,
                                                webhook_url=body.lead_webhook_url, email=body.lead_email,
                                                actor=_admin_actor(x_admin_user_id))
    except tp_site.LeadRouteError as e:
        raise _refuse_route(e) from None
