"""
api/routers/admin_atoms.py — AA-300 curation UI backend.

Auth: x-admin-secret header only (no tenant JWT) — same convention as
admin.py/admin_pipeline.py (STEP 0/PHẦN A decision, AA-300). Reuses
verify_admin_secret from admin.py rather than redefining it. Reached only
through the existing frontend/app/api/admin/[...path]/route.ts BFF proxy —
no new proxy route needed (it already forwards any /api/admin/* path to
/admin/* on this backend).

Does NOT touch api/routers/v1_atoms.py (decompose) at all — this is a
separate, purely additive resource: list/filter + star/delete/edit on
already-decomposed atoms, plus a read-only preview wrapper around the real
N4/N5/N6 pipeline (services/acp_planning/) for the first visual look at the
whole ACP v2 pipeline end to end.

GET   /admin/atoms                    — list/filter, batch of 50 by default
GET   /admin/atoms/summary            — dashboard counts + by-tour accordion data
PATCH /admin/atoms/{atom_id}          — star / soft-delete / light text edit

Route order note: /atoms/summary is registered BEFORE /atoms/{atom_id} — this
repo's own CRITICAL rule (CLAUDE.md: "/{id}/full MUST come BEFORE /{id}")
applies here too, since FastAPI would otherwise greedily match "summary" as
{atom_id} on a GET /atoms/summary request.

AA-475: PATCH /admin/atoms/bulk and GET /admin/atoms/preview-slotgrid were
deleted along with /admin/curation + /admin/curation/preview (their only
callers, STEP0-confirmed no owner_scope/JWT path ever reached them from T6) —
see docs/claude_audit/AA-475-step0-atomize-curation-teardown.md.
"""
import asyncio
import json
from decimal import Decimal
from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from api.routers.admin import verify_admin_secret
from api.routers.auth import verify_jwt
from services.acp_shared.atom_constants import THIN_TRIP_ATOM_MIN

router = APIRouter(prefix="/admin", tags=["admin-atoms"])

# AA-564 3.1 — strong refs for the delete-triggered recompute background task, same GC-safety
# pattern as services/export/handler.py's own module-level `_background_tasks` set (a bare
# asyncio.create_task() with no reference can be garbage-collected mid-flight).
_recompute_tasks: set = set()
_recompute_logger = structlog.get_logger()


# ── AA-431 — tenant-JWT auth + owner_scope filter for list/summary/patch ────────
#
# Before this, every endpoint here was x-admin-secret only (module header comment,
# AA-300 STEP 0 decision) — fine while the only UI was /admin/curation (staff-only,
# platform-owned atoms). AA-425's T5 (services/acp_produce/tenant_pipeline.py)
# started writing atoms with owner_scope = <tenant_id> (a TENANT's own atoms, from
# their own rewritten content) alongside the pre-existing owner_scope = 'platform'
# rows — but nothing here ever filtered by owner_scope, and there was no tenant-JWT
# auth path at all. Building a tenant-facing curation UI (AA-431, /portal/t6-atoms)
# straight on top of these endpoints as they were would have let tenant A see/edit
# tenant B's atoms (or platform's) — same class of gap AA-424 already closed for
# brand-identity. Mirrors that fix's exact shape (_resolve_brand_tenant_id,
# admin_pipeline.py): try a tenant Bearer JWT first, fall back to X-Admin-Secret.
#
# owner_scope is NEVER accepted as a client-supplied query param for a tenant
# caller — it's derived only from the verified JWT's `sub` claim, so a tenant can't
# request a different owner_scope than its own. An admin/staff caller (X-Admin-
# Secret) gets owner_scope=None (no filter) — unchanged from before, staff still
# need to see platform + every tenant's atoms for curation/support.
_atoms_bearer = HTTPBearer(auto_error=False)


def _resolve_atom_owner_scope(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_atoms_bearer),
    x_admin_secret: str = Header(None),
) -> Optional[str]:
    """Returns the owner_scope to filter tour_atoms by (a tenant_id string), or
    None for a staff/admin caller (no filter — sees platform + every tenant)."""
    if credentials is not None:
        try:
            payload = verify_jwt(credentials.credentials)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        return payload["sub"]
    verify_admin_secret(x_admin_secret)
    return None


def _safe(row) -> dict:
    """Same pattern as v1_tours.py's local safe() helper — UUID/Decimal/
    datetime -> JSON-safe. No shared api/utils.safe() exists in this repo
    (checked); every router defines its own local copy.

    Also parses `media` (tour_atoms.media, JSONB) if it comes back as a raw
    string — asyncpg has no jsonb codec registered on this app's
    connections (same gap AA-314 already found for src_highlights
    elsewhere, api/routers/v1_tours.py; also just found for
    cooldown_until/usage_log in services/acp_planning/quarter.py). Without
    this, atom.media?.has_photo on the frontend silently reads undefined
    off a JSON string instead of the real boolean — no crash, just always
    "no photo" regardless of actual data."""
    if not row:
        return {}
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, UUID):
            d[k] = str(v)
        elif isinstance(v, Decimal):
            d[k] = float(v)
        elif hasattr(v, "isoformat"):
            d[k] = v.isoformat()
    if "media" in d and isinstance(d["media"], str):
        d["media"] = json.loads(d["media"]) if d["media"] else {}
    return d


# ── GET /admin/atoms — list/filter, batch of 50 ─────────────────────────────

_LIST_FROM = """
    FROM acp_contract.tour_atoms ta
    JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = ta.tour_id
    JOIN (
        SELECT tour_id, count(*) AS atom_count
        FROM acp_contract.tour_atoms
        WHERE NOT deleted AND NOT is_empty_marker
        GROUP BY tour_id
    ) tc ON tc.tour_id = ta.tour_id
    -- AA-509 — Segment: LEFT JOIN, an atom atomized before migration 129 (or not yet
    -- re-atomized since) has no place/action and so was never fed to segment_matching.py —
    -- segment_id/canonical_* come back NULL for it, same "ungrouped" treatment the frontend
    -- already gives an atom with no segment.
    LEFT JOIN acp_contract.atom_segment_member asm ON asm.atom_id = ta.atom_id
    LEFT JOIN acp_contract.atom_segment asg ON asg.segment_id = asm.segment_id
    -- AA-519 Việc 2 — atom_ranking is Segment-grain (migration 130, AA-515), not per-atom: a
    -- lone/ungrouped atom (asm.segment_id NULL) simply never matches, ar.total_rank comes back
    -- NULL, same "nothing to show" convention canonical_place/canonical_action already use for
    -- an ungrouped atom.
    -- AA-545 — atom_ranking now has up to 6 rows per (tour, segment), one per finite market
    -- (PK (market, tour_id, segment_id)); pinned to 'US' here (the platform's own established
    -- default, services/seo_intelligence/seed_builder.py::_DEFAULT_MARKET) so this admin listing
    -- shows exactly one representative row per atom, not a 6x fan-out — this page is a curation
    -- list, not a per-market audit (that's admin_dashboard.py's job).
    LEFT JOIN acp_contract.atom_ranking ar
        ON ar.tour_id = ta.tour_id AND ar.segment_id = asm.segment_id AND ar.market = 'US'
    -- Route/Hub link — a Segment is "part of a Route" when some Route's ordered_segment_ids
    -- (migration 131, AA-510) contains it. LATERAL + LIMIT 1: a Segment could in principle
    -- appear in more than one Route (re-detection across tours); this page only ever needs ONE
    -- name to display, not an exhaustive list — same "best-effort single representative" choice
    -- services/acp_content_writing/service.py::_fetch_route_segments() already makes elsewhere.
    LEFT JOIN LATERAL (
        SELECT r.route_id, r.hub_name
        FROM acp_contract.route r
        -- AA-532: current version only — a superseded route (versioning replaced the old
        -- DELETE+INSERT-whole rebuild, never deleted) must not still read as "part of Route X"
        -- once re-detection has moved this Segment on.
        WHERE r.tour_id = ta.tour_id AND r.superseded_at IS NULL
          AND r.ordered_segment_ids @> jsonb_build_array(asm.segment_id)
        LIMIT 1
    ) rte ON asm.segment_id IS NOT NULL
    -- AA-527 (bổ sung, "Bổ sung yêu cầu kiểm tra" comment, point 3) — real usage count: how many
    -- DISTINCT T8 requests (acp_shared.angle_gate_request, keyed by atom_id) this atom has ever
    -- been written from, regardless of write outcome (held/approved/failed) — "was a write
    -- actually attempted from this atom", not attempt-count (2 attempts of 1 request = 1 use).
    -- Confirmed NOT backlog (STEP0, live DB read 05/09/2026): angle_gate_request.atom_id already
    -- exists (migration 113, AA-449) and real repeat usage already exists in prod data (e.g. atom
    -- 338e362c081d98d5 written from 3 separate times) — this is a plain aggregate over data that
    -- already exists, distinct from atom_ranking.recurrence (which counts Segment/itinerary
    -- overlap at atomize time, not actual downstream write usage).
    LEFT JOIN (
        SELECT atom_id, count(DISTINCT request_id) AS usage_count
        FROM acp_shared.angle_gate_request
        GROUP BY atom_id
    ) uc ON uc.atom_id = ta.atom_id
    WHERE NOT ta.is_empty_marker
"""

_LIST_SELECT_COLS = """
    SELECT ta.atom_id, ta.tour_id, rt.src_name AS tour_name, ta.text,
           ta.activity_type, ta.emotional_hook, ta.visual_potential,
           ta.distinctiveness, ta.media, ta.deleted,
           ta.created_at, ta.updated_at,
           (ta.updated_at = ta.created_at) AS unreviewed,
           tc.atom_count AS tour_atom_count,
           asm.segment_id, asg.canonical_place, asg.canonical_action,
           ar.total_rank AS segment_score, rte.route_id, rte.hub_name AS route_hub_name,
           ta.owner_scope,
           -- AA-527 (bổ sung): recurrence (Phần 12 kết luận #3 — "N itinerary/Segment", ĐÃ CÓ ở
           -- atom_ranking, chỉ thiếu JOIN vào response, effort thấp) + usage_count (điểm 3 kiểm
           -- tra bổ sung — real content_piece write-usage, see uc CTE above) +
           -- rt.lifecycle_stage (điểm 2 kiểm tra bổ sung — tour active/phasing_out/retired,
           -- silver_aa_internal.raw_tours, migration 086, đã có sẵn từ AA-301).
           ar.recurrence, COALESCE(uc.usage_count, 0) AS usage_count,
           rt.lifecycle_stage::text AS lifecycle_stage
"""


@router.get("/atoms")
async def list_atoms(
    request: Request,
    tour_id: Optional[str] = Query(None),
    tour_ids: Optional[str] = Query(None),
    atom_ids: Optional[str] = Query(None),
    distinctiveness: Optional[str] = Query(None, pattern="^(HIGH|MED|LOW)$"),
    unreviewed_only: bool = Query(False),
    thin_only: bool = Query(False),
    include_deleted: bool = Query(False),
    owner_scope_class: Optional[str] = Query(
        None, pattern="^(platform|legacy)$",
        description="AA-527 (bổ sung) — admin-only coarse filter: 'platform' (owner_scope='platform', "
                    "post-AA-526 shared pool) vs 'legacy' (any other owner_scope — a real tenant_id, "
                    "pre-AA-526 row not yet cleaned up). Ignored for a tenant-JWT caller (owner_scope "
                    "already pinned to that tenant by _resolve_atom_owner_scope, nothing to choose).",
    ),
    lifecycle_stage: Optional[str] = Query(
        None, pattern="^(active|phasing_out|retired)$",
        description="AA-527 (bổ sung, kiểm tra điểm 2) — filter by the tour's own "
                    "silver_aa_internal.raw_tours.lifecycle_stage (migration 086), so AA can find "
                    "atoms sitting on a phasing_out/retired tour, not just active ones.",
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    owner_scope: Optional[str] = Depends(_resolve_atom_owner_scope),
):
    """List/filter atoms for curation. Defaults to a 50-atom batch (issue
    AA-300: "50 atom trên 1 màn hình", not one atom at a time). Returns a
    `total` matching-filter count (a second COUNT(*) query, same WHERE
    clause) alongside the page — needed so the frontend can reuse the
    existing Pagination.tsx component as-is, which requires a total item
    count to compute page numbers (not something the original single-query
    design produced; added specifically for that reuse — self-chosen, see
    AA-300 implementation notes).

    "unreviewed" is derived from updated_at == created_at rather than a new
    column — tour_atoms has no reviewed/reviewed_at field, and both
    timestamps are set to the same now() at insert time (v1_atoms.py's
    INSERT, migration 079/084/085), so they stay exactly equal until the
    first PATCH touches the row. Self-chosen — see AA-300 implementation
    notes. "thin" reuses the tour_atom_count already computed in the JOIN
    (< THIN_TRIP_ATOM_MIN, the same constant N5's B5 fix imports).

    AA-431: owner_scope resolved server-side from the caller's identity (tenant JWT
    -> that tenant's own atoms only; admin secret -> no filter) — never a query
    param, so a tenant can't ask to see another owner_scope's atoms."""
    pool = request.app.state.pool

    clauses = []
    params: list = []

    def _add(clause: str, value) -> None:
        params.append(value)
        clauses.append(clause.format(n=len(params)))

    if owner_scope is not None:
        _add("ta.owner_scope = ${n}", owner_scope)
    elif owner_scope_class == "platform":
        clauses.append("ta.owner_scope = 'platform'")
    elif owner_scope_class == "legacy":
        clauses.append("ta.owner_scope != 'platform'")

    if lifecycle_stage:
        # rt.lifecycle_stage is a Postgres ENUM (silver_aa_internal.tour_lifecycle_stage_enum) —
        # cast to text rather than the param, Postgres has no implicit text->enum cast for `=`.
        _add("rt.lifecycle_stage::text = ${n}", lifecycle_stage)

    # AA-345 round 2, Việc 4: tour_ids (comma-separated, plural) is the deep
    # link from /admin/atomize after a multi-tour decompose run — lets the
    # curation page filter to exactly the tours just processed instead of
    # losing them in a long list. tour_id (singular) is the older single-tour
    # link, kept unchanged for backward compat; tour_ids wins if both given.
    if tour_ids:
        id_list = [t.strip() for t in tour_ids.split(",") if t.strip()]
        if id_list:
            _add("ta.tour_id = ANY(${n}::uuid[])", id_list)
    elif tour_id:
        _add("ta.tour_id = ${n}::uuid", tour_id)
    # AA-323 Gap 4 — Preview's slot cards link an atom_id list straight to this
    # endpoint for a detail panel, rather than adding a separate single-purpose
    # route. Independent of tour_id/tour_ids so it can be combined or used alone.
    if atom_ids:
        atom_id_list = [a.strip() for a in atom_ids.split(",") if a.strip()]
        if atom_id_list:
            _add("ta.atom_id = ANY(${n})", atom_id_list)
    if distinctiveness:
        _add("ta.distinctiveness = ${n}", distinctiveness)
    if not include_deleted:
        clauses.append("NOT ta.deleted")
    if unreviewed_only:
        clauses.append("ta.updated_at = ta.created_at")
    if thin_only:
        _add("tc.atom_count < ${n}", THIN_TRIP_ATOM_MIN)

    where_sql = _LIST_FROM
    if clauses:
        where_sql += " AND " + " AND ".join(clauses)

    count_query = "SELECT count(*) " + where_sql
    select_query = _LIST_SELECT_COLS + where_sql

    select_params = list(params)
    select_params.append(limit)
    select_query += f" ORDER BY ta.tour_id, ta.created_at LIMIT ${len(select_params)}"
    select_params.append(offset)
    select_query += f" OFFSET ${len(select_params)}"

    async with pool.acquire() as conn:
        total = await conn.fetchval(count_query, *params)
        rows = await conn.fetch(select_query, *select_params)

    return {
        "atoms": [_safe(r) for r in rows], "count": len(rows),
        "total": total, "limit": limit, "offset": offset,
    }


# ── GET /admin/atoms/summary — dashboard counts + by-tour accordion data ───

@router.get("/atoms/summary")
async def atoms_summary(
    request: Request,
    owner_scope: Optional[str] = Depends(_resolve_atom_owner_scope),
):
    """Whole-dataset counts, deliberately independent of whatever filter is
    currently applied on the paginated /atoms list — a separate endpoint
    rather than extra fields bolted onto GET /atoms, because the dashboard
    only needs to change when the underlying data changes (a star/delete/
    bulk action), not on every filter click or load-more page fetch. Folding
    it into the list response would mean recomputing 3 extra aggregate
    queries on every single filter/pagination round-trip for no reason —
    fewer total queries over a real editing session with this split.

    AA-431: same owner_scope resolution as GET /atoms — a tenant caller only
    ever sees counts for its own atoms."""
    pool = request.app.state.pool
    scope_clause = "AND owner_scope = $1" if owner_scope is not None else ""
    scope_params = [owner_scope] if owner_scope is not None else []
    scope_clause_ta = "AND ta.owner_scope = $1" if owner_scope is not None else ""

    async with pool.acquire() as conn:
        breakdown_rows = await conn.fetch(f"""
            SELECT distinctiveness, count(*) AS c
            FROM acp_contract.tour_atoms
            WHERE NOT deleted AND NOT is_empty_marker {scope_clause}
            GROUP BY distinctiveness
        """, *scope_params)
        totals = await conn.fetchrow(f"""
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE updated_at != created_at) AS reviewed
            FROM acp_contract.tour_atoms
            WHERE NOT deleted AND NOT is_empty_marker {scope_clause}
        """, *scope_params)
        by_tour_rows = await conn.fetch(f"""
            SELECT ta.tour_id, rt.src_name AS tour_name,
                   rt.lifecycle_stage::text AS lifecycle_stage,
                   count(*) AS atom_count,
                   count(*) FILTER (WHERE ta.updated_at = ta.created_at) AS unreviewed_count,
                   count(*) FILTER (WHERE uc.atom_id IS NOT NULL) AS used_atom_count,
                   MAX(ta.created_at) AS atomized_at,
                   array_agg(DISTINCT ta.owner_scope) AS owner_scopes
            FROM acp_contract.tour_atoms ta
            JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = ta.tour_id
            -- AA-527 (bổ sung, kiểm tra điểm 3) — per-tour "how many distinct atoms of this tour
            -- have ever been used to write content" (same angle_gate_request.atom_id source as
            -- the per-atom usage_count added to GET /atoms above). The subquery is DISTINCT
            -- (atom_id, trip_id) up front, so it can join at most 1 row per ta row — required so
            -- this LEFT JOIN can't fan out ta rows and inflate the plain count(*) used for
            -- atom_count/unreviewed_count just above (a JOIN straight onto angle_gate_request,
            -- which can have several requests per atom, would double-count both).
            LEFT JOIN (
                SELECT DISTINCT atom_id, trip_id
                FROM acp_shared.angle_gate_request
            ) uc ON uc.atom_id = ta.atom_id AND uc.trip_id = ta.tour_id
            WHERE NOT ta.deleted AND NOT ta.is_empty_marker {scope_clause_ta}
            GROUP BY ta.tour_id, rt.src_name, rt.lifecycle_stage
            ORDER BY rt.src_name
        """, *scope_params)

    breakdown = {"HIGH": 0, "MED": 0, "LOW": 0}
    for r in breakdown_rows:
        if r["distinctiveness"] in breakdown:
            breakdown[r["distinctiveness"]] = r["c"]

    by_tour = [
        {
            "tour_id": str(r["tour_id"]), "tour_name": r["tour_name"],
            "atom_count": r["atom_count"], "is_thin": r["atom_count"] < THIN_TRIP_ATOM_MIN,
            "unreviewed_count": r["unreviewed_count"],
            # AA-527 (bổ sung, kiểm tra điểm 3) — atoms of this tour with >=1 real write attempt.
            "used_atom_count": r["used_atom_count"],
            # AA-527 (bổ sung, kiểm tra điểm 2) — active|phasing_out|retired (migration 086,
            # AA-301). Lets the dashboard flag a tour that's winding down/stopped but still has
            # atoms sitting in the curation pool.
            "lifecycle_stage": r["lifecycle_stage"],
            # AA-345 round 2, Việc 4: MAX(created_at) — same "last touched"
            # choice as GET /admin/tours-for-atomization's atomized_at, for
            # the new "Newest first" sort + section-header date display.
            "atomized_at": r["atomized_at"].isoformat() if r["atomized_at"] else None,
            # AA-527 — which owner_scope(s) this tour's (live) atoms actually carry: always
            # ["platform"] for anything atomized after AA-526 (A3), or a real tenant_id for a
            # pre-AA-526 legacy row (still exists, not yet cleaned up — see AA-526's own
            # implementation notes). Exposed so the new admin curation page can flag legacy data
            # distinctly rather than silently mixing it with the new platform-wide atoms.
            "owner_scopes": list(r["owner_scopes"]) if r["owner_scopes"] else [],
        }
        for r in by_tour_rows
    ]

    return {
        "distinctiveness_breakdown": breakdown,
        "total_count": totals["total"] if totals else 0,
        "reviewed_count": totals["reviewed"] if totals else 0,
        "by_tour": by_tour,
    }


# ── PATCH /admin/atoms/{atom_id} — star / delete / light edit ──────────────

class AtomPatchRequest(BaseModel):
    deleted: Optional[bool] = None
    text: Optional[str] = None


@router.patch("/atoms/{atom_id}")
async def patch_atom(
    atom_id: str,
    body: AtomPatchRequest,
    request: Request,
    owner_scope: Optional[str] = Depends(_resolve_atom_owner_scope),
):
    """Soft-delete / light text edit. `deleted=true` is the existing
    tour_atoms.deleted column (soft delete) — already excluded from the N6
    allocator's eligible pool (services/acp_planning/allocator.py's
    _eligible_atoms(): `if a.deleted ... continue`) and the segment_matching
    filter (`WHERE NOT ta.deleted`).

    AA-609: the `starred` curation flag was removed — it had no live effect
    (its only reader sat behind compute_slot_grid(), which has no production
    caller), so `deleted` is the only real curation lever left here.

    AA-431: same owner_scope guard as the bulk endpoint — a tenant's UPDATE
    is WHERE-scoped, so a guessed atom_id from outside their own scope 404s
    (not found) instead of being editable."""
    if body.text is not None and not body.text.strip():
        raise HTTPException(status_code=400, detail="text cannot be empty")
    if body.deleted is None and body.text is None:
        raise HTTPException(status_code=400, detail="no fields to update")

    sets = []
    params: list = []

    def _set(column: str, value) -> None:
        params.append(value)
        sets.append(f"{column} = ${len(params)}")

    if body.deleted is not None:
        _set("deleted", body.deleted)
    if body.text is not None:
        _set("text", body.text)
    sets.append("updated_at = now()")

    params.append(atom_id)
    atom_id_idx = len(params)
    scope_clause = ""
    if owner_scope is not None:
        params.append(owner_scope)
        scope_clause = f" AND owner_scope = ${len(params)}"
    query = f"""
        UPDATE acp_contract.tour_atoms
        SET {", ".join(sets)}
        WHERE atom_id = ${atom_id_idx}{scope_clause} AND NOT is_empty_marker
        RETURNING atom_id, tour_id, text, distinctiveness, deleted,
                  visual_potential, media, created_at, updated_at
    """

    pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(query, *params)

    if not row:
        raise HTTPException(status_code=404, detail=f"Atom {atom_id} not found (or is an empty-marker row)")

    # AA-564 3.1 (decision 2, AA-563) — `deleted` is the only atom-curation flag that actually
    # affects Segment eligibility (`WHERE NOT ta.deleted`, segment_matching.py). Before this,
    # curating an atom's `deleted` flag never recomputed Segment/Score/Route at all — a real
    # staleness gap
    # AA-563's investigation confirmed matches Nghiệp's own suspicion. Fire-and-forget, same
    # pattern as `_run_a3_atomize_background()` (own connection via the request's pool, best-effort
    # — a recompute failure must never surface as this PATCH having failed, the star/delete itself
    # already committed above).
    if body.deleted is not None:
        from services.export.handler import recompute_segment_score_route

        async def _recompute():
            try:
                await recompute_segment_score_route(str(row["tour_id"]), pool, log_tour_id=atom_id)
            except Exception:
                _recompute_logger.warning("atom_delete_recompute_failed", atom_id=atom_id, exc_info=True)

        _task = asyncio.create_task(_recompute())
        _recompute_tasks.add(_task)
        _task.add_done_callback(_recompute_tasks.discard)

    return _safe(row)


# ── GET /admin/atoms/unatomized-tours + POST /admin/atoms/atomize ──────────────
# AA-564 3.1/3.2 (decision 1, AA-563) — a manual backfill trigger. Per AA-563's investigation,
# atomize has exactly ONE automatic trigger point (services/export/handler.py::process_export(),
# fired the instant a tour is admin-approved/published) and NO backfill ever existed — any tour
# that entered Master Content before that mechanism went live (~04-05/09/2026), or by any other
# path than a fresh publish, never gets atomized on its own. These two endpoints are admin-only
# (x-admin-secret, no tenant-JWT path — matches AA-526's decision that tenants never trigger
# atomize directly anymore).

_MASTER_TENANT_ID = "00000000-0000-0000-0000-000000000001"  # api/routers/admin_pipeline.py's own constant


@router.get("/atoms/unatomized-tours")
async def list_unatomized_tours(
    request: Request,
    x_admin_secret: str = Header(None),
):
    """Master Content tours (gold_aa_internal.published_tours, the aa_internal sentinel tenant)
    with zero real atoms yet — the FE's "N tours not yet atomized" warning + picker, and also the
    poll target while a manual atomize run is in flight (a tour drops off this list once its
    atoms actually land)."""
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT pt.tour_id, pt.aa_name AS tour_name
            FROM gold_aa_internal.published_tours pt
            WHERE pt.tenant_id = $1::uuid
              AND NOT EXISTS (
                  SELECT 1 FROM acp_contract.tour_atoms ta
                  WHERE ta.tour_id = pt.tour_id AND NOT ta.is_empty_marker
              )
            ORDER BY pt.aa_name
            """,
            _MASTER_TENANT_ID,
        )
    return {
        "total": len(rows),
        "tours": [{"tour_id": str(r["tour_id"]), "tour_name": r["tour_name"]} for r in rows],
    }


class AtomizeTriggerRequest(BaseModel):
    tour_id: Optional[str] = None
    all: bool = False


@router.post("/atoms/atomize")
async def trigger_atomize(
    body: AtomizeTriggerRequest,
    request: Request,
    x_admin_secret: str = Header(None),
):
    """Manually fires the same atomize + Segment/Score/Route recompute chain
    (services/export/handler.py::_run_a3_atomize_background()) that a fresh publish triggers
    automatically — for a Master Content tour that never went through that path. Body is either
    `{"tour_id": "..."}` (one tour) or `{"all": true}` (every currently un-atomized tour).

    Runs the tours SEQUENTIALLY in one background task, not N parallel tasks — `run_t5_atomize()`
    can issue several Bedrock calls per tour (one per itinerary day), and this endpoint has no
    cap on how many tours "all" might mean; sequential avoids a burst of concurrent LLM calls
    against the account's rate limit. Fire-and-forget, same 202-style contract as
    `run_write_background()` (T9) — no separate job/status row is created; the client polls
    `GET /admin/atoms/unatomized-tours` (or `/admin/atoms/summary`) until the tour(s) drop off /
    gain a real atom_count, the same "poll the resulting resource" pattern T9 already uses rather
    than a dedicated job-status table."""
    verify_admin_secret(x_admin_secret)
    if not body.tour_id and not body.all:
        raise HTTPException(status_code=400, detail="must specify tour_id or all=true")

    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT pt.tour_id, pt.generated_content_id, pt.aa_name, pt.aa_summary,
                   pt.aa_highlights, pt.aa_itineraries, rt.country
            FROM gold_aa_internal.published_tours pt
            JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
            WHERE pt.tenant_id = $1::uuid
              AND NOT EXISTS (
                  SELECT 1 FROM acp_contract.tour_atoms ta
                  WHERE ta.tour_id = pt.tour_id AND NOT ta.is_empty_marker
              )
              AND ($2::uuid IS NULL OR pt.tour_id = $2::uuid)
            """,
            _MASTER_TENANT_ID, None if body.all else body.tour_id,
        )

    if not rows:
        raise HTTPException(status_code=404, detail="No un-atomized Master Content tour matches this request")

    from services.export.handler import _run_a3_atomize_background

    async def _run_all():
        for r in rows:
            try:
                await _run_a3_atomize_background(
                    tour_id=str(r["tour_id"]),
                    rewritten={
                        "name": r["aa_name"], "summary": r["aa_summary"],
                        "highlights": r["aa_highlights"], "itineraries": r["aa_itineraries"],
                    },
                    country=r["country"] or "",
                    version_id=str(r["generated_content_id"]),
                )
            except Exception:
                _recompute_logger.error("manual_atomize_failed", tour_id=str(r["tour_id"]), exc_info=True)

    _task = asyncio.create_task(_run_all())
    _recompute_tasks.add(_task)
    _task.add_done_callback(_recompute_tasks.discard)

    return {
        "accepted": True, "tour_count": len(rows),
        "tour_ids": [str(r["tour_id"]) for r in rows],
    }
