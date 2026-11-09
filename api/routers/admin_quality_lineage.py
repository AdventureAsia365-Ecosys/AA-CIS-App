"""AA-1052 W6 (root ADR 0020 layer 3) — lineage of every writer output, read side ($0, read-only).

  GET /admin/quality/lineage/coverage?days=7      per workflow (s1, t2, t9, eval): how many objects of the last
                                                  `days` days (1-90) carry each lineage field (policy_sha, fs_id,
                                                  prompt_version, route_config_version), with its share.
  GET /admin/quality/lineage/{workflow}/{id}      one object (an S1 generated_content id, a T2 tenant_tour_versions
                                                  id, a T9 content_piece id or an eval_item_result id): its stored
                                                  lineage, the prompt and route versions its model calls carried
                                                  (shared.llm_call_log), and the expected fields it lacks
                                                  (`missing`, `complete`).

Stored fields only (services/quality/lineage.py says where each lives); ids and shas, never text.
Who may call (api/core/staff_authz.py POLICY): ops.read or lab.read, as GET /admin/quality/funnel.
"""
from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from api.core.staff_authz import staff_policy
from services.quality import lineage

router = APIRouter(tags=["admin-quality"], dependencies=[Depends(staff_policy)])

Workflow = Literal["s1", "t2", "t9", "eval"]


def _pool(request: Request):
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="DB not ready")
    return pool


# Literal path first: /coverage must not be read as a workflow.
@router.get("/admin/quality/lineage/coverage")
async def get_lineage_coverage(request: Request,
                               days: int = Query(7, ge=1, le=lineage.COVERAGE_DAYS_MAX)) -> dict:
    pool = _pool(request)
    out = []
    async with pool.acquire() as conn:
        for wf in lineage.WORKFLOWS:
            row = await conn.fetchrow(lineage.COVERAGE_SQL[wf], days)
            out.append(lineage.coverage_from_row(wf, row))
    return {"days": days, "lineage_version": lineage.LINEAGE_VERSION, "fields": list(lineage.FIELDS),
            "workflows": out}


@router.get("/admin/quality/lineage/{workflow}/{object_id}")
async def get_object_lineage(request: Request, workflow: Workflow, object_id: UUID) -> dict:
    pool = _pool(request)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(lineage.OBJECT_SQL[workflow], str(object_id))
        if row is None:
            raise HTTPException(status_code=404, detail=f"no {workflow} object {object_id}")
        ledger, join = [], None
        if lineage.can_read_ledger(workflow, row):
            ledger = await conn.fetch(lineage.LEDGER_SQL[workflow], *lineage.ledger_args(workflow, row))
            join = {"s1": "job" if row["job_id"] else "tour_window", "t2": "job", "t9": "piece",
                    "eval": "run_item"}[workflow]
    return lineage.assemble(workflow, row, ledger, ledger_join=join)
