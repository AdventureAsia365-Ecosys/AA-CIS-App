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
- ``GET    /admin/tenants/{tenant_id}/tripplanner-site/domains``            the custom hosts of the tenant's
                                                                          TripPlanner site with their DNS records and
                                                                          status (AA-1090 R8, CIS 428)
- ``POST   /admin/tenants/{tenant_id}/tripplanner-site/domains``            add one ({"domain"}; audited; queues the
                                                                          tripplanner_custom_domain job; limits are
                                                                          settings)
- ``DELETE /admin/tenants/{tenant_id}/tripplanner-site/domains/{domain_id}`` remove it (the job detaches the alias and
                                                                          deletes the certificate)
- ``POST   /admin/tenants/{tenant_id}/tripplanner-site/domains/{domain_id}/retry``  retry a failed one
- ``POST   /admin/tenants/{tenant_id}/pilot/end``                         leave pilot mode (reason required, audited;
                                                                          AA-1086 G2, the "End pilot" button)

- ``GET    /admin/tenants/{tenant_id}/addons``                            AA-1116: the tenant's add-ons (custom domain,
                                                                          AI pack; shared.tenant_addon, CIS 435)
- ``POST   /admin/tenants/{tenant_id}/addons``                            grant one or change its quantity / end
                                                                          ({plan_key, quantity, ends_at, reason})
- ``POST   /admin/tenants/{tenant_id}/addons/{addon_id}/cancel``          cancel it ({reason})
- ``GET|PUT /admin/tenants/{tenant_id}/billing-details``                  bill-to, billing currency, founding offer
- ``PUT    /admin/tenants/{tenant_id}/subscriptions/{product}/terms``     billing interval, agreed price / lock
- ``GET    /admin/tenants/{tenant_id}/quote``                             the monthly / annual quote (option B rules)
Product add-ons (crm, tripplanner) are attached / changed / cancelled through the ``tenant_provision`` saga
(``admin_tenant_provision.py``).
Reads: ``tenant.read``; writes: admin role (``tenant.write``, re-checked here). Rules:
``services/tenant_account/subscription.py``. Included before ``admin.router``.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request
from pydantic import BaseModel, Field

from api.core.staff_authz import staff_policy
from api.core.tenant_scope import TENANT_SCOPED
from api.routers.admin import _admin_actor, _require_tenant_admin, verify_admin_secret
from services.tenant_account import addons as tenant_addons
from services.tenant_account import billing_terms
from services.tenant_account import pilot as tenant_pilot
from services.tenant_account import subscription as sub
from services.tenant_account import tripplanner_domain as tp_domain
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
        if out is not None:
            await _name_override_actors(conn, out["overrides"])
    if out is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return out


async def _name_override_actors(conn, overrides: list[dict]) -> None:
    """S280 (AA-1096): each override's ``granted_by`` ("admin:<staff id>") also as a person (``granted_by_name`` /
    ``granted_by_kind``, ai_gateway.actor_view), so the table names who granted it instead of a raw id. Best effort:
    a failed name read leaves the names empty."""
    from api.services import ai_gateway

    try:
        names = await ai_gateway.actor_names(conn, [o.get("granted_by") for o in overrides])
    except asyncpg.PostgresError:
        names = {}
    for o in overrides:
        view = ai_gateway.actor_view(o.get("granted_by"), names)
        o["granted_by_kind"], o["granted_by_name"] = view["updated_by_kind"], view["updated_by_name"]


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


# ── TripPlanner custom hosts (AA-1090 R8) ─────────────────────────────────────────

class DomainIn(BaseModel):
    domain: str = Field(..., min_length=1, max_length=300, description="The agency's host, e.g. trip.agency-name.com")


class DnsRecordOut(BaseModel):
    purpose: Literal["route", "certificate"]
    type: Literal["CNAME"]
    name: str
    value: str


class DomainOut(BaseModel):
    domain_id: str
    domain: str
    status: Literal["pending", "validating", "attaching", "active", "removing", "failed"]
    dns_records: list[DnsRecordOut]
    failure_code: Optional[str] = None
    failure_message: Optional[str] = None
    requested_at: Optional[str] = None
    validated_at: Optional[str] = None
    active_at: Optional[str] = None
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None
    job_id: Optional[str] = Field(None, description="The tripplanner_custom_domain job queued by this change")


class DomainListOut(BaseModel):
    tenant_id: str
    has_tripplanner: bool
    available: bool = Field(..., description="False: own domains are not switched on in this environment")
    max_domains: int = Field(..., description="The agency's limit "
                                              "(setting cis.tripplanner.custom_domain_max_per_tenant)")
    domains: list[DomainOut]


DOMAIN_ID = Path(..., description="shared.tenant_tripplanner_domain.domain_id")


def _refuse_domain(e: tp_domain.DomainError) -> HTTPException:
    detail = {"detail": e.code, "message": e.message}
    if e.field:
        detail["field"] = e.field
    return HTTPException(status_code=e.status, detail=detail, headers={"X-Error-Code": e.code})


@router.get("/tenants/{tenant_id}/tripplanner-site/domains", response_model=DomainListOut,
            summary="The custom hosts of a tenant's TripPlanner site, with the DNS records the agency creates",
            dependencies=TENANT_SCOPED)
async def get_domains(tenant_id: uuid.UUID, request: Request, x_admin_secret: str = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    async with request.app.state.pool.acquire() as conn:
        out = await tp_domain.list_domains(conn, str(tenant_id))
    if out is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return out


@router.post("/tenants/{tenant_id}/tripplanner-site/domains", response_model=DomainOut, status_code=201,
             summary="Add a custom host to a tenant's TripPlanner site (audited; queues the certificate job)",
             dependencies=TENANT_SCOPED)
async def post_domain(tenant_id: uuid.UUID, body: DomainIn, request: Request, x_admin_secret: str = Header(None),
                      x_admin_user_id: Optional[str] = Header(None),
                      x_admin_role: Optional[str] = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    _require_tenant_admin(x_admin_role)
    try:
        async with request.app.state.pool.acquire() as conn:
            return await tp_domain.add_domain(conn, str(tenant_id), domain=body.domain,
                                              actor=_admin_actor(x_admin_user_id))
    except tp_domain.DomainError as e:
        raise _refuse_domain(e) from None
    except asyncpg.UniqueViolationError:
        raise _refuse_domain(tp_domain.DomainError(tp_domain.DOMAIN_TAKEN, "domain", status=409)) from None


@router.delete("/tenants/{tenant_id}/tripplanner-site/domains/{domain_id}", response_model=DomainOut,
               summary="Remove a custom host (the job detaches it from the distribution and deletes its certificate)",
               dependencies=TENANT_SCOPED)
async def delete_domain(tenant_id: uuid.UUID, request: Request, domain_id: uuid.UUID = DOMAIN_ID,
                        x_admin_secret: str = Header(None), x_admin_user_id: Optional[str] = Header(None),
                        x_admin_role: Optional[str] = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    _require_tenant_admin(x_admin_role)
    try:
        async with request.app.state.pool.acquire() as conn:
            return await tp_domain.remove_domain(conn, str(tenant_id), str(domain_id),
                                                 actor=_admin_actor(x_admin_user_id))
    except tp_domain.DomainError as e:
        raise _refuse_domain(e) from None


@router.post("/tenants/{tenant_id}/tripplanner-site/domains/{domain_id}/retry", response_model=DomainOut,
             summary="Retry a failed custom host (back to pending; a new certificate job)",
             dependencies=TENANT_SCOPED)
async def retry_domain(tenant_id: uuid.UUID, request: Request, domain_id: uuid.UUID = DOMAIN_ID,
                       x_admin_secret: str = Header(None), x_admin_user_id: Optional[str] = Header(None),
                       x_admin_role: Optional[str] = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    _require_tenant_admin(x_admin_role)
    try:
        async with request.app.state.pool.acquire() as conn:
            return await tp_domain.retry_domain(conn, str(tenant_id), str(domain_id),
                                                actor=_admin_actor(x_admin_user_id))
    except tp_domain.DomainError as e:
        raise _refuse_domain(e) from None


# ── POST /admin/tenants/{id}/pilot/end — leave pilot mode (AA-1086 G2) ─────────

class EndPilotRequest(BaseModel):
    reason: str = Field(..., max_length=tenant_pilot.REASON_MAX,
                        description="Why the pilot ends now (required, recorded in the audit trail)")


class EndPilotResponse(BaseModel):
    tenant_id: str
    is_pilot: bool
    changed: bool = Field(..., description="False when the tenant was not the pilot: nothing was written")
    previous_pilot_ends_at: Optional[str] = None
    ended_early: Optional[bool] = None


@router.post("/tenants/{tenant_id}/pilot/end", response_model=EndPilotResponse,
             summary="End a tenant's pilot (clears the pilot marker; the Dev pilot slot is free again)",
             dependencies=TENANT_SCOPED)
async def end_tenant_pilot(
    tenant_id: str,
    body: EndPilotRequest,
    request: Request,
    x_admin_secret: str = Header(None),
    x_admin_user_id: Optional[str] = Header(None),
    x_admin_role: Optional[str] = Header(None),
):
    """AA-1086 gap G2 (root ADR 0019 d16): clear ``shared.tenants.is_pilot`` and ``pilot_ends_at`` so the unique index
    ``tenants_one_pilot_key`` frees Dev's one pilot slot. Administrator only (``tenant.write``), reason required,
    audited ``tenant.pilot_ended``, one account event so the CRM's pilot marker follows at its next pull. Idempotent: a
    tenant that is not the pilot answers 200 with ``changed`` false and nothing is written
    (services/tenant_account/pilot.py)."""
    verify_admin_secret(x_admin_secret)
    _require_tenant_admin(x_admin_role)
    try:
        uuid.UUID(tenant_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid tenant_id UUID")
    async with request.app.state.pool.acquire() as conn:
        try:
            out = await tenant_pilot.end_pilot(conn, tenant_id, reason=body.reason,
                                               actor=_admin_actor(x_admin_user_id))
        except tenant_pilot.PilotError as e:
            status = 404 if e.code == "tenant_not_found" else 422
            raise HTTPException(status_code=status, detail={"detail": e.code, "message": e.message}) from None
    return EndPilotResponse(**out)


# ── AA-1116 option B: add-ons, billing details, subscription terms, quote ─────────────────────────────────────────
ADDON_ID = Path(..., description="The add-on row id")


class AddonIn(BaseModel):
    plan_key: str = Field(..., pattern=r"^addon\.[a-z][a-z0-9_]*$", max_length=64)
    quantity: int = Field(1, ge=1)
    ends_at: Optional[datetime] = Field(None, description="Optional end (an AI pack for one month); null = no end")
    reason: str = Field(..., min_length=3, max_length=500)


class ReasonIn(BaseModel):
    reason: str = Field(..., min_length=3, max_length=500)


class BillingDetailsIn(BaseModel):
    """Only the fields sent change. founding_offer_from null clears the offer; until left out = from + the setting."""
    legal_entity_name: Optional[str] = Field(None, max_length=300)
    tax_id: Optional[str] = Field(None, max_length=64)
    billing_email: Optional[str] = Field(None, max_length=254)
    billing_address: Optional[str] = Field(None, max_length=1000)
    billing_country: Optional[str] = Field(None, max_length=2)
    billing_currency: Optional[Literal["USD", "VND"]] = None
    founding_offer_from: Optional[str] = None
    founding_offer_until: Optional[str] = None


class SubscriptionTermsIn(BaseModel):
    billing_interval: Optional[Literal["month", "year"]] = None
    agreed_price_monthly: Optional[str | float | int] = None
    agreed_currency: Optional[Literal["USD", "VND"]] = None
    agreed_until: Optional[str] = None
    lock: bool = Field(False, description="Lock the list price now for cis.billing.price_lock_months")


def _refuse_terms(e: billing_terms.TermsError) -> HTTPException:
    return HTTPException(status_code=e.status, detail={"detail": e.code, "message": e.message, "field": e.field})


def _refuse_addon(e: tenant_addons.AddonError) -> HTTPException:
    return HTTPException(status_code=e.status, detail={"detail": e.code, "message": e.message})


@router.get("/tenants/{tenant_id}/addons", summary="The tenant's add-ons (custom domain, AI pack)",
            dependencies=TENANT_SCOPED)
async def get_addons(tenant_id: uuid.UUID, request: Request, x_admin_secret: str = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    async with request.app.state.pool.acquire() as conn:
        return {"tenant_id": str(tenant_id), "add_ons": await tenant_addons.list_addons(conn, str(tenant_id))}


@router.post("/tenants/{tenant_id}/addons", summary="Grant an add-on or change its quantity / end (audited)")
async def post_addon(tenant_id: uuid.UUID, body: AddonIn, request: Request, x_admin_secret: str = Header(None),
                     x_admin_user_id: Optional[str] = Header(None), x_admin_role: Optional[str] = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    _require_tenant_admin(x_admin_role)
    try:
        async with request.app.state.pool.acquire() as conn:
            return await tenant_addons.set_addon(conn, str(tenant_id), plan_key=body.plan_key, quantity=body.quantity,
                                                 ends_at=body.ends_at, reason=body.reason,
                                                 actor=_admin_actor(x_admin_user_id))
    except tenant_addons.AddonError as e:
        raise _refuse_addon(e) from None


@router.post("/tenants/{tenant_id}/addons/{addon_id}/cancel", summary="Cancel an add-on (audited)")
async def cancel_addon(tenant_id: uuid.UUID, body: ReasonIn, request: Request, addon_id: uuid.UUID = ADDON_ID,
                       x_admin_secret: str = Header(None), x_admin_user_id: Optional[str] = Header(None),
                       x_admin_role: Optional[str] = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    _require_tenant_admin(x_admin_role)
    try:
        async with request.app.state.pool.acquire() as conn:
            return await tenant_addons.cancel_addon(conn, str(tenant_id), str(addon_id), reason=body.reason,
                                                    actor=_admin_actor(x_admin_user_id))
    except tenant_addons.AddonError as e:
        raise _refuse_addon(e) from None


@router.get("/tenants/{tenant_id}/billing-details", summary="Bill-to, billing currency and founding offer",
            dependencies=TENANT_SCOPED)
async def get_billing_details(tenant_id: uuid.UUID, request: Request, x_admin_secret: str = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    async with request.app.state.pool.acquire() as conn:
        out = await billing_terms.details(conn, str(tenant_id))
    if out is None:
        raise HTTPException(status_code=404, detail="No tenant with this id")
    return out


@router.put("/tenants/{tenant_id}/billing-details", summary="Set billing details (audited)")
async def put_billing_details(tenant_id: uuid.UUID, body: BillingDetailsIn, request: Request,
                              x_admin_secret: str = Header(None), x_admin_user_id: Optional[str] = Header(None),
                              x_admin_role: Optional[str] = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    _require_tenant_admin(x_admin_role)
    payload = {k: getattr(body, k) for k in body.model_fields_set}
    try:
        async with request.app.state.pool.acquire() as conn:
            return await billing_terms.set_details(conn, str(tenant_id), payload, actor=_admin_actor(x_admin_user_id))
    except billing_terms.TermsError as e:
        raise _refuse_terms(e) from None


@router.put("/tenants/{tenant_id}/subscriptions/{product}/terms", summary="Set a subscription's terms (audited)")
async def put_subscription_terms(tenant_id: uuid.UUID, product: Literal["content", "crm", "tripplanner"],
                                 body: SubscriptionTermsIn, request: Request, x_admin_secret: str = Header(None),
                                 x_admin_user_id: Optional[str] = Header(None),
                                 x_admin_role: Optional[str] = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    _require_tenant_admin(x_admin_role)
    payload = {k: getattr(body, k) for k in body.model_fields_set}
    try:
        async with request.app.state.pool.acquire() as conn:
            return await billing_terms.set_terms(conn, str(tenant_id), product, payload,
                                                 actor=_admin_actor(x_admin_user_id))
    except billing_terms.TermsError as e:
        raise _refuse_terms(e) from None


@router.get("/tenants/{tenant_id}/quote", summary="The tenant's monthly and annual quote (option B rules)",
            dependencies=TENANT_SCOPED)
async def get_quote(tenant_id: uuid.UUID, request: Request, x_admin_secret: str = Header(None)) -> dict:
    verify_admin_secret(x_admin_secret)
    async with request.app.state.pool.acquire() as conn:
        out = await billing_terms.tenant_quote(conn, str(tenant_id))
    if out is None:
        raise HTTPException(status_code=404, detail="No tenant with this id")
    return out
