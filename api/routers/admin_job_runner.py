"""AA-650 — admin view of the durable job queue (shared.job).

  GET  /admin/job-runner/jobs              list, filter by kind/status
  GET  /admin/job-runner/jobs/{id}         one job (status, progress, result, cost, error)
  POST /admin/job-runner/jobs/{id}/cancel  queued -> cancelled; running -> flagged, worker stops it
  POST /admin/job-runner/jobs/{id}/retry   failed / stopped_budget / cancelled -> queued
  GET  /admin/job-runner/summary           counts per kind and status, registered kinds
  GET  /admin/job-runner/jobs/{id}/llm-calls  LLM calls the job made (AA-687)
  GET  /admin/job-runner/workers           worker liveness, running per worker, queue depth (AA-687)

Not under /admin/jobs: that path serves the older shared.pipeline_jobs (AA-223, A1 run-tour).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import structlog
from fastapi import APIRouter, Header, HTTPException, Query, Request

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
    is not a public queue-size probe."""
    verify_admin_secret(x_admin_secret)
    counts = await queue.active_count(request.app.state.pool)
    return {**counts, "safe_to_deploy": counts["active"] == 0}


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
