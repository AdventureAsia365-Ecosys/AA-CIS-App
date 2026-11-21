"""AA-1170 (S287, phase 1) — Platform › Compliance: readiness per framework for the admin page /admin/platform/compliance.

  GET  /admin/platform/compliance                       Overview: Evidenced % per framework (strict, weighted, the
                                                        S286 reference), trend, stale / failing evidence, collector
                                                        heartbeat, settings in force; reads the published evidence file
                                                        first (cached)                              compliance.read
  GET  /admin/platform/compliance/controls              Controls: every control with its state and checks
                                                                                                    compliance.read
  GET  /admin/platform/compliance/controls/{control_id} the control drawer: requirements covered, checks, evidence
                                                        history; 404 for an unknown control         compliance.read
  POST /admin/platform/compliance/refresh               re-read the published evidence file now (at most every
                                                        30 s per process) and ingest it             compliance.write

Tech lead S287 Q10: compliance.read / compliance.write, administrator only at first (api/core/staff_authz.py
ROLE_PERMS; a custom role may be given them on Roles & permissions). The BFF refuses other roles first
(frontend/lib/rbac.ts API_READ_RULES / API_RULES "platform/compliance"). Every number is a Dev readiness number, not a
certification (the page says so). Logic: api/services/compliance.py and compliance_score.py.

Included in main.py WITHOUT an include prefix: staff_policy reads the route template from scope["route"].path.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse

from api.core.staff_authz import staff_policy
from api.services import compliance

router = APIRouter(prefix="/admin/platform/compliance", tags=["admin-platform-compliance"],
                   dependencies=[Depends(staff_policy)])

_NO_STORE = {"Cache-Control": "private, no-store"}
_CONTROL_ID = r"^[A-Z][A-Z0-9]{1,9}(-[A-Z0-9]{1,10}){1,3}$"


def _pool(request: Request) -> Any:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="Database pool is not ready")
    return pool


@router.get("", summary="Compliance readiness: Evidenced % per framework, trend, stale evidence, heartbeat (Dev)")
async def get_overview(request: Request):
    pool = _pool(request)
    state = await compliance.sync(pool)
    return JSONResponse(await compliance.overview(pool, ingest_state=state), headers=_NO_STORE)


@router.get("/controls", summary="Compliance controls with their state and checks")
async def get_controls(request: Request):
    return JSONResponse(await compliance.controls(_pool(request)), headers=_NO_STORE)


@router.get("/controls/{control_id}", summary="One control: requirements covered, checks and evidence history")
async def get_control(request: Request, control_id: str = Path(..., pattern=_CONTROL_ID, max_length=40)):
    try:
        body = await compliance.control_detail(_pool(request), control_id)
    except compliance.ComplianceMissing as e:
        raise HTTPException(status_code=404, detail=str(e))
    return JSONResponse(body, headers=_NO_STORE)


@router.post("/refresh", summary="Re-read the published compliance evidence file now and ingest it")
async def post_refresh(request: Request):
    state = await compliance.sync(_pool(request), refresh=True)
    return JSONResponse({"ingest": state}, headers=_NO_STORE)
