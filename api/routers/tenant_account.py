"""tenant.entitlements.v1 — the tenant account, subscriptions and entitlements, for the other apps (AA-1028 R1).

Contract: root ``docs/contracts/tenant.entitlements.v1.openapi.yaml`` (byte copy in ``docs/contracts/`` of this repo,
checked by ``tests/unit/test_aa1028_tenant_account_contract.py``). Root ADR 0019 d1 / d2: CIS ``shared`` is the master
of the tenant account; the CRM (``crm_entitlement_pull``) and Booking (``bk_tenant_pull``) keep read-only projections
of it; apps check feature keys, never plan names.

  GET /tenant/v1/tenants/{tenant_id}/account   snapshot of one tenant (status, in_service, kind, pilot marker, region,
                                               time zone, subscriptions, resolved entitlements), ETag +
                                               If-None-Match -> 304
  GET /tenant/v1/account-events?after=&limit=  cursor feed of account.status_changed, subscription.changed and
                                               entitlement.changed, oldest first, never skipping a late commit
                                               (services/tenant_account/feed.py)

Storage: ``shared.tenants``, ``shared.tenant_subscription``, ``shared.v_tenant_entitlement_v1`` and
``shared.tenant_account_event`` (CIS 353-355), read on the main pool (cis_app), one READ ONLY transaction per request.
503 while the migrations are not applied.

Auth: the M2M client-credentials token of the ``aa-m2m-<env>`` pool, client = AA-Partner (CRM), scope
``cis.tenant/read`` (``api/core/m2m_auth.require_m2m``, the same dependency as /tenant/membership/v1; the Booking
client joins with ``bk_tenant_pull``). 401 no / bad token, 403 another client or a token without the scope, 503 key
set or configuration unavailable. ``/tenant`` is neither a staff path nor ``/v1``: neither the staff middleware nor the
tenant rate limiter touches these routes. The API Gateway route is ``/tenant/v1/{tenant_v1_proxy+}`` (AA-CIS-Infra).

Caching: the snapshot carries a strong ETag over the body without ``as_of``, ``Cache-Control: private, no-cache`` and
``Vary: Authorization``; the feed is never cached. Errors use the contract Problem body ``{error, message}``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from api.core import m2m_auth
from api.core.m2m_auth import M2MProblem
from api.schemas.public_api import TenantAccountBody, TenantAccountEventPage  # AA-899: doc only
from services.tenant_account import feed, reader
from services.tenant_membership.reader import InvalidRequest, parse_tenant_id

logger = structlog.get_logger()
router = APIRouter(prefix="/tenant/v1", tags=["tenant-account"])

CACHE_CONTROL = "private, no-cache"
NO_STORE = "no-store"

require_account_reader = m2m_auth.require_m2m(
    m2m_auth.SCOPE_TENANT_READ, realm="tenant.entitlements.v1",
    refused="This client may not read tenant accounts.", log_prefix="tenant_account")


def _unavailable(exc: Exception) -> M2MProblem:
    logger.error("tenant_account_db_unavailable", reason=str(exc))
    return M2MProblem(503, "unavailable", "Tenant accounts are unavailable; retry later.",
                      headers={"Retry-After": "5"})


def _pool(request: Request) -> Any:
    return getattr(request.app.state, "pool", None)


def _body_hash(body: Any) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _matches(if_none_match: Optional[str], etag: str) -> bool:
    """RFC 9110 weak comparison of If-None-Match against our strong tag (``*`` matches any current body)."""
    if not if_none_match:
        return False
    for candidate in if_none_match.split(","):
        candidate = candidate.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == etag:
            return True
    return False


@router.get("/tenants/{tenant_id}/account", responses={200: {"model": TenantAccountBody}, 304: {}})
async def get_tenant_account(tenant_id: str, request: Request,
                             caller: m2m_auth.M2MCaller = Depends(require_account_reader)) -> Response:
    key = parse_tenant_id(tenant_id)
    if key is None:
        raise M2MProblem(404, "not_found", "No tenant has this id.")
    try:
        async with reader.account_connection(_pool(request)) as conn:
            data = await reader.tenant_account(conn, key)
    except reader.AccountUnavailable as exc:
        raise _unavailable(exc) from exc
    logger.info("tenant_account_served", op="getTenantAccount", client_id=caller.client_id, tenant_id=key,
                found=data is not None, entitlements=len(data["entitlements"]) if data else 0)
    if data is None:
        raise M2MProblem(404, "not_found", "No tenant has this id.")
    etag = f'"{_body_hash(data)}"'
    headers = {"ETag": etag, "Cache-Control": CACHE_CONTROL, "Vary": "Authorization"}
    if _matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    as_of = reader.iso_utc(datetime.now(timezone.utc))
    return JSONResponse(content={**data, "as_of": as_of}, headers=headers)


@router.get("/account-events", responses={200: {"model": TenantAccountEventPage}})
async def list_account_events(request: Request, after: Optional[str] = None, limit: Optional[str] = None,
                              caller: m2m_auth.M2MCaller = Depends(require_account_reader)) -> Response:
    try:
        page_size = feed.parse_limit(limit)
        feed.decode_cursor(after)
    except InvalidRequest as exc:
        raise M2MProblem(400, "invalid", str(exc)) from exc
    try:
        async with reader.account_connection(_pool(request)) as conn:
            body = await feed.list_events(conn, after, page_size)
    except reader.AccountUnavailable as exc:
        raise _unavailable(exc) from exc
    logger.info("tenant_account_served", op="listAccountEvents", client_id=caller.client_id, items=len(body["items"]))
    return JSONResponse(content=body, headers={"Cache-Control": NO_STORE})
