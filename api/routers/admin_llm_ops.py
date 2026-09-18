"""AA-518 Việc C + AA-505 — admin-only LLM ops: per-stage model config + cost/quality monitoring.

Two concerns, one router (they share the same `stage` vocabulary, see migration 137's header):
  - GET/PATCH /admin/llm-config     — Việc C, admin picks the model for each of the 16 stages.
  - GET /admin/llm-usage/tree       — AA-505, Tenant -> Model -> Stage cost+quality rollup.
  - GET /admin/llm-usage/calls      — AA-505, flat recent-calls list (reused by AA-501's own
                                        AA/A4 piece view via ?content_piece_id=, and by the tree
                                        page's own "show recent calls for this branch" drill-in).

Same admin-secret gate as every other admin-mutation router in this app (admin_a4.py's
verify_admin_secret + x-admin-user-id header convention, AA-232/AA-455) — reads are open behind
the BFF's requireAdmin() layer (frontend/app/api/admin/[...path]/route.ts), same as every other
/admin/* endpoint; only the PATCH additionally re-checks the secret server-side, matching
admin_a4.py's own force_unpublish() precedent.
"""
from __future__ import annotations

from typing import Optional

import structlog
from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel

from api.routers.admin import verify_admin_secret
from shared.llm_client.role_config import list_stage_configs, set_stage_config

logger = structlog.get_logger()

router = APIRouter(prefix="/admin", tags=["admin-llm-ops"])

# ── Static availability metadata (AA-518 STEP0, docs/implementation-notes/AA-518.md) ──────────
# Not queried live on every request — this reflects the real Bedrock/OpenAI access state
# confirmed via STEP0 (AA-518/AA-351), which changes on the order of "AWS Support unblocks
# GPT-5.6" events, not per-request. Update this table (not the DB) when that STEP0 picture
# changes — it is presentation metadata for the dropdown, not itself a source of truth for what
# a stage IS configured to (that's shared.llm_role_config).
_WRITER_OPTIONS = [
    {"model_id": "haiku", "label": "Claude Haiku 4.5", "available": True},
    {"model_id": "sonnet", "label": "Claude Sonnet 4.5", "available": True},
]
_JUDGE_OPTIONS = [
    {"model_id": "gpt-4.1", "label": "GPT-4.1 (OpenAI direct)", "available": True},
    {"model_id": "gpt-5.6", "label": "GPT-5.6 (Bedrock)", "available": False,
     "reason": "AccessDeniedException trên cả 3 account AWS — chờ gỡ chặn (AA-351)"},
    {"model_id": "nova_pro", "label": "Amazon Nova Pro (Bedrock)", "available": False,
     "reason": "Cùng vendor rủi ro self-preference bias thấp hơn GPT nhưng KHÔNG phải "
               "frontier-judge-quality — chỉ còn dùng thủ công (JUDGE_MODEL=nova_pro), "
               "không phải lựa chọn khuyến nghị qua UI (AA-518, 02/09/2026)"},
]
_ACCOUNT_ROUTE_OPTIONS = [
    {"value": "acc3", "label": "acc3 (satellite chính)"},
    {"value": "acc1", "label": "acc1 (satellite fallback)"},
]
# Permanently rejected — never shown, not even as "blocked" (different from a temporarily-
# blocked model like GPT-5.6 above; see AA-518.md round-1 wireframe's own footer distinction).
# Palmyra X5: hard 1 req/min channel-program throttle, AA-334/AA-392 permanently rejected.


def _options_for_role(role: str) -> list[dict]:
    return _JUDGE_OPTIONS if role == "judge" else _WRITER_OPTIONS


class LlmConfigPatch(BaseModel):
    model_id: str
    account_route: Optional[str] = None


@router.get("/llm-config", summary="Việc C — list all 16 per-stage LLM configs + option metadata")
async def get_llm_config():
    rows = await list_stage_configs()
    for r in rows:
        r["updated_at"] = r["updated_at"].isoformat() if r["updated_at"] else None
        r["options"] = _options_for_role(r["role"])
        r["account_route_options"] = _ACCOUNT_ROUTE_OPTIONS if r["provider"] == "claude" else []
    return {"stages": rows}


@router.patch("/llm-config/{stage}", summary="Việc C — change one stage's model (admin-only, confirm-gated on the FE)")
async def patch_llm_config(
    stage: str,
    body: LlmConfigPatch,
    x_admin_secret: str = Header(None),
    x_admin_user_id: Optional[str] = Header(None),
):
    verify_admin_secret(x_admin_secret)
    admin_actor = x_admin_user_id or "unknown"  # same fallback shape as admin_a4.py::force_unpublish
    try:
        updated = await set_stage_config(
            stage, body.model_id, body.account_route, updated_by=f"admin:{admin_actor}",
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    updated["updated_at"] = updated["updated_at"].isoformat() if updated["updated_at"] else None
    logger.info("admin_llm_config_changed", stage=stage, model_id=body.model_id,
                account_route=body.account_route, admin_actor=admin_actor)
    return updated


# ── AA-505 — Tenant -> Model -> Stage cost/quality tree ────────────────────────────────────────

_TREE_SQL = """
    SELECT
        l.tenant_id::text AS tenant_id, COALESCE(t.slug, 'aa_internal') AS tenant_label,
        l.model, l.stage, l.role,
        -- AA-617: account (acc1/acc2/acc3, NULL for OpenAI/legacy) + provider so the tree can
        -- split acc3 vs acc1 satellite spend, which l.model alone can't (model no longer carries
        -- the "satellite-" prefix). COALESCE keeps legacy NULL rows grouped under a visible label.
        COALESCE(l.account, 'unknown') AS account,
        COALESCE(l.provider, 'unknown') AS provider,
        COUNT(*) FILTER (WHERE l.fallback_used) AS fallback_count,
        COUNT(*) AS call_count,
        COALESCE(SUM(l.cost_usd), 0)::float AS total_cost_usd,
        -- "ok" = any of the boolean-shaped success keys this task's 16 stages actually log
        -- (see docs/implementation-notes/AA-518.md's per-stage quality_signal table) — a
        -- generic OR across all of them since different stages name their own signal
        -- differently (judge stages: "passed"; heuristic stages: "landed_in_clamp"/
        -- "meta_landed_in_band"/"required_markers_present"/"produced_statement"/"json_parsed";
        -- brand_audit: status='pass').
        COUNT(*) FILTER (WHERE
            (l.quality_signal->>'passed') = 'true'
            OR (l.quality_signal->>'landed_in_clamp') = 'true'
            OR (l.quality_signal->>'meta_landed_in_band') = 'true'
            OR (l.quality_signal->>'required_markers_present') = 'true'
            OR (l.quality_signal->>'produced_statement') = 'true'
            OR (l.quality_signal->>'json_parsed') = 'true'
            OR (l.quality_signal->>'output_parsed') = 'true'
            OR (l.quality_signal->>'status') = 'pass'
        ) AS ok_count,
        COUNT(*) FILTER (WHERE
            l.quality_signal ? 'passed' OR l.quality_signal ? 'landed_in_clamp'
            OR l.quality_signal ? 'meta_landed_in_band' OR l.quality_signal ? 'required_markers_present'
            OR l.quality_signal ? 'produced_statement' OR l.quality_signal ? 'json_parsed'
            OR l.quality_signal ? 'output_parsed' OR l.quality_signal ? 'status'
        ) AS ok_eligible_count,
        AVG((l.quality_signal->>'atoms_extracted')::numeric) AS avg_atoms_extracted,
        AVG((l.quality_signal->>'output_len_chars')::numeric) AS avg_output_len_chars,
        -- AA-493: how many of this branch's calls were cut off at the token limit rather than
        -- finishing normally — the whole point of persisting stop_reason. NULL stop_reason
        -- (any row written before migration 141, or a call site not yet threaded through) is
        -- correctly excluded here, not counted as truncated.
        COUNT(*) FILTER (WHERE l.stop_reason = 'max_tokens') AS truncated_count,
        MAX(l.created_at) AS last_call_at
    FROM shared.llm_call_log l
    LEFT JOIN shared.tenants t ON t.tenant_id = l.tenant_id
    WHERE l.created_at >= now() - ($1 || ' days')::interval
    GROUP BY l.tenant_id, t.slug, l.model, l.stage, l.role, l.account, l.provider
    ORDER BY tenant_label, l.account, l.model, l.stage
"""


@router.get("/llm-usage/tree", summary="AA-505 — Tenant -> Model -> Stage cost/quality rollup")
async def get_llm_usage_tree(request: Request, days: int = Query(30, ge=1, le=365)):
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(_TREE_SQL, str(days))
    branches = []
    for r in rows:
        d = dict(r)
        d["last_call_at"] = d["last_call_at"].isoformat() if d["last_call_at"] else None
        d["avg_atoms_extracted"] = float(d["avg_atoms_extracted"]) if d["avg_atoms_extracted"] is not None else None
        d["avg_output_len_chars"] = float(d["avg_output_len_chars"]) if d["avg_output_len_chars"] is not None else None
        d["ok_rate"] = (d["ok_count"] / d["ok_eligible_count"]) if d["ok_eligible_count"] else None
        branches.append(d)
    logger.info("admin_llm_usage_tree_queried", days=days, branch_count=len(branches))
    return {"days": days, "branches": branches}


@router.get("/llm-usage/calls", summary="AA-505 — flat recent-calls list, filterable; reused by AA-501/A4")
async def get_llm_usage_calls(
    request: Request,
    content_piece_id: Optional[str] = None,
    angle_gate_request_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    stage: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
):
    pool = request.app.state.pool
    # (column, sql_type, value) — stage/role columns are text, the 3 attribution columns are uuid.
    filters = [
        ("content_piece_id", "uuid", content_piece_id),
        ("angle_gate_request_id", "uuid", angle_gate_request_id),
        ("tenant_id", "uuid", tenant_id),
        ("stage", "text", stage),
    ]
    clauses, params = [], []
    for column, sql_type, value in filters:
        if value:
            params.append(value)
            clauses.append(f"{column} = ${len(params)}::{sql_type}")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    sql = f"""
        SELECT id::text, tenant_id::text, stage, role, model, account, provider, fallback_used,
               tokens_in, tokens_out,
               cost_usd::float, quality_signal, content_piece_id::text, angle_gate_request_id::text,
               stop_reason, created_at
        FROM shared.llm_call_log
        {where}
        ORDER BY created_at DESC
        LIMIT ${len(params)}
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    calls = []
    for r in rows:
        d = dict(r)
        d["created_at"] = d["created_at"].isoformat() if d["created_at"] else None
        calls.append(d)
    return {"calls": calls, "total": len(calls)}


# ── AA-618 — DataForSEO usage/cost rollup (shared.dfs_call_log) ─────────────────────────────────

_DFS_TREE_SQL = """
    SELECT
        endpoint,
        COALESCE(tenant_id::text, 'platform') AS tenant_label,
        COUNT(*)                                    AS call_count,
        COUNT(*) FILTER (WHERE fetched_live)        AS live_count,
        COUNT(*) FILTER (WHERE cache_hit)           AS cache_hit_count,
        COALESCE(SUM(cost_usd), 0)::float           AS total_cost_usd,
        COALESCE(SUM(keyword_count), 0)             AS keywords_total,
        MAX(created_at)                             AS last_call_at
    FROM shared.dfs_call_log
    WHERE created_at >= now() - ($1 || ' days')::interval
    GROUP BY endpoint, tenant_id
    ORDER BY total_cost_usd DESC NULLS LAST, endpoint
"""

_DFS_SUMMARY_SQL = """
    SELECT
        COUNT(*)                              AS total_calls,
        COUNT(*) FILTER (WHERE fetched_live)  AS live_calls,
        COUNT(*) FILTER (WHERE cache_hit)     AS cache_hits,
        COALESCE(SUM(cost_usd), 0)::float     AS total_cost_usd
    FROM shared.dfs_call_log
    WHERE created_at >= now() - ($1 || ' days')::interval
"""


@router.get("/dfs-usage", summary="AA-618 — DataForSEO cost/cache-hit rollup by endpoint+tenant")
async def get_dfs_usage(request: Request, days: int = Query(30, ge=1, le=365)):
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(_DFS_TREE_SQL, str(days))
        summary = await conn.fetchrow(_DFS_SUMMARY_SQL, str(days))
    branches = []
    for r in rows:
        d = dict(r)
        d["last_call_at"] = d["last_call_at"].isoformat() if d["last_call_at"] else None
        # real per-DFS-call cache-hit rate (distinct from the Redis-global one on the dashboard)
        total = d["call_count"] or 0
        d["cache_hit_rate"] = (d["cache_hit_count"] / total) if total else None
        branches.append(d)
    s = dict(summary) if summary else {}
    total = s.get("total_calls") or 0
    s["cache_hit_rate"] = (s.get("cache_hits", 0) / total) if total else None
    logger.info("admin_dfs_usage_queried", days=days, branch_count=len(branches))
    return {"days": days, "summary": s, "branches": branches}
