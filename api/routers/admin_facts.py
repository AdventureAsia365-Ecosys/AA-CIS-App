"""AA-814 — Quality › Facts: staff CRUD of the platform Facts Entries (`acp_shared.facts`, scope 'platform').

  GET    /admin/facts?kind=&country=&q=&include_expired=   the platform facts, newest edit first, plus `kinds`
  POST   /admin/facts                                      {kind, title, body, provenance, country?, stated_on?,
                                                           valid_until?}: a new platform fact
  GET    /admin/facts/{fact_id}                            one platform fact
  PATCH  /admin/facts/{fact_id}                            the fields to change + expected_updated_at (409 when the
                                                           row changed since it was loaded)
  DELETE /admin/facts/{fact_id}                            remove it (the next writer seed no longer offers it)
  GET    /admin/facts/{fact_id}/history                    its audit rows (fact.created / updated / deleted), newest
                                                           first, with the changed fields

A platform fact reaches every tenant's T9 seed (services/acp_content_writing/facts.py), unless the tenant has its own
fact on the same subject (kind, title, country) or the fact is past valid_until. Tenants' own facts are not listed
here: they are tenant data (the tenant writes them in Content studio, /v1/facts).

Who may call (api/core/staff_authz.py POLICY): every read is open to every staff role (content.read); writes need
quality.write (admin only), like the place-card fact edits (AA-774). The viewer is refused on every write.
Every write leaves an acp_shared.audit_log row (tenant_id NULL, actor admin:<staff id>, details before / after) in the
same transaction. The router is included without an include prefix (staff_policy reads scope["route"].path).
"""
from __future__ import annotations

import json
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from api.core.staff_authz import staff_policy
from services.acp_shared import facts_store as fs
from services.acp_shared.audit_log import PlatformAuditAction, write_audit_log

logger = structlog.get_logger()
router = APIRouter(tags=["admin-facts"], dependencies=[Depends(staff_policy)])

_FACT_ID_MAX = 64


def _actor(x_admin_user_id: Optional[str]) -> str:
    return f"admin:{(x_admin_user_id or 'admin').strip()[:56] or 'admin'}"


def _fact_id(fact_id: str) -> str:
    if not fact_id or len(fact_id) > _FACT_ID_MAX:
        raise HTTPException(status_code=404, detail="Fact not found")
    return fact_id


def _pool(request: Request):
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="DB not ready")
    return pool


@router.get("/admin/facts")
async def list_platform_facts(request: Request, kind: Optional[fs.FactKind] = None,
                              country: Optional[str] = Query(None, max_length=fs.MAX_COUNTRY),
                              q: Optional[str] = Query(None, max_length=200), include_expired: bool = True):
    async with _pool(request).acquire() as conn:
        facts = await fs.list_facts(conn, scope="platform", kind=kind, country=country, q=q,
                                    include_expired=include_expired)
    return {"facts": facts, "kinds": list(fs.FACT_KINDS), "count": len(facts)}


@router.post("/admin/facts", status_code=201)
async def create_platform_fact(body: fs.FactFields, request: Request,
                               x_admin_user_id: Optional[str] = Header(None)):
    actor = _actor(x_admin_user_id)
    async with _pool(request).acquire() as conn, conn.transaction():
        try:
            fact = await fs.create_fact(conn, body, scope="platform", tenant_id=None, actor=actor)
        except fs.FactError as e:
            raise HTTPException(status_code=422, detail=str(e))
        await write_audit_log(
            conn, tenant_id=None, actor=actor, action=PlatformAuditAction.FACT_CREATED, resource_type="fact",
            resource_id=fact["fact_id"], details={"scope": "platform", "after": body.model_dump(mode="json")})
    logger.info("admin_fact_created", fact_id=fact["fact_id"], kind=fact["kind"])
    return fact


@router.get("/admin/facts/{fact_id}")
async def get_platform_fact(fact_id: str, request: Request):
    async with _pool(request).acquire() as conn:
        row = await fs.get_fact(conn, _fact_id(fact_id), scope="platform")
    if row is None:
        raise HTTPException(status_code=404, detail="Fact not found")
    return fs.to_api(row)


@router.patch("/admin/facts/{fact_id}")
async def update_platform_fact(fact_id: str, body: fs.FactPatch, request: Request,
                               x_admin_user_id: Optional[str] = Header(None)):
    actor = _actor(x_admin_user_id)
    fid = _fact_id(fact_id)
    async with _pool(request).acquire() as conn, conn.transaction():
        try:
            res = await fs.update_fact(conn, fid, body, scope="platform", tenant_id=None, actor=actor)
        except fs.FactConflict as e:
            raise HTTPException(status_code=409, detail=str(e))
        except fs.FactError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if res is None:
            raise HTTPException(status_code=404, detail="Fact not found")
        fact, changes = res
        await write_audit_log(
            conn, tenant_id=None, actor=actor, action=PlatformAuditAction.FACT_UPDATED, resource_type="fact",
            resource_id=fid, details={"scope": "platform", "changes": changes})
    logger.info("admin_fact_updated", fact_id=fid, fields=sorted(changes))
    return fact


@router.delete("/admin/facts/{fact_id}")
async def delete_platform_fact(fact_id: str, request: Request, x_admin_user_id: Optional[str] = Header(None)):
    actor = _actor(x_admin_user_id)
    fid = _fact_id(fact_id)
    async with _pool(request).acquire() as conn, conn.transaction():
        before = await fs.delete_fact(conn, fid, scope="platform", tenant_id=None)
        if before is None:
            raise HTTPException(status_code=404, detail="Fact not found")
        await write_audit_log(
            conn, tenant_id=None, actor=actor, action=PlatformAuditAction.FACT_DELETED, resource_type="fact",
            resource_id=fid, details={"scope": "platform", "before": before})
    logger.info("admin_fact_deleted", fact_id=fid)
    return {"fact_id": fid, "deleted": True}


_HISTORY_SQL = """
    SELECT action, actor, details, created_at
    FROM acp_shared.audit_log
    WHERE resource_type = 'fact' AND resource_id = $1 AND tenant_id IS NULL
      AND action IN ('fact.created', 'fact.updated', 'fact.deleted')
    ORDER BY created_at DESC
    LIMIT 100
"""


@router.get("/admin/facts/{fact_id}/history")
async def platform_fact_history(fact_id: str, request: Request):
    """The audit rows of one platform fact (also after it was deleted)."""
    async with _pool(request).acquire() as conn:
        rows = await conn.fetch(_HISTORY_SQL, _fact_id(fact_id))
    out = []
    for r in rows:
        details = r["details"]
        if isinstance(details, str):
            details = json.loads(details or "{}")
        out.append({"action": r["action"], "actor": r["actor"], "details": details or {},
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None})
    return {"fact_id": fact_id, "history": out}
