"""AA-650 — admin view of the durable job queue (shared.job).

  GET  /admin/job-runner/jobs              list, filter by kind/status
  GET  /admin/job-runner/jobs/{id}         one job (status, progress, result, cost, error)
  POST /admin/job-runner/jobs/{id}/cancel  queued -> cancelled; running -> flagged, worker stops it
  POST /admin/job-runner/jobs/{id}/retry   failed / stopped_budget / cancelled -> queued
  GET  /admin/job-runner/summary           counts per kind and status, registered kinds
  GET  /admin/job-runner/jobs/{id}/llm-calls  LLM calls the job made (AA-687)
  GET  /admin/job-runner/workers           worker liveness, running per worker, queue depth (AA-687)
  GET  /admin/job-runner/retention         retention kinds: policy, table size, last runs (AA-825)
  POST /admin/job-runner/retention/run     enqueue one retention kind (DRY RUN unless approved);
                                           with older_than: an admin-only manual override proof run
  POST /admin/job-runner/retention/schedule  the daily ops scheduler enqueues every retention kind
  POST /admin/job-runner/rds-role-connections/schedule  every 5 min: one RDS per-role connection
                                           sample to CloudWatch AA/RDS (AA-832)

Not under /admin/jobs: that path serves the older shared.pipeline_jobs (AA-223, A1 run-tour).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import structlog
from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from api.routers.admin import verify_admin_secret
from shared.jobs import queue
from shared.jobs.registry import kinds, run_terminal_hook

logger = structlog.get_logger()

router = APIRouter(prefix="/admin/job-runner", tags=["admin-job-runner"])

_STATUSES = ("queued", "running") + queue.TERMINAL


_TIME_KEYS = ("locked_until", "run_after", "created_at", "started_at", "finished_at", "last_seen_at",
              "stopped_at", "last_reap_at", "oldest_started_at", "oldest_created_at", "first_at", "last_at")


def _iso(d: dict) -> dict:
    for k in _TIME_KEYS:
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    return d


def _parse_dt(value: Optional[str], field: str) -> Optional[datetime]:
    """AA-755 — parse an ISO-8601 datetime filter (or a plain date). 422 on a bad value so a typo in
    the URL is a clear error, not a silently-ignored filter."""
    if value is None or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{field} must be an ISO-8601 datetime")


@router.get("/jobs", summary="AA-650 — list jobs")
async def list_jobs(request: Request, kind: Optional[str] = None,
                    status: Optional[str] = Query(None, description="|".join(_STATUSES)),
                    limit: int = Query(50, ge=1, le=200),
                    page: int = Query(1, ge=1, description="AA-755: 1-based page (with page_size)"),
                    page_size: Optional[int] = Query(None, ge=1, le=200,
                                                     description="AA-755: rows per page; overrides limit"),
                    sort: str = Query("created_at", description="AA-755: " + "|".join(queue.JOB_SORTS)),
                    sort_dir: str = Query("desc", description="asc|desc"),
                    tour_id: Optional[str] = Query(None, description="AA-687: jobs whose payload names this tour"),
                    since: Optional[str] = Query(None, description="AA-755: created_at >= this ISO datetime"),
                    until: Optional[str] = Query(None, description="AA-755: created_at < this ISO datetime"),
                    created_by: Optional[str] = Query(None, description="AA-755: exact created_by match"),
                    q: Optional[str] = Query(None, description="AA-755: id prefix / kind / created_by search")):
    if status is not None and status not in _STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {_STATUSES}")
    if sort not in queue.JOB_SORTS:
        raise HTTPException(status_code=422, detail=f"sort must be one of {sorted(queue.JOB_SORTS)}")
    if sort_dir.lower() not in ("asc", "desc"):
        raise HTTPException(status_code=422, detail="sort_dir must be 'asc' or 'desc'")
    since_dt = _parse_dt(since, "since")
    until_dt = _parse_dt(until, "until")
    q_clean = q.strip() if q and q.strip() else None
    size = page_size or limit
    offset = (page - 1) * size
    pool = request.app.state.pool
    filters = dict(kind=kind, status=status, tour_id=tour_id, since=since_dt, until=until_dt,
                   created_by=created_by, q=q_clean)
    rows = await queue.list_jobs(pool, limit=size, offset=offset, sort=sort, sort_dir=sort_dir,
                                 **filters)
    total = await queue.count_jobs(pool, **filters)
    return {
        "jobs": [_iso(r) for r in rows],
        "total": total,
        "pagination": {"page": page, "page_size": size, "total": total,
                       "pages": max(1, (total + size - 1) // size)},
    }


@router.get("/summary", summary="AA-650 — counts per kind/status + registered kinds")
async def summary(request: Request):
    rows = await request.app.state.pool.fetch(
        "SELECT kind, status, count(*)::int AS n, "
        f"coalesce(sum(j.cost_usd + {queue.LLM_COST_SQL}), 0)::float AS cost_usd "
        "FROM shared.job j WHERE created_at >= now() - interval '30 days' GROUP BY kind, status")
    return {
        "counts": [dict(r) for r in rows],
        "kinds": [{"kind": k.name, "concurrency": k.concurrency, "max_attempts": k.max_attempts,
                   "expected_seconds": k.expected_seconds}
                  for k in kinds().values()],
        # AA-687: a job released this many times on shutdown is failed (queue.release()).
        "max_releases": queue.MAX_RELEASES,
    }


@router.get("/deploy-gate", summary="AA-725 — queued+running count for the pre-deploy CI guard")
async def deploy_gate(request: Request, x_admin_secret: str = Header(None)):
    """Pre-deploy guard (AA-725 part 1). CI has no RDS access (private subnet), so it asks the live
    API instead of querying shared.job directly. Returns the number of jobs that a deploy would
    interrupt; CI blocks the roll when `safe_to_deploy` is false. Gated by the admin secret so it
    is not a public queue-size probe.

    AA-832: kinds registered with `deploy_gate_exempt` (today only the 5-minute
    `ops_rds_role_connections` sample) are not counted, and are listed in `exempt_kinds` so the CI
    log says why a queued row of theirs did not block."""
    verify_admin_secret(x_admin_secret)
    from shared.jobs.worker import load_kinds
    load_kinds()  # the exemption is a registry flag: make sure every kind is registered here
    exempt = sorted(k.name for k in kinds().values() if k.deploy_gate_exempt)
    counts = await queue.active_count(request.app.state.pool, exempt_kinds=exempt)
    return {**counts, "safe_to_deploy": counts["active"] == 0, "exempt_kinds": exempt}


@router.get("/workers", summary="AA-687 — worker liveness, running jobs per worker, queue depth")
async def workers(request: Request):
    health = await queue.worker_health(request.app.state.pool)
    return {
        "workers": [_iso(w) for w in health["workers"]],
        "running": [_iso(r) for r in health["running"]],
        "queued": [_iso(q) for q in health["queued"]],
        "alive_seconds": health["alive_seconds"],
    }


@router.get("/jobs/{job_id}", summary="AA-650 — one job")
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
    return _iso(row)


@router.get("/jobs/{job_id}/llm-calls", summary="AA-687 — LLM calls made by one job")
async def job_llm_calls(job_id: str, request: Request, limit: int = Query(500, ge=1, le=2000)):
    pool = request.app.state.pool
    if await queue.get(pool, job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")
    out = await queue.llm_calls(pool, job_id, limit=limit)
    out["calls"] = [_iso(c) for c in out["calls"]]
    out["totals"] = _iso(out["totals"])
    return out


@router.post("/jobs/{job_id}/cancel", summary="AA-650 — cancel a queued or running job")
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


@router.post("/jobs/{job_id}/retry", summary="AA-650 — re-queue a failed/stopped/cancelled job")
async def retry_job(job_id: str, request: Request, x_admin_secret: str = Header(None),
                    x_admin_user_id: Optional[str] = Header(None)):
    verify_admin_secret(x_admin_secret)
    if not await queue.retry(request.app.state.pool, job_id):
        raise HTTPException(status_code=409, detail="only failed, stopped_budget or cancelled jobs can be retried")
    logger.info("admin_job_retry", job_id=job_id, admin_user=x_admin_user_id)
    return {"job_id": job_id, "status": "queued"}


# ── AA-825 — retention kinds (services/jobs/retention_job.py) ─────────────────────────────────


@router.get("/retention", summary="AA-825 — retention kinds: policy, table size, last runs")
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


@router.post("/retention/run", summary="AA-825 — enqueue one retention kind (DRY RUN by default)")
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


@router.post("/retention/schedule", summary="AA-825 — daily: enqueue every retention kind (scheduler)")
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


# ── AA-832 — RDS connections per LOGIN role (services/jobs/ops_rds_role_connections_job.py) ─────


@router.post("/rds-role-connections/schedule",
             summary="AA-832 — every 5 min: enqueue one RDS per-role connection sample (scheduler)")
async def rds_role_connections_schedule(request: Request, x_admin_secret: str = Header(None)):
    """Driven every 5 minutes by the ops-probe Lambda (AA-CIS-Infra lambda_src/dfs_balance_check, its
    own EventBridge schedule naming this path). Only enqueues: the worker runs the query and the
    CloudWatch PutMetricData. While a job of the kind is still queued or running nothing new is
    enqueued (`already_pending`), so a stopped worker leaves one waiting row, not a pile."""
    verify_admin_secret(x_admin_secret)
    from services.jobs import ops_rds_role_connections_job
    out = await ops_rds_role_connections_job.schedule(request.app.state.pool)
    logger.info("admin_rds_role_connections_scheduled", **out)
    return out
