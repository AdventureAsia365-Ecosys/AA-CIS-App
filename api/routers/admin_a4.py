"""
api/routers/admin_a4.py — AA-437 [A4] Cross-Tenant Oversight v1.

STEP0 (docs/claude_audit/AA-437-01-a4-step0-audit.md) confirmed: A4 was 0% built, and neither
existing `review_queue` reader (`admin_pipeline.py`'s `/admin/review-queue`,
`v1_pipeline.py`'s `/v1/pipeline/review-queue`) can serve T3 rows — both `INNER JOIN
generated_content`, which is NULL by design for every T3 (tenant QA-gate escalate) row (AA-425).
This router is deliberately new/separate rather than a patch to either — those two are wired to
the older N0-N6 admin HITL flow (approve/reject, step_fn_task_token) with actions that don't
apply to a T3 row.

v1 scope, per Nghiep's 5 decisions (Linear AA-437, 23/08/2026):
  1. Both use cases (review-log + trust-ramp) built together, read-only.
  2. Trust ramp shows CURRENT state only — no suggest_ramp_transition() automation, no
     engagement_ok/weeks_active formula (STEP0 confirmed neither is computed anywhere).
     SUPERSEDED by AA-464 (06/09/2026, see that section below) — the suggestion is now
     surfaced (still never auto-applied; an explicit admin click is still required).
  3. No per-tenant single ramp "level" — ramp state lives on acp_deliver.packets.publish_mode,
     per-PACKET (STEP0 finding: nothing in the schema aggregates this to one tenant-level value,
     and packets for the same tenant CAN sit at different modes) — so this returns every packet
     with its own level, never collapsed.
  4/5. Route `/admin/a4-oversight` (FE), endpoints `/admin/a4/review-log` + `/admin/a4/trust-ramp`.

AA-455 bước 1 (24/08/2026) added a 3rd use case: `publish_log` list + force-unpublish. Per
STEP0 (docs/claude_audit/AA-455-01-step0-a4-force-unpublish.md §4/§7), this stays on the SAME
`/admin/a4-oversight` FE page (already allowlisted in middleware.ts since AA-437) and the SAME
`/admin/a4` router prefix here — no new route, so no middleware change needed. Still no
flag/suspend — that stays deferred to the Command Center backlog (AA-255->259); force-unpublish
is the one action this issue scoped in.

AA-469 Việc 5 (30/08/2026) added a 4th use case: `GET /content-log` — T9/T10's quality-gate
outcomes (`acp_shared.content_piece.gate_ledger`/`held_reason`, `status IN ('held','failed')`),
the stage with the best structured error data of any LLM-using T-step but zero prior A4 path
(STEP0: docs/claude_audit/AA-469-viec5-step0-a4-feedback-loop-investigation.md). T5 (atomize)
failures did NOT need a new endpoint — they write into the SAME `review-log` table/join key as T3
(see `services/acp_produce/tenant_pipeline.py::escalate_t5_atomize_failure()`), so they surface
through the existing `/review-log` endpoint above automatically.

AA-464 (06/09/2026) — nối dây `suggest_ramp_transition()`, per Nghiep's explicit follow-up
confirmation (S159) to AA-437 decision #2 above. `GET /trust-ramp` gains 4 fields per row
(`engagement_ok`/`weeks_active`/`suggested_mode`/`eligible`), computed on-demand (no scheduler,
per the issue's own recommendation — see docs/implementation-notes/AA-464.md) via
`trust_ramp.compute_ramp_suggestion()` — still a pure read, no writes. Two new mutating
endpoints, both requiring an explicit admin click, never automatic (ADR-2026-038 §0.2):
`POST /trust-ramp/{packet_id}/approve` (first real caller of the already-built
`confirm_ramp_transition()`) and `POST /trust-ramp/{packet_id}/skip` (new — logs a dismissal to
`acp_shared.audit_log` without touching `packets.publish_mode`). Both re-compute the suggestion
fresh server-side rather than trusting a client-supplied mode, same "never stale" principle
`services/acp_planning/trip_reallocation.py::confirm_trip_reallocation()` already uses.

AA-560 (09/09/2026) — the old `/admin/a4-oversight` FE page is retired (replaced by "07 · Platform
Stats" in Social Content, Review Log + Trust Ramp moved verbatim; Content Log/Publish Log's READ
side stays covered by Content Trace/06, AA-568). Adds `GET /platform-stats`: a real backend
aggregate for "gate/lỗi mắc phải nhiều nhất toàn platform" (AA-558 Phần 1 Q3's own finding — the
old "F1_GROUNDING × 4" tags on the deleted page's Content Log section were a CLIENT-SIDE rollup
over only the loaded page (`limit=200`), not the true platform-wide count). This is a fresh
`GROUP BY` over the FULL `content_piece.gate_ledger` column, no row cap. Also returns
total-pieces/by-channel/by-status counts (AA-560's own item 3, "platform-level, no lineage
detail" — Segment/Route lineage is explicitly out per AA-558's "not ready to display" finding).
"""
from __future__ import annotations

import json
from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Header, HTTPException, Query, Request

from api.routers.admin import verify_admin_secret
from services.acp_produce import trust_ramp
from services.acp_produce.packets import PublishModeBlockedError
from services.acp_produce.trust_ramp import BofuVetoBlockedError, confirm_ramp_transition

logger = structlog.get_logger()
router = APIRouter(prefix="/admin/a4", tags=["admin-a4"])


def _parse_jsonb(val, default):
    """escalate_detail arrives as a raw JSON-encoded string on this app's connections (no jsonb
    codec registered — same gap AA-314/AA-425 already found/fixed elsewhere, e.g. v1_tours.py's
    forbidden_words handling). Parse defensively rather than assume asyncpg decoded it."""
    if val is None:
        return default
    if isinstance(val, (list, dict)):
        return val
    import json
    try:
        return json.loads(val)
    except (TypeError, ValueError):
        return default


@router.get("/review-log")
async def get_review_log(
    request: Request,
    tenant_id: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    x_admin_secret: str = Header(None),
):
    """T3 QA-gate escalation log (silver_aa_internal.review_queue, tenant_tour_version_id NOT
    NULL rows only — the N0-N6 admin-pipeline rows, keyed by generated_content_id, are a
    different flow and excluded here, same distinction STEP0 confirmed matters).

    Returns raw rows, not a server-side check_id aggregate — STEP0 already confirmed a plain
    GROUP BY over escalate_detail is enough at current volume (52 total rows, 11 T3-style) and
    the task's own guidance was to pick whichever side needs less BE logic; the FE groups
    client-side (same flat-list-first approach AA-436's own STEP0 recommended, modeled on
    AtomsTab.tsx).
    """
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool

    conditions = ["rq.tenant_tour_version_id IS NOT NULL"]
    params: list = []
    if tenant_id:
        params.append(tenant_id)
        conditions.append(f"rq.tenant_id = ${len(params)}::uuid")
    where = " AND ".join(conditions)
    params.append(limit)

    async with pool.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT
                rq.id::text, rq.tour_id::text, rq.tenant_id::text,
                t.name AS tenant_name, t.slug AS tenant_slug,
                rq.tenant_tour_version_id::text, rq.failure_summary,
                rq.escalate_detail, rq.review_status, rq.created_at
            FROM silver_aa_internal.review_queue rq
            LEFT JOIN shared.tenants t ON t.tenant_id = rq.tenant_id
            WHERE {where}
            ORDER BY rq.created_at DESC
            LIMIT ${len(params)}
        """, *params)

    data = [
        {
            "id": r["id"],
            "tour_id": r["tour_id"],
            "tenant_id": r["tenant_id"],
            "tenant_name": r["tenant_name"],
            "tenant_slug": r["tenant_slug"],
            "tenant_tour_version_id": r["tenant_tour_version_id"],
            "failure_summary": r["failure_summary"],
            "escalate_detail": _parse_jsonb(r["escalate_detail"], []),
            "review_status": r["review_status"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]
    logger.info("a4_review_log_queried", count=len(data), tenant_filter=tenant_id)
    return {"data": data, "total": len(data), "tenant_filter": tenant_id}


@router.get("/trust-ramp")
async def get_trust_ramp(request: Request, x_admin_secret: str = Header(None)):
    """Every acp_deliver.packets row with its own publish_mode (ramp state) — no per-tenant
    rollup (decision #3 above). Pure read; AA-464 adds a fresh, on-demand
    engagement_ok/weeks_active/suggested_mode/eligible computation per row (still no writes —
    approving/skipping a suggestion is the 2 new endpoints below, not this one).

    Signals are computed once per DISTINCT tenant_id on the page (not once per packet row) to
    avoid redundant repeat queries for tenants with multiple packets —
    trust_ramp.compute_tenant_ramp_signals() is the single per-tenant source; the pure
    trust_ramp.suggest_ramp_transition() is then applied per packet's own current publish_mode
    (the single-packet path below, trust_ramp.compute_ramp_suggestion(), does the same 2 steps
    for exactly one packet — this bulk endpoint doesn't call it, to avoid re-fetching each
    tenant's signals once per packet row)."""
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                p.packet_id::text, p.tenant_id, t.name AS tenant_name, t.slug AS tenant_slug,
                p.year, p.month, p.week, p.status, p.publish_mode,
                p.created_at, p.delivered_at
            FROM acp_deliver.packets p
            LEFT JOIN shared.tenants t ON t.tenant_id::text = p.tenant_id
            ORDER BY p.tenant_id, p.year DESC, p.month DESC, p.week DESC
        """)

        signals_by_tenant: dict = {}
        for tenant_id in {r["tenant_id"] for r in rows}:
            signals_by_tenant[tenant_id] = await trust_ramp.compute_tenant_ramp_signals(conn, tenant_id)

    data = []
    for r in rows:
        signals = signals_by_tenant.get(r["tenant_id"], {"engagement_ok": False, "weeks_active": 0})
        suggested_mode = trust_ramp.suggest_ramp_transition(
            r["publish_mode"], engagement_ok=signals["engagement_ok"],
            weeks_active=signals["weeks_active"],
        )
        data.append({
            "packet_id": r["packet_id"],
            "tenant_id": r["tenant_id"],
            "tenant_name": r["tenant_name"],
            "tenant_slug": r["tenant_slug"],
            "year": r["year"],
            "month": r["month"],
            "week": r["week"],
            "status": r["status"],
            "publish_mode": r["publish_mode"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "delivered_at": r["delivered_at"].isoformat() if r["delivered_at"] else None,
            "engagement_ok": signals["engagement_ok"],
            "weeks_active": signals["weeks_active"],
            "suggested_mode": suggested_mode,
            "eligible": suggested_mode != r["publish_mode"],
        })
    logger.info("a4_trust_ramp_queried", count=len(data),
                eligible_count=sum(1 for d in data if d["eligible"]))
    return {"data": data, "total": len(data)}


def _resolve_admin_actor(x_admin_user_id: Optional[str]) -> str:
    """Same tolerant UUID-parse-or-'unknown' convention force_unpublish() below already
    established (AA-455) — a legacy ADMIN_SECRET-only session doesn't 500, it just records
    "admin:unknown". Extracted here since AA-464 adds 2 more mutating endpoints needing the
    exact same actor resolution."""
    if not x_admin_user_id:
        return "unknown"
    try:
        return str(UUID(x_admin_user_id))
    except (ValueError, AttributeError):
        return "unknown"


@router.post("/trust-ramp/{packet_id}/approve")
async def approve_ramp_suggestion(
    packet_id: UUID,
    request: Request,
    x_admin_secret: str = Header(None),
    x_admin_user_id: Optional[str] = Header(None),
):
    """AA-464: first real caller of trust_ramp.confirm_ramp_transition(). Re-computes the
    suggestion fresh (never trusts a client-supplied mode — same "never stale" principle
    services/acp_planning/trip_reallocation.py::confirm_trip_reallocation() already uses), 400s
    if the packet is no longer eligible (suggestion may have changed since the page loaded, or
    an admin double-clicks), and otherwise calls confirm_ramp_transition() UNCHANGED — that
    function already writes the acp_shared.audit_log entry (blocked or not) and already enforces
    the BOFU hard-block independently of this endpoint."""
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    packet_id_str = str(packet_id)
    admin_actor = _resolve_admin_actor(x_admin_user_id)

    async with pool.acquire() as conn:
        try:
            suggestion = await trust_ramp.compute_ramp_suggestion(conn, packet_id_str)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

        if not suggestion["eligible"]:
            raise HTTPException(
                status_code=400,
                detail="Packet is not currently eligible for a ramp transition suggestion",
            )

        try:
            await confirm_ramp_transition(
                conn, packet_id=packet_id_str, tenant_id=suggestion["tenant_id"],
                mode=suggestion["suggested_mode"], actor=f"admin:{admin_actor}",
            )
        except BofuVetoBlockedError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except PublishModeBlockedError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    logger.info("a4_ramp_suggestion_approved", packet_id=packet_id_str,
                from_mode=suggestion["current_mode"], to_mode=suggestion["suggested_mode"],
                admin_actor=admin_actor)
    return {
        "packet_id": packet_id_str,
        "tenant_id": suggestion["tenant_id"],
        "from_mode": suggestion["current_mode"],
        "to_mode": suggestion["suggested_mode"],
        "status": "approved",
    }


@router.post("/trust-ramp/{packet_id}/skip")
async def skip_ramp_suggestion(
    packet_id: UUID,
    request: Request,
    x_admin_secret: str = Header(None),
    x_admin_user_id: Optional[str] = Header(None),
):
    """AA-464: logs an explicit admin dismissal of a ramp-transition suggestion. Does NOT touch
    acp_deliver.packets.publish_mode — this is the "Bỏ qua" (skip) half of the issue's #4 ask
    ("ghi log mỗi lần gợi ý được đưa ra + admin duyệt/bỏ qua"), which had no existing mechanism
    at all before this issue (only the approve/confirm path had a log, via
    confirm_ramp_transition()). Reuses acp_shared.audit_log (migration 030) — same table every
    other real gate/approval decision in this repo already writes to, no new logging shape."""
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    packet_id_str = str(packet_id)
    admin_actor = _resolve_admin_actor(x_admin_user_id)

    async with pool.acquire() as conn:
        try:
            suggestion = await trust_ramp.compute_ramp_suggestion(conn, packet_id_str)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

        if not suggestion["eligible"]:
            raise HTTPException(
                status_code=400,
                detail="Packet is not currently eligible for a ramp transition suggestion",
            )

        await conn.execute(
            """
            INSERT INTO acp_shared.audit_log
                (tenant_id, actor, action, resource_type, resource_id, details)
            VALUES ($1, $2, 'ramp_suggestion_skipped', 'packet', $3, $4::jsonb)
            """,
            suggestion["tenant_id"], f"admin:{admin_actor}", packet_id_str,
            json.dumps({
                "from": suggestion["current_mode"], "to": suggestion["suggested_mode"],
                "dismissed": True,
            }),
        )

    logger.info("a4_ramp_suggestion_skipped", packet_id=packet_id_str,
                from_mode=suggestion["current_mode"], to_mode=suggestion["suggested_mode"],
                admin_actor=admin_actor)
    return {
        "packet_id": packet_id_str,
        "tenant_id": suggestion["tenant_id"],
        "from_mode": suggestion["current_mode"],
        "to_mode": suggestion["suggested_mode"],
        "status": "skipped",
    }


@router.get("/publish-log")
async def get_publish_log(
    request: Request,
    tenant_id: Optional[str] = Query(None),
    tour_id: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=500),
    x_admin_secret: str = Header(None),
):
    """AA-455 bước 1 — T11 delivery-state rows (acp_shared.publish_log). Deploys against an
    empty table until T11's own write path (bước 2, not built here) starts producing rows.
    Same flat-list-first shape as review-log/trust-ramp above — no server-side aggregation.

    AA-527 (bổ sung, Phương án C dashboard): optional `tour_id` — publish_log has no tour_id
    column of its own, so this JOINs through content_piece -> angle_gate_request.trip_id (the
    same path get_content_log below already reads) rather than adding a denormalized column.
    Used by the dashboard's "Publish" panel when a tour is selected as the page's anchor."""
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool

    conditions = ["1=1"]
    params: list = []
    join_sql = ""
    if tenant_id:
        params.append(tenant_id)
        conditions.append(f"pl.tenant_id = ${len(params)}::uuid")
    if tour_id:
        join_sql = (
            "JOIN acp_shared.content_piece cp ON cp.piece_id = pl.piece_id "
            "JOIN acp_shared.angle_gate_request agr ON agr.request_id = cp.angle_gate_request_id"
        )
        params.append(tour_id)
        conditions.append(f"agr.trip_id = ${len(params)}::uuid")
    where = " AND ".join(conditions)
    params.append(limit)

    async with pool.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT
                pl.publish_id::text, pl.piece_id::text, pl.tenant_id::text,
                t.name AS tenant_name, t.slug AS tenant_slug,
                pl.channel, pl.status, pl.external_id, pl.external_url,
                pl.published_at, pl.unpublished_at, pl.unpublished_by,
                pl.last_error, pl.created_at
            FROM acp_shared.publish_log pl
            LEFT JOIN shared.tenants t ON t.tenant_id = pl.tenant_id
            {join_sql}
            WHERE {where}
            ORDER BY pl.created_at DESC
            LIMIT ${len(params)}
        """, *params)

    data = [
        {
            "publish_id": r["publish_id"],
            "piece_id": r["piece_id"],
            "tenant_id": r["tenant_id"],
            "tenant_name": r["tenant_name"],
            "tenant_slug": r["tenant_slug"],
            "channel": r["channel"],
            "status": r["status"],
            "external_id": r["external_id"],
            "external_url": r["external_url"],
            "published_at": r["published_at"].isoformat() if r["published_at"] else None,
            "unpublished_at": r["unpublished_at"].isoformat() if r["unpublished_at"] else None,
            "unpublished_by": r["unpublished_by"],
            "last_error": r["last_error"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]
    logger.info("a4_publish_log_queried", count=len(data), tenant_filter=tenant_id, tour_filter=tour_id)
    return {"data": data, "total": len(data), "tenant_filter": tenant_id, "tour_filter": tour_id}


@router.get("/content-log")
async def get_content_log(
    request: Request,
    tenant_id: Optional[str] = Query(None),
    tour_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    channel: Optional[str] = Query(None),
    published: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=500),
    x_admin_secret: str = Header(None),
):
    """AA-469 Việc 5 — T9/T10's own gap, closed: `content_piece.gate_ledger`/`held_reason` is the
    best structured error data of any LLM-using stage (per-gate pass/fail + violations, plus
    `repair_log`'s retry-feedback trail) but had ZERO A4 path before this — the data existed,
    only the read route was missing (STEP0's own "easiest gap to patch" ranking).

    AA-501 widened this from "held/failed only" to EVERY content_piece row, with full write
    context (atom/tour/goal/angle/DFS-PAA/channel) added — per Nghiệp's explicit decision this is
    the WIDEST of the two AA-501 views: "AA cần thấy MỌI THỨ tenant thấy, CỘNG THÊM chi tiết kỹ
    thuật — không phải tập con khác biệt" (AA must see everything Tenant sees, PLUS technical
    detail — not a different subset). The old held/failed-only filter would have hidden exactly
    the 'approved'/'processing' rows a lesson-log/comparison use case needs to see alongside the
    failures. `repair_log` (retry-feedback trail) is now selected too — it existed on the table
    since migration 115 but this endpoint never fetched it (STEP0 §1.5's own flagged gap).

    No real numeric "score" exists for T10 (per-criterion pass/fail, not T3's quality_score) —
    `gate_pass_count`/`gate_total_count` are computed here from `gate_ledger`'s own pass/fail
    entries as a summary, NOT a replacement for the full per-gate detail already in `gate_ledger`
    (Nghiệp: "cần phải xem chi tiết được, biết nguyên nhân rõ ràng gate nào bị held" — a total
    alone would not satisfy that).

    `publish_status` (`published`/`pending_publish`/`n/a`) — a LEFT JOIN to
    `acp_shared.publish_log` (`status = 'published'`, same convention `v1_publish.py`'s own
    `/pending` query uses): `published` when a publish_log row exists, `pending_publish` when the
    piece is `approved` but has none yet, `n/a` for anything not yet ready to publish at all
    (`held`/`failed`/`processing`).

    Same cross-tenant-by-default shape as review-log/publish-log above: optional `tenant_id`
    filter (already existed, unchanged), no hard tenant scoping — A4 is cross-tenant oversight by
    design (STEP0/AA-437).

    `channel` reads `COALESCE(cp.channel, agr.channel)` — same reasoning AA-469 Việc 4's
    flow-order fix already applied to `v1_publish.py`'s two queries on this same table.
    `angle_gate_option`/`tour_atoms` joins follow the same option_id-first (AA-497) and
    owner_scope=tenant_id conventions the tenant-facing `fetch_review()`
    (services/acp_content_writing/service.py, AA-501) uses — `tour_atoms`'s `owner_scope` filter
    here uses `cp.tenant_id` directly (a SQL column reference, not a bound per-request tenant_id
    param — this endpoint is cross-tenant, unlike the tenant-scoped Python helper).

    NOT built here (explicitly out of scope, AA-505 instead): LLM cost/token tracking — no such
    column exists on any of these tables yet.

    AA-527 (bổ sung, Phương án C dashboard): optional `tour_id` filters on `agr.trip_id` (already
    selected/joined below for the `tour` display block) — lets the dashboard's Write-Gate and
    Review panels scope this same dataset to whichever tour is the page's current header anchor,
    with no schema change.

    AA-568 — merged "06 · Content Trace" page: this is now the SOLE data source for the combined
    table (the old 3-tab Write/Gate + Review + Publish split on `/admin/tenant-activity` read the
    exact same rows twice, per AA-558's confirmed finding — see that page's own STEP0 comment).
    Gains 4 new optional filters matching the merged page's filter bar (`status` one of
    processing/approved/held/failed — the real `content_piece.status` CHECK values, no `draft`;
    `channel`; `date_from`/`date_to`, inclusive day-range on `cp.created_at`) plus `published`
    (`yes`/`no` — derived from the existing `pl.publish_id IS [NOT] NULL` join, never a 5th status
    value, per STEP0's explicit terminology finding). `content_text` is now selected in FULL
    (was `LEFT(cp.content_text, 280)`) so the merged page's row-click accordion needs no second
    per-piece fetch — same "one already-fetched row, no second fetch" principle `PieceLineageCard`
    documented before it (this endpoint is now this codebase's only reader of that field, the old
    component is deleted, see auditPanels.tsx). `retry_count` (`len(repair_log)`) and `topic` (a
    real proxy — the underlying atom's text, or the T8 goal when no atom text exists — content_
    piece has no title/topic column of its own, confirmed in STEP0, never fabricated) are new
    computed fields. `publish_external_url`/`publish_published_at` are added to the SELECT so the
    merged table's "Published Y/N + link" column needs no second call to `/publish-log`."""
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool

    conditions = ["1 = 1"]
    params: list = []
    if tenant_id:
        params.append(tenant_id)
        conditions.append(f"cp.tenant_id = ${len(params)}::uuid")
    if tour_id:
        params.append(tour_id)
        conditions.append(f"agr.trip_id = ${len(params)}::uuid")
    if status:
        params.append(status)
        conditions.append(f"cp.status = ${len(params)}")
    if channel:
        params.append(channel)
        conditions.append(f"COALESCE(cp.channel, agr.channel) = ${len(params)}")
    if date_from:
        params.append(date_from)
        conditions.append(f"cp.created_at >= ${len(params)}::date")
    if date_to:
        params.append(date_to)
        conditions.append(f"cp.created_at < (${len(params)}::date + interval '1 day')")
    if published == "yes":
        conditions.append("pl.publish_id IS NOT NULL")
    elif published == "no":
        conditions.append("pl.publish_id IS NULL")
    where = " AND ".join(conditions)
    params.append(limit)

    async with pool.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT
                cp.piece_id::text, cp.tenant_id::text, t.name AS tenant_name, t.slug AS tenant_slug,
                cp.angle_gate_request_id::text, agr.atom_id, agr.goal, agr.cta, agr.subject_id::text,
                agr.dfs_paa_snapshot, agr.trip_id,
                COALESCE(cp.channel, agr.channel) AS channel,
                cp.status, cp.held_reason, cp.gate_ledger, cp.repair_log, cp.attempt_number,
                cp.content_text, cp.created_at,
                -- AA-561 3a — lineage: subject (Slate proposal) -> Segment OR Route it came from.
                -- subject_id is nullable (the pre-existing atom-picker entry point, AA-449, still
                -- creates a request with no Subject at all — NOT retired by this build, per its
                -- own migration 133 comment) so every join below is LEFT and the frontend must
                -- show an explicit "chosen atom directly, not via Slate" label when subject_id
                -- IS NULL, never a blank/broken-looking cell.
                sub.segment_id AS segment_id, sub.route_id AS route_id,
                seg.canonical_place AS segment_place, seg.canonical_action AS segment_action,
                rte.hub_name AS route_hub_name, rte.first_day AS route_first_day, rte.last_day AS route_last_day,
                ta.text AS atom_text, ta.activity_type AS atom_activity_type,
                ta.emotional_hook AS atom_emotional_hook, ta.season_note AS atom_season_note,
                rt.src_name AS tour_name, rt.country AS tour_destination,
                pl.publish_id::text AS publish_id, pl.external_url AS publish_external_url,
                pl.published_at AS publish_published_at,
                -- AA-561 3a — retry: how many content_piece rows exist for this SAME request, and
                -- whether THIS row is the earliest one. >1 sibling + not-earliest = a buffer retry
                -- of an earlier held piece (AA-485's _run_buffer_retry_attempt() inserts a NEW
                -- piece row per retry, resets attempt_number to 1 on it — there is no
                -- previous_piece_id column by design, migration 115's own comment — so "is this a
                -- retry" is derived here from sibling count + created_at order, not read off a
                -- single column).
                (SELECT count(*) FROM acp_shared.content_piece sib
                 WHERE sib.angle_gate_request_id = cp.angle_gate_request_id) AS sibling_piece_count,
                (SELECT min(sib2.created_at) FROM acp_shared.content_piece sib2
                 WHERE sib2.angle_gate_request_id = cp.angle_gate_request_id) AS request_first_piece_at,
                -- AA-561 3a — ALL 3 angles the request generated, not just the chosen one (the old
                -- query's ago/ago_chosen COALESCE only ever surfaced 1 of 3) — ordered by idx so
                -- the frontend can show "2 not chosen" alongside the pick.
                (
                    SELECT jsonb_agg(jsonb_build_object(
                        'option_id', o.option_id::text, 'idx', o.idx, 'name', o.name,
                        'why_it_works', o.why_it_works, 'formula_fit', o.formula_fit,
                        'best_final_style', o.best_final_style,
                        'recommended', o.recommended, 'chosen', o.chosen
                    ) ORDER BY o.idx)
                    FROM acp_shared.angle_gate_option o WHERE o.request_id = agr.request_id
                ) AS angles
            FROM acp_shared.content_piece cp
            JOIN acp_shared.angle_gate_request agr ON agr.request_id = cp.angle_gate_request_id
            LEFT JOIN shared.tenants t ON t.tenant_id = cp.tenant_id
            LEFT JOIN acp_shared.subject sub ON sub.subject_id = agr.subject_id
            LEFT JOIN acp_contract.atom_segment seg ON seg.segment_id = sub.segment_id
            LEFT JOIN acp_contract.route rte ON rte.route_id = sub.route_id
            -- AA-561 STEP0 finding: the OLD join here was `ta.owner_scope = cp.tenant_id::text`,
            -- which stopped matching anything the moment AA-526 made atoms platform-wide
            -- (owner_scope='platform' since then) — atom_text/activity_type/etc silently went
            -- NULL for every post-AA-526 piece. Fixed to accept either the current platform scope
            -- or a legacy tenant-owned atom (pre-AA-526 rows, never backfilled).
            LEFT JOIN acp_contract.tour_atoms ta
                ON ta.atom_id = agr.atom_id AND (ta.owner_scope = 'platform' OR ta.owner_scope = cp.tenant_id::text)
            LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = agr.trip_id
            LEFT JOIN acp_shared.publish_log pl
                ON pl.piece_id = cp.piece_id AND pl.status = 'published'
            WHERE {where}
            ORDER BY cp.created_at DESC
            LIMIT ${len(params)}
        """, *params)

    def _publish_status(status: str, published: bool) -> str:
        if published:
            return "published"
        if status == "approved":
            return "pending_publish"
        return "n/a"

    def _gate_counts(gate_ledger: list) -> dict:
        passed = sum(1 for g in gate_ledger if isinstance(g, dict) and g.get("passed"))
        return {"passed": passed, "total": len(gate_ledger)}

    def _content_log_source(r) -> dict:
        """AA-561 3a — 2/12 real pieces (AA-561's own issue text, the pre-existing atom-picker
        path) have no subject_id/Slate lineage at all — this must read as an explicit, understood
        state, not an empty/broken-looking cell."""
        if r["segment_id"]:
            return {"kind": "segment", "segment_id": r["segment_id"],
                    "place": r["segment_place"], "action": r["segment_action"]}
        if r["route_id"]:
            return {"kind": "route", "route_id": r["route_id"], "hub_name": r["route_hub_name"],
                    "first_day": r["route_first_day"], "last_day": r["route_last_day"]}
        return {"kind": "direct_atom"}

    def _topic(atom_text: Optional[str], goal: Optional[str]) -> str:
        """AA-568 — content_piece has no title/topic column (confirmed in STEP0). Best honest
        proxy: the underlying atom's own text (what real moment/activity this piece is about),
        falling back to the T8 goal when no atom is on record (the pre-Slate direct-atom path can
        still lack atom_text if the atom itself was since deleted)."""
        if atom_text:
            return atom_text[:90] + ("…" if len(atom_text) > 90 else "")
        if goal:
            return goal
        return "Untitled"

    data = []
    for r in rows:
        gate_ledger = _parse_jsonb(r["gate_ledger"], [])
        gate_counts = _gate_counts(gate_ledger)
        repair_log = _parse_jsonb(r["repair_log"], [])
        content_text = r["content_text"]
        data.append({
            "piece_id": r["piece_id"],
            "tenant_id": r["tenant_id"],
            "tenant_name": r["tenant_name"],
            "tenant_slug": r["tenant_slug"],
            "angle_gate_request_id": r["angle_gate_request_id"],
            "atom_id": r["atom_id"],
            "goal": r["goal"],
            "topic": _topic(r["atom_text"], r["goal"]),
            "channel": r["channel"],
            "status": r["status"],
            "held_reason": r["held_reason"],
            "gate_ledger": gate_ledger,
            "gate_pass_count": gate_counts["passed"],
            "gate_total_count": gate_counts["total"],
            "repair_log": repair_log,
            "retry_count": len(repair_log),
            "attempt_number": r["attempt_number"],
            "content_text": content_text,
            "content_preview": (content_text[:280] if content_text else ""),
            "cta": r["cta"],
            # AA-561 3a — every angle the request generated (idx 0-2), not just the chosen one.
            "angles": _parse_jsonb(r["angles"], []),
            "atom": {
                "text": r["atom_text"], "activity_type": r["atom_activity_type"],
                "emotional_hook": r["atom_emotional_hook"], "season_note": r["atom_season_note"],
            } if r["atom_text"] else None,
            "tour": {
                "name": r["tour_name"], "destination": r["tour_destination"],
            } if r["tour_name"] else None,
            # AA-561 3a — where this request came from: a Slate Segment pick, a Slate Route pick,
            # or (subject_id NULL) the pre-existing atom-picker entry point that never went
            # through Slate at all (AA-449's original path, not retired) — the frontend must show
            # this last case as an explicit label, never a blank cell.
            "source": _content_log_source(r),
            # AA-561 3a — a buffer retry (AA-485) writes a NEW content_piece row for the same
            # request rather than incrementing this row's own attempt_number; derived from sibling
            # rows since there is no previous_piece_id column (migration 115, by design).
            "is_buffer_retry": bool(
                r["sibling_piece_count"] and r["sibling_piece_count"] > 1
                and r["created_at"] != r["request_first_piece_at"]
            ),
            "sibling_piece_count": r["sibling_piece_count"],
            "dfs_paa_snapshot": _parse_jsonb(r["dfs_paa_snapshot"], None),
            "publish_status": _publish_status(r["status"], r["publish_id"] is not None),
            # AA-560 — exposed so the frontend's new "Force unpublish" button (moved here from the
            # deleted a4-oversight page) can address the right publish_log row. Previously used
            # only for the is-not-None check above and discarded — never returned on its own
            # before this (already cast to text in the SELECT, same convention every other
            # UUID-as-text field in this query already uses).
            "publish_id": r["publish_id"],
            "publish_external_url": r["publish_external_url"],
            "publish_published_at": r["publish_published_at"].isoformat() if r["publish_published_at"] else None,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        })
    logger.info(
        "a4_content_log_queried", count=len(data), tenant_filter=tenant_id, tour_filter=tour_id,
        status_filter=status, channel_filter=channel, published_filter=published,
    )
    return {"data": data, "total": len(data), "tenant_filter": tenant_id, "tour_filter": tour_id}


@router.post("/publish-log/{publish_id}/unpublish")
async def force_unpublish(
    publish_id: UUID,
    request: Request,
    x_admin_secret: str = Header(None),
    x_admin_user_id: Optional[str] = Header(None),
):
    """AA-455 bước 1 — A4's one mutating action. Only flips a `status='published'` row to
    'unpublished'; a row already unpublished/failed 404s rather than double-acting (verified
    live, see AA-455-01 implementation notes). `unpublished_by` records "admin:<id>" using the
    same `x-admin-user-id` header AA-232 already established (BFF forwards the verified JWT's
    `sub` claim) — tolerant of a missing/malformed header, same fallback shape
    admin_pipeline.py's reviewed_by handling already uses, so a legacy ADMIN_SECRET-only session
    doesn't 500, it just records "admin:unknown"."""
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool

    admin_actor = "unknown"
    if x_admin_user_id:
        try:
            admin_actor = str(UUID(x_admin_user_id))
        except (ValueError, AttributeError):
            admin_actor = "unknown"

    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE acp_shared.publish_log
            SET status = 'unpublished', unpublished_at = now(), unpublished_by = $2
            WHERE publish_id = $1 AND status = 'published'
            RETURNING publish_id::text, tenant_id::text, channel, status, unpublished_at
        """, publish_id, f"admin:{admin_actor}")

    if not row:
        raise HTTPException(status_code=404, detail="publish_log row not found or already unpublished")

    logger.info("a4_force_unpublish", publish_id=str(publish_id), admin_actor=admin_actor)
    return {
        "publish_id": row["publish_id"],
        "tenant_id": row["tenant_id"],
        "channel": row["channel"],
        "status": row["status"],
        "unpublished_at": row["unpublished_at"].isoformat() if row["unpublished_at"] else None,
        "unpublished_by": f"admin:{admin_actor}",
    }


@router.get("/platform-stats")
async def get_platform_stats(
    request: Request,
    top_n: int = Query(15, ge=1, le=100),
    x_admin_secret: str = Header(None),
):
    """AA-560 — real backend aggregate for "07 · Platform Stats", replacing the deleted
    `/admin/a4-oversight` page's client-side "F1_GROUNDING × 4" tag rollup (which only ever
    counted whatever was in the currently-loaded `limit=200` page, per AA-558 Phần 1 Q3's own
    finding). Every number here is computed by a fresh `GROUP BY` over the FULL
    `acp_shared.content_piece` table — no row cap, no page-load window.

    `top_gate_failures`: unnests every row's `gate_ledger` (a JSONB array of
    `{gate, passed, violations}`, migration 115) via `jsonb_array_elements`, keeps only entries
    where `passed` is false, groups by `gate`. This works directly in SQL regardless of this
    app's own asyncpg jsonb-codec gap (`_parse_jsonb()` above) — that gap is about how Python
    decodes an already-fetched value, not how Postgres evaluates a jsonb function server-side.
    """
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool

    async with pool.acquire() as conn:
        total_pieces = await conn.fetchval("SELECT count(*) FROM acp_shared.content_piece")

        by_status_rows = await conn.fetch("""
            SELECT status, count(*) AS n
            FROM acp_shared.content_piece
            GROUP BY status
            ORDER BY n DESC
        """)

        by_channel_rows = await conn.fetch("""
            SELECT COALESCE(cp.channel, agr.channel) AS channel, count(*) AS n
            FROM acp_shared.content_piece cp
            JOIN acp_shared.angle_gate_request agr ON agr.request_id = cp.angle_gate_request_id
            GROUP BY 1
            ORDER BY n DESC
        """)

        top_gate_rows = await conn.fetch("""
            SELECT elem->>'gate' AS gate, count(*) AS fail_count
            FROM acp_shared.content_piece cp
            CROSS JOIN LATERAL jsonb_array_elements(cp.gate_ledger) AS elem
            WHERE COALESCE((elem->>'passed')::boolean, false) = false
            GROUP BY 1
            ORDER BY fail_count DESC
            LIMIT $1
        """, top_n)

        published_count = await conn.fetchval("""
            SELECT count(DISTINCT piece_id) FROM acp_shared.publish_log WHERE status = 'published'
        """)

    data = {
        "total_pieces": total_pieces,
        "published_count": published_count,
        "by_status": [{"status": r["status"], "count": r["n"]} for r in by_status_rows],
        "by_channel": [{"channel": r["channel"], "count": r["n"]} for r in by_channel_rows],
        "top_gate_failures": [{"gate": r["gate"], "fail_count": r["fail_count"]} for r in top_gate_rows],
    }
    logger.info(
        "a4_platform_stats_queried", total_pieces=total_pieces,
        top_gate_count=len(data["top_gate_failures"]),
    )
    return {"data": data}


@router.get("/gate-telemetry")
async def get_gate_telemetry(
    request: Request,
    date_from: Optional[str] = Query(None, description="ISO date, inclusive lower bound on created_at"),
    date_to: Optional[str] = Query(None, description="ISO date, exclusive upper bound (+1 day applied)"),
    top_n: int = Query(20, ge=1, le=100),
    x_admin_secret: str = Header(None),
):
    """AA-615 — the ONE place admin sees the gate/severity/retry/publish signal the tenant never
    does (the tenant experience is flat `ready_state` only, AA-613). Answers "which gate fails
    most for which channel" and "which tenant keeps shipping warn/held content" so the team can
    decide what prompt/gate/rubric to improve.

    Every metric is a fresh server-side `GROUP BY` over the FULL `acp_shared.content_piece` table
    (plus `publish_log` for publishes, `audit_log` for exports) — no row cap, no page window, same
    approach as /platform-stats. gate_ledger/flags are unnested SERVER-SIDE via
    `jsonb_array_elements` (not fetched into Python), so this app's asyncpg jsonb-codec gap
    (`_parse_jsonb`) is irrelevant here.

    Severity tiers (AA-613): a piece ships as `approved` even with non-blocking gate notes
    (`flags`, "warn"); it only becomes `held` when a BLOCKING product-truth gate stays unresolved
    after ≤2 retries — held content is still delivered to the tenant, only its publish is gated.
    So per (tenant, channel):
      - warn_count  = approved pieces carrying a non-empty `flags` array.
      - held_count  = held pieces (blocked-after-retry, shipped but publish-gated).
      - retry_count = pieces whose write took a 2nd internal attempt (attempt_number >= 2).
    `processing`/`failed` rows are excluded from these three (no shipped content to grade).
    """
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool

    # Shared created_at window, applied to the content_piece-based aggregates. Bound params are
    # appended per-query (asyncpg is positional) so each query gets exactly the params it uses.
    cp_conds = ["cp.status IN ('approved', 'held', 'failed')"]
    cp_params: list = []
    if date_from:
        cp_params.append(date_from)
        cp_conds.append(f"cp.created_at >= ${len(cp_params)}::date")
    if date_to:
        cp_params.append(date_to)
        cp_conds.append(f"cp.created_at < (${len(cp_params)}::date + interval '1 day')")
    cp_where = " AND ".join(cp_conds)

    async with pool.acquire() as conn:
        # Per (tenant, channel): shipped/warn/held/retry counts. status='failed' counts toward
        # `total` but never warn/held/retry (it produced no gradable content) — surfaced so admin
        # sees system-error volume alongside the quality signal.
        by_tc_rows = await conn.fetch(f"""
            SELECT
                cp.tenant_id::text AS tenant_id,
                t.name AS tenant_name,
                COALESCE(cp.channel, agr.channel) AS channel,
                count(*) AS total,
                count(*) FILTER (
                    WHERE cp.status = 'approved'
                      AND cp.flags IS NOT NULL
                      AND jsonb_typeof(cp.flags) = 'array'
                      AND jsonb_array_length(cp.flags) > 0
                ) AS warn_count,
                count(*) FILTER (WHERE cp.status = 'held') AS held_count,
                count(*) FILTER (WHERE cp.status = 'failed') AS failed_count,
                count(*) FILTER (
                    WHERE cp.status IN ('approved', 'held') AND cp.attempt_number >= 2
                ) AS retry_count
            FROM acp_shared.content_piece cp
            JOIN acp_shared.angle_gate_request agr ON agr.request_id = cp.angle_gate_request_id
            LEFT JOIN shared.tenants t ON t.tenant_id = cp.tenant_id
            WHERE {cp_where}
            GROUP BY cp.tenant_id, t.name, COALESCE(cp.channel, agr.channel)
            ORDER BY total DESC
        """, *cp_params)

        # Top failing gates per channel: unnest gate_ledger, keep failed entries, group by
        # channel + gate. `blocking` (migration 139 / AA-528) distinguishes a product-truth
        # block from a non-blocking warn — surfaced so admin can tell "gate X hard-fails on
        # channel Y" from "gate X is just a warn".
        top_gate_rows = await conn.fetch(f"""
            SELECT
                COALESCE(cp.channel, agr.channel) AS channel,
                elem->>'gate' AS gate,
                COALESCE((elem->>'blocking')::boolean, true) AS blocking,
                count(*) AS fail_count
            FROM acp_shared.content_piece cp
            JOIN acp_shared.angle_gate_request agr ON agr.request_id = cp.angle_gate_request_id
            CROSS JOIN LATERAL jsonb_array_elements(cp.gate_ledger) AS elem
            WHERE {cp_where}
              AND COALESCE((elem->>'passed')::boolean, false) = false
            GROUP BY 1, 2, 3
            ORDER BY fail_count DESC
            LIMIT ${len(cp_params) + 1}
        """, *cp_params, top_n)

        # Publish count per tenant/channel — from publish_log (the source of truth for a real
        # publish), not the audit feed. Its own window, on published_at.
        pub_conds = ["pl.status = 'published'"]
        pub_params: list = []
        if date_from:
            pub_params.append(date_from)
            pub_conds.append(f"pl.published_at >= ${len(pub_params)}::date")
        if date_to:
            pub_params.append(date_to)
            pub_conds.append(f"pl.published_at < (${len(pub_params)}::date + interval '1 day')")
        pub_where = " AND ".join(pub_conds)
        publish_rows = await conn.fetch(f"""
            SELECT pl.tenant_id::text AS tenant_id, pl.channel, count(*) AS publish_count
            FROM acp_shared.publish_log pl
            WHERE {pub_where}
            GROUP BY pl.tenant_id, pl.channel
            ORDER BY publish_count DESC
        """, *pub_params)

        # Export count per tenant/channel — only the audit_log records this (there is no
        # export_log table). action='content_piece.exported', channel lives in details JSONB
        # (details->>'channel'). tenant_id on audit_log is VARCHAR(50), not uuid — compared as
        # text. Its own window, on created_at.
        exp_conds = ["al.action = 'content_piece.exported'"]
        exp_params: list = []
        if date_from:
            exp_params.append(date_from)
            exp_conds.append(f"al.created_at >= ${len(exp_params)}::date")
        if date_to:
            exp_params.append(date_to)
            exp_conds.append(f"al.created_at < (${len(exp_params)}::date + interval '1 day')")
        exp_where = " AND ".join(exp_conds)
        export_rows = await conn.fetch(f"""
            SELECT al.tenant_id, al.details->>'channel' AS channel, count(*) AS export_count
            FROM acp_shared.audit_log al
            WHERE {exp_where}
            GROUP BY al.tenant_id, al.details->>'channel'
            ORDER BY export_count DESC
        """, *exp_params)

    # Fold publish/export counts into the per-(tenant,channel) rows keyed by (tenant_id, channel),
    # so the FE renders one row per tenant/channel with every metric. A publish/export with no
    # matching content_piece row in the window (e.g. published today, written last week) still
    # gets its own row rather than being dropped.
    def _key(tid, ch):
        return (tid or "", ch or "")

    merged: dict[tuple, dict] = {}
    for r in by_tc_rows:
        merged[_key(r["tenant_id"], r["channel"])] = {
            "tenant_id": r["tenant_id"], "tenant_name": r["tenant_name"], "channel": r["channel"],
            "total": r["total"], "warn_count": r["warn_count"], "held_count": r["held_count"],
            "failed_count": r["failed_count"], "retry_count": r["retry_count"],
            "publish_count": 0, "export_count": 0,
        }
    for r in publish_rows:
        row = merged.setdefault(_key(r["tenant_id"], r["channel"]), {
            "tenant_id": r["tenant_id"], "tenant_name": None, "channel": r["channel"],
            "total": 0, "warn_count": 0, "held_count": 0, "failed_count": 0, "retry_count": 0,
            "publish_count": 0, "export_count": 0,
        })
        row["publish_count"] = r["publish_count"]
    for r in export_rows:
        row = merged.setdefault(_key(r["tenant_id"], r["channel"]), {
            "tenant_id": r["tenant_id"], "tenant_name": None, "channel": r["channel"],
            "total": 0, "warn_count": 0, "held_count": 0, "failed_count": 0, "retry_count": 0,
            "publish_count": 0, "export_count": 0,
        })
        row["export_count"] = r["export_count"]

    by_tenant_channel = sorted(
        merged.values(),
        key=lambda x: (x["total"], x["publish_count"], x["export_count"]),
        reverse=True,
    )

    data = {
        "by_tenant_channel": by_tenant_channel,
        "top_gate_failures_by_channel": [
            {"channel": r["channel"], "gate": r["gate"],
             "blocking": r["blocking"], "fail_count": r["fail_count"]}
            for r in top_gate_rows
        ],
    }
    logger.info(
        "a4_gate_telemetry_queried",
        tenant_channel_rows=len(by_tenant_channel),
        gate_rows=len(data["top_gate_failures_by_channel"]),
        date_from=date_from, date_to=date_to,
    )
    return {"data": data}
