"""AA-941 (card CRM-13a, screen S-01) — Platform › Tenants › Agencies (CRM): the AA admin L1 console of the partner CRM.

Each route mirrors one ``crm.ops.v1`` route and relays it with the signed-in staff member's own WORKFORCE access token
(api/services/crm_console.py). The CRM decides who may do what (cis-admin or crm-platform-admin read, only
crm-platform-admin writes) and returns only L1 data (no trip, pipeline or revenue keys; its ops serializer allow-list).

  GET  /admin/platform/crm/console                                   {can_write} (+ CIS ``environment``)
  GET  /admin/platform/crm/agencies?cursor=&limit=                   agency page        -> GET  /tenants
  POST /admin/platform/crm/agencies                                  create an agency   -> POST /tenants
  GET  /admin/platform/crm/agencies/{tid}                            one agency         -> GET  /tenants/{tid}
  GET  …/agencies/{tid}/usage | entitlements | data-requests | members | jobs
  POST …/agencies/{tid}/lock | unlock | owner-invite | data-requests | owner-recovery
  POST …/agencies/{tid}/owner-recovery/{rid}/complete
  POST …/agencies/{tid}/members/{mid}/deactivate
  PUT  …/agencies/{tid}/entitlements-allowed | insights-opt-out
  POST …/agencies/{tid}/jobs/{jid}/retry

Staff policy (api/core/staff_authz.py): GETs tenant.read, writes tenant.write (admin role; the read-only viewer is
refused on every write). Included in main.py WITHOUT an include prefix (staff_policy reads the route template).
Request bodies are passed on as JSON objects without a CIS model: the CRM validates them (``extra="forbid"``) and its
422/409 codes reach the page as they are. Bodies are never logged (the owner invite carries an e-mail address).
"""
from __future__ import annotations

import os
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import JSONResponse

from api.core.staff_authz import staff_policy
from api.routers.admin import verify_admin_secret
from api.services import crm_console
from api.services.platform_apps import relay_authorization

router = APIRouter(prefix="/admin/platform/crm", tags=["admin-crm-console"],
                   dependencies=[Depends(verify_admin_secret), Depends(staff_policy)])

_MAX_BODY_KEYS = 20


async def _relay(request: Request, method: str, path: str, *, json: Any = None,
                 params: Optional[dict[str, Any]] = None) -> JSONResponse:
    if isinstance(json, dict) and len(json) > _MAX_BODY_KEYS:
        return JSONResponse({"detail": "body_too_large", "message": "Too many fields."}, status_code=422)
    status, body = await crm_console.crm_ops_call(method, path, relay_authorization(request), json=json,
                                                  params=params)
    return JSONResponse(body, status_code=status)


def _t(tenant_id: uuid.UUID) -> str:
    return f"/tenants/{tenant_id}"


# ── reads ───────────────────────────────────────────────────────────────────────────────────────

@router.get("/console", summary="AA-941 — what the S-01 page may show (can_write) for this staff member")
async def console(request: Request):
    status, body = await crm_console.crm_ops_call("GET", "/console", relay_authorization(request))
    if status == 200:
        # CIS's own environment: the create dialog forces "synthetic" outside prod (the CRM refuses otherwise).
        body = {**body, "environment": os.getenv("ENVIRONMENT", "dev")}
    return JSONResponse(body, status_code=status)


@router.get("/agencies", summary="AA-941 — the CRM agencies (keyset page)")
async def list_agencies(request: Request, cursor: Optional[str] = Query(None, max_length=64),
                        limit: int = Query(50, ge=1, le=200)):
    return await _relay(request, "GET", "/tenants", params={"cursor": cursor, "limit": limit})


@router.get("/agencies/{tenant_id}", summary="AA-941 — one CRM agency")
async def get_agency(request: Request, tenant_id: uuid.UUID):
    return await _relay(request, "GET", _t(tenant_id))


@router.get("/agencies/{tenant_id}/usage", summary="AA-941 — usage meters of one agency")
async def get_usage(request: Request, tenant_id: uuid.UUID):
    return await _relay(request, "GET", f"{_t(tenant_id)}/usage")


@router.get("/agencies/{tenant_id}/entitlements", summary="AA-941 — plan, seats, allowed features, insights opt-out")
async def get_entitlements(request: Request, tenant_id: uuid.UUID):
    return await _relay(request, "GET", f"{_t(tenant_id)}/entitlements")


@router.get("/agencies/{tenant_id}/data-requests", summary="AA-941 — agency export / deletion requests")
async def get_data_requests(request: Request, tenant_id: uuid.UUID):
    return await _relay(request, "GET", f"{_t(tenant_id)}/data-requests")


@router.get("/agencies/{tenant_id}/members", summary="AA-941 — member ids, roles, statuses, pending owner recovery")
async def get_members(request: Request, tenant_id: uuid.UUID):
    return await _relay(request, "GET", f"{_t(tenant_id)}/members")


@router.get("/agencies/{tenant_id}/jobs", summary="AA-941 — failed jobs of one agency (last 7 days)")
async def get_jobs(request: Request, tenant_id: uuid.UUID):
    return await _relay(request, "GET", f"{_t(tenant_id)}/jobs")


# ── writes (the CRM requires crm-platform-admin and a ticket reference) ──────────────────────────────

@router.post("/agencies", summary="AA-941 — create a CRM agency (201 created, 200 already there)")
async def create_agency(request: Request, payload: dict[str, Any] = Body(...)):
    return await _relay(request, "POST", "/tenants", json=payload)


@router.post("/agencies/{tenant_id}/lock", summary="AA-941 — lock an agency")
async def lock(request: Request, tenant_id: uuid.UUID, payload: dict[str, Any] = Body(...)):
    return await _relay(request, "POST", f"{_t(tenant_id)}/lock", json=payload)


@router.post("/agencies/{tenant_id}/unlock", summary="AA-941 — unlock an agency")
async def unlock(request: Request, tenant_id: uuid.UUID, payload: dict[str, Any] = Body(...)):
    return await _relay(request, "POST", f"{_t(tenant_id)}/unlock", json=payload)


@router.post("/agencies/{tenant_id}/owner-invite", summary="AA-941 — invite the first owner (empty agency only)")
async def owner_invite(request: Request, tenant_id: uuid.UUID, payload: dict[str, Any] = Body(...)):
    return await _relay(request, "POST", f"{_t(tenant_id)}/owner-invite", json=payload)


@router.post("/agencies/{tenant_id}/data-requests", summary="AA-941 — raise an export / deletion request")
async def raise_data_request(request: Request, tenant_id: uuid.UUID, payload: dict[str, Any] = Body(...)):
    return await _relay(request, "POST", f"{_t(tenant_id)}/data-requests", json=payload)


@router.post("/agencies/{tenant_id}/owner-recovery", summary="AA-941 — start an owner recovery (14 days)")
async def open_recovery(request: Request, tenant_id: uuid.UUID, payload: dict[str, Any] = Body(...)):
    return await _relay(request, "POST", f"{_t(tenant_id)}/owner-recovery", json=payload)


@router.post("/agencies/{tenant_id}/owner-recovery/{request_id}/complete",
             summary="AA-941 — complete a due owner recovery")
async def complete_recovery(request: Request, tenant_id: uuid.UUID, request_id: uuid.UUID,
                            payload: Optional[dict[str, Any]] = Body(None)):
    return await _relay(request, "POST", f"{_t(tenant_id)}/owner-recovery/{request_id}/complete", json=payload)


@router.post("/agencies/{tenant_id}/members/{member_id}/deactivate", summary="AA-941 — disable a member")
async def deactivate_member(request: Request, tenant_id: uuid.UUID, member_id: uuid.UUID,
                            payload: Optional[dict[str, Any]] = Body(None)):
    return await _relay(request, "POST", f"{_t(tenant_id)}/members/{member_id}/deactivate", json=payload)


@router.put("/agencies/{tenant_id}/entitlements-allowed", summary="AA-941 — allow features (the owner switches on)")
async def put_entitlements_allowed(request: Request, tenant_id: uuid.UUID, payload: dict[str, Any] = Body(...)):
    return await _relay(request, "PUT", f"{_t(tenant_id)}/entitlements-allowed", json=payload)


@router.put("/agencies/{tenant_id}/insights-opt-out", summary="AA-941 — insights opt-out on the owner's request")
async def put_insights_opt_out(request: Request, tenant_id: uuid.UUID, payload: dict[str, Any] = Body(...)):
    return await _relay(request, "PUT", f"{_t(tenant_id)}/insights-opt-out", json=payload)


@router.post("/agencies/{tenant_id}/jobs/{job_id}/retry", summary="AA-941 — re-queue one failed job")
async def retry_job(request: Request, tenant_id: uuid.UUID, job_id: uuid.UUID,
                    payload: dict[str, Any] = Body(...)):
    return await _relay(request, "POST", f"{_t(tenant_id)}/jobs/{job_id}/retry", json=payload)
