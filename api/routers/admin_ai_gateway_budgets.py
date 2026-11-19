"""AA-1143 (tech lead decision G3, S285; CIS 455 / 456) — one budget per LLM connection, edited on AI gateway › Budgets
(frontend ConnectionBudgetsSection.tsx; the service api/services/llm_connection_budgets.py).

  GET  /admin/ai-gateway/connection-budgets                   every connection: total, each app's share, the CIS
                                                              remainder, this month's spend, the CRM's copy. spend.read
  GET  /admin/ai-gateway/connection-budgets/{key}/history     the versions of one connection's rows.        spend.read
  PUT  /admin/ai-gateway/connection-budgets/{key}             change the total and / or apps' shares (reason 20..500
                                                              characters, audited, versioned; a tighten-only role may
                                                              only lower a cap).                           spend.write
  POST /admin/ai-gateway/connection-budgets/crm-sync          send the CRM its copy again.                 spend.write

The literal ``crm-sync`` path is declared before the ``{key}`` routes of the same method.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from api.core.staff_authz import staff_policy
from api.routers.admin import verify_admin_secret
from api.services import llm_connection_budgets as budgets
from api.services import llm_connection_split as rules
from api.services.platform_apps import relay_authorization

router = APIRouter(prefix="/admin/ai-gateway", tags=["admin-ai-gateway"],
                   dependencies=[Depends(verify_admin_secret), Depends(staff_policy)])

_KEY = Path(..., pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")


def _actor(x_admin_user_id: Optional[str]) -> str:
    return f"admin:{x_admin_user_id}" if x_admin_user_id else "admin"


class ConnectionBudgetPut(BaseModel):
    """``total_usd`` null = unchanged; ``shares`` {app: USD | null (remove the share)} = only the apps that change."""
    model_config = ConfigDict(extra="forbid")
    total_usd: Optional[float] = Field(None, ge=0, le=1_000_000)
    shares: dict[str, Optional[float]] = Field(default_factory=dict)
    reason: str = Field(..., min_length=rules.REASON_MIN, max_length=rules.REASON_MAX)


@router.get("/connection-budgets", summary="Each LLM connection's budget: total, each app's share, the CIS remainder "
                                           "and this month's spend ($0)")
async def get_connection_budgets(request: Request) -> dict:
    return await budgets.read(request.app.state.pool, relay_authorization(request))


@router.post("/connection-budgets/crm-sync", summary="Send the partner CRM its copy of its connection shares again "
                                                     "(audited)")
async def post_crm_sync(request: Request, x_admin_user_id: Optional[str] = Header(None)) -> dict:
    try:
        out = await budgets.sync_crm(request.app.state.pool, relay_authorization(request),
                                     actor=_actor(x_admin_user_id))
    except budgets.BudgetsUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    if not out["sent"]:
        raise HTTPException(status_code=502, detail=f"the partner CRM did not take its shares: {out['error']}")
    return out


@router.get("/connection-budgets/{connection_key}/history", summary="The versions of one connection's budget rows "
                                                                    "($0)")
async def get_connection_budget_history(request: Request, connection_key: str = _KEY) -> dict:
    try:
        return await budgets.history(request.app.state.pool, connection_key)
    except budgets.BudgetsUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.put("/connection-budgets/{connection_key}", summary="Change a connection's total and / or apps' shares; CIS "
                                                            "takes the remainder (audited)")
async def put_connection_budget(body: ConnectionBudgetPut, request: Request, connection_key: str = _KEY,
                                x_admin_user_id: Optional[str] = Header(None),
                                x_admin_role: Optional[str] = Header(None)) -> dict:
    try:
        return await budgets.save(request.app.state.pool, relay_authorization(request), connection_key,
                                  total=body.total_usd, shares=body.shares, reason=body.reason,
                                  actor=_actor(x_admin_user_id), role=x_admin_role)
    except LookupError:
        raise HTTPException(status_code=404, detail="no such LLM connection")
    except rules.BudgetEditRefused as e:
        raise HTTPException(status_code=422, detail={"code": e.code, "message": str(e)})
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except budgets.BudgetsUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except budgets.CrmRefused as e:
        raise HTTPException(status_code=502, detail={
            "code": "crm_refused", "message": f"the partner CRM did not take the lower share ({e}); nothing changed"})
