"""tenant.membership.v1 — who belongs to which agency, for the partner CRM (AA-978, CRM-P1).

Contract: root ``docs/contracts/tenant.membership.v1.openapi.yaml`` (copy in ``docs/contracts/`` of this repo, checked
by ``tests/unit/test_aa978_tenant_membership_contract.py``). Root ADR 0006b decision 4 / ADR 0010 decision 5: CIS is
the directory of agencies and their members; the CRM uses it as a revocation and lifecycle source (removals and
locked / unlocked / deleted agencies), never to grant a CRM role.

  GET /tenant/membership/v1/tenants/{tenant_id}/members    snapshot of one agency (invited + active members and
                                                           tenant_state), ETag + If-None-Match -> 304
  GET /tenant/membership/v1/memberships?iss=&sub=          the memberships of one identity, keyed by iss AND sub
  GET /tenant/membership/v1/events?after=&limit=           cursor feed of tenant.* and membership.* events, oldest
                                                           first, never skipping a late commit (services/
                                                           tenant_membership/feed.py)
  POST /tenant/membership/v1/invitations/{invitation_id}/activate   (contract 1.1, AA-979 P2b) the CRM binds the
                                                           partner-pool identity of an accepted studio invitation:
                                                           body {"identity": {iss, sub}}, scope cis.tenant/activate

Storage: ``shared.tenant_members`` and ``shared.tenant_membership_event`` (CIS 276), read on the main pool (cis_app),
one READ ONLY transaction per request. 503 while the migration is not applied.

Auth: an M2M client-credentials token of the ``aa-m2m-<env>`` pool, client = AA-Partner (CRM), scope
``cis.tenant/read`` (``api/core/m2m_auth.require_m2m``). 401 no / bad token, 403 another client or a token without
the scope (a ``cis.catalog/read``-only token included), 503 key set or configuration unavailable. ``/tenant`` is not a
staff path and not ``/v1``, so neither the staff middleware nor the tenant rate limiter touches these routes.

Caching: the snapshot carries a strong ETag over the body without ``as_of`` (so an unchanged agency keeps its tag),
``Cache-Control: private, no-cache`` and ``Vary: Authorization``. The lookup and the feed are never cached. Errors use
the contract Problem body ``{error, message}``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg
import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from api.core import m2m_auth
from api.core.m2m_auth import M2MProblem
from api.schemas.public_api import (  # AA-899: doc only
    MembershipActivated, MembershipEventPage, MembershipIdentityMemberships, MembershipTenantMembers,
)
from services.tenant_membership import feed
from services.tenant_membership import reader
from services.tenant_membership import session as studio

logger = structlog.get_logger()
router = APIRouter(prefix="/tenant/membership/v1", tags=["tenant-membership"])

CACHE_CONTROL = "private, no-cache"
NO_STORE = "no-store"

require_tenant_reader = m2m_auth.require_m2m(
    m2m_auth.SCOPE_TENANT_READ, realm="tenant.membership.v1",
    refused="This client may not read tenant memberships.", log_prefix="tenant_membership")


require_tenant_activator = m2m_auth.require_m2m(
    m2m_auth.SCOPE_TENANT_ACTIVATE, realm="tenant.membership.v1",
    refused="This client may not activate memberships.", log_prefix="tenant_membership_activate")

ACTIVATE_ACTOR = "m2m:crm"


def _unavailable(exc: Exception) -> M2MProblem:
    logger.error("tenant_membership_db_unavailable", reason=str(exc))
    return M2MProblem(503, "unavailable", "Memberships are unavailable; retry later.", headers={"Retry-After": "5"})


def _invalid(exc: reader.InvalidRequest) -> M2MProblem:
    return M2MProblem(400, "invalid", str(exc))


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


def _now() -> str:
    return reader.iso_utc(datetime.now(timezone.utc))


@router.get("/tenants/{tenant_id}/members", responses={200: {"model": MembershipTenantMembers}, 304: {}})
async def get_tenant_members(tenant_id: str, request: Request,
                             caller: m2m_auth.M2MCaller = Depends(require_tenant_reader)) -> Response:
    key = reader.parse_tenant_id(tenant_id)
    if key is None:
        raise M2MProblem(404, "not_found", "No agency has this id.")
    try:
        async with reader.membership_connection(_pool(request)) as conn:
            data = await reader.tenant_members(conn, key)
    except reader.MembershipUnavailable as exc:
        raise _unavailable(exc) from exc
    logger.info("tenant_membership_served", op="getTenantMembers", client_id=caller.client_id, tenant_id=key,
                found=data is not None, members=len(data["members"]) if data else 0)
    if data is None:
        raise M2MProblem(404, "not_found", "No agency has this id.")
    etag = f'"{_body_hash(data)}"'
    headers = {"ETag": etag, "Cache-Control": CACHE_CONTROL, "Vary": "Authorization"}
    if _matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return JSONResponse(content={**data, "as_of": _now()}, headers=headers)


@router.get("/memberships", responses={200: {"model": MembershipIdentityMemberships}})
async def find_memberships(request: Request, iss: Optional[str] = None, sub: Optional[str] = None,
                           caller: m2m_auth.M2MCaller = Depends(require_tenant_reader)) -> Response:
    try:
        iss_value, sub_value = reader.check_identity(iss, sub)
    except reader.InvalidRequest as exc:
        raise _invalid(exc) from exc
    try:
        async with reader.membership_connection(_pool(request)) as conn:
            body = await reader.identity_memberships(conn, iss_value, sub_value)
    except reader.MembershipUnavailable as exc:
        raise _unavailable(exc) from exc
    # No identity in the log line: the count says what was served.
    logger.info("tenant_membership_served", op="findMemberships", client_id=caller.client_id,
                memberships=len(body["memberships"]))
    return JSONResponse(content=body, headers={"Cache-Control": NO_STORE})


@router.get("/events", responses={200: {"model": MembershipEventPage}})
async def list_membership_events(request: Request, after: Optional[str] = None, limit: Optional[str] = None,
                                 caller: m2m_auth.M2MCaller = Depends(require_tenant_reader)) -> Response:
    try:
        page_size = feed.parse_limit(limit)
        feed.decode_cursor(after)
    except reader.InvalidRequest as exc:
        raise _invalid(exc) from exc
    try:
        async with reader.membership_connection(_pool(request)) as conn:
            body = await feed.list_events(conn, after, page_size)
    except reader.MembershipUnavailable as exc:
        raise _unavailable(exc) from exc
    logger.info("tenant_membership_served", op="listMembershipEvents", client_id=caller.client_id,
                items=len(body["items"]))
    return JSONResponse(content=body, headers={"Cache-Control": NO_STORE})


class _Identity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    iss: str
    sub: str


class _ActivateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    identity: _Identity


@router.post("/invitations/{invitation_id}/activate", responses={200: {"model": MembershipActivated}})
async def activate_invitation(invitation_id: str, request: Request,
                              caller: m2m_auth.M2MCaller = Depends(require_tenant_activator)) -> Response:
    """AA-979 P2b (contract 1.1): bind the accepted person's partner-pool identity to the invited membership.

    The identity must be of the partner pool (``api.core.partner_identity.partner_issuer``): a Content studio member
    signs in there and nowhere else. 200 with the Membership (``changed`` false for the same identity again: the CRM's
    retry), 404 unknown invitation, 409 ``conflict`` with ``reason`` identity_mismatch / membership_removed /
    already_member, 400 a body the contract does not accept."""
    from api.core.partner_identity import partner_issuer

    key = reader.parse_tenant_id(invitation_id)
    if key is None:
        raise M2MProblem(404, "not_found", "No membership has this invitation id.")
    try:
        body = _ActivateIn.model_validate_json(await request.body())
        iss, sub = reader.check_identity(body.identity.iss, body.identity.sub)
    except (ValidationError, ValueError) as exc:
        raise M2MProblem(400, "invalid", "The body must be {\"identity\": {\"iss\", \"sub\"}}.") from exc
    if iss != partner_issuer():
        raise M2MProblem(400, "invalid", "The identity must be of the partner pool.")
    pool = _pool(request)
    if pool is None:
        raise _unavailable(RuntimeError("pool not configured"))
    try:
        async with pool.acquire(timeout=reader.ACQUIRE_TIMEOUT_S) as conn:
            row, changed = await studio.activate(conn, key, iss, sub, ACTIVATE_ACTOR)
    except studio.InvitationNotFound as exc:
        raise M2MProblem(404, "not_found", "No membership has this invitation id.") from exc
    except studio.InvitationClosed as exc:
        logger.info("tenant_membership_activate_refused", reason=exc.code, client_id=caller.client_id)
        raise M2MProblem(409, "conflict", "This invitation cannot be activated for this identity.",
                         reason=exc.code) from exc
    except (asyncpg.UndefinedTableError, asyncpg.PostgresConnectionError, asyncpg.InterfaceError,
            asyncpg.TooManyConnectionsError, OSError, TimeoutError) as exc:
        raise _unavailable(exc) from exc
    logger.info("tenant_membership_activated", client_id=caller.client_id, tenant_id=str(row["tenant_id"]),
                membership_id=str(row["membership_id"]), changed=changed)
    return JSONResponse(content={"membership": reader.membership_body(row), "changed": changed},
                        headers={"Cache-Control": NO_STORE})
