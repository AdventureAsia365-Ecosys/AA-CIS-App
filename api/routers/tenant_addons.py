"""tenant.addons.v1 — an agency's add-ons and its add-on requests, for the partner CRM (AA-1036 R3, root ADR 0019 d7).

Contract: root ``docs/contracts/tenant.addons.v1.openapi.yaml`` (byte copy in ``docs/contracts/`` of this repo, checked
by ``tests/unit/test_aa1036_tenant_addons_contract.py``). The Content studio asks for an add-on with its owner's tenant
token (``/v1/account/add-ons``, ``/v1/account/add-on-requests``: ``api/routers/v1_account_addons.py``). An agency whose
owner works in the CRM without a Content studio membership had no way to ask: the CRM API now relays its CRM owner's
request here, service to service, so the requests keep one store (``shared.tenant_addon_request``, CIS 374) and one
staff decision path (Tenant 360 › Subscription).

  GET  /tenant/v1/tenants/{tenant_id}/add-ons            the same list as /v1/account/add-ons
  POST /tenant/v1/tenants/{tenant_id}/add-on-requests    {"product", "note"?, "crm_member_id"} -> 201 the new request,
                                                         200 the request already pending (created false)

The rules are the studio's (``services/tenant_account/addon_requests.py``, unchanged): 409 ``already_subscribed`` /
``tenant_not_in_service``, one pending request per add-on, 10 a day (429), a note of at most 500 characters. The CRM
decides who may ask (its owner: CRM roles are the CRM's, root ADR 0010 decision 5) and names the member by its CRM
member id: the request's ``requested_by`` and its audit actor are ``crm-member:<id>`` (never a name or an e-mail).
A new request writes the CIS audit row and the staff bell row in the same transaction, as the studio's does.

Auth: the M2M client-credentials token of the ``aa-m2m-<env>`` pool, client = AA-Partner (CRM), scope
``cis.tenant/addons`` (``api/core/m2m_auth.require_m2m``). 401 no / bad token, 403 another client or a token without
the scope, 503 key set or configuration unavailable. ``/tenant`` is neither a staff path nor ``/v1``: no staff
middleware, no tenant role middleware, no tenant rate limiter. The API Gateway route ``/tenant/v1/{tenant_v1_proxy+}``
(ANY, AA-CIS-Infra) already carries these paths. Errors use the contract Problem body ``{error, message[, reason]}``.
Runs on the main pool (cis_app): CIS 374 grants the table to cis_app only. 503 while CIS 374 is not applied.
"""
from __future__ import annotations

import uuid
from typing import Any, Literal, Optional

import asyncpg
import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from api.core import m2m_auth
from api.core.m2m_auth import M2MProblem
from api.routers.v1_account_addons import AddOnList, AddOnRequestCreated
from services.tenant_account import addon_requests as ar
from services.tenant_account.plans import PlanError
from services.tenant_membership.reader import ACQUIRE_TIMEOUT_S, parse_tenant_id

logger = structlog.get_logger()
router = APIRouter(prefix="/tenant/v1", tags=["tenant-addons"])

NO_STORE = "no-store"
ACTOR_PREFIX = "crm-member:"
# PlanError status -> the contract's Problem ``error``.
ERRORS = {400: "invalid", 404: "not_found", 409: "conflict", 429: "too_many_requests"}

require_addons = m2m_auth.require_m2m(
    m2m_auth.SCOPE_TENANT_ADDONS, realm="tenant.addons.v1",
    refused="This client may not read or request add-ons.", log_prefix="tenant_addons")


class TenantAddOnRequestIn(BaseModel):
    """The CRM's relay of its owner's request (contract AddOnRequestIn)."""
    model_config = ConfigDict(extra="forbid")
    product: Literal["crm", "tripplanner"]
    note: Optional[str] = Field(None, max_length=ar.NOTE_MAX)
    crm_member_id: uuid.UUID


def _unavailable(exc: BaseException) -> M2MProblem:
    logger.error("tenant_addons_db_unavailable", reason=type(exc).__name__)
    return M2MProblem(503, "unavailable", "Add-ons are unavailable; retry later.", headers={"Retry-After": "5"})


def _refused(e: PlanError) -> M2MProblem:
    return M2MProblem(e.status if e.status in ERRORS else 400, ERRORS.get(e.status, "invalid"), e.message,
                      reason=e.code[:40])


def _tenant(tenant_id: str) -> str:
    key = parse_tenant_id(tenant_id)
    if key is None:
        raise M2MProblem(404, "not_found", "No tenant has this id.", reason="tenant_not_found")
    return key


def _pool(request: Request) -> Any:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise _unavailable(RuntimeError("pool not configured"))
    return pool


DB_DOWN = (asyncpg.UndefinedTableError, asyncpg.PostgresConnectionError, asyncpg.InterfaceError,
           asyncpg.TooManyConnectionsError, OSError, TimeoutError)


@router.get("/tenants/{tenant_id}/add-ons", responses={200: {"model": AddOnList}})
async def list_tenant_add_ons(tenant_id: str, request: Request,
                              caller: m2m_auth.M2MCaller = Depends(require_addons)) -> Response:
    """The agency's add-ons (active, requested or available), the pending request and the last decision."""
    key = _tenant(tenant_id)
    try:
        async with _pool(request).acquire(timeout=ACQUIRE_TIMEOUT_S) as conn:
            body = await ar.tenant_view(conn, key)
    except PlanError as e:
        raise _refused(e) from None
    except DB_DOWN as exc:
        raise _unavailable(exc) from exc
    logger.info("tenant_addons_served", op="listTenantAddOns", client_id=caller.client_id, tenant_id=key)
    return JSONResponse(content=body, headers={"Cache-Control": NO_STORE})


@router.post("/tenants/{tenant_id}/add-on-requests",
             responses={201: {"model": AddOnRequestCreated}, 200: {"model": AddOnRequestCreated}})
async def request_tenant_add_on(tenant_id: str, request: Request,
                                caller: m2m_auth.M2MCaller = Depends(require_addons)) -> Response:
    """Ask Adventure Asia for an add-on on behalf of the agency's CRM owner (the CRM checked the role)."""
    key = _tenant(tenant_id)
    try:
        body = TenantAddOnRequestIn.model_validate_json(await request.body())
    except ValidationError as exc:
        raise M2MProblem(400, "invalid",
                         "The body must be {\"product\": crm | tripplanner, \"note\"?: <= 500 characters, "
                         "\"crm_member_id\": uuid}.") from exc
    actor = f"{ACTOR_PREFIX}{body.crm_member_id}"
    try:
        async with _pool(request).acquire(timeout=ACQUIRE_TIMEOUT_S) as conn:
            row, created = await ar.create_request(conn, key, body.product, body.note, actor=actor)
    except PlanError as e:
        logger.info("tenant_addons_request_refused", client_id=caller.client_id, tenant_id=key,
                    product=body.product, code=e.code)
        raise _refused(e) from None
    except DB_DOWN as exc:
        raise _unavailable(exc) from exc
    logger.info("tenant_addons_requested", client_id=caller.client_id, tenant_id=key, product=body.product,
                created=created)
    return JSONResponse(status_code=201 if created else 200, content={"created": created, "request": row},
                        headers={"Cache-Control": NO_STORE})
