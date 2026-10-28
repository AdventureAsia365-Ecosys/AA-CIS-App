"""
/v1/account/members — AA-978 follow-up: the tenant portal's Account › Members page (AA-Partner ``portal/``).

  GET /v1/account/members   the agency's current Content studio memberships (invited + active) from the CIS
                            membership directory (``shared.tenant_members``, CIS 276), read only

Tenant-JWT-gated (``get_tenant``; a staff token is 403). The tenant id comes ONLY from the verified token. The read
runs on the main pool (cis_app): CIS 276 grants the directory to cis_app only, so the tenant pool cannot read it
(the /v1/billing pattern). What a row shows: ``services.tenant_membership.portal`` (no raw identity, no staff actor,
no CRM role, no e-mail or name: the directory has none). Changes stay with AA staff (Tenant 360 › Members) until
AA-979 brings invitations; CRM roles are managed in the partner CRM.

Its own router file (same ``/v1/account`` prefix as AA-761's ``v1_account.py``) so the two changes do not overlap.
"""
from __future__ import annotations

import uuid
from typing import Literal, Optional

import asyncpg
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from api.routers.v1_tours import get_tenant
from services.tenant_membership import portal

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/account", tags=["tenant-account"])


class AccountMember(BaseModel):
    membership_id: str
    member_ref: Optional[str] = None
    role: Literal["owner", "editor", "viewer"]
    status: Literal["invited", "active"]
    added_at: str
    updated_at: str


class AccountMembers(BaseModel):
    data: list[AccountMember]
    total: int
    active: int
    invited: int


def _tenant_id(tenant: dict) -> str:
    sub = tenant.get("sub")
    if not isinstance(sub, str) or not sub:
        raise HTTPException(status_code=403, detail="Tenant token required")
    try:
        return str(uuid.UUID(sub))
    except ValueError:
        raise HTTPException(status_code=403, detail="Tenant token required")


@router.get("/members", response_model=AccountMembers,
            summary="The agency's Content studio members (invited and active), read only")
async def list_members(request: Request, tenant=Depends(get_tenant)):
    tenant_id = _tenant_id(tenant)
    pool = request.app.state.pool
    try:
        async with pool.acquire() as conn:
            body = await portal.list_for_tenant(conn, tenant_id)
    except asyncpg.UndefinedTableError as exc:
        logger.error("tenant_account_members_unavailable", reason=type(exc).__name__)
        raise HTTPException(status_code=503, detail="Members are not available yet.") from exc
    logger.info("tenant_account_members_listed", tenant_id=tenant_id, total=body["total"])
    return body
