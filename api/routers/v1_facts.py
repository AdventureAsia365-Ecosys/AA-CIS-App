"""AA-814 — /v1/facts: a tenant's own Facts Entries (Content studio › Facts), plus the platform facts it can override.

  GET    /v1/facts            {"own": [...], "platform": [...], "kinds": [...]}: the tenant's own facts (also expired
                              ones, flagged `expired`) and the platform facts that are still valid. A platform fact the
                              tenant replaces carries `overridden_by` (the own fact on the same subject).
  POST   /v1/facts            {kind, title, body, provenance, country?, stated_on?, valid_until?}: a new own fact
  PATCH  /v1/facts/{fact_id}  the fields to change + expected_updated_at (409 when it changed since it was loaded)
  DELETE /v1/facts/{fact_id}  remove an own fact

What the writer does with them (services/acp_content_writing/facts.py): the next T9 write offers every valid platform
fact and the tenant's own valid facts; an own fact replaces the platform facts on the same subject (kind, title,
country). To override a platform fact, the tenant writes an own fact with the same kind, title and country (the
Content studio "Use my own" action pre-fills them).

Isolation. Tenant JWT (`get_tenant`); the tenant id comes from the token, never from the body or the path. Every query
runs on the tenant pool (aa_app_user) through acquire_scoped_conn(FACTS_FLAG, fail closed): RLS `tenant_isolation`
(CIS 229) admits only the tenant's own scope='tenant' rows for every command and `facts_platform_read` the platform
rows for SELECT; DELETE needs CIS 302. The statements also filter scope and tenant_id themselves. Another tenant's
fact, or a platform fact, is a 404 on PATCH / DELETE. Content studio roles (api/middleware/tenant_role.py): a viewer
reads only (every write is a 403 before the route runs); editor and owner may write.

Audit (AA-859 MAIN exception): aa_app_user has no grant on acp_shared.audit_log, so the fact.* row is written on the
main pool right after the tenant transaction committed (best effort: a lost audit row is logged, the write stands).
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from api.core.aa544_tenant_pool import FACTS_FLAG, acquire_scoped_conn
from api.routers.v1_tours import get_tenant
from services.acp_shared import facts_store as fs
from services.acp_shared.audit_log import TenantAuditAction, write_audit_log

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/facts", tags=["facts"])

_FACT_ID_MAX = 64


class TenantFact(BaseModel):
    """One fact as Content studio sees it: content only (no editor ids, no tenant id)."""
    fact_id: str
    scope: str                         # 'tenant' (the agency's own) | 'platform' (Adventure Asia's, read only)
    kind: str
    title: str
    body: str
    provenance: str
    country: Optional[str] = None
    stated_on: Optional[str] = None    # ISO date
    valid_until: Optional[str] = None  # ISO date; after it the writers skip the fact
    created_at: Optional[str] = None
    updated_at: Optional[str] = None   # send back as expected_updated_at on PATCH
    expired: bool = False
    overridden_by: Optional[str] = None  # platform facts only: the own fact that replaces it in the writer seed


class TenantFactList(BaseModel):
    own: list[TenantFact]
    platform: list[TenantFact]
    kinds: list[str]
    max_own: int


class TenantFactDeleted(BaseModel):
    fact_id: str
    deleted: bool


def _tenant_uuid(tenant: dict) -> UUID:
    try:
        return UUID(str(tenant.get("sub")))
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid or expired token")


def _updated_by(tenant: dict, tenant_id: UUID) -> str:
    mid = tenant.get("mid")
    return f"tenant:{tenant_id}:{mid}" if mid else f"tenant:{tenant_id}"


def _fact_id(fact_id: str) -> str:
    if not fact_id or len(fact_id) > _FACT_ID_MAX:
        raise HTTPException(status_code=404, detail="Fact not found")
    return fact_id


def _public(fact: dict) -> dict:
    """What a tenant sees of a fact: content only (no internal editor ids)."""
    return {k: v for k, v in fact.items() if k not in ("updated_by", "tenant_id")}


async def _audit(request: Request, tenant: dict, tenant_id: UUID, action: str, fact_id: str, details: dict) -> None:
    try:
        await write_audit_log(
            request.app.state.pool, tenant_id=str(tenant_id), actor=f"tenant:{tenant_id}", action=action,
            resource_type="fact", resource_id=fact_id,
            details={**details, "scope": "tenant", "membership_id": tenant.get("mid")})
    except Exception as exc:  # noqa: BLE001 - the write already committed; a lost audit row must not undo it
        logger.warning("tenant_fact_audit_failed", tenant_id=str(tenant_id), fact_id=fact_id, action=action,
                       error=str(exc)[:200])


@router.get("", response_model=TenantFactList)
async def list_my_facts(request: Request, tenant=Depends(get_tenant)):
    tenant_id = _tenant_uuid(tenant)
    async with acquire_scoped_conn(request, str(tenant_id), FACTS_FLAG, fail_closed=True) as (conn, used_tenant_pool):
        own = await fs.list_facts(conn, scope="tenant", tenant_id=tenant_id)
        platform = await fs.list_facts(conn, scope="platform", include_expired=False)
    logger.info("aa814_facts_read", tenant_id=str(tenant_id), used_tenant_pool=used_tenant_pool, own=len(own),
                platform=len(platform))
    live_own = {fs.subject_key(f): f["fact_id"] for f in own if not f["expired"]}
    for f in platform:
        f["overridden_by"] = live_own.get(fs.subject_key(f))
    return {"own": [_public(f) for f in own], "platform": [_public(f) for f in platform],
            "kinds": list(fs.FACT_KINDS), "max_own": fs.MAX_TENANT_FACTS}


@router.post("", status_code=201, response_model=TenantFact)
async def create_my_fact(body: fs.FactFields, request: Request, tenant=Depends(get_tenant)):
    tenant_id = _tenant_uuid(tenant)
    async with acquire_scoped_conn(request, str(tenant_id), FACTS_FLAG, fail_closed=True) as (conn, used_tenant_pool):
        try:
            fact = await fs.create_fact(conn, body, scope="tenant", tenant_id=tenant_id,
                                        actor=_updated_by(tenant, tenant_id))
        except fs.FactError as e:
            raise HTTPException(status_code=422, detail=str(e))
    logger.info("aa814_fact_created", tenant_id=str(tenant_id), fact_id=fact["fact_id"], kind=fact["kind"],
                used_tenant_pool=used_tenant_pool)
    await _audit(request, tenant, tenant_id, TenantAuditAction.FACT_CREATED, fact["fact_id"],
                 {"title": fact["title"], "after": body.model_dump(mode="json")})
    return _public(fact)


@router.patch("/{fact_id}", response_model=TenantFact)
async def update_my_fact(fact_id: str, body: fs.FactPatch, request: Request, tenant=Depends(get_tenant)):
    tenant_id = _tenant_uuid(tenant)
    fid = _fact_id(fact_id)
    async with acquire_scoped_conn(request, str(tenant_id), FACTS_FLAG, fail_closed=True) as (conn, used_tenant_pool):
        try:
            res = await fs.update_fact(conn, fid, body, scope="tenant", tenant_id=tenant_id,
                                       actor=_updated_by(tenant, tenant_id))
        except fs.FactConflict as e:
            raise HTTPException(status_code=409, detail=str(e))
        except fs.FactError as e:
            raise HTTPException(status_code=422, detail=str(e))
    if res is None:
        raise HTTPException(status_code=404, detail="Fact not found")
    fact, changes = res
    logger.info("aa814_fact_updated", tenant_id=str(tenant_id), fact_id=fid, fields=sorted(changes),
                used_tenant_pool=used_tenant_pool)
    await _audit(request, tenant, tenant_id, TenantAuditAction.FACT_UPDATED, fid,
                 {"title": fact["title"], "changes": changes})
    return _public(fact)


@router.delete("/{fact_id}", response_model=TenantFactDeleted)
async def delete_my_fact(fact_id: str, request: Request, tenant=Depends(get_tenant)):
    tenant_id = _tenant_uuid(tenant)
    fid = _fact_id(fact_id)
    async with acquire_scoped_conn(request, str(tenant_id), FACTS_FLAG, fail_closed=True) as (conn, used_tenant_pool):
        before = await fs.delete_fact(conn, fid, scope="tenant", tenant_id=tenant_id)
    if before is None:
        raise HTTPException(status_code=404, detail="Fact not found")
    logger.info("aa814_fact_deleted", tenant_id=str(tenant_id), fact_id=fid, used_tenant_pool=used_tenant_pool)
    await _audit(request, tenant, tenant_id, TenantAuditAction.FACT_DELETED, fid,
                 {"title": before.get("title"), "before": before})
    return {"fact_id": fid, "deleted": True}
