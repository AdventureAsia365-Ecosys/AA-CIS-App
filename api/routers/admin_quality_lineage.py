"""AA-1052 W6 (root ADR 0020 layer 3) — lineage of every writer output, read side ($0, read-only).

  GET /admin/quality/lineage/coverage?days=7      per workflow (s1, t2, t9, eval): how many objects of the last
                                                  `days` days (1-90) carry each lineage field (policy_sha, fs_id,
                                                  prompt_version, route_config_version), with its share.
  GET /admin/quality/lineage/{workflow}/{id}      one object (an S1 generated_content id, a T2 tenant_tour_versions
                                                  id, a T9 content_piece id or an eval_item_result id): its stored
                                                  lineage, the prompt and route versions its model calls carried
                                                  (shared.llm_call_log), and the expected fields it lacks
                                                  (`missing`, `complete`).

  GET  /admin/quality/lineage/fanout/{tour_id}    the source-change fan-out of one tour (shared.source_change_mark,
                                                  CIS 392): its open marks (and the last 30 days of cleared ones),
                                                  per writer object (s1 / t2 / t9 id), source kind and typed reason,
                                                  counted per object kind and reason.
  POST /admin/quality/lineage/fanout/{tour_id}/recheck
                                                  queue the $0 `source_fanout` job for one tour ({source_kind:
                                                  raw_tour | master, dry_run: true by default}; reason
                                                  manual_recheck): the dependents written before now, re-checked
                                                  and, unless dry_run, marked. Never regenerates, never calls a model.

Stored fields only (services/quality/lineage.py and services/quality/source_fanout.py say where each lives); ids and
shas, never text.
Who may call (api/core/staff_authz.py POLICY): the reads ops.read or lab.read, as GET /admin/quality/funnel; the
re-check quality.write.
"""
from __future__ import annotations

from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from api.core.staff_authz import staff_policy
from services.quality import lineage, source_fanout

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


# Literal path first: /fanout/{tour_id} must not be read as a workflow and an object id.
@router.get("/admin/quality/lineage/fanout/{tour_id}")
async def get_tour_fanout(request: Request, tour_id: UUID,
                          limit: int = Query(source_fanout.MARKS_LIMIT, ge=1, le=source_fanout.MARKS_LIMIT)) -> dict:
    pool = _pool(request)
    try:
        async with pool.acquire() as conn:
            return await source_fanout.read_tour(conn, str(tour_id), limit=limit)
    except asyncpg.exceptions.UndefinedTableError:
        raise HTTPException(status_code=503, detail="the source-change fan-out is not set up yet (CIS migration 392)")


class RecheckBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_kind: Literal["raw_tour", "master"] = "raw_tour"
    dry_run: bool = True


@router.post("/admin/quality/lineage/fanout/{tour_id}/recheck", status_code=202)
async def post_tour_recheck(request: Request, tour_id: UUID, body: RecheckBody | None = None,
                            x_admin_user_id: str | None = Header(None)) -> dict:
    """Queue the $0 re-check of one tour's dependents (job source_fanout, reason manual_recheck)."""
    body = body or RecheckBody()
    pool = _pool(request)
    exists = await pool.fetchval("SELECT EXISTS (SELECT 1 FROM silver_aa_internal.raw_tours WHERE tour_id = $1::uuid)"
                                 " AS ok", str(tour_id))
    if not exists:
        raise HTTPException(status_code=404, detail=f"no tour {tour_id}")
    changed_at = await pool.fetchval("SELECT now() AS ok")
    change = source_fanout.Change(source_kind=body.source_kind, reason="manual_recheck", changed_at=changed_at,
                                  tour_id=str(tour_id))
    from shared.jobs import queue
    payload = {**change.payload(), "dry_run": body.dry_run}
    actor = f"admin:{x_admin_user_id}" if x_admin_user_id else "admin"
    job_id, created = await queue.enqueue(pool, source_fanout.KIND, payload, max_attempts=3, created_by=actor[:128])
    return {"job_id": job_id, "created": created, "kind": source_fanout.KIND, "tour_id": str(tour_id),
            "source_kind": body.source_kind, "dry_run": body.dry_run}


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
