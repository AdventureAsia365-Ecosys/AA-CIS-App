"""AA-1003 R1 — report core job kinds (root AA-1001 automation-plan.md §1-2; registry services/reports/registry/).

  kpi_snapshot_daily  payload {"day": "YYYY-MM-DD" (ICT; default the day the daily chain snapshots now),
                               "trigger": "schedule" | "manual"}
                      Computes the R1 KPIs (services/reports/kpis.py: CIS tables only, no LLM, no paid API) for the
                      day and upserts them into shared.kpi_daily (re-running a day overwrites its values). A computer
                      that fails does not stop the others: their values are written, then the job raises so the
                      runner retries (a failed KPI is never a silent success). A scheduled run that wrote every KPI
                      queues report_generate for each CIS-generated daily/live report (EX-04, EX-06) for the same
                      day (one scheduled run per report and day, shared.report_run_schedule_uq).
                      Scheduled once a day by the api's ops metrics sampler (api/core/ops_metrics_sampler.py,
                      _reports_part) from 00:30 UTC, idempotency key kpi_snapshot_daily:<day>.
  report_generate     payload {"run_id": <shared.report_run id>, "report_id", "period_start", "period_end"}
                      Renders the report (services/reports/generate.py) into its report_run row: status running ->
                      succeeded (summary, output JSON, output_markdown) or failed (error). Created with its run row by
                      enqueue_report_run (POST /admin/reports/{id}/run, or the chain above). A job that ends without
                      success (failed after its retries, cancelled) leaves the run failed (on_terminal).

Both are background kinds (never take the worker's last free slots) and cost $0 (DB reads only).
"""
from __future__ import annotations

import json
from datetime import date
from typing import Optional

import structlog

from services.reports import generate, periods, store
from services.reports.kpis import COMPUTERS, DayContext, KpiValue
from services.reports.registry import Report, load
from shared.jobs.registry import JobContext, NonRetryable, enqueue, job_kind

logger = structlog.get_logger()

SNAPSHOT_KIND = "kpi_snapshot_daily"
REPORT_KIND = "report_generate"
SCHEDULER = "scheduler:aa-1003"
CHAIN_SCHEDULES = ("daily", "live")


def snapshot_key(day: date) -> str:
    return f"{SNAPSHOT_KIND}:{day.isoformat()}"


def _json(v):
    return json.loads(v) if isinstance(v, str) else (v or {})


def resolve_day(payload: dict, now=None) -> date:
    """The ICT day to snapshot: payload day, else the day the daily chain snapshots now. Refused (NonRetryable): a day
    that has not ended, or one older than periods.BACKFILL_DAYS (raw ledgers would undercount)."""
    raw = payload.get("day")
    try:
        day = periods.parse_day(raw) if raw is not None else periods.snapshot_day(now)
    except ValueError as e:
        raise NonRetryable(str(e))
    newest = periods.latest_closed_day(now)
    if day > newest:
        raise NonRetryable(f"day {day.isoformat()} has not ended yet (newest closed ICT day: {newest.isoformat()})")
    if day < periods.oldest_allowed_day(now):
        raise NonRetryable(f"day {day.isoformat()} is older than {periods.BACKFILL_DAYS} days")
    return day


async def compute_day(conn, ctx: DayContext, allowed: set[str]) -> tuple[list[KpiValue], dict[str, str]]:
    """Every computer for one day -> (values of registered snapshot KPIs, {computer: error})."""
    values: list[KpiValue] = []
    errors: dict[str, str] = {}
    for name, (fn, _ids) in COMPUTERS.items():
        try:
            values += [v for v in await fn(conn, ctx) if v.kpi_id in allowed]
        except Exception as e:      # one broken KPI must not hide the others; the job still raises below
            errors[name] = f"{type(e).__name__}: {str(e)[:300]}"
            logger.warning("kpi_snapshot_part_failed", part=name, day=ctx.day.isoformat(), error=errors[name])
    return values, errors


async def enqueue_report_run(pool, report: Report, first: date, last: date, *, trigger: str, triggered_by: str,
                             parent_job_id: Optional[str] = None) -> Optional[dict]:
    """A queued shared.report_run row + its report_generate job. None when a scheduled run of the same report and
    period exists already. The run row comes first, so the job always finds it."""
    async with pool.acquire() as conn:
        run_id = await store.create_run(conn, report.id, first, last, trigger=trigger, triggered_by=triggered_by)
        if run_id is None:
            return None
        try:
            job_id, _ = await enqueue(
                conn, REPORT_KIND,
                {"run_id": run_id, "report_id": report.id, "period_start": first.isoformat(),
                 "period_end": last.isoformat()},
                idempotency_key=f"{REPORT_KIND}:{run_id}", created_by=triggered_by, parent_job_id=parent_job_id)
        except Exception as e:
            await store.finish_failed(conn, run_id, f"could not queue the job: {type(e).__name__}: {str(e)[:300]}")
            raise
        await store.set_job(conn, run_id, job_id)
    return {"run_id": run_id, "job_id": job_id, "report_id": report.id, "period_start": first.isoformat(),
            "period_end": last.isoformat(), "status": "queued", "trigger": trigger}


async def chain_reports(pool, day: date, parent_job_id: Optional[str]) -> list[dict]:
    reg = load()
    out = []
    for report in reg.runnable():
        if report.schedule not in CHAIN_SCHEDULES:
            continue
        run = await enqueue_report_run(pool, report, day, day, trigger="schedule", triggered_by=SCHEDULER,
                                       parent_job_id=parent_job_id)
        out.append({"report_id": report.id, "run_id": run and run["run_id"], "created": run is not None})
    return out


@job_kind(SNAPSHOT_KIND, concurrency=1, max_attempts=3, expected_seconds=300, background=True)
async def run_snapshot(ctx: JobContext) -> dict:
    payload = ctx.payload or {}
    trigger = payload.get("trigger", "manual")
    if trigger not in ("schedule", "manual"):
        raise NonRetryable(f"trigger {trigger!r} is not schedule or manual")
    day = resolve_day(payload)
    reg = load()
    snap = reg.snapshot_kpis()
    units = {k.id: k.unit for k in snap}
    versions = {k.id: k.version for k in snap}
    ctx_day = DayContext.for_day(day)
    async with ctx.pool.acquire() as conn:
        values, errors = await compute_day(conn, ctx_day, set(units))
        async with conn.transaction():
            written = await store.upsert_kpis(conn, day, values, units=units, versions=versions, job_id=ctx.job_id)
    by_kpi: dict[str, int] = {}
    for v in values:
        by_kpi[v.kpi_id] = by_kpi.get(v.kpi_id, 0) + 1
    result = {"kind": SNAPSHOT_KIND, "day": day.isoformat(), "trigger": trigger, "rows_written": written,
              "by_kpi": by_kpi, "no_value": sorted(set(units) - set(by_kpi)), "errors": errors, "chained": []}
    await ctx.progress(phase="written", day=day.isoformat(), rows_written=written)
    if errors:
        ctx.set_result(result)
        raise RuntimeError(f"kpi_snapshot_daily {day.isoformat()}: {len(errors)} part(s) failed: "
                           + "; ".join(f"{k}: {v}" for k, v in errors.items()))
    if trigger == "schedule":
        result["chained"] = await chain_reports(ctx.pool, day, ctx.job_id)
    logger.info("kpi_snapshot_done", day=day.isoformat(), rows=written, chained=len(result["chained"]))
    ctx.set_result(result)
    return result


async def _report_terminal(pool, job_row: dict) -> None:
    """The job ended without success (failed after retries, cancelled, budget): the run must not stay queued or
    running."""
    run_id = _json(job_row.get("payload")).get("run_id")
    if not run_id:
        return
    async with pool.acquire() as conn:
        run = await store.get_run(conn, run_id)
        if run and run["status"] in ("queued", "running"):
            await store.finish_failed(conn, run_id, job_row.get("error") or f"job {job_row.get('status')}")


@job_kind(REPORT_KIND, concurrency=2, max_attempts=2, expected_seconds=120, background=True,
          on_terminal=_report_terminal)
async def run_report(ctx: JobContext) -> dict:
    run_id = (ctx.payload or {}).get("run_id")
    if not run_id:
        raise NonRetryable("payload has no run_id")
    reg = load()
    async with ctx.pool.acquire() as conn:
        run = await store.get_run(conn, run_id)
        if run is None:
            raise NonRetryable(f"report run {run_id} not found")
        report = reg.report(run["report_id"])
        if report is None or not report.runnable:
            msg = f"report {run['report_id']} has no generator here"
            await store.finish_failed(conn, run_id, msg)
            raise NonRetryable(msg)
        await store.mark_running(conn, run_id, ctx.job_id)
        try:
            out = await generate.render(conn, reg, report, run["period_start"], run["period_end"])
        except Exception as e:
            await store.finish_failed(conn, run_id, f"{type(e).__name__}: {str(e)[:1000]}")
            raise
        await store.finish_ok(conn, run_id, summary=out.summary, output=out.output, markdown=out.markdown)
    result = {"kind": REPORT_KIND, "run_id": run_id, "report_id": report.id,
              "period_start": run["period_start"].isoformat(), "period_end": run["period_end"].isoformat(),
              "summary": out.summary}
    ctx.set_result(result)
    logger.info("report_generated", run_id=run_id, report_id=report.id, summary=out.summary)
    return result


SNAPSHOT_EXISTS_SQL = "SELECT EXISTS (SELECT 1 FROM shared.job WHERE idempotency_key = $1)"


async def enqueue_scheduled_snapshot(pool, now=None) -> tuple[Optional[str], bool, date]:
    """The daily snapshot of the day the chain covers now (idempotent per day). Called by the ops metrics sampler on
    every tick: one indexed EXISTS (job_idempotency_key_uq) first, so only the first tick of the day INSERTs."""
    day = periods.snapshot_day(now)
    key = snapshot_key(day)
    if await pool.fetchval(SNAPSHOT_EXISTS_SQL, key):
        return None, False, day
    job_id, created = await enqueue(pool, SNAPSHOT_KIND, {"trigger": "schedule", "day": day.isoformat()},
                                    idempotency_key=key, created_by=SCHEDULER)
    return job_id, created, day
