"""AA-1170 (S287, phase 1) — Platform › Compliance: readiness per framework (admin page /admin/platform/compliance).

  GET  /admin/platform/compliance                       Overview: Evidenced % per framework (strict, weighted, the
                                                        S286 reference), trend, stale / failing evidence, collector
                                                        heartbeat, settings in force; reads the published evidence file
                                                        first (cached)                              compliance.read
  GET  /admin/platform/compliance/controls              Controls: every control with its state and checks
                                                                                                    compliance.read
  GET  /admin/platform/compliance/controls/{control_id} the control drawer: requirements covered, checks, evidence
                                                        history; 404 for an unknown control         compliance.read
  GET  /admin/platform/compliance/obligations           AA-1174: the law obligations as a met / not met / not yet
                                                        checked checklist per law (counsel flag, evidence or document
                                                        link) and the applicability matrix          compliance.read
  POST /admin/platform/compliance/refresh               re-read the published evidence file now (at most every
                                                        30 s per process) and ingest it; AA-1174: also collect the
                                                        API's own sources (cis_db, crm_ops; each at most every 10
                                                        minutes); the answer carries the counts     compliance.write

AA-1174 (S288 lane contract rule 3): a GET never collects the live sources, and under a read-only "view as" session
(``request.state.staff_view_as``, set by staff auth) a GET writes nothing at all (no ingest, no snapshot upsert).

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
from api.services import compliance, compliance_obligations
from api.services.platform_apps import relay_authorization

router = APIRouter(prefix="/admin/platform/compliance", tags=["admin-platform-compliance"],
                   dependencies=[Depends(staff_policy)])

_NO_STORE = {"Cache-Control": "private, no-store"}
_CONTROL_ID = r"^[A-Z][A-Z0-9]{1,9}(-[A-Z0-9]{1,10}){1,3}$"


def _pool(request: Request) -> Any:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="Database pool is not ready")
    return pool


def _view_as(request: Request) -> bool:
    """A read-only "view as" session (staff auth sets the state; absent = a normal session)."""
    return bool(getattr(request.state, "staff_view_as", None))


async def _read_sync(request: Request, pool: Any) -> dict:
    if _view_as(request):
        return {"state": "skipped", "reason": "view_as_read_only"}
    return await compliance.sync(pool)


@router.get("", summary="Compliance readiness: Evidenced % per framework, trend, stale evidence, heartbeat (Dev)")
async def get_overview(request: Request):
    pool = _pool(request)
    state = await _read_sync(request, pool)
    return JSONResponse(await compliance.overview(pool, ingest_state=state, write=not _view_as(request)),
                        headers=_NO_STORE)


@router.get("/obligations", summary="Legal obligations per law (met / not met / not yet checked) and the "
                                    "applicability matrix; readiness, not legal advice")
async def get_obligations(request: Request):
    pool = _pool(request)
    state = await _read_sync(request, pool)
    body = await compliance_obligations.obligations(pool)
    body["ingest"] = state
    return JSONResponse(body, headers=_NO_STORE)


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


@router.post("/refresh", summary="Re-read the published compliance evidence file now, collect the API's own sources "
                                 "and ingest them")
async def post_refresh(request: Request):
    state = await compliance.sync(_pool(request), refresh=True, authorization=relay_authorization(request),
                                  collect_live=True)
    return JSONResponse({"ingest": state}, headers=_NO_STORE)
