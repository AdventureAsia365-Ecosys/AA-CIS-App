"""AA-650 — admin view of the durable job queue (shared.job).

  GET  /admin/job-runner/jobs              list, filter by kind/status (fields=summary: no payload/result)
  GET  /admin/job-runner/jobs/{id}         one job (status, progress, result, cost, error)
  POST /admin/job-runner/jobs/{id}/cancel  queued -> cancelled; running -> flagged, worker stops it
  POST /admin/job-runner/jobs/{id}/retry   failed / stopped_budget / cancelled -> queued
  POST /admin/job-runner/jobs/retry-llm-denied  re-queue the jobs an LLM account block failed (DRY RUN by default,
                                           audited; AA-1018)
  POST /admin/job-runner/llm-probe/run     enqueue the LLM connection probe (DRY RUN by default; live only with
                                           LLM_DAILY_PROBE=on; AA-1018)
  GET  /admin/job-runner/summary           counts per kind and status, registered kinds
  GET  /admin/job-runner/jobs/{id}/llm-calls  LLM calls the job made (AA-687)
  GET  /admin/job-runner/workers           worker liveness, running per worker, queue depth (AA-687)
  GET  /admin/job-runner/released          jobs released on worker shutdown since a time (AA-993 G1)
  GET  /admin/job-runner/retention         retention kinds: policy, table size, last runs (AA-825)
  POST /admin/job-runner/retention/run     enqueue one retention kind (DRY RUN unless approved);
                                           with older_than: an admin-only manual override proof run
  POST /admin/job-runner/retention/schedule  the daily ops scheduler enqueues every retention kind
  POST /admin/job-runner/staff-reconcile/schedule  the daily ops scheduler enqueues the staff directory
                                           reconcile (AA-816, report only)
  GET  /admin/job-runner/erasure           erasure inventory and apply settings (AA-839)
  POST /admin/job-runner/erasure/run       enqueue a data_erasure DRY RUN (default) or an approved apply (AA-839)

Restricted results (AA-816 review M1): the job reads are open to every staff role, so a kind whose result
holds data only some roles may see registers a (capability, redact) pair in _restricted_results(); callers
without the capability get the redacted job from /jobs and /jobs/{id}.

Not under /admin/jobs: that path serves the older shared.pipeline_jobs (AA-223, A1 run-tour).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal, Optional

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from api.core.staff_authz import has_perm, request_staff_identity, staff_policy
from api.routers.admin import verify_admin_secret
from shared.jobs import queue
from shared.jobs.registry import get_kind, kinds, run_terminal_hook

logger = structlog.get_logger()

# S241 (GĐ −1): every route of this router needs the admin secret. Router-level so a new GET can not
# ship open again (the reads here were public until S241). The inline verify_admin_secret() calls in
# the write routes are kept as-is (same check, run twice).
# AA-882 (GĐ1): staff_policy narrows the writes listed in api/core/staff_authz.py POLICY by role.
router = APIRouter(prefix="/admin/job-runner", tags=["admin-job-runner"],
                   dependencies=[Depends(verify_admin_secret), Depends(staff_policy)])

_STATUSES = ("queued", "running") + queue.TERMINAL


_TIME_KEYS = ("locked_until", "run_after", "created_at", "started_at", "finished_at", "last_seen_at",
              "stopped_at", "last_reap_at", "oldest_started_at", "oldest_created_at", "first_at", "last_at")


def _iso(d: dict) -> dict:
    for k in _TIME_KEYS:
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    return d


def _restricted_results() -> dict[str, tuple[str, Callable[[dict], dict]]]:
    """kind -> (capability needed for the full result, redact function). Imported lazily, like the
    retention_job routes below, so loading this router does not import the job modules."""
    from services.jobs import erasure_job, staff_reconcile_job

    return {staff_reconcile_job.KIND: (staff_reconcile_job.RESULT_CAPABILITY, staff_reconcile_job.redact_job),
            erasure_job.KIND: (erasure_job.RESULT_CAPABILITY, erasure_job.redact_job)}


def _caller_role(request: Request) -> Optional[str]:
    """x-admin-role as staff_policy saw it (the BFF's role, or the one staff_bearer set from Cognito groups).
    No header = the machine, which has no capability."""
    role = request.headers.get("x-admin-role") if hasattr(request, "headers") else None
    return role.strip() if isinstance(role, str) else None


def _redact_for_caller(request: Request, jobs: list[dict]) -> list[dict]:
    rules = _restricted_results()
    if not any(j.get("kind") in rules for j in jobs):
        return jobs
    role = _caller_role(request)
    for j in jobs:
        rule = rules.get(j.get("kind"))
        if rule is not None and not has_perm(role, rule[0]):
            rule[1](j)
    return jobs


def _parse_dt(value: Optional[str], field: str) -> Optional[datetime]:
    """AA-755 — parse an ISO-8601 datetime filter (or a plain date). 422 on a bad value so a typo in
    the URL is a clear error, not a silently-ignored filter."""
    if value is None or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{field} must be an ISO-8601 datetime")


@router.get("/jobs", summary="List jobs")
async def list_jobs(request: Request, kind: Optional[str] = None,
                    status: Optional[str] = Query(None, description="|".join(_STATUSES)),
                    limit: int = Query(50, ge=1, le=200),
                    page: int = Query(1, ge=1, description="1-based page (with page_size)"),
                    page_size: Optional[int] = Query(None, ge=1, le=200,
                                                     description="rows per page; overrides limit"),
                    sort: str = Query("created_at", description="sort: " + "|".join(queue.JOB_SORTS)),
                    sort_dir: str = Query("desc", description="asc|desc"),
                    tour_id: Optional[str] = Query(None, description="jobs whose payload names this tour"),
                    since: Optional[str] = Query(None, description="created_at >= this ISO datetime"),
                    until: Optional[str] = Query(None, description="created_at < this ISO datetime"),
                    created_by: Optional[str] = Query(None, description="exact created_by match"),
                    q: Optional[str] = Query(None, description="id prefix / kind / created_by search"),
                    reason: Optional[str] = Query(None, max_length=64,
                                                  description="AA-1018: the error starts with this text, e.g. "
                                                              "llm_access_denied"),
                    fields: Optional[str] = Query(None, description="S241: full (default) | summary — summary "
                                                  "returns payload/result as null (read them from /jobs/{id})")):
    # `fields` is compared as a str: a direct call (unit tests) gets the Query default object.
    if isinstance(fields, str) and fields not in ("full", "summary"):
        raise HTTPException(status_code=422, detail="fields must be 'full' or 'summary'")
    if status is not None and status not in _STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {_STATUSES}")
    if sort not in queue.JOB_SORTS:
        raise HTTPException(status_code=422, detail=f"sort must be one of {sorted(queue.JOB_SORTS)}")
    if sort_dir.lower() not in ("asc", "desc"):
        raise HTTPException(status_code=422, detail="sort_dir must be 'asc' or 'desc'")
    since_dt = _parse_dt(since, "since")
    until_dt = _parse_dt(until, "until")
    q_clean = q.strip() if q and q.strip() else None
    # `reason` is compared as a str: a direct call (unit tests) gets the Query default object.
    reason_clean = reason.strip() if isinstance(reason, str) and reason.strip() else None
    size = page_size or limit
    offset = (page - 1) * size
    pool = request.app.state.pool
    filters = dict(kind=kind, status=status, tour_id=tour_id, since=since_dt, until=until_dt,
                   created_by=created_by, q=q_clean, reason=reason_clean)
    rows = await queue.list_jobs(pool, limit=size, offset=offset, sort=sort, sort_dir=sort_dir,
                                 include_blobs=fields != "summary", **filters)
    total = await queue.count_jobs(pool, **filters)
    return {
        "jobs": _redact_for_caller(request, [_iso(r) for r in rows]),
        "total": total,
        "pagination": {"page": page, "page_size": size, "total": total,
                       "pages": max(1, (total + size - 1) // size)},
    }


@router.get("/summary", summary="Counts per kind/status + registered kinds")
async def summary(request: Request):
    rows = await request.app.state.pool.fetch(
        "SELECT kind, status, count(*)::int AS n, "
        f"coalesce(sum(j.cost_usd + {queue.LLM_COST_SQL}), 0)::float AS cost_usd "
        "FROM shared.job j WHERE created_at >= now() - interval '30 days' GROUP BY kind, status")
    return {
        "counts": [dict(r) for r in rows],
        "kinds": [{"kind": k.name, "concurrency": k.concurrency, "max_attempts": k.max_attempts,
                   "expected_seconds": k.expected_seconds,
                   # AA-993 G1: what a deploy does to this kind's running job.
                   "deploy_gate_exempt": k.deploy_gate_exempt, "deploy_blocking": k.deploy_blocking,
                   "release_delay_seconds": k.release_delay_seconds}
                  for k in kinds().values()],
        # AA-687: a job released this many times on shutdown is failed (queue.release()).
        "max_releases": queue.MAX_RELEASES,
    }


@router.get("/deploy-gate", summary="Open waves and running deploy-blocking jobs, for the CI guard")
async def deploy_gate(request: Request, x_admin_secret: str = Header(None)):
    """Pre-deploy guard (AA-725 part 1; meaning changed by AA-993 G5, design D1.4). CI has no RDS access (private
    subnet), so it asks the live API instead of querying the database. CI blocks the roll when `safe_to_deploy` is
    false, which is only while a rerun wave is open (`open_waves`) or a job of a `deploy_blocking` kind is running
    (`blocking_running`) or queued due within 15 min (`blocking_due`); `blocked_by` says why in words. Queued/running
    jobs of other kinds no longer block (the worker drains and releases them, G1-G4); they are still reported in
    `active` / `by_status` / `scheduled_later` (kinds registered with `deploy_gate_exempt` left out, listed in
    `exempt_kinds`). Gated by the admin secret so it is not a public queue-size probe."""
    verify_admin_secret(x_admin_secret)
    from shared.jobs.worker import deploy_gate_status
    return await deploy_gate_status(request.app.state.pool)


@router.get("/workers", summary="Worker liveness, running jobs per worker, queue depth")
async def workers(request: Request):
    health = await queue.worker_health(request.app.state.pool)
    return {
        "workers": [_iso(w) for w in health["workers"]],
        "running": [_iso(r) for r in health["running"]],
        "queued": [_iso(q) for q in health["queued"]],
        "alive_seconds": health["alive_seconds"],
    }


RELEASED_DEFAULT_WINDOW = timedelta(hours=24)


@router.get("/released", summary="Jobs released on worker shutdown since a time")
async def released(request: Request,
                   since: Optional[str] = Query(None, description="ISO-8601 datetime; default: 24 h ago")):
    """AA-993 G1 (ADR 0016 D1.6) — how many running jobs the workers released on shutdown (SIGTERM, drain window
    over) at or after `since`, from each job's `progress.release_log`. `counted` = releases that counted toward
    queue.MAX_RELEASES (no worker of a newer task revision was alive); a deploy's releases are not counted. Read by
    the post-deploy verify (CI cannot reach RDS); machine GET, see api/core/staff_authz.py MACHINE_ROUTES."""
    since_dt = _parse_dt(since, "since") if isinstance(since, str) else None
    if since_dt is None:
        since_dt = datetime.now(timezone.utc) - RELEASED_DEFAULT_WINDOW
    elif since_dt.tzinfo is None:
        since_dt = since_dt.replace(tzinfo=timezone.utc)
    out = await queue.released_since(request.app.state.pool, since_dt)
    return {"since": since_dt.isoformat(), **out, "max_releases": queue.MAX_RELEASES}


@router.get("/jobs/{job_id}", summary="One job")
async def get_job(job_id: str, request: Request):
    pool = request.app.state.pool
    row = await queue.get(pool, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    # AA-687: what the job wrote, so the drawer can link to the tour version / piece / tour.
    try:
        row["links"] = await queue.domain_links(pool, job_id, row.get("payload"))
    except Exception as e:  # a missing domain table must not hide the job itself
        logger.warning("job_links_failed", job_id=job_id, error=str(e)[:200])
        row["links"] = None
    return _redact_for_caller(request, [_iso(row)])[0]


@router.get("/jobs/{job_id}/llm-calls", summary="LLM calls made by one job")
async def job_llm_calls(job_id: str, request: Request, limit: int = Query(500, ge=1, le=2000)):
    pool = request.app.state.pool
    if await queue.get(pool, job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")
    out = await queue.llm_calls(pool, job_id, limit=limit)
    out["calls"] = [_iso(c) for c in out["calls"]]
    out["totals"] = _iso(out["totals"])
    return out


@router.post("/jobs/{job_id}/cancel", summary="Cancel a queued or running job")
async def cancel_job(job_id: str, request: Request, x_admin_secret: str = Header(None),
                     x_admin_user_id: Optional[str] = Header(None)):
    verify_admin_secret(x_admin_secret)
    status = await queue.request_cancel(request.app.state.pool, job_id)
    if status is None:
        raise HTTPException(status_code=409, detail="job is not queued or running")
    if status == "cancelled":  # a queued job ends here, so its domain row must be told now
        await run_terminal_hook(request.app.state.pool, job_id)
    logger.info("admin_job_cancel", job_id=job_id, admin_user=x_admin_user_id, status=status)
    return {"job_id": job_id, "status": status,
            "note": "running jobs stop at their next heartbeat" if status == "running" else None}


@router.post("/jobs/{job_id}/retry", summary="Re-queue a failed/stopped/cancelled job")
async def retry_job(job_id: str, request: Request, x_admin_secret: str = Header(None),
                    x_admin_user_id: Optional[str] = Header(None)):
    verify_admin_secret(x_admin_secret)
    # AA-839: a kind may refuse a retry (a data_erasure apply is single use: approval, Cognito actor, DRY RUN)
    from shared.jobs.worker import load_kinds
    load_kinds()                         # registers every kind (as the ops metrics sampler does), idempotent
    row = await queue.get(request.app.state.pool, job_id)
    kind = get_kind(row["kind"]) if row else None
    refusal = kind.retry_refusal(row) if kind is not None and kind.retry_refusal is not None else None
    if refusal:
        raise HTTPException(status_code=409, detail=refusal)
    if not await queue.retry(request.app.state.pool, job_id):
        raise HTTPException(status_code=409, detail="only failed, stopped_budget or cancelled jobs can be retried")
    logger.info("admin_job_retry", job_id=job_id, admin_user=x_admin_user_id)
    return {"job_id": job_id, "status": "queued"}


# ── AA-1018 — re-queue the jobs an LLM account block failed ──────────────────────────────────────────────────────

RETRY_LLM_DENIED_DEFAULT_WINDOW = timedelta(days=14)


class RetryLlmDeniedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dry_run: bool = True
    # ISO-8601; default: 14 days ago (finished_at of the failed job).
    since: Optional[str] = None
    kinds: list[str] = Field(default_factory=list, max_length=50)
    limit: int = Field(queue.RETRY_REASON_LIMIT, ge=1, le=queue.RETRY_REASON_MAX_LIMIT)


@router.post("/jobs/retry-llm-denied", summary="Re-queue jobs an LLM account block failed (DRY RUN by default)")
async def retry_llm_denied(body: RetryLlmDeniedRequest, request: Request, x_admin_secret: str = Header(None),
                           x_admin_user_id: Optional[str] = Header(None)):
    """AA-1018 — the `failed` jobs whose error starts with `llm_access_denied` (every model was refused at account
    level, AA-900) get a fresh set of attempts, like the one-job retry. DRY RUN by default: lists the jobs an apply
    would re-queue and the ones it would hold (a kind that refuses retries, or a job whose LLM connection is still
    refused: shared/jobs/queue.py `retry_by_reason`). One audit row per call (`jobs_retry_llm_denied`)."""
    verify_admin_secret(x_admin_secret)
    since_dt = _parse_dt(body.since, "since") if body.since else None
    if since_dt is None:
        since_dt = datetime.now(timezone.utc) - RETRY_LLM_DENIED_DEFAULT_WINDOW
    elif since_dt.tzinfo is None:
        since_dt = since_dt.replace(tzinfo=timezone.utc)
    kinds_clean = [k.strip() for k in body.kinds if k and k.strip()]
    pool = request.app.state.pool
    out = await queue.retry_by_reason(pool, queue.RETRY_REASON_DEFAULT, since=since_dt, kinds=kinds_clean,
                                      dry_run=body.dry_run, limit=body.limit)
    actor = f"admin:{x_admin_user_id}" if x_admin_user_id else "admin"
    details = {k: out[k] for k in ("dry_run", "prefix", "since", "kinds", "limit", "matched", "eligible",
                                   "held_count", "requeued")}
    details["requeued_ids"] = out["requeued_ids"][:100]
    details["blocked_connections"] = out["blocked_connections"]
    async with pool.acquire() as db:
        await db.execute(
            "INSERT INTO acp_shared.audit_log (actor, action, resource_type, resource_id, details) "
            "VALUES ($1, $2, $3, $4, $5::jsonb)",
            actor[:64], "jobs_retry_llm_denied", "shared.job", queue.RETRY_REASON_DEFAULT, json.dumps(details))
    logger.info("admin_jobs_retry_llm_denied", admin_user=x_admin_user_id, **{k: details[k] for k in (
        "dry_run", "matched", "eligible", "held_count", "requeued")})
    return out


# ── AA-1018 — the daily LLM connection probe (services/jobs/llm_probe_job.py) ─────────────────────────────────────

class LlmProbeRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dry_run: bool = True


@router.post("/llm-probe/run", summary="Enqueue the LLM connection probe (DRY RUN by default)")
async def llm_probe_run(body: LlmProbeRunRequest, request: Request, x_admin_secret: str = Header(None),
                        x_admin_user_id: Optional[str] = Header(None)):
    """AA-1018 — one `llm_connection_probe` job. DRY RUN by default: its result lists the deployments a live run
    would call, why any is skipped and the estimated cost, with no provider call. A live run (one tiny paid call per
    enabled text deployment) is refused (409) while LLM_DAILY_PROBE is off."""
    verify_admin_secret(x_admin_secret)
    from services.jobs import llm_probe_job
    actor = f"admin:{x_admin_user_id}" if x_admin_user_id else "admin"
    try:
        job_id, _ = await llm_probe_job.enqueue_manual(request.app.state.pool, dry_run=body.dry_run, actor=actor)
    except PermissionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    logger.info("admin_llm_probe_run", job_id=job_id, dry_run=body.dry_run, admin_user=x_admin_user_id)
    return {"job_id": job_id, "kind": llm_probe_job.KIND, "dry_run": body.dry_run,
            "switch_on": llm_probe_job.schedule_enabled()}


# ── AA-825 — retention kinds (services/jobs/retention_job.py) ─────────────────────────────────


@router.get("/retention", summary="Retention kinds: policy, table size, last runs")
async def retention_status(request: Request):
    from services.jobs import retention_job
    return await retention_job.status(request.app.state.pool)


class RetentionRunRequest(BaseModel):
    kind: str
    dry_run: bool = True
    # Required when dry_run is false: the row count Nghiệp approved from the DRY RUN (DM-7.2).
    max_rows: Optional[int] = Field(None, ge=1)
    batch_size: Optional[int] = Field(None, ge=1, le=5000)
    # AA-825 S239 — manual override: an explicit UTC cutoff date (YYYY-MM-DD) instead of the policy
    # window, to prove the rollup + archive path on Dev before data ages. Only for the rolled-up
    # kinds, at least OVERRIDE_MIN_AGE_DAYS (21) days old, tenant usage in a closed month, live
    # runs at most 1,000 rows (retention_job.override_cutoff).
    older_than: Optional[str] = None


@router.post("/retention/run", summary="Enqueue one retention kind (DRY RUN by default)")
async def retention_run(body: RetentionRunRequest, request: Request, x_admin_secret: str = Header(None),
                        x_admin_user_id: Optional[str] = Header(None)):
    verify_admin_secret(x_admin_secret)
    from services.jobs import retention_job
    from shared.jobs.registry import enqueue
    if body.kind not in retention_job.KINDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of {list(retention_job.KINDS)}")
    if body.older_than is not None:
        return await _retention_override(body, request, x_admin_user_id)
    if not body.dry_run:
        if body.max_rows is None:
            raise HTTPException(status_code=422, detail="a live run needs max_rows (the approved DRY RUN count)")
        if body.max_rows > retention_job.RUN_MAX_ROWS:
            raise HTTPException(status_code=422, detail=f"max_rows is capped at {retention_job.RUN_MAX_ROWS} per run")
    payload = {"trigger": "manual", "dry_run": body.dry_run}
    if body.max_rows is not None:
        payload["max_rows"] = body.max_rows
    if body.batch_size is not None:
        payload["batch_size"] = body.batch_size
    created_by = f"admin:{x_admin_user_id}" if x_admin_user_id else "admin"
    job_id, _ = await enqueue(request.app.state.pool, body.kind, payload, created_by=created_by)
    logger.info("admin_retention_run", kind=body.kind, dry_run=body.dry_run, max_rows=body.max_rows,
                job_id=job_id, admin_user=x_admin_user_id)
    return {"job_id": job_id, "kind": body.kind, "dry_run": body.dry_run, "max_rows": body.max_rows}


async def _retention_override(body: RetentionRunRequest, request: Request, x_admin_user_id: Optional[str]):
    """Same checks as the job itself (it re-checks with the DB clock), as 422s before anything is
    enqueued. Logged as trigger manual_override in the job payload and result."""
    from datetime import datetime, timezone
    from services.jobs import retention_job
    from shared.jobs.registry import NonRetryable, enqueue
    payload = {"trigger": retention_job.OVERRIDE_TRIGGER, "older_than": body.older_than, "dry_run": body.dry_run}
    if body.max_rows is not None:
        payload["max_rows"] = body.max_rows
    if body.batch_size is not None:
        payload["batch_size"] = body.batch_size
    try:
        retention_job.override_cutoff(body.kind, payload, datetime.now(timezone.utc).date())
        retention_job.decide_mode(payload, {}, 0, None)
    except NonRetryable as e:
        raise HTTPException(status_code=422, detail=str(e))
    created_by = f"admin:{x_admin_user_id}" if x_admin_user_id else "admin"
    job_id, _ = await enqueue(request.app.state.pool, body.kind, payload, created_by=created_by)
    logger.info("admin_retention_override", kind=body.kind, older_than=body.older_than, dry_run=body.dry_run,
                max_rows=body.max_rows, job_id=job_id, admin_user=x_admin_user_id)
    return {"job_id": job_id, "kind": body.kind, "trigger": retention_job.OVERRIDE_TRIGGER,
            "older_than": body.older_than, "dry_run": body.dry_run, "max_rows": body.max_rows}


@router.post("/retention/schedule", summary="Daily: enqueue every retention kind (scheduler)")
async def retention_schedule(request: Request, x_admin_secret: str = Header(None)):
    """Driven once a day by the ops-probe Lambda (AA-CIS-Infra lambda_src/dfs_balance_check, one more
    path in _PATHS). Each kind decides at run time whether it is a DRY RUN, live or held."""
    verify_admin_secret(x_admin_secret)
    from datetime import datetime, timezone
    from services.jobs import retention_job
    day = datetime.now(timezone.utc).date().isoformat()
    out = await retention_job.schedule_all(request.app.state.pool, day=day)
    logger.info("admin_retention_scheduled", day=day, enqueued=len(out["enqueued"]), skipped=out["skipped"])
    return {"day": day, **out}


@router.post("/staff-reconcile/schedule", summary="Daily: enqueue the staff directory reconcile (scheduler)")
async def staff_reconcile_schedule(request: Request, x_admin_secret: str = Header(None)):
    """Driven once a day by the ops-probe Lambda (AA-CIS-Infra lambda_src/dfs_balance_check, one more path in
    _PATHS). One `staff_directory_reconcile` job per UTC day (idempotency key), report only: it lists the
    WORKFORCE pool and compares it with shared.admin_users. Not skipped during a rerun wave: it reads a few
    directory rows and calls Cognito, nothing a wave shares."""
    verify_admin_secret(x_admin_secret)
    from datetime import datetime, timezone
    from services.jobs import staff_reconcile_job
    day = datetime.now(timezone.utc).date().isoformat()
    job_id, created = await staff_reconcile_job.enqueue_scheduled(request.app.state.pool, day=day)
    logger.info("admin_staff_reconcile_scheduled", day=day, job_id=job_id, created=created)
    return {"day": day, "kind": staff_reconcile_job.KIND, "job_id": job_id, "created": created}


# ── AA-839 — data erasure (services/jobs/erasure_job.py, runbook docs/legal/dsar-erasure-runbook.md) ──────────

ERASURE_APPLY_NEEDS_COGNITO = "An erasure apply needs a company sign-in (Cognito); password sessions cannot"


@router.get("/erasure", summary="Erasure inventory and apply settings")
async def erasure_status():
    """What a tenant / traveller erasure covers in the CIS schemas, and whether an apply is possible here."""
    from services.erasure import inventory as inv
    from services.jobs import erasure_job as ej
    return {
        "kind": ej.KIND,
        "apply_enabled": ej.apply_enabled(),
        "tenant_retention_days": ej.tenant_retention_days(),
        "dry_run_max_age_hours": int(ej.dry_run_max_age().total_seconds() // 3600),
        "tenant_tables": [{"table": t.table, "action": t.action, "reason": t.reason} for t in inv.TENANT_TABLES],
        "traveller_scans": [{"table": s.table, "column": s.column.removeprefix("t."), "action": s.action,
                             "reason": s.reason} for s in inv.TRAVELLER_EXACT + inv.TRAVELLER_TEXT],
        "tenant_checklist": ej.checklist("tenant", None),
        "traveller_checklist": ej.checklist("traveller", None),
    }


class ErasureRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: Literal["tenant", "traveller"]
    tenant_id: Optional[str] = None
    # sha256 hex of the trimmed, lowercased address (runbook section 2 step 2). Never the address.
    email_sha256: Optional[str] = Field(None, max_length=64)
    dry_run: bool = True
    # traveller DRY RUN after an apply (runbook section 3): the job strips the hash from its own row when done
    verify: bool = False
    # apply only: the approved DRY RUN (destructive-ops.md)
    dry_run_job_id: Optional[str] = None
    approved_fingerprint: Optional[str] = Field(None, max_length=64)
    approved_by: Optional[str] = Field(None, max_length=120)
    snapshot_id: Optional[str] = Field(None, max_length=255)


@router.post("/erasure/run", summary="Enqueue a data erasure DRY RUN (default) or an approved apply")
async def erasure_run(body: ErasureRunRequest, request: Request, x_admin_secret: str = Header(None),
                      x_admin_user_id: Optional[str] = Header(None)):
    """DRY RUN by default: counts per CIS table, blockers, the per-app checklist and a fingerprint. An apply needs
    ERASURE_APPLY_ENABLED=true on the API and worker, a verified Cognito staff session, and the fingerprint of an
    approved DRY RUN of the same subject; the job re-checks everything inside its transaction."""
    verify_admin_secret(x_admin_secret)
    from services.jobs import erasure_job as ej
    from shared.jobs.registry import NonRetryable, enqueue
    payload: dict = {"subject": body.subject, "mode": "dry_run" if body.dry_run else "apply"}
    if body.tenant_id is not None:
        payload["tenant"] = body.tenant_id
    if body.email_sha256 is not None:
        payload["email_sha256"] = body.email_sha256
    if body.verify:
        payload["verify"] = True
    for k in ("dry_run_job_id", "approved_fingerprint", "approved_by", "snapshot_id"):
        v = getattr(body, k)
        if v is not None:
            payload[k] = v
    if body.dry_run and any(k in payload for k in ("dry_run_job_id", "approved_fingerprint", "snapshot_id")):
        raise HTTPException(status_code=422, detail="approval fields belong to an apply (dry_run=false)")
    try:
        payload = ej.validate_payload(payload)
    except NonRetryable as e:
        raise HTTPException(status_code=422, detail=str(e))
    created_by = f"admin:{x_admin_user_id}" if x_admin_user_id else "admin"
    if not body.dry_run:
        if not ej.apply_enabled():
            raise HTTPException(status_code=403, detail=f"erasure apply is off here ({ej.APPLY_FLAG} is not 'true')")
        identity = request_staff_identity(request)
        if identity is None or getattr(identity, "auth", None) != "cognito" or not getattr(identity, "admin_id", None):
            raise HTTPException(status_code=403, detail=ERASURE_APPLY_NEEDS_COGNITO)
        payload["actor"] = f"admin:{identity.admin_id}"
        created_by = payload["actor"]
    job_id, _ = await enqueue(request.app.state.pool, ej.KIND, payload, created_by=created_by)
    logger.info("admin_erasure_run", subject=body.subject, mode=payload["mode"], job_id=job_id,
                tenant=payload.get("tenant"), admin_user=x_admin_user_id)
    return {"job_id": job_id, "kind": ej.KIND, "subject": body.subject, "mode": payload["mode"]}
