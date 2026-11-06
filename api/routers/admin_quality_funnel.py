"""AA-900 (S271 W0, root ADR 0020 "Measurement") — the content quality funnel, read side ($0).

  GET /admin/quality/funnel?history=14   the latest succeeded CIS-08 snapshot as stored (services/quality/funnel.py:
                                         per workflow and ISO week: created, delivered vs held, first-pass yield,
                                         attempts and LLM USD per delivered object, every failing gate per attempt,
                                         stages with rung / fallback / cut shares, the watch window and its findings)
                                         and the headline totals of the last `history` daily snapshots (1-90).

The snapshot is computed nightly by the report chain (report_generate, CIS-08) and never recomputed here: the page
shows exactly what the watchdog alerted on. No snapshot yet: 200 with `snapshot: null`. Stored rows only.
Who may call (api/core/staff_authz.py POLICY): ops.read or lab.read (numbers only, no tenant text).
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from api.core.staff_authz import staff_policy
from services.reports.funnel_report import HISTORY_SQL, LATEST_SQL, REPORT_ID

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


@router.get("/admin/quality/funnel")
async def get_funnel(request: Request, history: int = Query(14, ge=1, le=90)) -> dict:
    pool = _pool(request)
    async with pool.acquire() as conn:
        latest = await conn.fetchrow(LATEST_SQL, REPORT_ID)
        rows = await conn.fetch(HISTORY_SQL, REPORT_ID, history)
    run = None
    if latest is not None:
        run = {"id": str(latest["id"]), "day": _iso(latest["period_end"]), "trigger": latest["trigger"],
               "finished_at": _iso(latest["finished_at"]), "summary": latest["summary"]}
    return {
        "report_id": REPORT_ID,
        "run": run,
        "snapshot": _json(latest["output"]) if latest is not None else None,
        "history": [{"run_id": str(r["id"]), "day": _iso(r["period_end"]), "finished_at": _iso(r["finished_at"]),
                     "summary": r["summary"], "totals": _json(r["totals"]), "findings": int(r["findings"])}
                    for r in rows],
    }
