"""AA-769 — /admin/content-quality: the admin home for every quality signal the tenant no longer
sees (AA-770: tenant always receives T2/T9 content; gate results, held reasons, flagged
sentences, writer inputs and per-gate stats live here, for AA admins to improve the system).

Cross-tenant by design: admin pool (aa_cis_admin, BYPASSRLS), X-Admin-Secret checked on every
route like the other /admin routers. Read-only except the place-card verdict, the place-card
merge (both audited in acp_shared.audit_log) and the place-card build job.

Counting rule (S232 lesson): `held_reason` names only the first blocking gate; every count and
filter here reads `gate_ledger` (+ `flags`), never `held_reason`.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel

from api.routers.admin import verify_admin_secret
from services.acp_content_writing import sentence_repair
from services.acp_shared import place_cards
from services.acp_shared.audit_log import PlatformAuditAction, write_audit_log

router = APIRouter(prefix="/admin/content-quality", tags=["admin-content-quality"])

_PIECE_SORTS = {"created_at": "cp.created_at", "status": "cp.status", "channel": "channel",
                "tenant": "t.name", "cost_usd": "cost_usd", "attempt": "cp.attempt_number"}
_VERSION_SORTS = {"created_at": "ttv.created_at", "status": "ttv.status", "tenant": "t.name",
                  "quality_score": "ttv.quality_score", "cost_usd": "cost_usd"}


def _j(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v
    return v


def _row(r) -> dict:
    out = {}
    for k, v in dict(r).items():
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        elif isinstance(v, UUID):
            v = str(v)
        elif isinstance(v, Decimal):
            v = float(v)
        out[k] = _j(v) if k in ("gate_ledger", "flags", "repair_log", "discarded_attempts", "writer_input",
                                "facts", "escalate_detail", "failed_gates", "flag_gates") else v
    return out


# ── T9 pieces ──────────────────────────────────────────────────────────────────────────────────

_PIECE_FROM = """
    FROM acp_shared.content_piece cp
    JOIN shared.tenants t ON t.tenant_id = cp.tenant_id
    LEFT JOIN acp_shared.angle_gate_request agr ON agr.request_id = cp.angle_gate_request_id
    LEFT JOIN acp_shared.angle_gate_option ago
        ON ago.option_id = cp.angle_gate_option_id
        OR (cp.angle_gate_option_id IS NULL AND ago.request_id = agr.request_id AND ago.chosen = true)
"""

_FAILED_GATES_SQL = """
    COALESCE((SELECT jsonb_agg(DISTINCT g->>'gate') FROM jsonb_array_elements(cp.gate_ledger) g
              WHERE (g->>'passed')::boolean = false AND COALESCE((g->>'blocking')::boolean, true)),
             '[]'::jsonb) AS failed_gates,
    COALESCE((SELECT jsonb_agg(DISTINCT g->>'gate') FROM jsonb_array_elements(COALESCE(cp.flags, '[]'::jsonb)) g),
             '[]'::jsonb) AS flag_gates
"""

_COST_SQL = """
    COALESCE((SELECT sum(l.cost_usd) FROM shared.llm_call_log l WHERE l.job_id = cp.job_id), 0) AS cost_usd
"""


def _piece_filters(tenant_id, channel, status, gate, q) -> tuple[str, list]:
    where, params = ["true"], []
    if tenant_id:
        params.append(tenant_id)
        where.append(f"cp.tenant_id = ${len(params)}::uuid")
    if channel:
        params.append(channel)
        where.append(f"COALESCE(cp.channel, agr.channel) = ${len(params)}")
    if status:
        params.append(status)
        where.append(f"cp.status = ${len(params)}")
    if gate:
        params.append(gate)
        where.append(
            f"EXISTS (SELECT 1 FROM jsonb_array_elements(cp.gate_ledger || COALESCE(cp.flags, '[]'::jsonb)) g "
            f"WHERE g->>'gate' = ${len(params)} AND (g->>'passed')::boolean = false)")
    if q:
        params.append(f"%{q}%")
        where.append(f"(cp.seo_title ILIKE ${len(params)} OR ago.name ILIKE ${len(params)} "
                     f"OR cp.content_text ILIKE ${len(params)})")
    return " AND ".join(where), params


@router.get("/pieces")
async def list_pieces(
    request: Request,
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    tenant_id: Optional[str] = None, channel: Optional[str] = None, status: Optional[str] = None,
    gate: Optional[str] = None, q: Optional[str] = None,
    sort: str = "created_at", sort_dir: str = "desc",
    x_admin_secret: str = Header(None),
):
    """Every tenant's T9 pieces: final status, the blocking gates that failed (from gate_ledger),
    the warn flags, attempts and the LLM cost of the write job."""
    verify_admin_secret(x_admin_secret)
    where, params = _piece_filters(tenant_id, channel, status, gate, q)
    order = _PIECE_SORTS.get(sort, "cp.created_at")
    direction = "ASC" if sort_dir == "asc" else "DESC"
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT count(*) {_PIECE_FROM} WHERE {where}", *params)
        rows = await conn.fetch(
            f"""
            SELECT cp.piece_id, cp.tenant_id, t.name AS tenant_name,
                   COALESCE(cp.channel, agr.channel) AS channel, cp.status, cp.held_reason,
                   cp.attempt_number, cp.created_at, cp.job_id,
                   COALESCE(cp.seo_title, ago.name) AS title, ago.name AS angle_name, agr.goal,
                   length(cp.content_text) AS content_chars,
                   (cp.writer_input IS NOT NULL) AS has_writer_input,
                   {_FAILED_GATES_SQL}, {_COST_SQL}
            {_PIECE_FROM}
            WHERE {where}
            ORDER BY {order} {direction} NULLS LAST, cp.piece_id
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
            """, *params, limit, offset)
    return {"data": [_row(r) for r in rows], "total": total, "limit": limit, "offset": offset}


# Highlights: where in the final text each flagged sentence / phrase sits.
_F9_PHRASE_RE = re.compile(r'exact flagged phrase\(s\): (.+)$')
_QUOTED_RE = re.compile(r'"([^"]{3,})"')
_BRAND_RE = re.compile(r"^(.+?): '(.+)'\s*$", re.DOTALL)


def highlights(content_text: str, gate_ledger: list, flags: list) -> list[dict]:
    """[{start, end, gate, blocking, problem}] for every flagged sentence or phrase found in the
    final text. Parses the same violation shapes sentence_repair.py reads; a quote that cannot
    be found (rewritten later, or too short) is left out."""
    out: list[dict] = []
    text = content_text or ""
    for g in list(gate_ledger or []) + list(flags or []):
        if not isinstance(g, dict) or g.get("passed", False):
            continue
        gate, blocking = g.get("gate"), g.get("blocking", True)
        for v in g.get("violations") or []:
            if not isinstance(v, str):
                continue
            spans: list[tuple[str, str]] = []
            if gate == "F1_grounding":
                m = sentence_repair._F1_QUOTE_RE.search(v)
                if m:
                    spans.append((m.group(1), v.split(" not present", 1)[0]))
            elif gate in ("F2_brand_forbidden", "F11_brand_name"):
                m = _BRAND_RE.search(v)
                if m:
                    spans.append((m.group(2), m.group(1)))
            elif gate == "F9_cta_fact" and "unsupported claim(s):" in v:
                for sent, claim, why in sentence_repair._F9_CLAIM_RE.findall(v.split("unsupported claim(s):", 1)[1]):
                    spans.append((sent, f"{claim.strip()} — {why.strip()}"))
            elif gate == "F9_brand_style":
                m = _F9_PHRASE_RE.search(v)
                for phrase in _QUOTED_RE.findall(m.group(1)) if m else []:
                    i = text.find(phrase)
                    if i >= 0:
                        out.append({"start": i, "end": i + len(phrase), "gate": gate, "blocking": blocking,
                                    "problem": v.split(" — ", 1)[0][:200]})
            for quote, problem in spans:
                span = sentence_repair.locate_sentence(text, quote)
                if span:
                    out.append({"start": span[0], "end": span[1], "gate": gate, "blocking": blocking,
                                "problem": problem[:300]})
    # A non-blocking gate sits in both gate_ledger and flags: keep one highlight per span + gate.
    seen, unique = set(), []
    for h in sorted(out, key=lambda h: (h["start"], h["end"])):
        if (h["start"], h["end"], h["gate"]) not in seen:
            seen.add((h["start"], h["end"], h["gate"]))
            unique.append(h)
    return unique


@router.get("/pieces/{piece_id}")
async def get_piece(piece_id: str, request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT cp.piece_id, cp.tenant_id, t.name AS tenant_name, COALESCE(cp.channel, agr.channel) AS channel,
                   cp.status, cp.held_reason, cp.attempt_number, cp.created_at, cp.job_id,
                   cp.content_text, cp.seo_title, cp.meta_description, cp.slug, cp.gate_ledger, cp.flags,
                   cp.repair_log, cp.discarded_attempts, cp.writer_input, cp.route_hub_name,
                   agr.goal, agr.atom_id, agr.request_id,
                   ago.name AS angle_name, ago.why_it_works AS angle_why, ago.best_final_style AS angle_style,
                   {_COST_SQL}
            {_PIECE_FROM}
            WHERE cp.piece_id = $1::uuid
            """, piece_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Piece not found")
        cost_rows = await conn.fetch(
            """
            SELECT stage, role, count(*) AS calls, sum(cost_usd) AS cost_usd
            FROM shared.llm_call_log WHERE job_id = $1 GROUP BY stage, role ORDER BY sum(cost_usd) DESC NULLS LAST
            """, row["job_id"]) if row["job_id"] else []
        siblings = await conn.fetch(
            """
            SELECT piece_id, status, attempt_number, created_at FROM acp_shared.content_piece
            WHERE angle_gate_request_id = $1 AND piece_id <> $2::uuid ORDER BY created_at
            """, row["request_id"], piece_id)
    piece = _row(row)
    piece["highlights"] = highlights(piece["content_text"], piece["gate_ledger"], piece["flags"])
    piece["cost_by_stage"] = [_row(r) for r in cost_rows]
    piece["other_pieces_for_request"] = [_row(r) for r in siblings]
    return piece


@router.get("/stats")
async def stats(request: Request, days: int = Query(30, ge=1, le=365), tenant_id: Optional[str] = None,
                x_admin_secret: str = Header(None)):
    """Per-gate runs / fails / blocking fails on final pieces, and per-tenant outcomes."""
    verify_admin_secret(x_admin_secret)
    params: list = [days]
    tenant_clause = ""
    if tenant_id:
        params.append(tenant_id)
        tenant_clause = "AND cp.tenant_id = $2::uuid"
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        gates = await conn.fetch(
            f"""
            SELECT g->>'gate' AS gate, COALESCE(cp.channel, 'unknown') AS channel,
                   count(*) AS runs,
                   count(*) FILTER (WHERE (g->>'passed')::boolean = false) AS failed,
                   count(*) FILTER (WHERE (g->>'passed')::boolean = false
                                      AND COALESCE((g->>'blocking')::boolean, true)) AS blocking_failed
            FROM acp_shared.content_piece cp, jsonb_array_elements(cp.gate_ledger) g
            WHERE cp.created_at > now() - make_interval(days => $1) AND cp.status <> 'processing' {tenant_clause}
            GROUP BY 1, 2 ORDER BY failed DESC, gate
            """, *params)
        tenants = await conn.fetch(
            f"""
            SELECT t.name AS tenant_name, cp.tenant_id,
                   count(*) AS pieces,
                   count(*) FILTER (WHERE cp.status = 'approved') AS approved,
                   count(*) FILTER (WHERE cp.status = 'held') AS held,
                   count(*) FILTER (WHERE cp.status = 'failed') AS failed,
                   COALESCE(sum((SELECT sum(l.cost_usd) FROM shared.llm_call_log l WHERE l.job_id = cp.job_id)), 0)
                       AS cost_usd
            FROM acp_shared.content_piece cp JOIN shared.tenants t ON t.tenant_id = cp.tenant_id
            WHERE cp.created_at > now() - make_interval(days => $1) {tenant_clause}
            GROUP BY 1, 2 ORDER BY pieces DESC
            """, *params)
    return {"days": days, "gates": [_row(r) for r in gates], "tenants": [_row(r) for r in tenants]}


# ── T2 tenant tour versions ────────────────────────────────────────────────────────────────────

@router.get("/tour-versions")
async def list_tour_versions(
    request: Request,
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    tenant_id: Optional[str] = None, status: Optional[str] = None, q: Optional[str] = None,
    sort: str = "created_at", sort_dir: str = "desc",
    x_admin_secret: str = Header(None),
):
    """Every tenant's T2 tour versions with the scores and QA state the tenant no longer sees."""
    verify_admin_secret(x_admin_secret)
    where, params = ["true"], []
    if tenant_id:
        params.append(tenant_id)
        where.append(f"ttv.tenant_id = ${len(params)}::uuid")
    if status:
        params.append(status)
        where.append(f"ttv.status = ${len(params)}")
    if q:
        params.append(f"%{q}%")
        where.append(f"pt.aa_name ILIKE ${len(params)}")
    w = " AND ".join(where)
    order = _VERSION_SORTS.get(sort, "ttv.created_at")
    direction = "ASC" if sort_dir == "asc" else "DESC"
    base = """
        FROM gold_aa_internal.tenant_tour_versions ttv
        JOIN shared.tenants t ON t.tenant_id = ttv.tenant_id
        JOIN gold_aa_internal.published_tours pt ON pt.id = ttv.published_tour_id
    """
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT count(*) {base} WHERE {w}", *params)
        rows = await conn.fetch(
            f"""
            SELECT ttv.id, ttv.tenant_id, t.name AS tenant_name, pt.aa_name AS tour_name, ttv.version_number,
                   ttv.status, ttv.quality_score, ttv.qa_status, ttv.qa_auto_passed, ttv.qa_repair_count,
                   ttv.edit_source, ttv.created_at, ttv.job_id,
                   COALESCE((SELECT sum(l.cost_usd) FROM shared.llm_call_log l WHERE l.job_id = ttv.job_id), 0)
                       AS cost_usd,
                   (SELECT count(*) FROM silver_aa_internal.review_queue rq
                    WHERE rq.tenant_tour_version_id = ttv.id) AS escalations
            {base}
            WHERE {w}
            ORDER BY {order} {direction} NULLS LAST, ttv.id
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
            """, *params, limit, offset)
    return {"data": [_row(r) for r in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/tour-versions/{version_id}")
async def get_tour_version(version_id: str, request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT ttv.id, t.name AS tenant_name, pt.aa_name AS tour_name, ttv.version_number, ttv.status,
                   ttv.quality_score, ttv.qa_status, ttv.qa_auto_passed, ttv.qa_repair_count, ttv.qa_checked_at,
                   ttv.rewritten_content, ttv.edit_source, ttv.created_at, ttv.job_id
            FROM gold_aa_internal.tenant_tour_versions ttv
            JOIN shared.tenants t ON t.tenant_id = ttv.tenant_id
            JOIN gold_aa_internal.published_tours pt ON pt.id = ttv.published_tour_id
            WHERE ttv.id = $1::uuid
            """, version_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Version not found")
        reviews = await conn.fetch(
            """
            SELECT failure_summary, escalate_detail, review_status, created_at
            FROM silver_aa_internal.review_queue WHERE tenant_tour_version_id = $1::uuid ORDER BY created_at
            """, version_id)
        cost_rows = await conn.fetch(
            """
            SELECT stage, role, count(*) AS calls, sum(cost_usd) AS cost_usd
            FROM shared.llm_call_log WHERE job_id = $1 GROUP BY stage, role ORDER BY sum(cost_usd) DESC NULLS LAST
            """, row["job_id"]) if row["job_id"] else []
    out = _row(row)
    out["rewritten_content"] = _j(out.get("rewritten_content"))
    out["reviews"] = [_row(r) for r in reviews]
    out["cost_by_stage"] = [_row(r) for r in cost_rows]
    return out


# ── Place cards (AA-772) ───────────────────────────────────────────────────────────────────────

@router.get("/place-cards")
async def list_place_cards(
    request: Request, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    status: Optional[str] = None, q: Optional[str] = None, x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    where, params = ["true"], []
    if status:
        params.append(status)
        where.append(f"pc.status = ${len(params)}")
    if q:
        params.append(f"%{q}%")
        where.append(f"(pc.display_name ILIKE ${len(params)} OR pc.locality ILIKE ${len(params)} "
                     f"OR pc.country ILIKE ${len(params)})")
    w = " AND ".join(where)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT count(*) FROM acp_shared.place_card pc WHERE {w}", *params)
        rows = await conn.fetch(
            f"""
            SELECT pc.card_id, pc.display_name, pc.locality, pc.region, pc.country, pc.naming_note, pc.facts,
                   pc.status, pc.cost_usd, pc.verified_by, pc.verified_at, pc.created_at,
                   (SELECT count(*) FROM acp_shared.atom_place_card a WHERE a.card_id = pc.card_id) AS atoms
            FROM acp_shared.place_card pc
            WHERE {w}
            ORDER BY pc.created_at DESC, pc.card_id
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
            """, *params, limit, offset)
    return {"data": [_row(r) for r in rows], "total": total, "limit": limit, "offset": offset}


class PlaceCardVerdict(BaseModel):
    status: str  # verified | rejected | auto_checked


@router.get("/place-cards/duplicates")
async def list_place_card_duplicates(request: Request, x_admin_secret: str = Header(None)):
    """S234 — candidate duplicate clusters (same country + same core name, whole-word name
    containment, or an alias), for the admin to merge by hand. Never merged automatically:
    'Punakha' and 'Punakha Dzong' are two places."""
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        cards = await conn.fetch(
            """
            SELECT pc.card_id, pc.display_name, pc.locality, pc.region, pc.country, pc.status, pc.facts,
                   pc.cost_usd, pc.created_at,
                   (SELECT count(*) FROM acp_shared.atom_place_card a WHERE a.card_id = pc.card_id) AS atoms
            FROM acp_shared.place_card pc
            ORDER BY pc.country, pc.display_name, pc.created_at
            """)
        aliases = await conn.fetch("SELECT alias_norm, country_norm, card_id FROM acp_shared.place_card_alias")
    rows = [_row(c) for c in cards]
    for r in rows:
        r["facts"] = len(r.get("facts") or [])
    clusters = place_cards.duplicate_clusters(rows, [dict(a) for a in aliases])
    return {"data": clusters, "total": len(clusters)}


@router.patch("/place-cards/{card_id}")
async def set_place_card_status(card_id: str, body: PlaceCardVerdict, request: Request,
                                x_admin_secret: str = Header(None),
                                x_admin_user_id: Optional[str] = Header(None)):
    verify_admin_secret(x_admin_secret)
    if body.status not in ("verified", "rejected", "auto_checked"):
        raise HTTPException(status_code=422, detail="status must be verified, rejected or auto_checked")
    pool = request.app.state.pool
    actor = x_admin_user_id or "admin"
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """
            UPDATE acp_shared.place_card
            SET status = $2, verified_by = $3, verified_at = CASE WHEN $2 = 'auto_checked' THEN NULL ELSE now() END,
                updated_at = now()
            WHERE card_id = $1::uuid
            RETURNING card_id, status, verified_by, verified_at, display_name, country
            """, card_id, body.status, actor)
        if row is not None:
            await write_audit_log(
                conn, tenant_id=None, actor=f"admin:{actor}", action=PlatformAuditAction.PLACE_CARD_STATUS,
                resource_type="place_card", resource_id=card_id,
                details={"status": body.status, "display_name": row["display_name"], "country": row["country"]})
    if row is None:
        raise HTTPException(status_code=404, detail="Place card not found")
    return _row(row)


class PlaceCardMerge(BaseModel):
    from_ids: list[str]


@router.post("/place-cards/{keep_id}/merge")
async def merge_place_cards(keep_id: str, body: PlaceCardMerge, request: Request,
                            x_admin_secret: str = Header(None),
                            x_admin_user_id: Optional[str] = Header(None)):
    """S234 — merge duplicate cards into `keep_id` in one transaction: atoms re-linked, names kept
    as aliases (the next atom naming them links with no LLM call), facts/cost added, merged cards
    deleted, one audit_log row."""
    verify_admin_secret(x_admin_secret)
    try:
        ids = [str(UUID(i)) for i in [keep_id, *body.from_ids]]
    except ValueError:
        raise HTTPException(status_code=422, detail="card ids must be UUIDs")
    actor = x_admin_user_id or "admin"
    pool = request.app.state.pool
    async with pool.acquire() as conn, conn.transaction():
        try:
            out = await place_cards.merge_cards(conn, ids[0], ids[1:])
        except place_cards.MergeError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except LookupError as e:
            raise HTTPException(status_code=404, detail=str(e))
        await write_audit_log(
            conn, tenant_id=None, actor=f"admin:{actor}", action=PlatformAuditAction.PLACE_CARD_MERGE,
            resource_type="place_card", resource_id=ids[0], details=out)
    return out


class PlaceCardBuild(BaseModel):
    tour_id: str
    force: bool = False  # ignore stored misses (place_card_miss) for this tour


@router.post("/place-cards/build")
async def build_place_cards(body: PlaceCardBuild, request: Request, x_admin_secret: str = Header(None),
                            x_admin_user_id: Optional[str] = Header(None)):
    """Enqueue `place_cards_build` for one tour (missing cards only; ≤40 paid drafts, one per
    distinct place). `force` retries places whose last draft missed."""
    verify_admin_secret(x_admin_secret)
    from shared.jobs import queue as job_queue
    payload = {"tour_id": body.tour_id, **({"force": True} if body.force else {})}
    job_id, created = await job_queue.enqueue(
        request.app.state.pool, "place_cards_build", payload,
        max_attempts=2,
        created_by=x_admin_user_id or "admin",
    )
    return {"job_id": job_id, "created": created}
