"""AA-900 (S271 W0, root ADR 0020 "Measurement") — the content quality funnel, read side ($0).

  GET /admin/quality/funnel?history=14   the latest succeeded SCHEDULED CIS-08 snapshot as stored
                                         (services/quality/funnel.py: per workflow and ISO week: created, delivered
                                         vs held, first-pass yield, attempts and LLM USD per delivered object, every
                                         failing gate per attempt, stages with rung / fallback / cut shares, the watch
                                         window and its findings, each flagged new or ``repeat``), the headline totals
                                         of the last `history` scheduled daily snapshots (1-90), and the newest manual
                                         run apart (`manual_run`: id, day, summary; its output on
                                         GET /admin/reports/runs/{id}).

The snapshot is computed by the report chain (report_generate, CIS-08) and never recomputed here. Only scheduled
runs queue the watchdog (quality_funnel_watch), so `snapshot` is the run the watchdog read: its findings without
``repeat`` are the ones it raised (as one digest). A manual "Generate now" run is never alerted on and never replaces
it. No scheduled snapshot yet: 200 with `snapshot: null`. Stored rows only.
Who may call (api/core/staff_authz.py POLICY): ops.read or lab.read (numbers only, no tenant text).
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from api.core.staff_authz import staff_policy
from services.reports.funnel_report import HISTORY_SQL, LATEST_MANUAL_SQL, LATEST_SQL, REPORT_ID

router = APIRouter(tags=["admin-quality"], dependencies=[Depends(staff_policy)])


def _pool(request: Request):
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="DB not ready")
    return pool


def _json(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


def _iso(v):
    return v.isoformat() if v is not None else None


def _run(row) -> Any:
    if row is None:
        return None
    return {"id": str(row["id"]), "day": _iso(row["period_end"]), "trigger": row["trigger"],
            "finished_at": _iso(row["finished_at"]), "summary": row["summary"]}


@router.get("/admin/quality/funnel")
async def get_funnel(request: Request, history: int = Query(14, ge=1, le=90)) -> dict:
    pool = _pool(request)
    async with pool.acquire() as conn:
        latest = await conn.fetchrow(LATEST_SQL, REPORT_ID)
        rows = await conn.fetch(HISTORY_SQL, REPORT_ID, history)
        manual = await conn.fetchrow(LATEST_MANUAL_SQL, REPORT_ID)
    return {
        "report_id": REPORT_ID,
        "run": _run(latest),
        "snapshot": _json(latest["output"]) if latest is not None else None,
        "history": [{"run_id": str(r["id"]), "day": _iso(r["period_end"]), "finished_at": _iso(r["finished_at"]),
                     "summary": r["summary"], "totals": _json(r["totals"]), "findings": int(r["findings"]),
                     "new_findings": int(r["new_findings"])}
                    for r in rows],
        "manual_run": _run(manual),
    }
