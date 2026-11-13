"""S279 thresholds L1 — Platform › Settings: the admin-editable settings registry (CIS 407).

  GET  /admin/settings/registry?app=cis&area=            every setting of the app with the value in force, its source
                                                          (env / tenant / platform / default), bounds, last change and
                                                          open proposal
  GET  /admin/settings/registry/{key}/history?tenant_id=  every value row of the key, newest first, with the value each
                                                          replaced (the page's diff)
  POST /admin/settings/registry/{key}/proposals           {value, reason (>= 20), evidence_ref?, effective_from?,
                                                          tenant_id?, base_version, apply?}: propose (apply = approve
                                                          in the same step; maker_checker: a recorded self-approval
                                                          while two-person approval is off)
  POST /admin/settings/registry/proposals/{id}/approve    {note?}
  POST /admin/settings/registry/proposals/{id}/reject     {note}

Who may call (api/core/staff_authz.py POLICY): reads ops.read, proposals settings.write, decisions settings.approve —
all three held by the admin role only (tech lead S279 Q1). The viewer is refused on every write. Before CIS 407 is
applied every route answers 503. Service: api/services/settings_registry.py; reader: shared/settings.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from api.core.staff_authz import SETTINGS_APPROVE, has_perm, staff_policy
from api.services import settings_registry as reg

router = APIRouter(prefix="/admin/settings/registry", tags=["admin-settings-registry"],
                   dependencies=[Depends(staff_policy)])


def _actor(x_admin_user_id: Optional[str]) -> str:
    return f"admin:{(x_admin_user_id or 'admin').strip()[:56] or 'admin'}"


def _pool(request: Request) -> Any:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="DB not ready")
    return pool


def _http(e: reg.SettingsError) -> HTTPException:
    return HTTPException(status_code=e.status, detail={"code": e.code, "message": e.detail})


class ProposalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any
    reason: str = Field(min_length=1, max_length=2000)
    evidence_ref: Optional[str] = Field(None, max_length=500)
    effective_from: Optional[datetime] = None
    tenant_id: Optional[str] = Field(None, pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    base_version: Optional[int] = Field(None, ge=0)
    apply: bool = False


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: Optional[str] = Field(None, max_length=2000)


@router.get("", summary="Settings registry: every setting of an app with its value in force")
async def list_registry(request: Request, app: str = Query("cis", max_length=20),
                        area: Optional[str] = Query(None, max_length=40)):
    try:
        return await reg.list_settings(_pool(request), app=app, area=area)
    except reg.SettingsError as e:
        raise _http(e) from None


@router.get("/{key}/history", summary="Settings registry: the value history of one setting")
async def key_history(request: Request, key: str,
                      tenant_id: Optional[str] = Query(
                          None, pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")):
    try:
        return await reg.history(_pool(request), key, tenant_id)
    except reg.SettingsError as e:
        raise _http(e) from None


@router.post("/{key}/proposals", summary="Settings registry: propose (and optionally apply) a new value")
async def propose(request: Request, key: str, body: ProposalBody = Body(...),
                  x_admin_user_id: Optional[str] = Header(None), x_admin_role: Optional[str] = Header(None)):
    try:
        return await reg.propose(
            _pool(request), key, value=body.value, reason=body.reason, actor=_actor(x_admin_user_id),
            base_version=body.base_version, evidence_ref=body.evidence_ref, effective_from=body.effective_from,
            tenant_id=body.tenant_id, apply=body.apply, can_approve=has_perm(x_admin_role, SETTINGS_APPROVE))
    except reg.SettingsError as e:
        raise _http(e) from None


@router.post("/proposals/{value_id}/approve", summary="Settings registry: approve an open proposal")
async def approve(request: Request, value_id: str, body: DecisionBody = Body(default_factory=DecisionBody),
                  x_admin_user_id: Optional[str] = Header(None)):
    try:
        return await reg.decide(_pool(request), value_id, actor=_actor(x_admin_user_id), approve=True, note=body.note)
    except reg.SettingsError as e:
        raise _http(e) from None


@router.post("/proposals/{value_id}/reject", summary="Settings registry: reject an open proposal")
async def reject(request: Request, value_id: str, body: DecisionBody = Body(...),
                 x_admin_user_id: Optional[str] = Header(None)):
    try:
        return await reg.decide(_pool(request), value_id, actor=_actor(x_admin_user_id), approve=False,
                                note=body.note)
    except reg.SettingsError as e:
        raise _http(e) from None
