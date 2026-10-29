"""AA-1003 R1 — Platform › Reports: the report register (AA-1001 EX-06), runs and on-request generation.

  GET  /admin/reports                         every report of the registry (catalog.yaml copy) with its last run, what
                                              it covers and its due state (ok / due / overdue / manual / not_automated);
                                              all rows at once (63 today, a fixed registry: the page filters them)
  GET  /admin/reports/{report_id}/runs        one report: its catalogue entry (sections, KPIs with definitions) and
                                              its runs, newest first (?page=&page_size=, server-side paging)
  GET  /admin/reports/runs/{run_id}           one run with its output (JSON) and rendered Markdown
  POST /admin/reports/{report_id}/run         {period_start?, period_end?} (ICT days): queue report_generate for a
                                              report the CIS engine renders (EX-04, EX-06); 409 for any other, 202
                                              with the run and job ids. Default period: the day the daily chain covers.

Who may call (api/core/staff_authz.py POLICY): reads ops.read, the run ops.write (admin only, as /admin/overview and
the job runner). The router is included without an include prefix (staff_policy reads scope["route"].path).
Numbers come from stored rows only (shared.kpi_daily, shared.report_run): no LLM, no paid API.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import date
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from api.core.staff_authz import staff_policy
from services.jobs import report_jobs
from services.reports import generate, periods, store
from services.reports.registry import load

logger = structlog.get_logger()
router = APIRouter(tags=["admin-reports"], dependencies=[Depends(staff_policy)])

_REPORT_ID = re.compile(r"^[A-Z]{2,4}-\d{2}$")
MAX_PERIOD_DAYS = 31


def _pool(request: Request):
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="DB not ready")
    return pool


def _actor(x_admin_user_id: Optional[str]) -> str:
    return f"admin:{(x_admin_user_id or 'admin').strip()[:56] or 'admin'}"


def _report(report_id: str):
    report = load().report(report_id) if _REPORT_ID.match(report_id or "") else None
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return report


def _iso(v):
    return v.isoformat() if v is not None else None


def _run_view(row: dict, *, with_output: bool = False) -> dict:
    out = {
        "id": str(row["id"]), "report_id": row["report_id"], "period_start": _iso(row["period_start"]),
        "period_end": _iso(row["period_end"]), "status": row["status"], "trigger": row["trigger"],
        "triggered_by": row["triggered_by"], "job_id": str(row["job_id"]) if row["job_id"] else None,
        "started_at": _iso(row["started_at"]), "finished_at": _iso(row["finished_at"]),
        "created_at": _iso(row["created_at"]), "summary": row["summary"], "error": row["error"],
        "s3_key": row["s3_key"],
    }
    if with_output:
        output = row.get("output")
        if isinstance(output, str):
            output = json.loads(output)
        out["output"] = output
        out["markdown"] = row.get("output_markdown")
    return out


@router.get("/admin/reports", summary="Report register: every report with its last run and due state")
async def list_reports(request: Request) -> dict:
    reg = load()
    now = periods.now_utc()
    async with _pool(request).acquire() as conn:
        last = await store.last_runs(conn)
        latest_kpi_day = await store.latest_kpi_day(conn)
    rows = generate.register_rows(reg, last, now)
    states: dict[str, int] = {}
    for r in rows:
        states[r["due"]["state"]] = states.get(r["due"]["state"], 0) + 1
    return {
        "registry": {"version": reg.version, "imported_from": reg.imported_from, "reports": len(reg.reports),
                     "kpis": len(reg.kpis), "snapshot_kpis": [k.id for k in reg.snapshot_kpis()]},
        "generated_at": now.isoformat(),
        "snapshot_day": periods.snapshot_day(now).isoformat(),
        "latest_kpi_day": _iso(latest_kpi_day),
        "states": states,
        "reports": rows,
    }


@router.get("/admin/reports/runs/{run_id}", summary="One report run with its output")
async def get_run(run_id: str, request: Request) -> dict:
    try:
        uuid.UUID(run_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="Run not found")
    async with _pool(request).acquire() as conn:
        row = await store.get_run(conn, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return _run_view(row, with_output=True)


@router.get("/admin/reports/{report_id}/runs", summary="One report: its catalogue entry and its runs")
async def list_report_runs(report_id: str, request: Request, page: int = Query(1, ge=1, le=10_000),
                           page_size: int = Query(20, ge=1, le=100)) -> dict:
    report = _report(report_id)
    reg = load()
    async with _pool(request).acquire() as conn:
        runs, total = await store.list_runs(conn, report.id, limit=page_size, offset=(page - 1) * page_size)
        last = (await store.last_runs(conn)).get(report.id)
    kpis = []
    for kid in report.kpis:
        k = reg.kpi(kid)
        kpis.append({**k.as_dict(), "threshold": k.threshold_for(report.id)})
    return {
        "report": {**report.detail(), "kpi_details": kpis,
                   "due": generate.due_state(report, last, periods.now_utc()),
                   "covered_to": _iso((last or {}).get("covered_to"))},
        "runs": [_run_view(r) for r in runs], "total": total, "page": page, "page_size": page_size,
    }


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    period_start: Optional[date] = None
    period_end: Optional[date] = None


def resolve_period(report, body: RunRequest, now=None) -> tuple[date, date]:
    """The period of a manual run: the given days, else the report's default; 422 when the period has not ended, is
    older than the backfill window or longer than MAX_PERIOD_DAYS."""
    first, last = generate.default_period(report, now)
    if body.period_end is not None:
        last = body.period_end
        first = body.period_start or last
    elif body.period_start is not None:
        first = last = body.period_start
    if last < first:
        raise HTTPException(status_code=422, detail="period_end is before period_start")
    if last > periods.latest_closed_day(now):
        raise HTTPException(status_code=422, detail="The period has not ended yet (days are ICT)")
    if first < periods.oldest_allowed_day(now):
        raise HTTPException(status_code=422, detail=f"The period starts more than {periods.BACKFILL_DAYS} days ago")
    if (last - first).days + 1 > MAX_PERIOD_DAYS:
        raise HTTPException(status_code=422, detail=f"A period is at most {MAX_PERIOD_DAYS} days")
    return first, last


@router.post("/admin/reports/{report_id}/run", status_code=202, summary="Generate a report now (admin)")
async def run_report(report_id: str, request: Request, body: Optional[RunRequest] = None,
                     x_admin_user_id: Optional[str] = Header(None)) -> dict:
    report = _report(report_id)
    if not report.runnable:
        raise HTTPException(status_code=409, detail="This report is not generated here yet")
    first, last = resolve_period(report, body or RunRequest())
    actor = _actor(x_admin_user_id)
    out = await report_jobs.enqueue_report_run(_pool(request), report, first, last, trigger="manual",
                                               triggered_by=actor)
    logger.info("admin_report_run_queued", report_id=report.id, run_id=out["run_id"], job_id=out["job_id"],
                period_start=out["period_start"], period_end=out["period_end"], admin_user=x_admin_user_id)
    return out
