"""AA-816 (S242 plan step C7) — job kind `staff_directory_reconcile`: WORKFORCE pool vs the staff directory.

Report only (services/staff_reconcile.py has the rules). The handler reads the pool with ListUsers and
ListUsersInGroup (paginated, in a thread), reads shared.admin_users + shared.staff_app_grant, stores the
report as the job's `result` and logs one line `staff_reconcile_summary` with counts only (no email or
username in the log; the result is read by admins on Platform › Staff through
GET /admin/platform/staff-directory/health).

Payload: {"trigger": "schedule" | "manual", "requested_by": "<admin user id>" (manual only)}.

Enqueued by:
  - POST /admin/job-runner/staff-reconcile/schedule   the daily ops-probe Lambda, idempotent per UTC day
                                                      (key staff_directory_reconcile:schedule:<day>)
  - POST /admin/platform/staff-directory/reconcile    on demand from the Staff page (staff.read)

Failures: access denied / pool or client misconfigured -> NonRetryable (a retry can not fix it; the job
fails with the reason). Throttling and network errors are retried once by the runner (max_attempts 2).
"""
from __future__ import annotations

import asyncio
from typing import Optional

import structlog

from services import staff_reconcile as sr
from shared.jobs.registry import JobContext, NonRetryable, job_kind

logger = structlog.get_logger()

KIND = "staff_directory_reconcile"
SCHEDULE_KEY_PREFIX = f"{KIND}:schedule:"


@job_kind(KIND, concurrency=1, max_attempts=2, expected_seconds=120, background=True)
async def run(ctx: JobContext) -> dict:
    issuer = sr.workforce_issuer()
    try:
        pool_id, region = sr.pool_from_issuer(issuer)
    except ValueError as e:
        raise NonRetryable(str(e)) from e
    client = ctx.resources.get("cognito") or await asyncio.to_thread(sr.cognito_client, region)
    await ctx.progress(phase="cognito")
    try:
        pool_data = await asyncio.to_thread(sr.fetch_pool, client, pool_id)
    except sr.CognitoReadError as e:
        logger.warning("staff_reconcile_cognito_error", job_id=ctx.job_id, error=str(e)[:300])
        raise NonRetryable(str(e)) from e
    await ctx.progress(phase="directory")
    rows, grants = await sr.load_directory(ctx.pool)
    report = sr.reconcile(pool_data["users"], pool_data["groups"], rows, grants,
                          issuer=issuer, breakglass=sr.breakglass_email())
    report.update(kind=KIND, trigger=(ctx.payload or {}).get("trigger", "manual"), pool_id=pool_id,
                  checked_at=sr.now_iso())
    await ctx.progress(phase="done", drift_total=report["drift_total"], ready=report["readiness"]["ready"])
    log = logger.warning if report["drift_total"] else logger.info
    log("staff_reconcile_summary", job_id=ctx.job_id, trigger=report["trigger"], **sr.summary_fields(report))
    ctx.set_result(report)
    return report


async def enqueue_scheduled(pool, *, day: str, created_by: str = "scheduler:aa-816") -> tuple[str, bool]:
    """The daily run, idempotent per UTC day: a Scheduler retry or a second call the same day adds nothing."""
    from shared.jobs.registry import enqueue

    return await enqueue(pool, KIND, {"trigger": "schedule"}, idempotency_key=f"{SCHEDULE_KEY_PREFIX}{day}",
                         created_by=created_by)


_ACTIVE_SQL = f"""
    SELECT id::text AS id FROM shared.job
     WHERE kind = '{KIND}' AND status IN ('queued', 'running')
     ORDER BY created_at DESC LIMIT 1
"""


async def enqueue_manual(pool, *, requested_by: Optional[str]) -> tuple[str, bool]:
    """An on-demand run; a queued or running one is returned instead of adding a second."""
    from shared.jobs.registry import enqueue

    active = await pool.fetchval(_ACTIVE_SQL)
    if active:
        return str(active), False
    payload = {"trigger": "manual"}
    if requested_by:
        payload["requested_by"] = requested_by
    return await enqueue(pool, KIND, payload, created_by=f"admin:{requested_by}" if requested_by else "admin")


_LATEST_SQL = f"""
    SELECT DISTINCT ON (bucket) bucket, id::text AS id, status, created_at, started_at, finished_at,
           result, error
      FROM (SELECT j.*, CASE WHEN j.status = 'succeeded' THEN 'succeeded'
                             WHEN j.status IN ('queued', 'running') THEN 'active'
                             ELSE 'failed' END AS bucket
              FROM shared.job j WHERE j.kind = '{KIND}') x
     ORDER BY bucket, created_at DESC
"""


def _iso(v):
    return v.isoformat() if v is not None else None


async def health(pool) -> dict:
    """The latest succeeded report, plus the latest active and failed run (if any) for the Staff page."""
    import json

    rows = await pool.fetch(_LATEST_SQL)
    out: dict = {"kind": KIND, "latest": None, "active": None, "last_failed": None}
    for r in rows:
        job = {"job_id": r["id"], "status": r["status"], "created_at": _iso(r["created_at"]),
               "started_at": _iso(r["started_at"]), "finished_at": _iso(r["finished_at"])}
        if r["bucket"] == "succeeded":
            result = r["result"]
            out["latest"] = {**job, "result": json.loads(result) if isinstance(result, str) else result}
        elif r["bucket"] == "active":
            out["active"] = job
        else:
            out["last_failed"] = {**job, "error": (r["error"] or "")[:500] or None}
    # A failure older than the latest success is history, not news.
    lf, latest = out["last_failed"], out["latest"]
    if lf and latest and (lf["created_at"] or "") < (latest["created_at"] or ""):
        out["last_failed"] = None
    return out
