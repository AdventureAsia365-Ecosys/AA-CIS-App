"""AA-1169 — Writing Lab › Eval runs: read an evaluation run by hand ($0, read-only; services/writing_memory/
eval_review.py says what each read holds).

  GET /admin/writing-memory/eval-runs?writer=&run_kind=&prereg=&limit=&offset=
                                     the writing-memory evaluation runs, newest first: kind, writer, verdict, stop
                                     reason, cost, pre-registration id and arm, result rows per verdict (no text)
  GET /admin/writing-memory/eval-runs/{run_id}
                                     one run and its golden items, each with its rows (arm, mode, verdict, failure
                                     codes, hard stop, judge claims counted per policy class; no text)
  GET /admin/writing-memory/eval-runs/{run_id}/items/{item_id}
                                     one item of the run side by side: the source the judge read and the raw stored
                                     input, then every row with its stored output, the judge's claims (typed and
                                     placed), its checks and, for a T9 draft, the deterministic gate replay

Who may call (api/core/staff_authz.py POLICY): the two reads without text as the other Lab reads without tenant text
(tenant_content.read or lab.read); the item read holds the writer input and output (a tenant item carries tenant
text), so it is a Writing Lab tenant-text read (tenant_content.read or lab.tenant_text.read), logged for ai_ops
before it runs (api/core/lab_read_log.py LAB_TEXT_LOGGED). Nothing is written, no model is called, no job is queued.
The router is included without an include prefix (staff_policy reads the route template from scope["route"].path).
"""
from __future__ import annotations

from typing import Literal, Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from api.core.lab_read_log import LAB_TEXT_LOGGED
from api.core.staff_authz import staff_policy
from api.routers.admin import verify_admin_secret
from services.writing_memory import eval_review

router = APIRouter(prefix="/admin/writing-memory/eval-runs", tags=["admin-writing-memory"],
                   dependencies=[Depends(verify_admin_secret), Depends(staff_policy)])

Writer = Literal["a1", "t2", "t9_blog", "t9_social"]
RunKind = Literal["baseline_import", "r0_replay", "r1", "r2", "reverify", "probe", "shadow"]


def _pool(request: Request):
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="DB not ready")
    return pool


def _unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail={"detail": "The evaluation tables are not applied yet (CIS 284)",
                                                  "code": "unavailable"})


@router.get("")
async def list_eval_runs(request: Request, writer: Optional[Writer] = None, run_kind: Optional[RunKind] = None,
                         prereg: Optional[str] = Query(None, min_length=1, max_length=64),
                         limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)) -> dict:
    try:
        async with _pool(request).acquire() as conn:
            return await eval_review.list_runs(conn, writer=writer, run_kind=run_kind, prereg=prereg, limit=limit,
                                               offset=offset)
    except asyncpg.exceptions.UndefinedTableError:
        raise _unavailable()


@router.get("/{run_id}")
async def get_eval_run(request: Request, run_id: UUID) -> dict:
    try:
        async with _pool(request).acquire() as conn:
            out = await eval_review.read_run(conn, str(run_id))
    except asyncpg.exceptions.UndefinedTableError:
        raise _unavailable()
    if out is None:
        raise HTTPException(status_code=404, detail=f"no evaluation run {run_id}")
    return out


@router.get("/{run_id}/items/{item_id}", dependencies=LAB_TEXT_LOGGED)
async def get_eval_run_item(request: Request, run_id: UUID, item_id: UUID) -> dict:
    try:
        async with _pool(request).acquire() as conn:
            out = await eval_review.read_item(conn, str(run_id), str(item_id))
    except asyncpg.exceptions.UndefinedTableError:
        raise _unavailable()
    if out is None:
        raise HTTPException(status_code=404, detail=f"no item {item_id} in evaluation run {run_id}")
    return out
