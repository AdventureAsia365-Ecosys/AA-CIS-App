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

  AA-1173 (S290, phase 3, CIS 479; api/services/compliance_exceptions.py):
  GET  /admin/platform/compliance/exceptions            every exception with its state (proposed, approved, expired,
                                                        rejected, revoked), the counts, the longest expiry (setting)
                                                                                                    compliance.read
  POST /admin/platform/compliance/exceptions            propose one (subject, title, reason, risk, expiry)
                                                                                                    compliance.write
  POST /admin/platform/compliance/exceptions/{id}/approve | /reject | /revoke
                                                        maker-checker decision (self-approval only while
                                                        cis.settings.two_person_approval is off, recorded); revoke
                                                        ends an approved one or withdraws an open one
                                                                                                    compliance.write
  GET  /admin/platform/compliance/reviews               the monthly posture reviews, newest first   compliance.read
  POST /admin/platform/compliance/reviews               record this month's review (the overview numbers + notes)
                                                                                                    compliance.write
  Every write needs a verified company sign-in (Cognito): the maker-checker identity; password, secret-only and
  read-only E2E sessions get 403 before any database call (the rule of Platform › Settings).

AA-1174 (S288 lane contract rule 3): a GET never collects the live sources, and under a read-only "view as" session
(``request.state.staff_view_as``, set by staff auth) a GET writes nothing at all (no ingest, no snapshot upsert).

Tech lead S287 Q10: compliance.read / compliance.write, administrator only at first (api/core/staff_authz.py
ROLE_PERMS; a custom role may be given them on Roles & permissions). The BFF refuses other roles first
(frontend/lib/rbac.ts API_READ_RULES / API_RULES "platform/compliance"). Every number is a Dev readiness number, not a
certification (the page says so). Logic: api/services/compliance.py and compliance_score.py.

Included in main.py WITHOUT an include prefix: staff_policy reads the route template from scope["route"].path.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from api.core.staff_authz import COMPLIANCE_WRITE, has_perm, request_staff_identity, staff_policy
from api.services import compliance, compliance_exceptions, compliance_obligations
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


# ── AA-1173: exceptions and monthly reviews ─────────────────────────────────────────────────────────────────────────

NEEDS_COGNITO = ("Exceptions and reviews need a company sign-in (Cognito): password sessions, secret-only callers and "
                 "read-only sessions cannot record them")


def _verified(request: Request) -> str:
    """The actor ("admin:<id>") of a verified Cognito caller holding compliance.write; 403 before any DB call."""
    identity = request_staff_identity(request)
    if identity is None or getattr(identity, "auth", None) != "cognito" or not getattr(identity, "admin_id", None):
        raise HTTPException(status_code=403, detail=NEEDS_COGNITO)
    role = getattr(identity, "role", None)
    if getattr(identity, "e2e_read_only", False) or _view_as(request) or not has_perm(role, COMPLIANCE_WRITE):
        raise HTTPException(status_code=403, detail=f"Role '{role}' lacks the '{COMPLIANCE_WRITE}' permission")
    return f"admin:{identity.admin_id}"


def _viewer(request: Request) -> Optional[str]:
    """The caller's actor id when it is a verified sign-in (the page marks "your" proposals), else None."""
    identity = request_staff_identity(request)
    if identity is None or getattr(identity, "auth", None) != "cognito" or not getattr(identity, "admin_id", None):
        return None
    return f"admin:{identity.admin_id}"


def _http(e: compliance_exceptions.ComplianceRefused) -> HTTPException:
    return HTTPException(status_code=e.status, detail={"code": e.code, "message": e.detail})


class ComplianceExceptionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_kind: Literal["control", "check", "vulnerability"]
    subject_ref: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=3, max_length=160)
    reason: str = Field(min_length=20, max_length=2000)
    compensating: Optional[str] = Field(None, max_length=2000)
    risk_level: Literal["low", "medium", "high"]
    expires_on: date


class ComplianceDecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: Optional[str] = Field(None, max_length=1000)


class ComplianceReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notes: str = Field(min_length=20, max_length=4000)
    actions: Optional[str] = Field(None, max_length=4000)
    period: Optional[date] = None


@router.get("/exceptions", summary="Compliance exceptions (accepted risks) with their state and the expiry limit")
async def get_exceptions(request: Request):
    body = await compliance_exceptions.list_exceptions(_pool(request))
    body["viewer"] = _viewer(request)
    return JSONResponse(body, headers=_NO_STORE)


@router.post("/exceptions", summary="Propose a compliance exception (maker)")
async def post_exception(request: Request, body: ComplianceExceptionBody = Body(...)):
    actor = _verified(request)
    try:
        return JSONResponse(await compliance_exceptions.propose(_pool(request), body.model_dump(), actor),
                            headers=_NO_STORE)
    except compliance_exceptions.ComplianceRefused as e:
        raise _http(e)


async def _decide(request: Request, exception_id: int, action: str, body: ComplianceDecisionBody) -> JSONResponse:
    actor = _verified(request)
    pool = _pool(request)
    try:
        if action == "revoke":
            out = await compliance_exceptions.revoke(pool, exception_id, actor, body.note)
        else:
            out = await compliance_exceptions.decide(pool, exception_id, actor, approve=action == "approve",
                                                     note=body.note)
    except compliance_exceptions.ComplianceRefused as e:
        raise _http(e)
    return JSONResponse(out, headers=_NO_STORE)


@router.post("/exceptions/{exception_id}/approve", summary="Approve an open compliance exception (checker)")
async def approve_exception(request: Request, exception_id: int = Path(..., ge=1),
                            body: ComplianceDecisionBody = Body(default_factory=ComplianceDecisionBody)):
    return await _decide(request, exception_id, "approve", body)


@router.post("/exceptions/{exception_id}/reject", summary="Reject an open compliance exception (checker)")
async def reject_exception(request: Request, exception_id: int = Path(..., ge=1),
                           body: ComplianceDecisionBody = Body(...)):
    return await _decide(request, exception_id, "reject", body)


@router.post("/exceptions/{exception_id}/revoke", summary="Revoke an approved exception or withdraw an open one")
async def revoke_exception(request: Request, exception_id: int = Path(..., ge=1),
                           body: ComplianceDecisionBody = Body(...)):
    return await _decide(request, exception_id, "revoke", body)


@router.get("/reviews", summary="Security posture monthly reviews, newest first")
async def get_reviews(request: Request):
    return JSONResponse(await compliance_exceptions.list_reviews(_pool(request)), headers=_NO_STORE)


@router.post("/reviews", summary="Record this month's Security posture review (the page's numbers and the notes)")
async def post_review(request: Request, body: ComplianceReviewBody = Body(...)):
    actor = _verified(request)
    pool = _pool(request)
    overview = await compliance.overview(pool)
    exceptions = await compliance_exceptions.list_exceptions(pool)
    summary = compliance_exceptions.review_summary(overview, exceptions)
    try:
        out = await compliance_exceptions.record_review(pool, body.model_dump(mode="json"), actor, summary)
    except compliance_exceptions.ComplianceRefused as e:
        raise _http(e)
    return JSONResponse(out, headers=_NO_STORE)
