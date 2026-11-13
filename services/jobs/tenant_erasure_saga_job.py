"""AA-1093 lane L2 — job kind ``tenant_erasure_saga``: moves one tenant erasure request through its phases
(services/tenant_erasure/saga.py has the rules and the phase list).

Payload: {"request_id": "<uuid>"} only. Never the tenant id: a running saga job must not count as a live job of the
tenant (the CIS erasure plan blocks on those) and must survive the CIS apply, which deletes the tenant's finished jobs.

One run = one pass over the request's current phase, every write a conditional UPDATE on that phase, so two runs, a
retry or a restore in between are harmless (idempotent per request and phase). A pass that has to wait (an app has not
reported, a CIS job is still running) raises Reschedule: the same job row looks again later (shared/jobs/registry.py,
at most RESCHEDULE_MAX_COUNT times; then it fails and the ops sampler queues a new one while the request is open).
A pass that cannot go on (a step failed or refused, a census is not 0, the plan changed, the approval expired, the
apply switch is off) moves the request to failed with its blockers and ends (the contract: the tech lead decides): a
person starts a new DRY RUN round or stops it (admin Tenant offboarding tab). Nothing is queued for a failed request.

Rounds: a round starts in dry_run when an app that is not done passes its not_before (stage 1 CIS, Booking,
TripPlanner at T0 + 30 days; stage 2 the CRM at T0 + 90 days); the apps of the round are fixed then (round_apps).
After verify those apps are done, and the request goes back to dry_run with the next round until every app is done.

Who queues it: the ops metrics sampler (``ensure_jobs``: a request whose phase has work and no live saga job; in
dry_run only once a round runs or the next not_before has passed), the approval route and the admin "Run now"
button.

CIS's own work goes through the jobs that already exist, under idempotency keys of this request and round:
  * dry_run: a data_erasure DRY RUN of the tenant (its saga plan hash is saga.cis_plan_hash of the DRY RUN rows);
  * apply, after the other active apps of the round report an ok apply: a tenant_backup of the tenant, a fresh
    data_erasure DRY RUN (its saga plan hash must equal the approved one: plan_changed otherwise), then the
    data_erasure apply with that DRY RUN's own fingerprint, the approver (the Cognito admin of the combined approval)
    and the snapshot id given at the approval. The data_erasure job keeps all of its own guards
    (ERASURE_APPLY_ENABLED, the retention window, the recount inside its transaction, single use).
$0: database reads and writes only (the CIS jobs it queues are $0 too).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

import structlog

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
# A CIS job of the saga that ended without a result.
TERMINAL_BAD = ("failed", "cancelled", "stopped_budget")

ACTIVE_JOB_SQL = ("SELECT id::text AS id FROM shared.job WHERE kind = $1 AND status IN ('queued', 'running') "
                  "AND payload->>'request_id' = $2 ORDER BY created_at, id LIMIT 1")
JOB_COUNT_SQL = "SELECT count(*) AS n FROM shared.job WHERE kind = $1 AND payload->>'request_id' = $2"
# A request with work: dry_run (a round running, or the next not_before of an app that is not done has passed),
# apply, verify. A handful of open requests at most.
DUE_SQL = """
    SELECT request_id::text AS request_id, phase, round
      FROM shared.tenant_erasure_request r
     WHERE phase = ANY($1::text[])
       AND (phase <> 'dry_run' OR cardinality(round_apps) > 0
            OR EXISTS (SELECT 1 FROM (VALUES ('cis', r.not_before_cis), ('booking', r.not_before_booking),
                                             ('tripplanner', r.not_before_tripplanner), ('crm', r.not_before_crm))
                                     AS a(app, nb)
                        WHERE a.nb <= $2 AND NOT a.app = ANY (r.done_apps)))
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
        result = json.loads(result)
    return {"id": job_id, "status": row.get("status"), "result": result or {}, "error": row.get("error")}


def _key(req: dict, part: str) -> str:
    return f"{KIND}:{req['request_id']}:r{req['round']}:{part}"


async def _app_steps(conn, req: dict, mode: str) -> dict[str, Optional[dict]]:
    """The step row of ``mode`` per active app, after copying the Booking / TripPlanner view reports of this round into
    the step table (an unreadable view = app_not_ready). The CRM's rows come from its POST route, CIS's from its
    jobs."""
    rid, rnd = req["request_id"], int(req["round"])
    current = store.by_app_mode(await store.steps(conn, rid, rnd))
    for app in req["round_apps"]:
        if app in saga.VIEW_APPS:
            prev = current.get((app, mode))
            not_ready = None
            try:
                rows = await reports.read(conn, app, rid)
            except reports.AppNotReady as e:
                not_ready, rows = e.code, []           # no readable view, or a view without the contract columns
            if not_ready is None and saga.report_shape_unknown(rows):
                not_ready = reports.SHAPE_CODE         # rows exist but none in the contract's shape
            if not_ready is not None:
                if prev is None or prev["status"] in ("pending", "app_not_ready"):
                    await store.upsert_step(conn, rid, rnd, app, mode, "app_not_ready", error_code=not_ready,
                                            blockers=[{"code": not_ready, "count": 1}])
                continue
            rep = saga.latest_reports(rows, current_round=rnd).get(mode)
            if rep is not None:
                await store.upsert_step(conn, rid, rnd, app, mode, rep["status"], plan_hash=rep["plan_hash"],
                                        tables=rep["tables"], blockers=rep["blockers"], census=rep["census"],
                                        error_code=rep["error_code"], report_id=rep["report_id"],
                                        job_id=rep["job_id"], reported_at=_ts(rep.get("reported_at")))
                continue
        if app != "cis" and ((app, mode) not in current or current[(app, mode)]["status"] == "app_not_ready"):
            await store.upsert_step(conn, rid, rnd, app, mode, "pending")
    after = store.by_app_mode(await store.steps(conn, rid, rnd))
    return {app: after.get((app, mode)) for app in req["round_apps"]}


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


def _code(kind: Any) -> str:
    """A CIS blocker kind as a contract code (lowercase, letters / digits / underscores, starts with a letter)."""
    code = re.sub(r"[^a-z0-9_]", "_", str(kind or "blocked").lower()).strip("_")[:63]
    return code if len(code) >= 2 and code[0].isalpha() else "blocked"


def _cis_tables(rows: list) -> list[dict]:
    """The CIS DRY RUN rows in the contract's TableEntry shape (no row digest in the CIS plan)."""
    return [{"table": str(r["table"]), "action": str(r["action"]).split(":", 1)[0], "rows": int(r["rows"]),
             "digest": None} for r in rows if saga.TABLE_RE.match(str(r.get("table") or ""))]


async def _stopped(conn, req: dict, blockers: list[dict]) -> dict:
    """A step needs a person: the request moves from its phase to failed with the blockers (the contract's "a step
    failed or refused; the tech lead decides"). It then leaves the open-request index and the ops sampler's list."""
    if not await store.move(conn, req["request_id"], (req["phase"],), "failed", blockers=blockers):
        return _stop(req["phase"], reason="moved meanwhile", blockers=blockers)
    logger.warning("tenant_erasure_failed", request_id=req["request_id"], round=int(req["round"]),
                   stopped_in=req["phase"], kinds=sorted({str(b.get("kind")) for b in blockers}))
    return _stop("failed", stopped_in=req["phase"], blockers=blockers)


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
        if req["phase"] in saga.OPEN_PHASES and (req["is_pilot"] or (tenant and tenant.get("is_pilot"))):
            await store.move(conn, request_id, saga.OPEN_PHASES, "failed",
                             blockers=[{"app": "cis", "kind": "pilot_refused",
                                        "detail": "the pilot tenant is never erased by the saga (decision D8)"}])
            return _stop("failed", reason="pilot_refused")
        phase = req["phase"]
        if phase == "dry_run":
            if not req["round_apps"]:
                due = saga.due_apps(req, now)
                if not due:
                    nxt = saga.next_due_at(req)
                    return _stop(phase, reason="not_due", next_due_at=nxt.isoformat() if nxt else None)
                if "cis" in due and (tenant is None or tenant.get("cancelled_at") is None):
                    await store.move(conn, request_id, ("dry_run",), "cancelled", cancelled_at=now,
                                     blockers=[{"app": "cis", "kind": "not_offboarded",
                                                "detail": "the tenant is no longer offboarded (restored)"}])
                    return _stop("cancelled", reason="not_offboarded")
                if not await store.move(conn, request_id, ("dry_run",), "dry_run", round_apps=due, blockers=[]):
                    return _stop(phase, reason="moved meanwhile")
                req["round_apps"] = due
            return await _dry_run(pool, conn, req, now)
        if phase == "apply":
            return await _apply(pool, conn, req, tenant, now, env)
        if phase == "verify":
            return await _verify(conn, req, now)
        return _stop(phase)


async def _dry_run(pool, conn, req: dict, now: datetime) -> dict:
    rid, rnd, tid = req["request_id"], int(req["round"]), req["tenant_id"]
    if "cis" in req["round_apps"]:
        job = await _cis_job(pool, ej.KIND, {"subject": "tenant", "tenant": tid, "mode": "dry_run"},
                             _key(req, "cis:dry_run"))
        if job["status"] == "succeeded":
            res = job["result"]
            tables = _cis_tables(res.get("rows") or [])
            if res.get("blocked"):
                codes: dict[str, int] = {}
                for b in res.get("blockers") or []:
                    code = _code(b.get("kind"))
                    codes[code] = codes.get(code, 0) + 1
                await store.upsert_step(conn, rid, rnd, "cis", "dry_run", "blocked", job_id=job["id"],
                                        reported_at=now, tables=tables, error_code="blocked",
                                        blockers=[{"code": c, "count": n} for c, n in sorted(codes.items())])
            else:
                await store.upsert_step(conn, rid, rnd, "cis", "dry_run", "ok", job_id=job["id"], reported_at=now,
                                        plan_hash=saga.cis_plan_hash(tid, rnd, res.get("rows") or []), tables=tables)
        elif job["status"] in TERMINAL_BAD:
            await store.upsert_step(conn, rid, rnd, "cis", "dry_run", "failed", job_id=job["id"],
                                    error_code="internal_error")
        else:
            await store.upsert_step(conn, rid, rnd, "cis", "dry_run", "pending", job_id=job["id"])
    steps = await _app_steps(conn, req, "dry_run")
    state = saga.phase_state(steps, req["round_apps"], need_hash=True)
    if state.ready:
        return await _await_approval(conn, req, now)
    if state.stopped:
        return await _stopped(conn, req, list(state.blockers))
    await store.set_blockers(conn, rid, "dry_run", list(state.blockers))
    delay = CIS_JOB_POLL_S if "cis" in state.waiting else APP_POLL_S
    return _wait("dry_run", delay, waiting_for=list(state.waiting), blockers=list(state.blockers))


async def _await_approval(conn, req: dict, now: datetime) -> dict:
    """Every active app reported an ok plan: compute the combined fingerprint and move to awaiting_approval under the
    request's row lock, from the step rows read inside that lock. The CRM report route takes the same lock and
    accepts a dry_run report only in phase dry_run, so no report can land between the fingerprint and the move."""
    rid, rnd, tid = req["request_id"], int(req["round"]), req["tenant_id"]
    async with conn.transaction():
        locked = await store.get_request(conn, rid, for_update=True)
        if locked is None or locked["phase"] != "dry_run" or int(locked["round"]) != rnd:
            return _stop(locked["phase"] if locked else "dry_run", reason="moved meanwhile")
        rows = store.by_app_mode(await store.steps(conn, rid, rnd))
        state = saga.phase_state({app: rows.get((app, "dry_run")) for app in req["round_apps"]}, req["round_apps"],
                                 need_hash=True)
        if not state.ready:                  # a report changed meanwhile: the next pass reads it again
            return _wait("dry_run", CIS_JOB_POLL_S, waiting_for=list(state.waiting) or ["a new report"],
                         blockers=list(state.blockers))
        fp = saga.combined_fingerprint(rid, tid, rnd, {app: rows[(app, "dry_run")]["plan_hash"]
                                                       for app in req["round_apps"]})
        await store.move(conn, rid, ("dry_run",), "awaiting_approval", combined_fingerprint=fp, fingerprint_at=now,
                         blockers=[])
    logger.info("tenant_erasure_awaiting_approval", request_id=rid, round=rnd, fingerprint=fp[:12])
    return _stop("awaiting_approval", fingerprint=fp)


async def _apply(pool, conn, req: dict, tenant: Optional[dict], now: datetime, env: Optional[dict]) -> dict:
    refused = await _apply_refused(conn, req, env)
    if refused is not None:
        return refused
    rid, rnd = req["request_id"], int(req["round"])
    peers = [a for a in req["round_apps"] if a != "cis"]
    steps = await _app_steps(conn, req, "apply")
    state = saga.phase_state({a: steps.get(a) for a in peers}, peers)
    expired = req.get("approval_expires_at") is None or now > req["approval_expires_at"]
    if not state.ready:
        return await _peers_not_applied(conn, req, state, expired)
    if "cis" not in req["round_apps"]:
        await store.move(conn, rid, ("apply",), "verify", blockers=[])
        return await _verify(conn, {**req, "phase": "verify"}, now)
    # ── CIS last in its stage: every other active app reported an ok apply ───────────────────────────────────────
    current = store.by_app_mode(await store.steps(conn, rid, rnd))
    cis_apply = current.get(("cis", "apply"))
    if cis_apply is None or cis_apply.get("job_id") is None:
        cis_apply, outcome = await _cis_apply_start(pool, conn, req, tenant, current, expired, now)
        if outcome is not None:
            return outcome
    return await _cis_apply_finish(pool, conn, req, cis_apply, now)


async def _apply_refused(conn, req: dict, env: Optional[dict]) -> Optional[dict]:
    """The apply switch (settings registry) and the request guards (pilot, Dev synthetic only)."""
    if not policy.apply_enabled():
        return await _stopped(conn, req, [{"app": "cis", "kind": "apply_disabled",
                                           "detail": f"the setting {policy.APPLY_SETTING} is off"}])
    reasons = saga.request_guard(req, environment=policy.environment(env))
    if reasons:
        return await _stopped(conn, req, [{"app": "cis", "kind": "guard", "detail": "; ".join(reasons)}])
    return None


async def _peers_not_applied(conn, req: dict, state, expired: bool) -> dict:
    """Another app of the stage has not reported an ok apply: wait, or stop on a failure / an expired approval."""
    blockers = list(state.blockers)
    if expired and not state.stopped:
        blockers.append({"app": "cis", "kind": "approval_expired",
                         "detail": "the approval expired before every app applied; start a new round"})
    if state.stopped or expired:
        return await _stopped(conn, req, blockers)
    await store.set_blockers(conn, req["request_id"], "apply", blockers)
    return _wait("apply", APP_POLL_S, waiting_for=list(state.waiting), blockers=blockers)


async def _cis_apply_start(pool, conn, req: dict, tenant: Optional[dict], current: dict, expired: bool,
                           now: datetime) -> tuple[Optional[dict], Optional[dict]]:
    """Backup, fresh DRY RUN, plan check, then the data_erasure apply job. Returns (the CIS apply step, None) once
    the apply job is queued, else (None, the pass outcome: wait or stop)."""
    rid, rnd, tid = req["request_id"], int(req["round"]), req["tenant_id"]
    if expired:
        return None, await _stopped(conn, req, [{"app": "cis", "kind": "approval_expired",
                                                 "detail": "the approval expired before the CIS apply; new round"}])
    why = saga.cis_tenant_guard(tenant)
    if why:
        return None, await _stopped(conn, req, [{"app": "cis", "kind": "guard", "detail": "; ".join(why)}])
    backup = await _cis_job(pool, tb.KIND, {"tenant_id": tid, "trigger": "manual"}, _key(req, "cis:backup"))
    if backup["status"] in TERMINAL_BAD:
        return None, await _stopped(conn, req, [{"app": "cis", "kind": "backup_failed",
                                                 "detail": str(backup.get("error") or "")[:200]}])
    if backup["status"] != "succeeded":
        await store.upsert_step(conn, rid, rnd, "cis", "apply", "pending")
        return None, _wait("apply", CIS_JOB_POLL_S, waiting_for=["cis:backup"])
    recheck = await _cis_job(pool, ej.KIND, {"subject": "tenant", "tenant": tid, "mode": "dry_run"},
                             _key(req, "cis:recheck"))
    if recheck["status"] in TERMINAL_BAD:
        await store.upsert_step(conn, rid, rnd, "cis", "apply", "failed", error_code="internal_error")
        return None, await _stopped(conn, req, [{"app": "cis", "kind": "internal_error",
                                                 "detail": "the CIS re-check DRY RUN failed"}])
    if recheck["status"] != "succeeded":
        await store.upsert_step(conn, rid, rnd, "cis", "apply", "pending")
        return None, _wait("apply", CIS_JOB_POLL_S, waiting_for=["cis:recheck"])
    res = recheck["result"]
    approved = (current.get(("cis", "dry_run")) or {}).get("approved_plan_hash")
    now_hash = saga.cis_plan_hash(tid, rnd, res.get("rows") or [])
    if res.get("blocked") or not approved or now_hash != approved:
        await store.upsert_step(conn, rid, rnd, "cis", "apply", "refused", plan_hash=now_hash,
                                error_code="plan_changed", reported_at=now)
        return None, await _stopped(conn, req, [{"app": "cis", "kind": "plan_changed",
                                                 "detail": "the CIS plan differs from the approved one"}])
    payload = ej.validate_payload({
        "subject": "tenant", "tenant": tid, "mode": "apply", "dry_run_job_id": recheck["id"],
        "approved_fingerprint": res["fingerprint"], "approved_by": req["approved_by"],
        "snapshot_id": req["snapshot_id"], "actor": req["approved_by"]})
    job = await _cis_job(pool, ej.KIND, payload, _key(req, "cis:apply"))
    await store.upsert_step(conn, rid, rnd, "cis", "apply", "pending", job_id=job["id"], plan_hash=now_hash)
    return {"job_id": job["id"]}, None


async def _cis_apply_finish(pool, conn, req: dict, cis_apply: dict, now: datetime) -> dict:
    """The data_erasure apply job ended: record its tables and its verification (rows left), then move to verify."""
    rid, rnd = req["request_id"], int(req["round"])
    job = await queue.get(pool, cis_apply["job_id"]) or {}
    status = job.get("status")
    if status in TERMINAL_BAD:
        await store.upsert_step(conn, rid, rnd, "cis", "apply", "failed", job_id=cis_apply["job_id"],
                                error_code="internal_error", reported_at=now)
        return await _stopped(conn, req, [{"app": "cis", "kind": "internal_error",
                                           "detail": "the CIS data_erasure apply failed"}])
    if status != "succeeded":
        return _wait("apply", CIS_JOB_POLL_S, waiting_for=["cis:apply"])
    res = job.get("result") or {}
    if isinstance(res, str):
        res = json.loads(res)
    applied = [{"table": t["table"], "action": str(t["action"]).split(":", 1)[0], "rows": int(t["rows"]),
                "digest": None} for t in res.get("applied") or [] if saga.TABLE_RE.match(str(t.get("table") or ""))]
    await store.upsert_step(conn, rid, rnd, "cis", "apply", "ok", job_id=cis_apply["job_id"], tables=applied,
                            plan_hash=(cis_apply.get("plan_hash") or None), reported_at=now)
    left = res.get("verified_left") or {}
    await store.upsert_step(
        conn, rid, rnd, "cis", "verify", "ok" if res.get("verified") else "failed", job_id=cis_apply["job_id"],
        census={"pii_columns_checked": len(res.get("applied") or []), "pii_rows_found": sum(int(v or 0)
                                                                                            for v in left.values())},
        tables=[{"table": t, "action": "delete", "rows": int(n or 0), "digest": None} for t, n in sorted(left.items())
                if saga.TABLE_RE.match(str(t))],
        error_code=None if res.get("verified") else "census_not_zero", reported_at=now)
    await store.move(conn, rid, ("apply",), "verify", blockers=[])
    logger.info("tenant_erasure_cis_applied", request_id=rid, round=rnd)
    return await _verify(conn, {**req, "phase": "verify"}, now)


async def _verify(conn, req: dict, now: datetime) -> dict:
    rid, rnd = req["request_id"], int(req["round"])
    steps = await _app_steps(conn, req, "verify")
    state = saga.phase_state(steps, req["round_apps"], need_census=True)
    if not state.ready:
        if state.stopped:                    # a failed verify or a census above 0: failed, the tech lead decides
            return await _stopped(conn, req, list(state.blockers))
        await store.set_blockers(conn, rid, "verify", list(state.blockers))
        return _wait("verify", APP_POLL_S, waiting_for=list(state.waiting))
    done = [a for a in saga.APPS if a in set(req["done_apps"]) | set(req["round_apps"])]
    clear = dict(combined_fingerprint=None, fingerprint_at=None, approved_fingerprint=None, approved_by=None,
                 approved_at=None, approval_expires_at=None, blockers=[])
    if len(done) < len(saga.APPS):
        # the next stage (the CRM at T0 + 90 days): a new round, no app active until its not_before
        await store.move(conn, rid, ("verify",), "dry_run", round=rnd + 1, round_apps=[], done_apps=done, **clear)
        logger.info("tenant_erasure_stage_done", request_id=rid, round=rnd, done=done)
        return _stop("dry_run", stage_done=list(req["round_apps"]), next_round=rnd + 1)
    rows = await store.steps(conn, rid)
    doc = saga.receipt({**req, "done_apps": done}, rows, done_at=now,
                       checklist=ej.checklist("tenant", req["tenant_id"]))
    async with conn.transaction():
        if await store.move(conn, rid, ("verify",), "done", receipt=doc, done_at=now, round_apps=[], done_apps=done,
                            blockers=[]):
            # the tenant row is gone (CIS erased in stage 1): the record is platform-scoped and names the request
            await write_audit_log(conn, tenant_id=None, actor=ACTOR, action=AdminTenantAuditAction.ERASURE_DONE,
                                  resource_type="tenant_erasure", resource_id=rid,
                                  details={"tenant": req["tenant_id"], "rounds": rnd,
                                           "apps": {a: doc["apps"][a].get("pii_rows_found") for a in saga.APPS}})
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
