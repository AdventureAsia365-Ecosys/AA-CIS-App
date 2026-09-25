"""AA-637 — GET /v1/progress/{kind}/{job_id}: live writing progress for the tenant's own jobs.

`kind` is "tour" (job_id = tenant_tour_versions.id of a T2 rewrite) or "piece" (job_id =
content_piece.piece_id of a T9 write). The Redis key is built from the verified JWT tenant id,
never from the request, so a tenant can only ever read its own jobs (docs/adr/0004).

`found: false` is a normal answer (job not started yet, finished more than an hour ago, or run
before this feature existed) — the portal then falls back to its existing status polling.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from api.routers.v1_tours import get_tenant
from services.acp_shared.writing_progress import KINDS, read_progress

router = APIRouter(prefix="/v1/progress", tags=["v1-progress"])


@router.get("/{kind}/{job_id}")
async def get_progress(kind: str, job_id: str, request: Request, tenant=Depends(get_tenant)):
    if kind not in KINDS:
        raise HTTPException(status_code=404, detail="Unknown progress kind")
    if not job_id or len(job_id) > 64:
        raise HTTPException(status_code=404, detail="Unknown job")
    try:
        snap = await read_progress(request.app.state.redis, tenant["sub"], kind, job_id)
    except Exception:
        snap = None  # Redis unavailable -> behave like "no live view", never a 500
    if snap is None:
        return {"found": False}
    return {"found": True, **snap}
