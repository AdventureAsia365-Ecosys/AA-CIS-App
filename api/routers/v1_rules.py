"""
/v1/rules — Output rules management API.

Routes (specific before parameterized):
  GET  /v1/rules              → list rules (filter by stage, tenant_id)
  PATCH /v1/rules/{rule_id}   → toggle active state
"""
import os
from typing import Optional
from uuid import UUID

import asyncpg
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.security import HTTPBearer as _HTTPBearer, HTTPAuthorizationCredentials as _Creds
from pydantic import BaseModel

from api.routers.auth import verify_jwt as _verify_jwt
from api.core.secret_compare import secrets_equal

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/rules", tags=["Rules"])


# ── Auth ──────────────────────────────────────────────────────────────────────

def _get_admin(
    request: Request,
    credentials: Optional[_Creds] = Depends(_HTTPBearer(auto_error=False)),
):
    admin_secret = os.environ.get("ADMIN_SECRET", "")
    x_admin = request.headers.get("X-Admin-Secret", "")
    if admin_secret and secrets_equal(x_admin, admin_secret):
        return {"sub": "00000000-0000-0000-0000-000000000001", "role": "admin"}
    if credentials:
        try:
            return _verify_jwt(credentials.credentials)
        except Exception:
            pass
    raise HTTPException(status_code=401, detail="Not authenticated")


# ── DB helper ─────────────────────────────────────────────────────────────────

def _get_pool(request: Request) -> asyncpg.Pool:
    pool = getattr(request.app.state, "pool", None)
    if not pool:
        raise HTTPException(status_code=503, detail="DB not ready")
    return pool


# ── Pydantic ──────────────────────────────────────────────────────────────────

class PatchRuleRequest(BaseModel):
    active: bool


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("")
async def list_rules(
    request: Request,
    stage: Optional[str] = Query(None, description="Filter by stage (e.g. S4, S3)"),
    tenant_id: Optional[str] = Query(None, description="Filter by tenant UUID"),
    _auth=Depends(_get_admin),
):
    pool = _get_pool(request)
    async with pool.acquire() as conn:
        query = """
            SELECT rule_id::text, tenant_id::text, stage, rule_type,
                   pattern, action_value, error_message,
                   source_type, run_count, is_active, created_at
            FROM acp_shared.acp_output_rules
            WHERE 1=1
        """
        params = []
        if stage:
            params.append(stage)
            query += f" AND (stage = ${len(params)} OR stage IS NULL)"
        if tenant_id:
            try:
                UUID(tenant_id)
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid tenant_id UUID")
            params.append(tenant_id)
            query += f" AND (tenant_id = ${len(params)} OR tenant_id IS NULL)"
        query += " ORDER BY source_type ASC, created_at ASC"
        rows = await conn.fetch(query, *params)

    return [
        {
            "rule_id": r["rule_id"],
            "tenant_id": r["tenant_id"],
            "stage": r["stage"],
            "rule_type": r["rule_type"],
            "pattern": r["pattern"],
            "action_value": r["action_value"],
            "error_message": r["error_message"],
            "source_type": r["source_type"],
            "run_count": r["run_count"],
            "active": r["is_active"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]


@router.patch("/{rule_id}")
async def toggle_rule(
    rule_id: str,
    request: Request,
    body: PatchRuleRequest,
    _auth=Depends(_get_admin),
):
    try:
        UUID(rule_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid rule_id UUID")

    pool = _get_pool(request)
    async with pool.acquire() as conn:
        result = await conn.fetchrow(
            """UPDATE acp_shared.acp_output_rules
               SET is_active = $1
               WHERE rule_id = $2::uuid
               RETURNING rule_id::text, is_active""",
            body.active,
            rule_id,
        )
    if not result:
        raise HTTPException(status_code=404, detail="Rule not found")

    logger.info("rule_toggled", rule_id=rule_id, active=body.active)
    return {"rule_id": result["rule_id"], "active": result["is_active"]}
