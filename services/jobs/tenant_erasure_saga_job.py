"""AA-1093 lane L2 — job kind ``tenant_erasure_saga``: moves one tenant erasure request through its phases
(services/tenant_erasure/saga.py has the rules and the phase list).

Payload: {"request_id": "<uuid>"} only. Never the tenant id: a running saga job must not count as a live job of the
tenant (the CIS erasure plan blocks on those) and must survive the CIS apply, which deletes the tenant's finished jobs.

One run = one pass over the request's current phase, every write a conditional UPDATE on that phase, so two runs, a
retry or a restore in between are harmless (idempotent per request and phase). A pass that has to wait (an app has not
reported, a CIS job is still running) raises Reschedule: the same job row looks again later (shared/jobs/registry.py,
at most RESCHEDULE_MAX_COUNT times; then it fails and the ops sampler queues a new one while the request is open).
A pass that cannot go on (a step failed, the plan changed, the approval expired, the apply switch is off) records the
blockers on the request and ends: a person starts a new DRY RUN round (admin Tenant offboarding tab).

Who queues it: the ops metrics sampler (``ensure_jobs``: a request whose phase has work and no live saga job, a
scheduled one only from its latest not_before), the approval route and the admin "Run now" button.

CIS's own work goes through the jobs that already exist, under idempotency keys of this request and round:
  * dry_run: a data_erasure DRY RUN of the tenant (its saga plan hash is saga.cis_plan_hash of the DRY RUN rows);
  * apply, after Booking, TripPlanner and the CRM all report done: a tenant_backup of the tenant, a fresh data_erasure
    DRY RUN (its saga plan hash must equal the approved one: plan_changed otherwise), then the data_erasure apply with
    that DRY RUN's own fingerprint, the approver (the Cognito admin of the combined approval) and the snapshot id given
    at the approval. The data_erasure job keeps all of its own guards (ERASURE_APPLY_ENABLED, the retention window, the
    recount inside its transaction, single use).
$0: database reads and writes only (the CIS jobs it queues are $0 too).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import structlog

from api.core import e2e_staff
from services.acp_shared.audit_log import AdminTenantAuditAction, write_audit_log
from services.jobs import erasure_job as ej
from services.jobs import tenant_backup_job as tb
from services.tenant_erasure import policy, reports, saga, store
from shared.jobs import queue
from shared.jobs.registry import RESCHEDULE_MAX_DELAY_SECONDS, JobContext, NonRetryable, Reschedule, enqueue, job_kind

logger = structlog.get_logger()

KIND = "tenant_erasure_saga"
ACTOR = "job:tenant_erasure_saga"
# How soon to look again: a CIS job of this request is still running (seconds of work) vs another app has not
# reported yet (its own pull runs on its schedule). Polling intervals, not policy values.
CIS_JOB_POLL_S = 30
APP_POLL_S = RESCHEDULE_MAX_DELAY_SECONDS

ACTIVE_JOB_SQL = ("SELECT id::text AS id FROM shared.job WHERE kind = $1 AND status IN ('queued', 'running') "
                  "AND payload->>'request_id' = $2 ORDER BY created_at, id LIMIT 1")
JOB_COUNT_SQL = "SELECT count(*) AS n FROM shared.job WHERE kind = $1 AND payload->>'request_id' = $2"
DUE_SQL = """
    SELECT request_id::text AS request_id, phase, round
      FROM shared.tenant_erasure_request
     WHERE phase = ANY($1::text[])
       AND (phase <> 'scheduled'
            OR greatest(not_before_cis, not_before_booking, not_before_tripplanner, not_before_crm) <= $2)
     ORDER BY created_at, request_id
     LIMIT 50
"""
TENANT_SQL = "SELECT tenant_id, is_pilot, cancelled_at FROM shared.tenants WHERE tenant_id = $1::uuid"


def validate_payload(payload: Optional[dict]) -> dict:
    rid = str((payload or {}).get("request_id") or "").strip().lower()
    if not ej.UUID_RE.match(rid):
        raise NonRetryable("request_id must be an erasure request id (uuid)")
    if "tenant_id" in (payload or {}) or "tenant" in (payload or {}):
        raise NonRetryable("a saga job carries the request id only, never the tenant id")
    return {"request_id": rid}


async def _cis_job(pool, kind: str, payload: dict, key: str) -> dict:
    """Queue (once per key) and read one CIS job of this request: {"id", "status", "result", "error"}."""
    job_id, _ = await enqueue(pool, kind, payload, idempotency_key=key, created_by=ACTOR)
    row = await queue.get(pool, job_id) or {}
    result = row.get("result")
    if isinstance(result, str):
        import json
        result = json.loads(result)
    return {"id": job_id, "status": row.get("status"), "result": result or {}, "error": row.get("error")}


def _key(req: dict, part: str) -> str:
    return f"{KIND}:{req['request_id']}:r{req['round']}:{part}"


async def _synthetic(conn, tenant_id: str) -> bool:
    return await e2e_staff.is_synthetic_tenant(conn, tenant_id)


async def _peer_steps(conn, req: dict, mode: str, apps: tuple[str, ...]) -> dict[str, Optional[dict]]:
    """The step row of ``mode`` per app, after copying the Booking / TripPlanner view reports into the step table (an
    unreadable view = app_not_ready). The CRM's rows come from its POST route."""
    rid, rnd = req["request_id"], int(req["round"])
    current = store.by_app_mode(await store.steps(conn, rid, rnd))
    for app in apps:
        if app not in saga.VIEW_APPS:
            continue
        try:
            rep = saga.latest_reports(await reports.read(conn, app, rid), current_round=rnd).get(mode)
        except reports.AppNotReady as e:
            prev = current.get((app, mode))
            if prev is None or prev["status"] in ("pending", "app_not_ready"):
                await store.upsert_step(conn, rid, rnd, app, mode, "app_not_ready", detail=e.detail)
            continue
        if rep is not None:
            await store.upsert_step(conn, rid, rnd, app, mode, rep["status"], plan_hash=rep["plan_hash"],
                                    counts=rep["counts"], pii_left=rep["pii_left"], detail=rep["detail"],
                                    reported_at=_ts(rep.get("reported_at")))
        elif (app, mode) not in current or current[(app, mode)]["status"] == "app_not_ready":
            await store.upsert_step(conn, rid, rnd, app, mode, "pending")
    for app in apps:
        if app not in saga.VIEW_APPS and (app, mode) not in current:
            await store.upsert_step(conn, rid, rnd, app, mode, "pending")
    after = store.by_app_mode(await store.steps(conn, rid, rnd))
    return {app: after.get((app, mode)) for app in apps}


def _ts(v: Any) -> Optional[datetime]:
    if v is None or isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def _wait(phase: str, delay: int, **progress) -> dict:
    return {"phase": phase, "waiting": True, "delay": delay, **progress}


def _stop(phase: str, **progress) -> dict:
    return {"phase": phase, "waiting": False, **progress}


# ── the pass ────────────────────────────────────────────────────────────────────────────────────────────────────────

async def advance(pool, request_id: str, *, now: Optional[datetime] = None, env: Optional[dict] = None) -> dict:
    """One pass over the request. Returns {"phase", "waiting", "delay"?, ...}; the job turns waiting into Reschedule."""
    now = now or datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        req = await store.get_request(conn, request_id)
        if req is None:
            raise NonRetryable(f"erasure request {request_id} does not exist")
        tenant_row = await conn.fetchrow(TENANT_SQL, req["tenant_id"])
        tenant = dict(tenant_row) if tenant_row else None
        if tenant and tenant.get("is_pilot") and req["phase"] in saga.OPEN_PHASES:
            await store.move(conn, request_id, saga.OPEN_PHASES, "failed",
                             blockers=[{"app": "cis", "kind": "pilot", "detail": "the pilot tenant is never erased "
                                                                                 "by the saga (decision D8)"}])
            return _stop("failed", reason="pilot")
        phase = req["phase"]
        if phase == "scheduled":
            if now < saga.due_at(req):
                return _stop(phase, reason="not due", due_at=saga.due_at(req).isoformat())
            if tenant is None or tenant.get("cancelled_at") is None:
                await store.move(conn, request_id, ("scheduled",), "cancelled", cancelled_at=now,
                                 blockers=[{"app": "cis", "kind": "not_offboarded",
                                            "detail": "the tenant is no longer offboarded (restored before the hook)"}])
                return _stop("cancelled", reason="not offboarded")
            if not await store.move(conn, request_id, ("scheduled",), "dry_run"):
                return _stop(phase, reason="moved meanwhile")
            req["phase"] = phase = "dry_run"
        if phase == "dry_run":
            return await _dry_run(pool, conn, req, now)
        if phase == "apply":
            return await _apply(pool, conn, req, tenant, now, env)
        if phase == "verify":
            return await _verify(conn, req, now)
        return _stop(phase)


async def _dry_run(pool, conn, req: dict, now: datetime) -> dict:
    rid, rnd, tid = req["request_id"], int(req["round"]), req["tenant_id"]
    # CIS: its own data_erasure DRY RUN (read only)
    job = await _cis_job(pool, ej.KIND, {"subject": "tenant", "tenant": tid, "mode": "dry_run"},
                         _key(req, "cis:dry_run"))
    if job["status"] == "succeeded":
        res = job["result"]
        if res.get("blocked"):
            kinds = sorted({str(b.get("kind")) for b in res.get("blockers") or []})
            await store.upsert_step(conn, rid, rnd, "cis", "dry_run", "refused", job_id=job["id"], reported_at=now,
                                    counts=res.get("totals") or {}, detail="DRY RUN blockers: " + ", ".join(kinds))
        else:
            await store.upsert_step(conn, rid, rnd, "cis", "dry_run", "done", job_id=job["id"], reported_at=now,
                                    plan_hash=saga.cis_plan_hash(tid, res.get("rows") or []),
                                    counts=res.get("totals") or {})
    elif job["status"] in ("failed", "cancelled", "stopped_budget"):
        await store.upsert_step(conn, rid, rnd, "cis", "dry_run", "failed", job_id=job["id"],
                                detail=str(job.get("error") or job["status"])[:300])
    else:
        await store.upsert_step(conn, rid, rnd, "cis", "dry_run", "pending", job_id=job["id"])
    steps = await _peer_steps(conn, req, "dry_run", saga.PEER_APPS)
    cis = store.by_app_mode(await store.steps(conn, rid, rnd)).get(("cis", "dry_run"))
    steps["cis"] = cis
    state = saga.phase_state(steps, saga.APPS, need_hash=True)
    if state.ready:
        fp = saga.combined_fingerprint(rid, tid, {app: steps[app]["plan_hash"] for app in saga.APPS})
        await store.move(conn, rid, ("dry_run",), "awaiting_approval", combined_fingerprint=fp, fingerprint_at=now,
                         blockers=[])
        logger.info("tenant_erasure_awaiting_approval", request_id=rid, fingerprint=fp[:12])
        return _stop("awaiting_approval", fingerprint=fp)
    await store.set_blockers(conn, rid, "dry_run", list(state.blockers))
    if state.stopped:
        return _stop("dry_run", blockers=list(state.blockers))
    delay = CIS_JOB_POLL_S if "cis" in state.waiting else APP_POLL_S
    return _wait("dry_run", delay, waiting_for=list(state.waiting), blockers=list(state.blockers))


async def _apply(pool, conn, req: dict, tenant: Optional[dict], now: datetime, env: Optional[dict]) -> dict:
    rid, rnd, tid = req["request_id"], int(req["round"]), req["tenant_id"]
    blockers: list[dict] = []
    if not policy.apply_enabled(env):
        blockers.append({"app": "cis", "kind": "apply_off", "detail": f"{policy.APPLY_SWITCH_ENV} is off"})
        await store.set_blockers(conn, rid, "apply", blockers)
        return _stop("apply", blockers=blockers)
    peers = await _peer_steps(conn, req, "apply", saga.PEER_APPS)
    state = saga.phase_state(peers, saga.PEER_APPS)
    expired = req.get("approval_expires_at") is None or now > req["approval_expires_at"]
    current = store.by_app_mode(await store.steps(conn, rid, rnd))
    cis_apply = current.get(("cis", "apply"))
    if not state.ready:
        blockers = list(state.blockers)
        if expired and not state.stopped:
            blockers.append({"app": "cis", "kind": "approval_expired",
                             "detail": "the approval expired before every app applied; start a new round"})
        await store.set_blockers(conn, rid, "apply", blockers)
        if state.stopped or expired:
            return _stop("apply", blockers=blockers)
        return _wait("apply", APP_POLL_S, waiting_for=list(state.waiting), blockers=blockers)
    # ── CIS last: every peer reported done ─────────────────────────────────────────────────────────────────────────
    if cis_apply is None or cis_apply.get("job_id") is None:
        if expired:
            blockers = [{"app": "cis", "kind": "approval_expired",
                         "detail": "the approval expired before the CIS apply started; start a new round"}]
            await store.set_blockers(conn, rid, "apply", blockers)
            return _stop("apply", blockers=blockers)
        reasons = saga.tenant_guard(tenant, environment=policy.environment(env),
                                    synthetic=bool(tenant) and await _synthetic(conn, tid),
                                    drill=req["mode"] == "drill")
        if reasons:
            blockers = [{"app": "cis", "kind": "tenant", "detail": "; ".join(reasons)}]
            await store.set_blockers(conn, rid, "apply", blockers)
            return _stop("apply", blockers=blockers)
        backup = await _cis_job(pool, tb.KIND, {"tenant_id": tid, "trigger": "manual"}, _key(req, "cis:backup"))
        if backup["status"] in ("failed", "cancelled", "stopped_budget"):
            blockers = [{"app": "cis", "kind": "backup_failed", "detail": str(backup.get("error") or "")[:200]}]
            await store.set_blockers(conn, rid, "apply", blockers)
            return _stop("apply", blockers=blockers)
        if backup["status"] != "succeeded":
            await store.upsert_step(conn, rid, rnd, "cis", "apply", "pending", detail="tenant backup running")
            return _wait("apply", CIS_JOB_POLL_S, waiting_for=["cis:backup"])
        recheck = await _cis_job(pool, ej.KIND, {"subject": "tenant", "tenant": tid, "mode": "dry_run"},
                                 _key(req, "cis:recheck"))
        if recheck["status"] in ("failed", "cancelled", "stopped_budget"):
            await store.upsert_step(conn, rid, rnd, "cis", "apply", "failed", detail="the CIS re-check DRY RUN failed")
            return _stop("apply", blockers=[{"app": "cis", "kind": "failed", "detail": "re-check DRY RUN failed"}])
        if recheck["status"] != "succeeded":
            await store.upsert_step(conn, rid, rnd, "cis", "apply", "pending", detail="re-check DRY RUN running")
            return _wait("apply", CIS_JOB_POLL_S, waiting_for=["cis:recheck"])
        res = recheck["result"]
        approved = (current.get(("cis", "dry_run")) or {}).get("approved_plan_hash")
        now_hash = saga.cis_plan_hash(tid, res.get("rows") or [])
        if res.get("blocked") or not approved or now_hash != approved:
            await store.upsert_step(conn, rid, rnd, "cis", "apply", "plan_changed", plan_hash=now_hash,
                                    detail="the CIS plan differs from the approved one (or has blockers)")
            blockers = [{"app": "cis", "kind": "plan_changed", "detail": "the CIS plan differs from the approved one"}]
            await store.set_blockers(conn, rid, "apply", blockers)
            return _stop("apply", blockers=blockers)
        payload = ej.validate_payload({
            "subject": "tenant", "tenant": tid, "mode": "apply", "dry_run_job_id": recheck["id"],
            "approved_fingerprint": res["fingerprint"], "approved_by": req["approved_by"],
            "snapshot_id": req["snapshot_id"], "actor": req["approved_by"]})
        job = await _cis_job(pool, ej.KIND, payload, _key(req, "cis:apply"))
        await store.upsert_step(conn, rid, rnd, "cis", "apply", "pending", job_id=job["id"], plan_hash=now_hash)
        cis_apply = {"job_id": job["id"]}
    job = await queue.get(pool, cis_apply["job_id"]) or {}
    status = job.get("status")
    if status in ("failed", "cancelled", "stopped_budget"):
        await store.upsert_step(conn, rid, rnd, "cis", "apply", "failed", job_id=cis_apply["job_id"],
                                detail=str(job.get("error") or status)[:300])
        blockers = [{"app": "cis", "kind": "failed", "detail": "the CIS data_erasure apply failed"}]
        await store.set_blockers(conn, rid, "apply", blockers)
        return _stop("apply", blockers=blockers)
    if status != "succeeded":
        return _wait("apply", CIS_JOB_POLL_S, waiting_for=["cis:apply"])
    res = job.get("result") or {}
    if isinstance(res, str):
        import json
        res = json.loads(res)
    await store.upsert_step(conn, rid, rnd, "cis", "apply", "done", job_id=cis_apply["job_id"],
                            counts=res.get("totals") or {}, reported_at=now)
    left = sum(int(v or 0) for v in (res.get("verified_left") or {}).values())
    await store.upsert_step(conn, rid, rnd, "cis", "verify", "done" if res.get("verified") else "failed",
                            job_id=cis_apply["job_id"], pii_left=left, reported_at=now)
    await store.move(conn, rid, ("apply",), "verify", blockers=[])
    logger.info("tenant_erasure_cis_applied", request_id=rid, totals=res.get("totals"))
    return await _verify(conn, {**req, "phase": "verify"}, now)


async def _verify(conn, req: dict, now: datetime) -> dict:
    rid, rnd = req["request_id"], int(req["round"])
    steps = await _peer_steps(conn, req, "verify", saga.PEER_APPS)
    steps["cis"] = store.by_app_mode(await store.steps(conn, rid, rnd)).get(("cis", "verify"))
    state = saga.phase_state(steps, saga.APPS, need_census=True)
    if not state.ready:
        await store.set_blockers(conn, rid, "verify", list(state.blockers))
        if state.stopped:
            return _stop("verify", blockers=list(state.blockers))
        return _wait("verify", APP_POLL_S, waiting_for=list(state.waiting))
    rows = store.by_app_mode(await store.steps(conn, rid, rnd))
    doc = saga.receipt(req, rows, done_at=now, checklist=ej.checklist("tenant", req["tenant_id"]))
    async with conn.transaction():
        if await store.move(conn, rid, ("verify",), "done", receipt=doc, done_at=now, blockers=[]):
            # the tenant row is gone: the record is platform-scoped and names the request
            await write_audit_log(conn, tenant_id=None, actor=ACTOR, action=AdminTenantAuditAction.ERASURE_DONE,
                                  resource_type="tenant_erasure", resource_id=rid,
                                  details={"tenant": req["tenant_id"], "round": rnd,
                                           "fingerprint": req.get("approved_fingerprint"),
                                           "apps": {a: doc["apps"][a]["pii_left"] for a in saga.APPS}})
    logger.info("tenant_erasure_done", request_id=rid)
    return _stop("done")


# ── who queues it ───────────────────────────────────────────────────────────────────────────────────────────────────

async def ensure_job(pool, request_id: str, phase: str, round_: int, *, created_by: str = ACTOR,
                     run_now: bool = False) -> tuple[str, bool]:
    """The live saga job of the request (pulled forward with ``run_now``), or a new one keyed by request, round,
    phase and a counter (idempotent per request and phase: one live job at a time)."""
    live = await pool.fetchval(ACTIVE_JOB_SQL, KIND, request_id)
    if live is not None:
        if run_now:
            await pool.execute("UPDATE shared.job SET run_after = now(), updated_at = now() "
                               "WHERE id = $1::uuid AND status = 'queued'", live)
        return live, False
    n = await pool.fetchval(JOB_COUNT_SQL, KIND, request_id)
    return await enqueue(pool, KIND, {"request_id": request_id},
                         idempotency_key=f"{KIND}:{request_id}:r{round_}:{phase}:{int(n)}",
                         created_by=str(created_by)[:64])


async def ensure_jobs(pool, now: Optional[datetime] = None) -> list[str]:
    """The ops sampler's tick: one live job per open request whose phase has work (a scheduled one from its latest
    not_before). Nothing before CIS 408 is applied."""
    now = now or datetime.now(timezone.utc)
    try:
        rows = await pool.fetch(DUE_SQL, list(saga.WORKING_PHASES), now)
    except store.NOT_MIGRATED:
        return []
    out = []
    for r in rows:
        job_id, created = await ensure_job(pool, r["request_id"], r["phase"], int(r["round"]),
                                           created_by="scheduler:aa-1093")
        if created:
            out.append(job_id)
    return out


@job_kind(KIND, concurrency=1, max_attempts=3, expected_seconds=120, background=True)
async def run(ctx: JobContext) -> dict:
    payload = validate_payload(ctx.payload)
    out = await advance(ctx.pool, payload["request_id"])
    if out.get("waiting"):
        raise Reschedule(int(out.get("delay") or APP_POLL_S),
                         progress={k: v for k, v in out.items() if k not in ("waiting", "delay")},
                         reason="waiting for " + ", ".join(out.get("waiting_for") or ["the apps"]))
    result = {"kind": KIND, "request_id": payload["request_id"], **out}
    ctx.set_result(result)
    return result
