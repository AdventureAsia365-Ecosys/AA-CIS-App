"""AA-1173 (S290, compliance readiness phase 2) — job kind ``compliance_db_checks``: the read-only database checks of
Platform › Compliance (evidence source ``cis_db``), once a day on the worker instead of only on the page's Refresh.

What it runs: ``api.services.compliance_db.collect`` (the retention runs of the job runner, the live retention runs,
the X2 RLS catalogue rules over every app schema split in ``db_rls_policies`` and ``db_grants``, and the age of the
newest monthly posture review), then ``compliance.ingest`` of those rows with the validity window of ``cis_db``
(cis.compliance.validity_hours.cis_db). Database reads only (pg_catalog views and CIS tables as cis_app), $0. Writes:
the evidence rows (append-only table, one row per check and run).

Payload: {"trigger": "schedule" | "manual"}. Result: rows, new, counts per status.

Schedule: queued by the api's ops metrics sampler (api/core/ops_metrics_sampler.py) once per UTC day from
``SCHEDULE_HOUR_UTC`` (idempotency key ``compliance_db_checks:<day>``), so two api tasks during a rolling deploy queue
one run. Switch ``COMPLIANCE_DB_CHECKS`` (ECS env, default ON: the run costs nothing). The heartbeat of source
``cis_db`` on the page (cis.compliance.collector_heartbeat_hours) shows a schedule that stopped. max_attempts 2 (a
transient DB error retries once; the next day is the next run), monitoring lane (background with monitor=True: its own
worker slots, AA-1178), deploy-gate exempt (it may sit queued across a deploy; a re-run adds rows with a new collected_at, harmless).
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from typing import Optional

import structlog

from shared.jobs.registry import JobContext, enqueue, job_kind

logger = structlog.get_logger()

KIND = "compliance_db_checks"
SCHEDULER = "scheduler:aa-1173"
SWITCH_ENV = "COMPLIANCE_DB_CHECKS"
SCHEDULE_HOUR_UTC = 3   # technical-constant: after the nightly retention runs, before the working day in Asia
_EXISTS_SQL = "SELECT EXISTS (SELECT 1 FROM shared.job WHERE idempotency_key = $1)"


def schedule_enabled() -> bool:
    """``COMPLIANCE_DB_CHECKS`` (default on): off / false / 0 / no stops the schedule."""
    return (os.environ.get(SWITCH_ENV) or "on").strip().lower() not in ("off", "false", "0", "no")


def schedule_key(day: date) -> str:
    return f"{KIND}:{day.isoformat()}"


async def enqueue_daily(pool, now: Optional[datetime] = None) -> tuple[Optional[str], bool, date]:
    """Today's (UTC) run from SCHEDULE_HOUR_UTC on (idempotent per day); nothing before the hour or with the switch
    off."""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    if not schedule_enabled() or now.hour < SCHEDULE_HOUR_UTC:
        return None, False, today
    key = schedule_key(today)
    if await pool.fetchval(_EXISTS_SQL, key):
        return None, False, today
    job_id, created = await enqueue(pool, KIND, {"trigger": "schedule"}, idempotency_key=key, created_by=SCHEDULER,
                                    max_attempts=2)
    return job_id, created, today


async def tick_part(pool, kinds: dict, now: datetime, out: dict) -> None:
    """The ops metrics sampler's part: today's run, only while the kind is registered. Field
    ``compliance_db_checks`` when a run was created."""
    if KIND not in kinds:
        return
    job_id, created, day = await enqueue_daily(pool, now=now)
    if created:
        out[KIND] = {"job_id": job_id, "day": day.isoformat()}


@job_kind(KIND, concurrency=1, max_attempts=2, expected_seconds=120, background=True, deploy_gate_exempt=True,
          monitor=True)
async def run(ctx: JobContext) -> dict:
    from api.services import compliance, compliance_db   # noqa: PLC0415 - the api modules, imported on use

    now = datetime.now(timezone.utc)
    rows = await compliance_db.collect(ctx.pool, now)
    new = await compliance.ingest(ctx.pool, rows, await compliance._validity_hours(compliance_db.SOURCE))
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    result = {"kind": KIND, "trigger": (ctx.payload or {}).get("trigger", "manual"), "rows": len(rows), "new": new,
              "by_status": by_status, "checks": sorted(r["check_id"] for r in rows)}
    logger.info("compliance_db_checks_done", job_id=ctx.job_id, rows=len(rows), new=new, **by_status)
    ctx.set_result(result)
    return result
