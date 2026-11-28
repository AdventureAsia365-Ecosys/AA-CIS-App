"""AA-1028 R1 slice 3 (root ADR 0019 d2) — the plan catalogue: CIS admin › Tenants › Plans & add-ons.

- ``GET  /admin/plans``                      the plans (``?product=``, ``?include_retired=true``) with their features
                                             and live subscription counts
- ``GET  /admin/plans/features``             the feature key catalogue (``shared.entitlement_feature``)
- ``POST /admin/plans``                      a new plan with its features
- ``PATCH /admin/plans/{plan_key}``          edit name, price, unit, seats, visibility, notes and the feature set
                                             (made from ``version``; a stale version is 409 ``version_conflict``)
- ``POST /admin/plans/{plan_key}/retire``    no new subscription takes it; current subscribers keep it

Reads: ``tenant.read``; writes: admin role (``tenant.write`` in staff_authz POLICY, re-checked here like the
AA-797 tenant writes). Every write is one transaction with its ``acp_shared.audit_log`` row (before / after); the
CIS 353 triggers re-sync the entitlements of every tenant on the plan (``aa.actor`` = the admin). Rules:
``services/tenant_account/plans.py``.
"""
from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from api.core.staff_authz import staff_policy
from api.routers.admin import _admin_actor, _require_tenant_admin, verify_admin_secret
from services.tenant_account import currency as cur_
from services.tenant_account import plans as pl
from services.tenant_account import pricing_settings as ps

# AA-1191: commercial terms that only matter while VND is an enabled currency (hidden from the list otherwise; still
# edited on Platform › Settings)
VND_TERMS = (ps.FX_VND_PER_USD, ps.VND_PRICE_REVIEW_MONTHS)

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(staff_policy)])

Product = Literal["content", "crm", "tripplanner"]
Decimalish = Optional[str | float | int]


class PlanFeatureIn(BaseModel):
    feature_key: str = Field(..., max_length=64)
    limit_value: Decimalish = Field(None, description="Decimal (string or number); null = no limit / a flag")


class PlanCreateIn(BaseModel):
    plan_key: str = Field(..., max_length=64, pattern=r"^[a-z]+\.[a-z][a-z0-9_]*$")
    product: Product
    name: str = Field(..., min_length=1, max_length=100)
    tier: Optional[Literal["starter", "growth", "business", "enterprise", "internal"]] = None
    price_usd_monthly: Decimalish = None
    price_unit: Optional[Literal["tenant", "seat", "site"]] = None
    seats_included: Optional[int] = Field(None, ge=0, le=100000)
    price_extra_seat_usd: Decimalish = None
    # AA-1116 option B (CIS 435): the VND list, the seat minimum, the contract floor and the overage rates
    price_vnd_monthly: Decimalish = None
    price_extra_seat_vnd: Decimalish = None
    seats_min: Optional[int] = Field(None, ge=1, le=500)
    price_floor_usd_monthly: Decimalish = None
    overage_usd_per_tour: Decimalish = None
    overage_usd_per_1k_calls: Decimalish = None
    is_public: bool = False
    notes: Optional[str] = Field(None, max_length=1000)
    features: list[PlanFeatureIn] = Field(default_factory=list, max_length=64)


class PlanPatchIn(BaseModel):
    version: int = Field(..., ge=1, description="The version the edit was made from")
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    price_usd_monthly: Decimalish = None
    price_unit: Optional[Literal["tenant", "seat", "site"]] = None
    seats_included: Optional[int] = Field(None, ge=0, le=100000)
    price_extra_seat_usd: Decimalish = None
    # AA-1116 option B (CIS 435): the VND list, the seat minimum, the contract floor and the overage rates
    price_vnd_monthly: Decimalish = None
    price_extra_seat_vnd: Decimalish = None
    seats_min: Optional[int] = Field(None, ge=1, le=500)
    price_floor_usd_monthly: Decimalish = None
    overage_usd_per_tour: Decimalish = None
    overage_usd_per_1k_calls: Decimalish = None
    is_public: Optional[bool] = None
    notes: Optional[str] = Field(None, max_length=1000)
    features: Optional[list[PlanFeatureIn]] = Field(None, max_length=64,
                                                     description="The full feature set; left out = unchanged")


class PlanRetireIn(BaseModel):
    version: int = Field(..., ge=1)
    reason: Optional[str] = Field(None, max_length=500)


def _refuse(e: pl.PlanError) -> HTTPException:
    return HTTPException(status_code=e.status, detail={"detail": e.code, "message": e.message},
                         headers={"X-Error-Code": e.code})


@router.get("/plans/commercial-terms", summary="The commercial terms in force and the price review badges")
async def commercial_terms(request: Request, x_admin_secret: str = Header(None)) -> dict:
    """AA-1116: the option B commercial settings in force (edited on Platform › Settings, maker-checker) with their
    labels, and per live priced plan its review dates and the VND drift against the reference rate."""
    verify_admin_secret(x_admin_secret)
    terms = ps.snapshot(ps.COMMERCIAL)
    allowed = cur_.enabled()
    vnd = "VND" in allowed
    async with request.app.state.pool.acquire() as conn:
        plans = await pl.list_plans(conn)
    return {"terms": [{"key": k, "value": terms[k], "label": ps.label(k)} for k in ps.COMMERCIAL
                      if vnd or k not in VND_TERMS],
            "reviews": pl.reviews(plans, terms, today=date.today(), vnd=vnd),
            "enabled_currencies": list(allowed)}


@router.get("/plans", summary="The plan catalogue with features and live subscription counts")
async def list_plans(request: Request, product: Optional[Product] = Query(None),
                     include_retired: bool = Query(False), x_admin_secret: str = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    async with request.app.state.pool.acquire() as conn:
        plans = await pl.list_plans(conn, product=product, include_retired=include_retired)
        tiers = await pl.assignable_tiers(conn)
    # AA-1191: the frontend hides the VND list (column, drawer inputs) while VND is not enabled
    return {"plans": plans, "assignable_tiers": tiers, "enabled_currencies": list(cur_.enabled())}


@router.get("/plans/features", summary="The feature key catalogue")
async def list_features(request: Request, x_admin_secret: str = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    async with request.app.state.pool.acquire() as conn:
        return {"features": await pl.list_features(conn)}


@router.post("/plans", status_code=201, summary="Create a plan with its features")
async def create_plan(body: PlanCreateIn, request: Request, x_admin_secret: str = Header(None),
                      x_admin_user_id: Optional[str] = Header(None),
                      x_admin_role: Optional[str] = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    _require_tenant_admin(x_admin_role)
    payload = body.model_dump(exclude_none=False)
    if body.price_unit is None:
        payload.pop("price_unit")
    payload["features"] = [f.model_dump() for f in body.features]
    try:
        async with request.app.state.pool.acquire() as conn:
            return await pl.create_plan(conn, payload, actor=_admin_actor(x_admin_user_id))
    except pl.PlanError as e:
        raise _refuse(e) from None


@router.patch("/plans/{plan_key}", summary="Edit a plan (a new version)")
async def update_plan(plan_key: str, body: PlanPatchIn, request: Request, x_admin_secret: str = Header(None),
                      x_admin_user_id: Optional[str] = Header(None),
                      x_admin_role: Optional[str] = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    _require_tenant_admin(x_admin_role)
    payload = {k: getattr(body, k) for k in body.model_fields_set}
    payload["version"] = body.version
    if body.features is not None:
        payload["features"] = [f.model_dump() for f in body.features]
    for required in ("name", "price_unit", "is_public"):
        if required in payload and payload[required] is None:
            payload.pop(required)
    try:
        async with request.app.state.pool.acquire() as conn:
            return await pl.update_plan(conn, plan_key, payload, actor=_admin_actor(x_admin_user_id))
    except pl.PlanError as e:
        raise _refuse(e) from None


@router.post("/plans/{plan_key}/retire", summary="Retire a plan (no new subscriptions)")
async def retire_plan(plan_key: str, body: PlanRetireIn, request: Request, x_admin_secret: str = Header(None),
                      x_admin_user_id: Optional[str] = Header(None),
                      x_admin_role: Optional[str] = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    _require_tenant_admin(x_admin_role)
    try:
        async with request.app.state.pool.acquire() as conn:
            return await pl.retire_plan(conn, plan_key, version=body.version, reason=body.reason,
                                        actor=_admin_actor(x_admin_user_id))
    except pl.PlanError as e:
        raise _refuse(e) from None
