"""AA-1028 R1 slice 2 (root ADR 0019 d1) — one-action tenant provisioning: the ``tenant_provision`` saga on
``shared.job`` (``services/jobs/tenant_provision_job.py``, rules in ``services/tenant_account/provision.py``).

- ``POST /admin/tenants/provision``: a new tenant (name, slug, tier, posts per week, kind agency / demo) with its
  content subscription and optional add-ons (crm, tripplanner), as one job. 202 with the job. ``trial_days`` (slice
  3) starts the account in trial (the hourly ``tenant_account_tick`` ends it). On Dev the default kind is ``demo``;
  an ``agency`` only as the one pilot (``pilot_weeks``, root ADR 0019 d16; ``services/tenant_account/dev_guard.py``).
- ``POST /admin/tenants/{tenant_id}/provision``: add-ons for an existing tenant (or a new plan / seats for one).
- ``POST /admin/tenants/{tenant_id}/provision/cancel``: cancel add-ons (slice 3, the revocation path).
- ``GET /admin/tenants/provision/{job_id}``: the job's status, steps and result.
- ``POST /admin/tenants/{tenant_id}/crm-onboarding``: the tenant's CRM agency and its first-owner invitation again
  (AA-1116, ``services/tenant_account/crm_onboarding.py``), e.g. after the CRM was unavailable.

AA-1116 (S282 gaps G5 / G11): a create or an attach with a ``crm`` add-on also gets its CRM agency and, with
``crm_owner_email``, its first-owner invitation in the same staff request, with no hand step on Platform › Agencies.
The CRM takes those writes only with a signed-in staff member's WORKFORCE token, which the saga job does not have, so
the route runs the saga's database transaction itself right after enqueueing it (the same idempotent
``provision.provision`` under the job's id and its per-key advisory lock: the job then finds it done and returns the
stored outcome, ``resumed``), then :func:`crm_onboarding.onboard` with the caller's own token. Its outcome is the
response's ``crm_onboarding``; a CRM refusal never fails the provisioning (the saga is committed). The owner address
goes to the CRM only: never into the job payload, the audit row or a log line.

Idempotent per ``request_key`` (the caller's, 8-64 characters): the same key twice returns the first job
(``created`` false). Admin role only for the writes (``tenant.write`` in staff_authz POLICY, re-checked here like
the AA-797 tenant writes). There are no tenant API keys (AA-1036 R3 slice 3, root ADR 0019 d6).
``POST /admin/tenants`` (the synchronous create) stays; the CIS 355 bridge trigger keeps its content subscription in
step.

This router is included before ``admin.router`` so ``/tenants/provision`` is never read as a tenant id.

AA-1094 (S278, root ADR 0006a A10b-4): the Dev-only E2E CIS admin (A10b) may attach or cancel a TripPlanner add-on of
a SYNTHETIC tenant (``shared.tenants.is_canary IS TRUE`` and not the pilot, api/core/e2e_staff.is_synthetic_tenant)
through the two routes below, so the agent can serve a synthetic agency on TripPlanner. Anything else is 403
``e2e_read_only`` before a job is enqueued (log ``tenant_provision_e2e_refused`` with ``reason``): a product other
than ``tripplanner`` (``product_not_allowed``), a tenant that is not synthetic (``tenant_not_synthetic``), any other
Dev-only E2E identity (A10d, ``not_allowlisted``). The create route stays refused by the bearer middleware. Every other
staff member is unaffected.
"""
from __future__ import annotations

import json
import uuid
from typing import Literal, Optional

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from api.core import e2e_staff
from api.core.staff_authz import request_staff_identity, staff_policy
from api.core.tenant_scope import TENANT_SCOPED
from api.routers.admin import _admin_actor, _require_tenant_admin, verify_admin_secret
from api.services.platform_apps import relay_authorization
from services.jobs import tenant_provision_job
from services.tenant_account import crm_onboarding
from services.tenant_account import provision as pv

logger = structlog.get_logger()

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(staff_policy)])

REQUEST_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,63}$"
CRM_OWNER_DOC = ("AA-1116: the agency owner's e-mail for the CRM first-owner invitation (needs a crm add-on in the "
                 "request; sent to the CRM only, never stored or logged by CIS)")
CRM_TICKET_DOC = "The CRM ticket reference of the agency create / owner invite (default CISONB-<unix time>)"


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
    trial_days: Optional[int] = Field(None, ge=1,
                                      description="The account starts in trial for this many days (the content "
                                                  "subscription and the add-ons of this request trialing); at most "
                                                  "cis.billing.trial_days_max (Platform › Settings), else 422")
    pilot_weeks: Optional[int] = Field(None, ge=0, description="Dev only: the one pilot agency for this many weeks "
                                                               "(0 = until go-live, ended by End pilot; at most "
                                                               "cis.tenant.pilot_max_weeks; root ADR 0019 d16)")
    add_ons: list[AddOnIn] = Field(default_factory=list, max_length=2)
    crm_owner_email: Optional[str] = Field(None, max_length=crm_onboarding.EMAIL_MAX, description=CRM_OWNER_DOC)
    crm_ticket_ref: Optional[str] = Field(None, pattern=crm_onboarding.TICKET_RE.pattern, description=CRM_TICKET_DOC)


class ProvisionAttachIn(BaseModel):
    request_key: str = Field(..., pattern=REQUEST_KEY_PATTERN)
    add_ons: list[AddOnIn] = Field(..., min_length=1, max_length=2)
    crm_owner_email: Optional[str] = Field(None, max_length=crm_onboarding.EMAIL_MAX, description=CRM_OWNER_DOC)
    crm_ticket_ref: Optional[str] = Field(None, pattern=crm_onboarding.TICKET_RE.pattern, description=CRM_TICKET_DOC)


class CrmOnboardingIn(BaseModel):
    owner_email: Optional[str] = Field(None, max_length=crm_onboarding.EMAIL_MAX, description=CRM_OWNER_DOC)
    ticket_ref: Optional[str] = Field(None, pattern=crm_onboarding.TICKET_RE.pattern, description=CRM_TICKET_DOC)


class CrmOnboardingError(BaseModel):
    step: str
    http_status: Optional[int] = None
    code: str


class CrmOnboardingOut(BaseModel):
    tenant_id: Optional[str] = None
    status: Literal["done", "partial", "failed", "skipped"]
    steps: list[str] = Field(default_factory=list)
    error: Optional[CrmOnboardingError] = None


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
    crm_onboarding: Optional[CrmOnboardingOut] = None


def _actor_id(x_admin_user_id: Optional[str]) -> Optional[str]:
    return x_admin_user_id[:40] if isinstance(x_admin_user_id, str) and x_admin_user_id else None


async def _refuse_e2e(request: Request, tenant_id: uuid.UUID, products: list[str]) -> None:
    """AA-1094 (A10b-4): the A10b session attaches / cancels a TripPlanner add-on of a synthetic tenant only; any other
    Dev-only E2E identity never. 403 ``e2e_read_only`` before a job is enqueued. No-op for every other staff member."""
    identity = request_staff_identity(request)
    if not getattr(identity, "e2e_read_only", False):
        return
    if not e2e_staff.has_allowlisted_writes(identity):
        reason = "not_allowlisted"
    elif not products or not set(products) <= e2e_staff.E2E_ADDON_PRODUCTS:
        reason = "product_not_allowed"
    else:
        async with request.app.state.pool.acquire() as conn:
            synthetic = await e2e_staff.is_synthetic_tenant(conn, tenant_id)
        if synthetic:
            return
        reason = "tenant_not_synthetic"
    logger.warning("tenant_provision_e2e_refused", tenant_id=str(tenant_id), products=sorted(set(products)),
                   reason=reason)
    raise HTTPException(status_code=403, detail=e2e_staff.E2E_READ_ONLY)


async def _enqueue(request: Request, payload: dict, crm: Optional[dict] = None) -> ProvisionJobOut:
    try:
        req = pv.parse(payload)
    except pv.ProvisionError as e:
        raise HTTPException(status_code=422, detail={"detail": e.code, "message": e.message}) from None
    job_id, created = await tenant_provision_job.enqueue_request(request.app.state.pool, req)
    out = await _job(request, job_id)
    out.created = created
    if crm is not None:
        out.crm_onboarding = await _crm_onboarding(request, req, out, crm)
    return out


def _crm_request(add_ons: list[AddOnIn], owner_email: Optional[str], ticket: Optional[str],
                 x_admin_user_id: Optional[str]) -> Optional[dict]:
    """The CRM onboarding of a create / attach (AA-1116): None without a crm add-on. The address is checked before
    anything is enqueued (422), so a typo never leaves a tenant half set up."""
    if not any(a.product == "crm" for a in add_ons):
        if owner_email is not None:
            raise HTTPException(status_code=422, detail={
                "detail": "crm_owner_email_without_crm", "message": "An owner e-mail goes with a CRM add-on."})
        return None
    try:
        email = crm_onboarding.clean_email(owner_email) if owner_email is not None else None
    except crm_onboarding.OnboardingError as e:
        raise HTTPException(status_code=e.status, detail={"detail": e.code, "message": e.message,
                                                          "field": "crm_owner_email"}) from None
    return {"owner_email": email, "ticket": ticket, "actor": _admin_actor(x_admin_user_id)}


async def _crm_onboarding(request: Request, req: pv.ProvisionRequest, out: ProvisionJobOut,
                          crm: dict) -> CrmOnboardingOut:
    """Run the saga's transaction now (idempotent per request key, under the job's id) to know the tenant, then
    the CRM agency and owner invite with the caller's own token. Never raises: the outcome is reported."""
    try:
        result = await pv.provision(request.app.state.pool, req, job_id=out.job_id)
    except pv.ProvisionError as e:   # the job is refused the same way; nothing to set up in the CRM
        return CrmOnboardingOut(status="skipped", error=CrmOnboardingError(step="provision", code=e.code))
    except Exception:  # noqa: BLE001 — a lost connection / deadlock: the job retries; onboarding runs again later
        logger.warning("crm_onboarding_provision_deferred", job_id=out.job_id, exc_info=True)
        return CrmOnboardingOut(status="skipped", error=CrmOnboardingError(step="provision", code="provision_pending"))
    out.tenant_id = result["tenant_id"]
    out.steps = list(result["steps"])
    try:
        done = await crm_onboarding.onboard(
            request.app.state.pool, result["tenant_id"], owner_email=crm["owner_email"],
            authorization=relay_authorization(request), actor=crm["actor"], ticket=crm["ticket"],
            request_key=req.request_key)
    except crm_onboarding.OnboardingError as e:
        return CrmOnboardingOut(tenant_id=result["tenant_id"], status="skipped",
                                error=CrmOnboardingError(step="check", http_status=e.status, code=e.code))
    return CrmOnboardingOut(**done)


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
    crm = _crm_request(body.add_ons, body.crm_owner_email, body.crm_ticket_ref, x_admin_user_id)
    payload = {**body.model_dump(exclude={"crm_owner_email", "crm_ticket_ref"}), "mode": "create",
               "add_ons": [a.model_dump() for a in body.add_ons], "requested_by": _actor_id(x_admin_user_id)}
    return await _enqueue(request, payload, crm)


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
    await _refuse_e2e(request, tenant_id, [a.product for a in body.add_ons])
    crm = _crm_request(body.add_ons, body.crm_owner_email, body.crm_ticket_ref, x_admin_user_id)
    payload = {"request_key": body.request_key, "mode": "attach", "tenant_id": str(tenant_id),
               "add_ons": [a.model_dump() for a in body.add_ons], "requested_by": _actor_id(x_admin_user_id)}
    return await _enqueue(request, payload, crm)


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
    await _refuse_e2e(request, tenant_id, list(body.products))
    payload = {"request_key": body.request_key, "mode": "detach", "tenant_id": str(tenant_id),
               "products": list(body.products), "requested_by": _actor_id(x_admin_user_id)}
    return await _enqueue(request, payload)


@router.post("/tenants/{tenant_id}/crm-onboarding", response_model=CrmOnboardingOut,
             summary="Create the tenant's CRM agency when it has none and invite its first owner (idempotent)",
             dependencies=TENANT_SCOPED)
async def crm_onboarding_route(
    tenant_id: uuid.UUID,
    body: CrmOnboardingIn,
    request: Request,
    x_admin_secret: str = Header(None),
    x_admin_user_id: Optional[str] = Header(None),
    x_admin_role: Optional[str] = Header(None),
):
    """AA-1116: the step the provisioning routes run, again (a live crm subscription is required). 200 with the
    outcome, a CRM refusal included (``status`` partial / failed with the CRM's code); 404 / 409 / 422 before any CRM
    call."""
    verify_admin_secret(x_admin_secret)
    _require_tenant_admin(x_admin_role)
    try:
        done = await crm_onboarding.onboard(
            request.app.state.pool, str(tenant_id), owner_email=body.owner_email,
            authorization=relay_authorization(request), actor=_admin_actor(x_admin_user_id), ticket=body.ticket_ref)
    except crm_onboarding.OnboardingError as e:
        raise HTTPException(status_code=e.status, detail={"detail": e.code, "message": e.message},
                            headers={"X-Error-Code": e.code}) from None
    return CrmOnboardingOut(**done)


@router.get("/tenants/provision/{job_id}", response_model=ProvisionJobOut, summary="One tenant provisioning job")
async def get_provision_job(job_id: uuid.UUID, request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    return await _job(request, str(job_id))
