"""AA-1126 (S283) — the Spend controls section of AI gateway › Budgets (frontend SpendControlsSection.tsx).

  GET    /admin/ai-gateway/spend-controls               the gateway's spend controls of this environment: the
                                                        cis.llm.* / cis.spend.run_cap_* settings in force (each with its
                                                        Platform › Settings link: the one place they change) and every
                                                        app's anomaly breaker verdict.                     spend.read
  POST   /admin/ai-gateway/anomaly-breaker/{app}/reset  reset one app's anomaly breaker: its spend is counted from now
                                                        for the rest of the UTC day (audited llm_anomaly_breaker_reset;
                                                        refused to a tighten-only role: a reset loosens a stop).
                                                                                                             ai.admin

Admin only (staff_authz POLICY; the page-access hint ``anomaly_reset`` is in admin_ai_gateway.PAGE_WRITES). The
sources: api/services/ai_gateway_spend_controls.py.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request

from api.core.staff_authz import staff_policy, require_cognito_session
from api.routers.admin import verify_admin_secret
from api.services import ai_gateway_spend_controls, settings_registry
from shared.llm_client import app_profile

router = APIRouter(prefix="/admin/ai-gateway", tags=["admin-ai-gateway"],
                   dependencies=[Depends(verify_admin_secret), Depends(staff_policy),
                                 Depends(require_cognito_session)])   # AA-1155 T11: Cognito only

_APP = Path(..., pattern=r"^[a-z][a-z0-9_]{0,30}$")


def _actor(x_admin_user_id: Optional[str]) -> str:
    return f"admin:{x_admin_user_id}" if x_admin_user_id else "admin"


@router.get("/spend-controls", summary="The gateway's spend controls of this environment (connection cap mode, "
                                       "anomaly breaker, run caps: settings in force) and each app's breaker ($0)")
async def get_spend_controls(request: Request) -> dict:
    return await ai_gateway_spend_controls.read(request.app.state.pool)


@router.post("/anomaly-breaker/{app}/reset", summary="Reset one app's AI anomaly breaker: its spend is counted from "
                                                     "now for the rest of the UTC day (audited)")
async def reset_anomaly_breaker(request: Request, app: str = _APP, x_admin_user_id: Optional[str] = Header(None),
                                x_admin_role: Optional[str] = Header(None)) -> dict:
    # A reset lets a stopped app spend again: a loosening, so a tighten-only role (S279 thresholds L1) may not.
    if settings_registry.tighten_only(x_admin_role):
        raise HTTPException(status_code=403, detail="resetting the anomaly breaker loosens a stop: an administrator "
                                                    "does it")
    try:
        return await ai_gateway_spend_controls.reset(request.app.state.pool, app, actor=_actor(x_admin_user_id))
    except LookupError:
        raise HTTPException(status_code=404, detail="no such app")
    except app_profile.AppProfileUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
