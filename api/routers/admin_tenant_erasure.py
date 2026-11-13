"""AA-1093 lane L2 — staff routes of the tenant offboarding erasure saga (admin › Data erasure › Tenant offboarding).

  GET  /admin/job-runner/erasure/requests                     the requests (newest first; ?tenant_id=, ?limit=)
  POST /admin/job-runner/erasure/requests/drill               {"tenant_id"}: a drill of an offboarded SYNTHETIC tenant
                                                              (window 0 for every app: the request is due at once)
  GET  /admin/job-runner/erasure/requests/{request_id}        one request, its steps per round and app, the settings
  POST /admin/job-runner/erasure/requests/{request_id}/advance  run the saga job now (queue one, or pull the queued
                                                              one forward); never approves or applies anything
  POST /admin/job-runner/erasure/requests/{request_id}/approve  {"fingerprint", "snapshot_id"}: approve the combined
                                                              fingerprint (Cognito administrator only, within 24 h
                                                              of the fingerprint, apply switch on, never the pilot,
                                                              Dev: synthetic only); the apps then apply, CIS last
  POST /admin/job-runner/erasure/requests/{request_id}/new-round  start a new DRY RUN round (in failed: after an
                                                              expired approval, a plan_changed, a failed step or a
                                                              census above 0; refused once the CIS apply job started)
  POST /admin/job-runner/erasure/requests/{request_id}/stop   the tech lead stops the request: it stays failed for
                                                              good (dry_run, awaiting_approval or failed only)

Under /admin/job-runner/erasure like the data_erasure routes, so the admin BFF rule (frontend lib/rbac.ts
`job-runner/erasure`: admin only) and POLICY (tenant.read / tenant.write) cover them. Reads hold ids, phases, hashes,
counts and staff actor ids only. 503 before CIS 408 is applied.

AA-1093 S279 (root ADR 0006a A10b-5): drill, advance and stop are on the A10b write allow-list for the live DRY RUN
drill; each refuses the Dev-only E2E session (403 e2e_read_only, before any write, job or audit row) unless the tenant
(the body's for drill, the request's for advance / stop) is synthetic (api/core/e2e_tenant_writes.py). Approve and
new-round refuse every Dev-only E2E identity.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

import asyncpg
import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from api.core import e2e_staff, e2e_tenant_writes
from api.core.staff_authz import request_staff_identity, staff_policy
from api.routers.admin import verify_admin_secret
from services.acp_shared.audit_log import AdminTenantAuditAction, write_audit_log
from services.tenant_erasure import policy, saga, store

logger = structlog.get_logger()

router = APIRouter(prefix="/admin/job-runner/erasure/requests", tags=["admin-tenant-erasure"],
                   dependencies=[Depends(verify_admin_secret), Depends(staff_policy)])

# the log event of a write refused to a Dev-only E2E session (A10b-5)
E2E_REFUSED = "tenant_erasure_e2e_refused"
TENANT_SQL = "SELECT tenant_id, is_pilot, cancelled_at FROM shared.tenants WHERE tenant_id = $1::uuid"


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


def _public(d: Optional[dict]) -> Optional[dict]:
    if d is None:
        return None
    return {k: _iso(v) for k, v in d.items()}


def _unavailable(e: BaseException) -> HTTPException:
    logger.error("tenant_erasure_not_migrated", reason=type(e).__name__)
    return HTTPException(status_code=503, detail="The tenant erasure saga is not migrated here (CIS 408)")


def _rid(request_id: str) -> str:
    try:
        return str(UUID(request_id))
    except ValueError:
        raise HTTPException(status_code=422, detail="request_id must be a uuid")


def settings_view(env: Optional[dict] = None) -> dict:
    live = policy.windows("live", env)
    return {
        "apply_enabled": policy.apply_enabled(),
        "apply_switch": policy.APPLY_SETTING,
        "approval_valid_hours": int(policy.approval_validity().total_seconds() // 3600),
        "window_days": {app: live[app].days for app in saga.APPS},
        "environment": policy.environment(env) or None,
    }


@router.get("", summary="Tenant erasure requests (offboarding saga)")
async def list_requests(request: Request, tenant_id: Optional[UUID] = None,
                        limit: int = Query(50, ge=1, le=200)):
    try:
        async with request.app.state.pool.acquire() as conn:
            rows = await store.list_requests(conn, limit=limit, tenant_id=str(tenant_id) if tenant_id else None)
            names = {str(r["tenant_id"]): r["name"] for r in await conn.fetch(
                "SELECT tenant_id, name::text AS name FROM shared.tenants WHERE tenant_id = ANY($1::uuid[])",
                list({r["tenant_id"] for r in rows}))} if rows else {}
    except store.NOT_MIGRATED as e:
        raise _unavailable(e)
    items = []
    for r in rows:
        item = _public(r)
        item["tenant_name"] = names.get(r["tenant_id"])        # None once the tenant row is erased
        item["next_due_at"] = _iso(saga.next_due_at(r))
        items.append(item)
    return {"requests": items, "settings": settings_view()}


class DrillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: UUID


@router.post("/drill", summary="Start a drill (window 0) for an offboarded synthetic tenant")
async def start_drill(body: DrillRequest, request: Request, x_admin_user_id: Optional[str] = Header(None)):
    """Plan section 6: the exit windows and the stages are proven with window 0 on a synthetic tenant. The tenant must
    be offboarded, synthetic (is_canary, never the pilot) and have no round started yet. Its open request becomes a
    drill (every not_before = T0), or a drill request is created. Nothing is approved or applied here."""
    tid = str(body.tenant_id)
    actor = f"admin:{x_admin_user_id}"[:64] if x_admin_user_id else "admin"
    try:
        async with request.app.state.pool.acquire() as conn, conn.transaction():
            await e2e_tenant_writes.refuse_unless_synthetic(request, conn, tid, event=E2E_REFUSED)
            row = await conn.fetchrow(TENANT_SQL, tid)
            tenant = dict(row) if row else None
            reasons = saga.cis_tenant_guard(tenant)
            if tenant is not None and not await e2e_staff.is_synthetic_tenant(conn, tid):
                reasons.append("only a synthetic tenant may be drilled")
            if reasons:
                raise HTTPException(status_code=409, detail="; ".join(reasons))
            req = await store.open_request(conn, tid, for_update=True)
            started = req is not None and (req["phase"] != "dry_run" or req["round_apps"] or req["round"] > 1)
            if started:
                raise HTTPException(status_code=409,
                                    detail=f"the tenant's request has started (phase {req['phase']}, round "
                                           f"{req['round']})")
            if req is None:
                req = await store.create_request(conn, tid, opened_at=tenant["cancelled_at"], mode="drill",
                                                 actor=actor)
            else:
                nb = saga.schedule(req["opened_at"], "drill")
                await store.move(conn, req["request_id"], ("dry_run",), "dry_run", mode="drill",
                                 **{f"not_before_{a}": nb[a] for a in saga.APPS})
                req = await store.get_request(conn, req["request_id"])
            await write_audit_log(conn, tenant_id=tid, actor=actor, action=AdminTenantAuditAction.ERASURE_DRILL,
                                  resource_type="tenant_erasure", resource_id=req["request_id"],
                                  details={"mode": "drill", "note": "window 0; synthetic tenant; no approval here"})
    except store.NOT_MIGRATED as e:
        raise _unavailable(e)
    logger.info("tenant_erasure_drill_started", request_id=req["request_id"], tenant_id=tid)
    return {"request": _public(req)}


@router.get("/{request_id}", summary="One tenant erasure request with its steps")
async def get_request(request_id: str, request: Request):
    rid = _rid(request_id)
    try:
        async with request.app.state.pool.acquire() as conn:
            req = await store.get_request(conn, rid)
            if req is None:
                raise HTTPException(status_code=404, detail="No erasure request has this id")
            steps = await store.steps(conn, rid)
            name = await conn.fetchval("SELECT name::text AS name FROM shared.tenants WHERE tenant_id = $1::uuid",
                                       req["tenant_id"])
            job = await conn.fetchrow(
                "SELECT id::text AS id, status, run_after, progress FROM shared.job WHERE kind = 'tenant_erasure_saga' "
                "AND payload->>'request_id' = $1 ORDER BY created_at DESC, id DESC LIMIT 1", rid)
    except store.NOT_MIGRATED as e:
        raise _unavailable(e)
    out = _public(req)
    out.update({"tenant_name": name, "next_due_at": _iso(saga.next_due_at(req)),
                "steps": [_public(s) for s in steps],
                "job": _public(dict(job)) if job else None})
    return {"request": out, "settings": settings_view()}


@router.post("/{request_id}/advance", summary="Run the erasure saga job of this request now")
async def advance_now(request_id: str, request: Request, x_admin_user_id: Optional[str] = Header(None)):
    from services.jobs import tenant_erasure_saga_job as sj

    rid = _rid(request_id)
    pool = request.app.state.pool
    try:
        req = await store.get_request(pool, rid)
    except store.NOT_MIGRATED as e:
        raise _unavailable(e)
    if req is None:
        raise HTTPException(status_code=404, detail="No erasure request has this id")
    await e2e_tenant_writes.refuse_unless_synthetic(request, pool, req["tenant_id"], event=E2E_REFUSED)
    if req["phase"] not in saga.WORKING_PHASES:
        raise HTTPException(status_code=409, detail=f"nothing to run in phase {req['phase']}")
    actor = f"admin:{x_admin_user_id}"[:64] if x_admin_user_id else "admin"
    job_id, created = await sj.ensure_job(pool, rid, req["phase"], int(req["round"]), created_by=actor, run_now=True)
    return {"request_id": rid, "phase": req["phase"], "job_id": job_id, "created": created}


class ApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fingerprint: str = Field(..., min_length=64, max_length=64)
    # the manual pre-erase RDS snapshot (decision D7: one shared snapshot), as the data_erasure apply needs it
    snapshot_id: str = Field(..., min_length=1, max_length=255)


@router.post("/{request_id}/approve", summary="Approve the combined fingerprint (Cognito administrator only)")
async def approve(request_id: str, body: ApproveRequest, request: Request):
    from services.jobs import tenant_erasure_saga_job as sj

    rid = _rid(request_id)
    e2e_tenant_writes.refuse_any_e2e(request, event=E2E_REFUSED)   # A10b-5: only a person approves an erasure
    identity = request_staff_identity(request)
    cognito_admin = (identity is not None and getattr(identity, "auth", None) == "cognito"
                     and bool(getattr(identity, "admin_id", None))
                     and request.headers.get("x-admin-role") == "admin")
    now = datetime.now(timezone.utc)
    validity = policy.approval_validity()
    pool = request.app.state.pool
    try:
        async with pool.acquire() as conn, conn.transaction():
            req = await store.get_request(conn, rid, for_update=True)
            if req is None:
                raise HTTPException(status_code=404, detail="No erasure request has this id")
            reasons = saga.request_guard(req, environment=policy.environment())
            # the fingerprint the step rows give now, under the request lock (a report after the fingerprint = stale)
            now_fp = saga.fingerprint_of_steps(req, await store.steps(conn, rid, int(req["round"])))
            if "cis" in req["round_apps"]:            # stage 1: CIS still erases, its tenant row must be right
                row = await conn.fetchrow(TENANT_SQL, req["tenant_id"])
                reasons += saga.cis_tenant_guard(dict(row) if row else None)
            refusal = saga.approval_refusal(req, fingerprint=body.fingerprint, now=now, cognito_admin=cognito_admin,
                                            apply_on=policy.apply_enabled(), tenant_reasons=reasons,
                                            validity=validity,
                                            plans_match=now_fp is not None and now_fp == req["combined_fingerprint"])
            if refusal is not None:
                logger.warning("tenant_erasure_approval_refused", request_id=rid, code=refusal.code)
                raise HTTPException(status_code=refusal.status, detail=refusal.message)
            actor = f"admin:{identity.admin_id}"[:64]
            moved = await store.move(conn, rid, ("awaiting_approval",), "apply", approved_fingerprint=req[
                "combined_fingerprint"], approved_by=actor, approved_at=now, approval_expires_at=now + validity,
                snapshot_id=body.snapshot_id, blockers=[])
            if not moved:
                raise HTTPException(status_code=409, detail="the request moved meanwhile; reload it")
            await conn.execute(
                "UPDATE shared.tenant_erasure_step SET approved_plan_hash = plan_hash, updated_at = now() "
                "WHERE request_id = $1::uuid AND round = $2 AND mode = 'dry_run'", rid, int(req["round"]))
            await write_audit_log(conn, tenant_id=req["tenant_id"], actor=actor,
                                  action=AdminTenantAuditAction.ERASURE_APPROVED, resource_type="tenant_erasure",
                                  resource_id=rid, details={"fingerprint": req["combined_fingerprint"],
                                                            "round": int(req["round"]),
                                                            "snapshot_id": body.snapshot_id,
                                                            "expires_at": (now + validity).isoformat()})
    except store.NOT_MIGRATED as e:
        raise _unavailable(e)
    job_id, _ = await sj.ensure_job(pool, rid, "apply", int(req["round"]), created_by=actor, run_now=True)
    logger.info("tenant_erasure_approved", request_id=rid, approver=actor, job_id=job_id)
    return {"request_id": rid, "phase": "apply", "approved_by": actor,
            "approval_expires_at": (now + validity).isoformat(), "job_id": job_id}


@router.post("/{request_id}/new-round", summary="Start a new DRY RUN round of this request")
async def new_round(request_id: str, request: Request, x_admin_user_id: Optional[str] = Header(None)):
    """A new round: phase dry_run, round + 1, no app active until the next pass picks the apps past their not_before
    that are not done, the fingerprint and the approval cleared (the step rows of earlier rounds stay as the record,
    their approved_plan_hash too: a restore stays refused). Allowed while no CIS apply job of the round has started:
    in dry_run, awaiting_approval, apply, or failed (the usual case: the saga stopped on an expired approval, a
    plan_changed, a failed step or a census above 0); never for the pilot or a request the tech lead stopped."""
    rid = _rid(request_id)
    e2e_tenant_writes.refuse_any_e2e(request, event=E2E_REFUSED)   # A10b-5: never an E2E write
    actor = f"admin:{x_admin_user_id}"[:64] if x_admin_user_id else "admin"
    try:
        async with request.app.state.pool.acquire() as conn, conn.transaction():
            req = await store.get_request(conn, rid, for_update=True)
            if req is None:
                raise HTTPException(status_code=404, detail="No erasure request has this id")
            cis_apply = await conn.fetchval(
                "SELECT job_id::text AS job_id FROM shared.tenant_erasure_step WHERE request_id = $1::uuid "
                "AND round = $2 AND app = 'cis' AND mode = 'apply' AND job_id IS NOT NULL", rid, int(req["round"]))
            refusal = saga.new_round_refusal(req, cis_apply_started=cis_apply is not None)
            if refusal is not None:
                raise HTTPException(status_code=refusal.status, detail=refusal.message)
            await store.move(conn, rid, saga.NEW_ROUND_PHASES, "dry_run",
                             round=int(req["round"]) + 1, round_apps=[], combined_fingerprint=None, fingerprint_at=None,
                             approved_fingerprint=None, approved_by=None, approved_at=None,
                             approval_expires_at=None, blockers=[])
            await write_audit_log(conn, tenant_id=req["tenant_id"], actor=actor,
                                  action=AdminTenantAuditAction.ERASURE_ROUND, resource_type="tenant_erasure",
                                  resource_id=rid, details={"round": int(req["round"]) + 1,
                                                            "previous_phase": req["phase"]})
    except store.NOT_MIGRATED as e:
        raise _unavailable(e)
    except asyncpg.exceptions.CheckViolationError:
        raise HTTPException(status_code=409, detail="too many rounds for one request")
    except asyncpg.exceptions.UniqueViolationError:
        # failed -> dry_run while the tenant has another open request (restored and offboarded again meanwhile)
        raise HTTPException(status_code=409, detail="the tenant has another open erasure request")
    return {"request_id": rid, "phase": "dry_run", "round": int(req["round"]) + 1}


@router.post("/{request_id}/stop", summary="Stop this request for good (it stays failed)")
async def stop(request_id: str, request: Request, x_admin_user_id: Optional[str] = Header(None)):
    """The contract's "the tech lead decides (a new round, or stop)": the request moves to (or stays in) failed with
    the blocker stopped_by_tech_lead and never starts a new round. Only in dry_run, awaiting_approval or failed:
    never while the apps may be erasing (apply, verify). Nothing is erased or restored here; what an app already
    erased is the runbook's manual follow-up."""
    rid = _rid(request_id)
    actor = f"admin:{x_admin_user_id}"[:64] if x_admin_user_id else "admin"
    try:
        async with request.app.state.pool.acquire() as conn, conn.transaction():
            req = await store.get_request(conn, rid, for_update=True)
            if req is None:
                raise HTTPException(status_code=404, detail="No erasure request has this id")
            await e2e_tenant_writes.refuse_unless_synthetic(request, conn, req["tenant_id"], event=E2E_REFUSED)
            refusal = saga.stop_refusal(req)
            if refusal is not None:
                raise HTTPException(status_code=refusal.status, detail=refusal.message)
            blockers = list(req["blockers"] or []) + [{"app": "cis", "kind": saga.STOPPED_KIND,
                                                       "detail": "stopped by the tech lead"}]
            if not await store.move(conn, rid, saga.STOP_PHASES, "failed", blockers=blockers):
                raise HTTPException(status_code=409, detail="the request moved meanwhile; reload it")
            await write_audit_log(conn, tenant_id=req["tenant_id"], actor=actor,
                                  action=AdminTenantAuditAction.ERASURE_STOPPED, resource_type="tenant_erasure",
                                  resource_id=rid, details={"round": int(req["round"]),
                                                            "previous_phase": req["phase"]})
    except store.NOT_MIGRATED as e:
        raise _unavailable(e)
    logger.info("tenant_erasure_stopped", request_id=rid, actor=actor)
    return {"request_id": rid, "phase": "failed", "stopped": True}
