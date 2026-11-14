"""S279 thresholds L1 — Platform › Settings: the admin-editable settings registry (CIS 407).

  GET  /admin/settings/registry?app=cis&area=            every setting of the app with the value in force, its source
                                                          (env / tenant / platform / default), bounds, last change,
                                                          open proposal and the tenant values in force
                                                          (tenant_overrides, a tenant-scoped key)
  GET  /admin/settings/registry/{key}/history?tenant_id=  every value row of the key, newest first, with the value each
                                                          replaced (the page's diff)
  POST /admin/settings/registry/{key}/proposals           {value, reason (>= 20), evidence_ref?, effective_from?,
                                                          tenant_id?, base_version, apply?}: propose (apply = approve
                                                          in the same step; maker_checker: a recorded self-approval
                                                          while two-person approval is off)
  POST /admin/settings/registry/proposals/{id}/approve    {note?}
  POST /admin/settings/registry/proposals/{id}/reject     {note}

AA-1096 L4: the CRM tab (``app=crm``, a ``crm.*`` key, a decision with ``app: crm``) is relayed to the partner CRM's
own registry (crm.ops.v1 1.13 ``/settings``; api/services/crm_settings_relay.py) with the staff member's own Cognito
token: the CRM checks the group itself (crm-platform-admin writes) and CIS writes its audit row of each relayed change.
The CRM list adds ``can_write`` (the CRM's /console answer) so the page offers Change / Approve only to a CRM writer.

Who may call (api/core/staff_authz.py POLICY): reads ops.read, proposals settings.write, decisions settings.approve —
all three held by the admin role only (tech lead S279 Q1). The viewer is refused on every write. Every write also
needs a verified company sign-in (Cognito): the proposer / approver identity (maker-checker, the self-approval e-mail)
is the verified staff row (admin:<shared.admin_users.id>) and its verified role, never the x-admin-* headers, which any
holder of the admin secret can set (S279 review; the same rule as staff.write and the alert recipients). Before CIS
407 is applied every route answers 503. Service: api/services/settings_registry.py; reader: shared/settings.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from api.core.staff_authz import SETTINGS_APPROVE, SETTINGS_WRITE, has_perm, request_staff_identity, staff_policy
from api.services import crm_settings_relay as crm
from api.services import settings_registry as reg
from api.services.platform_apps import relay_authorization

router = APIRouter(prefix="/admin/settings/registry", tags=["admin-settings-registry"],
                   dependencies=[Depends(staff_policy)])


NEEDS_COGNITO = "Changing a setting needs a company sign-in (Cognito); password sessions and secret-only callers cannot"


def _verified(request: Request, capability: str) -> tuple[str, str]:
    """(actor, role) of the verified Cognito caller holding ``capability``; 403 otherwise, before any DB call.
    staff_policy already checked the role header against the route's capability; this re-checks the verified role
    (the header is rewritten from the token on a Cognito request, but a legacy or secret-only caller sets it freely)."""
    identity = request_staff_identity(request)
    if identity is None or getattr(identity, "auth", None) != "cognito" or not getattr(identity, "admin_id", None):
        raise HTTPException(status_code=403, detail=NEEDS_COGNITO)
    role = getattr(identity, "role", None)
    if getattr(identity, "e2e_read_only", False) or not has_perm(role, capability):
        raise HTTPException(status_code=403, detail=f"Role '{role}' lacks the '{capability}' permission")
    return f"admin:{identity.admin_id}", role


def _pool(request: Request) -> Any:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail={"code": "db_not_ready", "message": "The database is not ready"})
    return pool


def _pool_or_none(request: Request) -> Any:
    """The pool when the API has one (a CRM read uses it only for the staff names; it works without)."""
    return getattr(request.app.state, "pool", None)


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
    # AA-1096 L4: a CRM change is decided in the CRM (the page names the app of the row); an agency's own CRM change
    # names its agency (the CRM sees that row only in the agency's context).
    app: Literal["cis", "crm"] = "cis"
    tenant_id: Optional[str] = Field(None, pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@router.get("", summary="Settings registry: every setting of an app with its value in force")
async def list_registry(request: Request, app: str = Query("cis", max_length=20),
                        area: Optional[str] = Query(None, max_length=40)):
    try:
        if app == "crm":  # AA-1096 L4: the CRM keeps its own registry (crm-064); relayed with the staff token
            return await crm.list_settings(relay_authorization(request), area=area, pool=_pool_or_none(request))
        return await reg.list_settings(_pool(request), app=app, area=area)
    except reg.SettingsError as e:
        raise _http(e) from None


@router.get("/{key}/history", summary="Settings registry: the value history of one setting")
async def key_history(request: Request, key: str,
                      tenant_id: Optional[str] = Query(
                          None, pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")):
    try:
        if crm.is_crm_key(key):
            return await crm.history(relay_authorization(request), key, tenant_id, pool=_pool_or_none(request))
        return await reg.history(_pool(request), key, tenant_id)
    except reg.SettingsError as e:
        raise _http(e) from None


@router.post("/{key}/proposals", summary="Settings registry: propose (and optionally apply) a new value")
async def propose(request: Request, key: str, body: ProposalBody = Body(...)):
    actor, role = _verified(request, SETTINGS_WRITE)
    try:
        if crm.is_crm_key(key):
            if body.apply and not has_perm(role, SETTINGS_APPROVE):
                raise reg.SettingsError(403, "You may propose this change, not approve it", "approve_not_allowed")
            return await crm.propose(
                _pool(request), relay_authorization(request), key, actor=actor, value=body.value, reason=body.reason,
                base_version=body.base_version, evidence_ref=body.evidence_ref, effective_from=body.effective_from,
                tenant_id=body.tenant_id, apply=body.apply)
        return await reg.propose(
            _pool(request), key, value=body.value, reason=body.reason, actor=actor,
            base_version=body.base_version, evidence_ref=body.evidence_ref, effective_from=body.effective_from,
            tenant_id=body.tenant_id, apply=body.apply, can_approve=has_perm(role, SETTINGS_APPROVE))
    except reg.SettingsError as e:
        raise _http(e) from None


@router.post("/proposals/{value_id}/approve", summary="Settings registry: approve an open proposal")
async def approve(request: Request, value_id: str, body: DecisionBody = Body(default_factory=DecisionBody)):
    actor, _role = _verified(request, SETTINGS_APPROVE)
    try:
        if body.app == "crm":
            return await crm.decide(_pool(request), relay_authorization(request), value_id, actor=actor, approve=True,
                                    note=body.note, tenant_id=body.tenant_id)
        return await reg.decide(_pool(request), value_id, actor=actor, approve=True, note=body.note)
    except reg.SettingsError as e:
        raise _http(e) from None


@router.post("/proposals/{value_id}/reject", summary="Settings registry: reject an open proposal")
async def reject(request: Request, value_id: str, body: DecisionBody = Body(...)):
    actor, _role = _verified(request, SETTINGS_APPROVE)
    try:
        if body.app == "crm":
            return await crm.decide(_pool(request), relay_authorization(request), value_id, actor=actor, approve=False,
                                    note=body.note, tenant_id=body.tenant_id)
        return await reg.decide(_pool(request), value_id, actor=actor, approve=False, note=body.note)
    except reg.SettingsError as e:
        raise _http(e) from None
