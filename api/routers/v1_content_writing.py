"""
api/routers/v1_content_writing.py — AA-450: T9 (write) + T10-inline (quality gates), tenant
self-service.

Same convention as v1_angle_gate.py: `/v1/*` tenant-JWT-only, reuses `get_tenant` unchanged, no
staff/admin path. Written fresh per ADR §0.5 — no import from services.acp_s4_social anywhere.

AA-466: POST .../write moved to 202 Accepted + poll (real API Gateway 504s on long LLM+T10 runs,
AA-453/465). `service.start_write()` does the fast pre-flight (unchanged 404/409/422 contract)
and inserts a `content_piece` placeholder (status='processing'); the slow write/check loop runs
in `service.run_write_background()`, launched via `asyncio.create_task()` with a strong ref
(`_background_tasks` set + `add_done_callback`) — same GC-safety pattern
`api/routers/v1_tours.py::trigger_rewrite()` already uses (AA-425), not the bare
`asyncio.create_task()` `v1_s4_blog.py` uses.

Endpoint shape:
  POST /v1/content-writing/requests/{angle_gate_request_id}/write — 202 Accepted immediately,
       body = the content_piece placeholder (status='processing'). Poll GET .../pieces/{piece_id}
       for the final result (status becomes approved/held/failed).
  GET  /v1/content-writing/pieces/{piece_id} — read a piece back, at any status. UNCHANGED by
       AA-466 — already returns every field a poller needs (status/content_text/held_reason).
"""
from __future__ import annotations

import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel

from api.routers.v1_tours import get_tenant
from services.acp_angle_gate.service import RequestNotFoundError
from services.acp_content_writing import service
from services.acp_content_writing.export import (
    render_content_text_to_document,
    render_content_text_to_fragment,
)
from services.acp_shared.audit_log import TenantAuditAction, write_audit_log

router = APIRouter(prefix="/v1/content-writing", tags=["tenant-content-writing"])

# AA-466: strong refs to the fire-and-forget write+check background task — asyncio only keeps a
# weak ref to a bare create_task() result, so an unreferenced task can be GC'd mid-flight (same
# class of bug AA-223 found/fixed for admin_pipeline.py, AA-425 found/fixed for this router's own
# sibling api/routers/v1_tours.py::trigger_rewrite() — this endpoint now runs the same length of
# background work T9's write/rewrite + T10 gate loop does, up to ~89s, so it needs the same guard).
_background_tasks: set = set()


class WriteBody(BaseModel):
    cta: str | None = None  # fallback CTA — used only when angle_gate_request.cta is NULL


@router.post(
    "/requests/{request_id}/write",
    status_code=202,
    summary="Start writing + quality-checking content for an approved angle-gate request "
            "(T9 + T10-inline) — 202 Accepted, poll GET .../pieces/{piece_id} for the result",
)
async def write(request_id: UUID, body: WriteBody, request: Request, tenant=Depends(get_tenant)):
    tenant_id = UUID(tenant["sub"])
    pool = request.app.state.pool
    try:
        started = await service.start_write(tenant_id, request_id, pool, cta_override=body.cta)
    except RequestNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except service.RequestNotReadyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except service.MissingCTAError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except service.ContentWritingError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    piece, context = started["piece"], started["context"]

    # AA-559 — semantic tenant-activity log: the tenant's own "start write" action (the T9 step
    # of the combined T8/T9 wizard, per AA-450's LIVE STATE record — no separate T9 route/page
    # exists). Distinct from `content_piece.created` (services/acp_content_writing/service.py::
    # _insert_placeholder_piece()), which fires ~simultaneously but records resource creation,
    # not the tenant-initiated command.
    await write_audit_log(
        pool, tenant_id=str(tenant_id), actor=f"tenant:{tenant_id}",
        action=TenantAuditAction.WRITE_STARTED, resource_type="angle_gate_request",
        resource_id=str(request_id), details={"piece_id": piece["piece_id"]},
    )

    task = asyncio.create_task(
        service.run_write_background(request_id, UUID(piece["piece_id"]), context, pool)
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    # AA-613 — tenant-safe 202 body: just enough for the FE to start polling GET .../pieces/{id}
    # (which is itself tenant-safe now). No raw status/gate fields on the placeholder either.
    return {
        "piece_id": piece["piece_id"],
        "angle_gate_request_id": piece["angle_gate_request_id"],
        "channel": piece.get("channel"),
        "ready_state": "in_progress",
    }


@router.get("/pieces/{piece_id}", summary="Read a previously written content piece")
async def get_piece(piece_id: UUID, request: Request, tenant=Depends(get_tenant)):
    tenant_id = UUID(tenant["sub"])
    pool = request.app.state.pool
    try:
        return await service.fetch_piece(tenant_id, piece_id, pool, request=request)
    except service.ContentWritingError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


class UpdateContentBody(BaseModel):
    content_text: str


@router.patch(
    "/pieces/{piece_id}",
    summary="AA-569 — tenant hand-edit of a piece's content_text (My Content). Only the owning "
            "tenant, only while status is approved/held (a piece with real content to edit).",
)
async def update_piece(
    piece_id: UUID, body: UpdateContentBody, request: Request, tenant=Depends(get_tenant),
):
    tenant_id = UUID(tenant["sub"])
    pool = request.app.state.pool
    if not body.content_text.strip():
        raise HTTPException(status_code=422, detail="Content cannot be empty")
    try:
        return await service.update_piece_content_text(tenant_id, piece_id, body.content_text, pool)
    except service.ContentWritingError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get(
    "/pieces/{piece_id}/export",
    summary="AA-569/AA-613 — download a piece's content. format=text (every channel) or "
            "format=html (Blog only). For html, mode=document (full standalone HTML file, "
            "default) or mode=fragment (bare <article> to embed in a CMS). Every export is "
            "audit-logged (AA-613) so admin can count exports per tenant/channel.",
)
async def export_piece(
    piece_id: UUID, request: Request, tenant=Depends(get_tenant),
    format: str = Query("text", pattern="^(text|html)$"),
    mode: str = Query("document", pattern="^(document|fragment)$"),
):
    tenant_id = UUID(tenant["sub"])
    pool = request.app.state.pool
    try:
        piece = await service.fetch_piece(tenant_id, piece_id, pool, request=request)
    except service.ContentWritingError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if piece["status"] not in ("approved", "held"):
        raise HTTPException(status_code=409, detail="Nothing to export yet")

    channel = piece["channel"]
    if format == "html":
        if channel != "blog":
            raise HTTPException(
                status_code=400,
                detail="HTML export is only available for the Blog channel — every other "
                       "channel's content isn't written as markdown.",
            )
        if mode == "fragment":
            body = render_content_text_to_fragment(piece["content_text"])
        else:
            body = render_content_text_to_document(piece["content_text"], title="Blog post")
    else:
        body = piece["content_text"]

    # AA-613 — audit every export so admin monitor can count exports per tenant/channel/time.
    # Best-effort: an audit failure must not block the tenant's download (this is the one export
    # path, and a lost audit row is far less bad than a failed export the tenant is waiting on).
    try:
        pool_ = request.app.state.pool
        await write_audit_log(
            pool_, tenant_id=str(tenant_id), actor=f"tenant:{tenant_id}",
            action=TenantAuditAction.CONTENT_EXPORTED, resource_type="content_piece",
            resource_id=str(piece_id),
            details={"channel": channel, "format": format,
                     "mode": mode if format == "html" else None},
        )
    except Exception:
        pass

    if format == "html":
        return Response(content=body, media_type="text/html")
    return Response(content=body, media_type="text/plain")


@router.get(
    "/requests/{request_id}/latest-piece",
    summary="AA-522 — resume support: the latest content_piece for this request's currently "
            "chosen angle, or {piece: null} if T9 hasn't written anything for it yet. Lets the "
            "FE restore the write step's real state after a reload (in-flight/finished piece, or "
            "'still needs a CTA') instead of relying on client-only React state — see AngleGate"
            "Tab.tsx's own header comment for the bug this closes.",
)
async def get_latest_piece(request_id: UUID, request: Request, tenant=Depends(get_tenant)):
    tenant_id = UUID(tenant["sub"])
    pool = request.app.state.pool
    piece = await service.fetch_latest_piece_for_request(tenant_id, request_id, pool)
    return {"piece": piece}


@router.get(
    "/reviews",
    summary="AA-501 — /portal/t10-review's list: one row per request (latest content_piece), "
            "full write context embedded, WITHOUT gate/retry/error detail",
)
async def list_reviews(request: Request, tenant=Depends(get_tenant)):
    tenant_id = UUID(tenant["sub"])
    pool = request.app.state.pool
    data = await service.fetch_review_list(tenant_id, pool)
    return {"data": data, "total": len(data)}


@router.get(
    "/requests/{request_id}/review",
    summary="AA-501 — tenant-facing pre-T11 review: full write context (atom/tour/goal/angle/"
            "DFS-PAA/channel) + the latest content_piece, WITHOUT gate/retry/error detail",
)
async def get_review(request_id: UUID, request: Request, tenant=Depends(get_tenant)):
    tenant_id = UUID(tenant["sub"])
    pool = request.app.state.pool
    try:
        return await service.fetch_review(tenant_id, request_id, pool)
    except RequestNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except service.ContentWritingError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
