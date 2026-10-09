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

import json
import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import structlog
from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from api.routers.admin import verify_admin_secret
from shared.aws_client.cost_explorer import (
    CostExplorerUnavailable,
    fetch_all_accounts_cost,
    read_cost_range,
    record_snapshot,
)
from shared.dfs_client.balance import read_latest_balance, record_balance_snapshot
from shared.dfs_client.unmapped_market import list_unmapped_market_requests
from shared.llm_client.catalog import (
    CatalogPriceRequired,
    list_catalog_rows,
    list_models,
    set_catalog_price,
)
from shared.llm_client.role_config import (
    list_stage_configs,
    set_stage_config,
    set_stage_route,
)

# AA-627 — DFS low-balance alert threshold (USD). Env-driven to match the repo's config
# convention (ADMIN_SECRET, DATAFORSEO_*, SECRET_* are all env). Default $10 per the issue.
_DFS_BALANCE_ALERT_THRESHOLD_USD = float(os.environ.get("DFS_BALANCE_ALERT_THRESHOLD_USD", "10"))
# Platform sentinel tenant (silver_aa_internal / master content) — the low-balance notification
# is platform-wide, not tenant-scoped, so it is attributed to this sentinel (shared.notifications
# requires a NOT NULL tenant_id). Same UUID used across ingestion/canary/s1_batch.
_PLATFORM_TENANT_ID = "00000000-0000-0000-0000-000000000001"
_DFS_BALANCE_LOW_EVENT = "platform.dfs_balance.low"

logger = structlog.get_logger()

router = APIRouter(prefix="/admin", tags=["admin-llm-ops"])

# ── Static availability metadata (AA-518 STEP0, docs/implementation-notes/AA-518.md) ──────────
# Not queried live on every request — this reflects the real Bedrock/OpenAI access state
# confirmed via STEP0 (AA-518/AA-351), which changes on the order of "AWS Support unblocks
# GPT-5.6" events, not per-request. Update this table (not the DB) when that STEP0 picture
# changes — it is presentation metadata for the dropdown, not itself a source of truth for what
# a stage IS configured to (that's shared.llm_role_config).
# AA-658 / ADR 0005 — options now come from shared.llm_model_catalog. These two lists are only
# the fallback when the catalog is unreachable.
_WRITER_OPTIONS = [
    {"model_id": "haiku", "label": "Claude Haiku 4.5", "via": "Bedrock", "available": True},
    {"model_id": "sonnet", "label": "Claude Sonnet 4.5", "via": "Bedrock", "available": True},
]
_JUDGE_OPTIONS = [
    {"model_id": "gpt-4.1", "label": "GPT-4.1", "via": "OpenAI API", "available": True},
]
_ACCOUNT_ROUTE_OPTIONS = [
    {"value": "acc3", "label": "acc3 (satellite chính)"},
    {"value": "acc1", "label": "acc1 (satellite fallback)"},
]
# Permanently rejected — never shown, not even as "blocked".
# Palmyra X5: hard 1 req/min channel-program throttle, AA-334/AA-392 permanently rejected.

# Used only when the catalog is unreachable; catalog rows win (_via_map).
_LEGACY_VIA = {"gpt-4.1": "OpenAI API", "haiku": "Bedrock acc2 → acc3 → acc1",
               "sonnet": "Bedrock acc2 → acc3 → acc1"}


def model_via(m) -> str:
    """AA-714 — where a Model Key is really served, for the admin UI. GPT models run either on
    Bedrock (acc3 only: the OpenAI-on-Bedrock agreement exists on acc3, not acc1/acc2) or on the
    OpenAI platform API; the same model name can be both (gpt-6-luna vs gpt-6-luna-openai)."""
    if m.provider == "openai":
        return "OpenAI API"
    if m.model_key in ("haiku", "sonnet"):
        # Legacy chain (LLMClient._legacy_chain): acc2 native first, then the satellites.
        return "Bedrock acc2 → acc3 → acc1"
    accts = [a for a in ("acc3", "acc1", "acc2") if a in (m.bedrock_profile_ids or {})]
    if m.api_style == "embed" and not accts:
        return "Bedrock acc2"
    return "Bedrock " + " → ".join(accts) if accts else "Bedrock"


def _via_map(models: Optional[list]) -> dict[str, str]:
    out = dict(_LEGACY_VIA)
    for m in models or []:
        out[m.model_key] = model_via(m)
    return out


def _route_view(row: dict, models: Optional[list]) -> dict:
    """AA-714 — the stage route as the gateway runs it: primary, fallbacks in order, shadow."""
    labels = {m.model_key: m.label for m in models or []}
    via = _via_map(models)

    def item(key: str) -> dict:
        return {"model_id": key, "label": labels.get(key, key), "via": via.get(key)}

    chain = [row["model_id"], *(row.get("fallback_model_ids") or [])]
    shadow = row.get("shadow_model_id")
    return {
        "chain": [item(k) for k in chain],
        "shadow": ({**item(shadow), "sample_pct": row.get("shadow_sample_pct") or 0} if shadow else None),
    }


def _catalog_options(role: str, stage: str, models: list) -> list[dict]:
    """AA-659: every stage now runs through the gateway route, so availability depends only on
    the catalog row and the writer/judge vendor rule (ADR-2026-014/027): writers are Anthropic,
    judges are never Anthropic."""
    options = []
    for m in models:
        # AA-685: embedding stages only offer embedding models, and text stages never do.
        if (m.api_style == "embed") != (role == "embed"):
            continue
        reason = None
        if not m.enabled:
            reason = m.blocked_reason or "Chưa bật trong model catalog"
        elif role == "embed":
            pass
        elif "llm_client" not in m.callable_via:
            reason = "Model này không gọi được qua gateway"
        elif role == "judge" and m.vendor == "anthropic":
            reason = "Judge phải khác vendor với writer (writer là Anthropic)"
        elif role != "judge" and m.vendor != "anthropic":
            reason = "Writer và judge phải khác vendor (judge là OpenAI)"
        opt = {"model_id": m.model_key, "label": m.label, "via": model_via(m),
               "available": reason is None}
        if reason:
            opt["reason"] = reason
        options.append(opt)
    options.sort(key=lambda o: (not o["available"], o["label"]))
    return options


async def _load_catalog() -> Optional[list]:
    try:
        return await list_models()
    except Exception as e:
        logger.warning("llm_model_catalog_unavailable_for_admin", error=str(e))
        return None


def _options_for(role: str, stage: str, models: Optional[list]) -> list[dict]:
    if models is None:
        if role == "embed":
            return []
        return _JUDGE_OPTIONS if role == "judge" else _WRITER_OPTIONS
    return _catalog_options(role, stage, models)


class LlmConfigPatch(BaseModel):
    model_id: str
    account_route: Optional[str] = None


@router.get("/llm-config", summary="Việc C — list all 16 per-stage LLM configs + option metadata")
async def get_llm_config():
    rows = await list_stage_configs()
    models = await _load_catalog()
    for r in rows:
        r["updated_at"] = r["updated_at"].isoformat() if r["updated_at"] else None
        r["options"] = _options_for(r["role"], r["stage"], models)
        r["account_route_options"] = _ACCOUNT_ROUTE_OPTIONS if r["provider"] == "claude" else []
        r["route"] = _route_view(r, models)
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
    # AA-658 — only a model the stage can really execute may be saved (the UI must not claim a
    # change that never takes effect).
    current = next((r for r in await list_stage_configs() if r["stage"] == stage), None)
    if current is None:
        raise HTTPException(status_code=404, detail=f"unknown stage: {stage!r}")
    allowed = {o["model_id"] for o in _options_for(current["role"], stage, await _load_catalog())
               if o["available"]}
    if body.model_id not in allowed:
        raise HTTPException(status_code=422,
                            detail=f"model {body.model_id!r} is not selectable for stage {stage!r}")
    try:
        updated = await set_stage_config(
            stage, body.model_id, body.account_route, updated_by=f"admin:{admin_actor}",
            audit_actor=f"admin:{admin_actor}",
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    updated["updated_at"] = updated["updated_at"].isoformat() if updated["updated_at"] else None
    updated["route"] = _route_view(updated, await _load_catalog())
    logger.info("admin_llm_config_changed", stage=stage, model_id=body.model_id,
                account_route=body.account_route, admin_actor=admin_actor)
    return updated


# ── AA-686 — PATCH a stage route (fallback chain + shadow) ─────────────────────────────────────
# Separate from PATCH /llm-config/{stage} (which sets the primary model): the route is the ordered
# fallbacks + optional shadow (ADR 0006). Every model named must be a catalog model that
# _options_for() marks `available` for the stage — the same vendor + enabled rule as the model
# PATCH — so the UI can never save a route the gateway would refuse to run.


class LlmRoutePatch(BaseModel):
    fallback_model_ids: Optional[list[str]] = None
    shadow_model_id: Optional[str] = None
    shadow_sample_pct: Optional[int] = None


@router.patch("/llm-config/{stage}/route",
              summary="AA-686 — change one stage's fallback chain + shadow (admin-only)")
async def patch_llm_route(
    stage: str,
    body: LlmRoutePatch,
    x_admin_secret: str = Header(None),
    x_admin_user_id: Optional[str] = Header(None),
):
    verify_admin_secret(x_admin_secret)
    admin_actor = x_admin_user_id or "unknown"
    current = next((r for r in await list_stage_configs() if r["stage"] == stage), None)
    if current is None:
        raise HTTPException(status_code=404, detail=f"unknown stage: {stage!r}")

    primary = current["model_id"]
    # Keep whatever the current row has for any field the caller omits (partial update).
    fallbacks = body.fallback_model_ids if body.fallback_model_ids is not None \
        else list(current.get("fallback_model_ids") or [])
    # An explicit `"shadow_model_id": null` clears the shadow; omitting the field keeps it.
    shadow = body.shadow_model_id if "shadow_model_id" in body.model_fields_set \
        else current.get("shadow_model_id")
    sample_pct = body.shadow_sample_pct if body.shadow_sample_pct is not None \
        else (current.get("shadow_sample_pct") or 0)

    if not 0 <= sample_pct <= 100:
        raise HTTPException(status_code=422, detail="shadow_sample_pct must be between 0 and 100")
    if shadow is None:
        sample_pct = 0  # no shadow model → nothing to sample

    allowed = {o["model_id"] for o in _options_for(current["role"], stage, await _load_catalog())
               if o["available"]}

    # Fallback list: no duplicates, must not contain the primary, every entry available.
    if len(fallbacks) != len(set(fallbacks)):
        raise HTTPException(status_code=422, detail="fallback_model_ids has duplicates")
    if primary in fallbacks:
        raise HTTPException(status_code=422,
                            detail=f"fallback_model_ids must not contain the primary model {primary!r}")
    for m in fallbacks:
        if m not in allowed:
            raise HTTPException(status_code=422,
                                detail=f"fallback model {m!r} is not selectable for stage {stage!r}")

    # Shadow: != primary, and available for the stage.
    if shadow is not None:
        if shadow == primary:
            raise HTTPException(status_code=422,
                                detail="shadow_model_id must differ from the primary model")
        if shadow not in allowed:
            raise HTTPException(status_code=422,
                                detail=f"shadow model {shadow!r} is not selectable for stage {stage!r}")

    try:
        updated = await set_stage_route(
            stage, fallback_model_ids=fallbacks, shadow_model_id=shadow,
            shadow_sample_pct=sample_pct, updated_by=f"admin:{admin_actor}",
            audit_actor=f"admin:{admin_actor}",
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    updated["updated_at"] = updated["updated_at"].isoformat() if updated["updated_at"] else None
    updated["route"] = _route_view(updated, await _load_catalog())
    logger.info("admin_llm_route_changed", stage=stage, fallback_model_ids=fallbacks,
                shadow_model_id=shadow, shadow_sample_pct=sample_pct, admin_actor=admin_actor)
    return updated


# ── AA-686 — model catalog read + price/enabled edit ───────────────────────────────────────────


class LlmCatalogPatch(BaseModel):
    price_in_per_mtok: Optional[float] = Field(None, ge=0)
    price_out_per_mtok: Optional[float] = Field(None, ge=0)
    price_source: Optional[str] = None
    enabled: Optional[bool] = None


@router.get("/llm-catalog", summary="AA-686 — every catalog row (prices, provenance, enabled)")
async def get_llm_catalog():
    return {"models": await list_catalog_rows()}


@router.patch("/llm-catalog/{model_key}",
              summary="AA-686 — edit a catalog row's price / enabled (admin-only)")
async def patch_llm_catalog(
    model_key: str,
    body: LlmCatalogPatch,
    x_admin_secret: str = Header(None),
    x_admin_user_id: Optional[str] = Header(None),
):
    verify_admin_secret(x_admin_secret)
    admin_actor = x_admin_user_id or "unknown"
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=422, detail="no fields to update")
    try:
        updated = await set_catalog_price(
            model_key, fields=fields, updated_by=f"admin:{admin_actor}",
            audit_actor=f"admin:{admin_actor}",
        )
    except CatalogPriceRequired as e:
        raise HTTPException(status_code=422, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    logger.info("admin_llm_catalog_changed", model_key=model_key, fields=list(fields),
                admin_actor=admin_actor)
    return updated


# ── AA-686 — shadow A/B report (shared.llm_shadow_log) ─────────────────────────────────────────
# Groups shadow rows by (stage, primary_model, shadow_model) and reports, per group: n, shadow
# errors, agreement rate (same pass/fail), mean |score delta|, cost (sum + per call) for primary
# vs shadow, shadow latency p50/p95, and repeat-scoring variance (stddev of primary and of shadow
# scores among rows that share a request_sha256). Scores/pass are parsed per stage with the
# EXISTING judge parsers (parse_judge_json + the per-stage field each judge stage already reads);
# rows whose output cannot be parsed are counted as `unparsed`, never dropped silently.

_SHADOW_REPORT_SQL = """
    SELECT stage, primary_model, shadow_model, primary_output, shadow_output, shadow_error,
           primary_cost_usd, shadow_cost_usd, shadow_latency_ms, request_sha256
      FROM shared.llm_shadow_log
     WHERE created_at >= now() - ($1 || ' days')::interval
     ORDER BY stage, primary_model, shadow_model
"""


def _parse_judge_output(stage: str, raw: Optional[str]) -> Optional[dict]:
    """Return {"passed": bool, "score": Optional[float]} for one judge output, or None if the
    output cannot be parsed. Reuses the production judge parser (parse_judge_json) and reads the
    SAME fields each judge stage already reads downstream — no new ad-hoc regex (AA-686 contract):

      * s1_judge / t2_judge*       — brand-fit JSON: numeric `brand_fit_score`, pass derived from it.
      * s1_brand_audit             — {"brand_audit": {"status": ...}} (or flat `status`); pass/fail only.
      * t10_judge / n7_judge       — rubric `items[].score` (1/0) and/or `status`: score = pass fraction.
    """
    if not raw or not raw.strip():
        return None
    from services.acp_produce.judge_client import parse_judge_json
    try:
        data = parse_judge_json(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    # brand_audit nests its result under "brand_audit".
    if "brand_audit" in data and isinstance(data["brand_audit"], dict):
        data = data["brand_audit"]

    score: Optional[float] = None
    passed: Optional[bool] = None

    # Rubric judges (F8/F9, t10/n7): list of items each scored 1/0.
    items = data.get("items")
    if isinstance(items, list) and items:
        scored = [str(i.get("score")) for i in items if isinstance(i, dict)]
        if scored:
            ones = sum(1 for s in scored if s == "1")
            score = ones / len(scored)
            passed = ones == len(scored)

    # A numeric brand-fit / overall score (s1_judge, t2 judge).
    for key in ("brand_fit_score", "overall_score", "score", "judge_score"):
        v = data.get(key)
        if isinstance(v, (int, float)):
            score = float(v)
            break

    # A status string is the authoritative pass/fail when present (brand_audit, F9).
    status = data.get("status")
    if isinstance(status, str):
        passed = status.strip().lower() == "pass"
    elif passed is None and isinstance(data.get("passed"), bool):
        passed = data["passed"]

    if score is None and passed is None:
        return None
    return {"passed": passed, "score": score}


def _percentile(values: list[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile (pct in 0..100). None for an empty list."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    import math
    rank = max(0, min(len(s) - 1, math.ceil(pct / 100 * len(s)) - 1))
    return s[rank]


def _stddev(values: list[float]) -> Optional[float]:
    """Population standard deviation; None for <2 values (one value has no spread to report)."""
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    return (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5


def _summarize_shadow_group(stage: str, rows: list[dict]) -> dict:
    n = len(rows)
    shadow_error = sum(1 for r in rows if r["shadow_error"])
    primary_cost = sum(float(r["primary_cost_usd"] or 0) for r in rows)
    shadow_cost = sum(float(r["shadow_cost_usd"] or 0) for r in rows)
    latencies = [r["shadow_latency_ms"] for r in rows if r["shadow_latency_ms"] is not None]

    agree_total = 0
    agree_same = 0
    deltas: list[float] = []
    unparsed = 0
    # request_sha256 -> parsed primary / shadow scores, for repeat-scoring variance.
    by_sha_primary: dict[str, list[float]] = {}
    by_sha_shadow: dict[str, list[float]] = {}

    for r in rows:
        p = _parse_judge_output(stage, r["primary_output"])
        s = None if r["shadow_error"] else _parse_judge_output(stage, r["shadow_output"])
        if p is None or (s is None and not r["shadow_error"]):
            unparsed += 1
            continue
        if s is not None:
            if p["passed"] is not None and s["passed"] is not None:
                agree_total += 1
                if p["passed"] == s["passed"]:
                    agree_same += 1
            if p["score"] is not None and s["score"] is not None:
                deltas.append(abs(p["score"] - s["score"]))
        sha = r["request_sha256"]
        if sha:
            if p["score"] is not None:
                by_sha_primary.setdefault(sha, []).append(p["score"])
            if s is not None and s["score"] is not None:
                by_sha_shadow.setdefault(sha, []).append(s["score"])

    # Repeat-scoring variance: over shas scored more than once, the stddev of the repeated scores,
    # averaged across such shas (how much the same model disagrees with itself on the same input).
    def _repeat_variance(by_sha: dict[str, list[float]]) -> Optional[float]:
        stds = [_stddev(v) for v in by_sha.values() if len(v) > 1]
        stds = [x for x in stds if x is not None]
        return (sum(stds) / len(stds)) if stds else None

    return {
        "stage": stage,
        "primary_model": rows[0]["primary_model"],
        "shadow_model": rows[0]["shadow_model"],
        "n": n,
        "shadow_error": shadow_error,
        "unparsed": unparsed,
        "agreement_rate": (agree_same / agree_total) if agree_total else None,
        "agreement_sample": agree_total,
        "mean_abs_score_delta": (sum(deltas) / len(deltas)) if deltas else None,
        "score_delta_sample": len(deltas),
        "primary_cost_usd": primary_cost,
        "shadow_cost_usd": shadow_cost,
        "primary_cost_per_call": (primary_cost / n) if n else None,
        "shadow_cost_per_call": (shadow_cost / n) if n else None,
        "shadow_latency_p50_ms": _percentile([float(x) for x in latencies], 50),
        "shadow_latency_p95_ms": _percentile([float(x) for x in latencies], 95),
        "primary_repeat_score_stddev": _repeat_variance(by_sha_primary),
        "shadow_repeat_score_stddev": _repeat_variance(by_sha_shadow),
    }


@router.get("/llm-shadow/report",
            summary="AA-686 — shadow A/B comparison (agreement, score delta, cost, variance)")
async def get_llm_shadow_report(request: Request, days: int = Query(30, ge=1, le=365)):
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(_SHADOW_REPORT_SQL, str(days))
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        d = dict(r)
        groups.setdefault((d["stage"], d["primary_model"], d["shadow_model"]), []).append(d)
    report = [_summarize_shadow_group(stage, grp) for (stage, _p, _s), grp in groups.items()]
    report.sort(key=lambda g: (g["stage"], g["primary_model"], g["shadow_model"]))
    logger.info("admin_llm_shadow_report_queried", days=days, group_count=len(report))
    return {"days": days, "groups": report}


# ── AA-623 follow-up — one time window for every External Spend endpoint ───────────────────────
# `days` = rolling preset (7/30/90). `start`/`end` (calendar dates, UTC, end INCLUSIVE) override it
# so the page can pin an exact window, e.g. "from the day X shipped" -- and so LLM/DFS estimated
# and Cost Explorer actual (daily, UTC-bucketed) cover the same days.
_MAX_WINDOW_DAYS = 366


def _resolve_window(days: int, start: Optional[date], end: Optional[date]) -> tuple[datetime, datetime]:
    """-> (since, until), tz-aware UTC, half-open [since, until)."""
    if start is None and end is None:
        until = datetime.now(timezone.utc)
        return until - timedelta(days=days), until
    today = datetime.now(timezone.utc).date()
    end = end or today
    start = start or (end - timedelta(days=days - 1))
    if start > end:
        raise HTTPException(status_code=422, detail="start must be on or before end")
    if (end - start).days + 1 > _MAX_WINDOW_DAYS:
        raise HTTPException(status_code=422, detail=f"window longer than {_MAX_WINDOW_DAYS} days")
    since = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
    until = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    return since, until


def _window_meta(days: int, since: datetime, until: datetime) -> dict:
    return {"days": days, "since": since.isoformat(), "until": until.isoformat()}


# ── AA-505 — Tenant -> Model -> Stage cost/quality tree ────────────────────────────────────────

_TREE_SQL = """
    SELECT
        -- AA-702: internal work is logged both as NULL and as the aa_internal UUID; fold them into
        -- one NULL key so External Spend shows one "aa_internal" row, not two.
        NULLIF(l.tenant_id, '00000000-0000-0000-0000-000000000001'::uuid)::text AS tenant_id,
        COALESCE(t.slug, 'aa_internal') AS tenant_label,
        l.model, l.stage, l.role,
        -- AA-617: account (acc1/acc2/acc3, NULL for OpenAI/legacy) + provider so the tree can
        -- split acc3 vs acc1 satellite spend, which l.model alone can't (model no longer carries
        -- the "satellite-" prefix). COALESCE keeps legacy NULL rows grouped under a visible label.
        COALESCE(l.account, 'unknown') AS account,
        COALESCE(l.provider, 'unknown') AS provider,
        COUNT(*) FILTER (WHERE l.fallback_used) AS fallback_count,
        COUNT(*) AS call_count,
        COALESCE(SUM(l.cost_usd), 0)::float AS total_cost_usd,
        -- AA-622: token totals so the External Spend UI can show cost/1K-token per model
        -- (the standard unit for "is this model expensive per unit of work"). Only /calls had
        -- per-call tokens before; the tree never rolled them up.
        COALESCE(SUM(l.tokens_in), 0)  AS tokens_in_total,
        COALESCE(SUM(l.tokens_out), 0) AS tokens_out_total,
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
    LEFT JOIN shared.tenants t
           ON t.tenant_id = NULLIF(l.tenant_id, '00000000-0000-0000-0000-000000000001'::uuid)
    WHERE l.created_at >= $1 AND l.created_at < $2
    GROUP BY 1, t.slug, l.model, l.stage, l.role, l.account, l.provider
    ORDER BY tenant_label, l.account, l.model, l.stage
"""


@router.get("/llm-usage/tree", summary="AA-505 — Tenant -> Model -> Stage cost/quality rollup")
async def get_llm_usage_tree(
    request: Request, days: int = Query(30, ge=1, le=365),
    start: Optional[date] = None, end: Optional[date] = None,
):
    since, until = _resolve_window(days, start, end)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(_TREE_SQL, since, until)
    branches = []
    for r in rows:
        d = dict(r)
        d["last_call_at"] = d["last_call_at"].isoformat() if d["last_call_at"] else None
        d["avg_atoms_extracted"] = float(d["avg_atoms_extracted"]) if d["avg_atoms_extracted"] is not None else None
        d["avg_output_len_chars"] = float(d["avg_output_len_chars"]) if d["avg_output_len_chars"] is not None else None
        d["ok_rate"] = (d["ok_count"] / d["ok_eligible_count"]) if d["ok_eligible_count"] else None
        branches.append(d)
    logger.info("admin_llm_usage_tree_queried", days=days, since=since.isoformat(), branch_count=len(branches))
    return {**_window_meta(days, since, until), "branches": branches}


@router.get(
    "/llm-usage/calls",
    summary="AA-505 flat recent-calls list, filterable; reused by AA-501/A4 + AA-622 fallback drill-down",
)
async def get_llm_usage_calls(
    request: Request,
    content_piece_id: Optional[str] = None,
    angle_gate_request_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    stage: Optional[str] = None,
    account: Optional[str] = None,
    fallback_used: Optional[bool] = None,   # AA-622: drill into which calls fell back acc3->acc1/GPT
    days: Optional[int] = Query(None, ge=1, le=365),
    limit: int = Query(50, ge=1, le=500),
    start: Optional[date] = None,
    end: Optional[date] = None,
):
    pool = request.app.state.pool
    # (column, sql_type, value) — stage/role/account columns are text, attribution columns are uuid.
    filters = [
        ("content_piece_id", "uuid", content_piece_id),
        ("angle_gate_request_id", "uuid", angle_gate_request_id),
        ("tenant_id", "uuid", tenant_id),
        ("stage", "text", stage),
        ("account", "text", account),
    ]
    clauses, params = [], []
    for column, sql_type, value in filters:
        if value:
            params.append(value)
            clauses.append(f"{column} = ${len(params)}::{sql_type}")
    # AA-622: fallback_used is a real boolean — filter on it explicitly (None = no filter, not "false")
    if fallback_used is not None:
        params.append(fallback_used)
        clauses.append(f"fallback_used = ${len(params)}::bool")
    if start is not None or end is not None:
        since, until = _resolve_window(days or 30, start, end)
        params.extend([since, until])
        clauses.append(f"created_at >= ${len(params) - 1} AND created_at < ${len(params)}")
    elif days is not None:
        params.append(str(days))
        clauses.append(f"created_at >= now() - (${len(params)} || ' days')::interval")
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
        d.endpoint,
        -- AA-702: same NULL/aa_internal fold as the LLM tree, and a slug label instead of a raw
        -- UUID, so the same tenant carries the same key on every External Spend tab.
        NULLIF(d.tenant_id, '00000000-0000-0000-0000-000000000001'::uuid)::text AS tenant_id,
        COALESCE(t.slug, 'aa_internal')                                         AS tenant_label,
        COUNT(*)                                    AS call_count,
        COUNT(*) FILTER (WHERE d.fetched_live)      AS live_count,
        COUNT(*) FILTER (WHERE d.cache_hit)         AS cache_hit_count,
        COALESCE(SUM(d.cost_usd), 0)::float         AS total_cost_usd,
        COALESCE(SUM(d.keyword_count), 0)           AS keywords_total,
        MAX(d.created_at)                           AS last_call_at
    FROM shared.dfs_call_log d
    LEFT JOIN shared.tenants t
           ON t.tenant_id = NULLIF(d.tenant_id, '00000000-0000-0000-0000-000000000001'::uuid)
    WHERE d.created_at >= $1 AND d.created_at < $2
    GROUP BY d.endpoint, 2, t.slug
    ORDER BY total_cost_usd DESC NULLS LAST, d.endpoint
"""

_DFS_SUMMARY_SQL = """
    SELECT
        COUNT(*)                              AS total_calls,
        COUNT(*) FILTER (WHERE fetched_live)  AS live_calls,
        COUNT(*) FILTER (WHERE cache_hit)     AS cache_hits,
        COALESCE(SUM(cost_usd), 0)::float     AS total_cost_usd
    FROM shared.dfs_call_log
    WHERE created_at >= $1 AND created_at < $2
"""


@router.get("/dfs-usage", summary="AA-618 — DataForSEO cost/cache-hit rollup by endpoint+tenant")
async def get_dfs_usage(
    request: Request, days: int = Query(30, ge=1, le=365),
    start: Optional[date] = None, end: Optional[date] = None,
):
    since, until = _resolve_window(days, start, end)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(_DFS_TREE_SQL, since, until)
        summary = await conn.fetchrow(_DFS_SUMMARY_SQL, since, until)
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
    return {**_window_meta(days, since, until), "summary": s, "branches": branches}


# ── AA-622 — DFS spend by DataForSEO location_code -> country name ──────────────────────────────
# dfs_call_log stores location_code (INTEGER, DFS's buyer-market code) not a country string, so
# the External Spend UI maps the codes we actually target here. Unknown/NULL codes fall through to
# a "—" label rather than dropping the row (a real spend row must always be counted somewhere).
# Source: DataForSEO location list — only the markets AA/tenants target are listed; extend as new
# markets are added (a code with no entry shows as its raw number, still counted).
_DFS_LOCATION_NAMES: dict[int, str] = {
    2840: "United States", 2826: "United Kingdom", 2036: "Australia", 2124: "Canada",
    2356: "India", 2276: "Germany", 2250: "France", 2392: "Japan", 2410: "South Korea",
    2702: "Singapore", 2458: "Malaysia", 2764: "Thailand", 2704: "Vietnam", 2360: "Indonesia",
    2554: "New Zealand", 2380: "Italy", 2724: "Spain", 2528: "Netherlands", 2756: "Switzerland",
    2784: "United Arab Emirates",
}

_DFS_COUNTRY_SQL = """
    SELECT
        location_code,
        COUNT(*)                                    AS call_count,
        COUNT(*) FILTER (WHERE fetched_live)        AS live_count,
        COUNT(*) FILTER (WHERE cache_hit)           AS cache_hit_count,
        COALESCE(SUM(cost_usd), 0)::float           AS total_cost_usd,
        COALESCE(SUM(keyword_count), 0)             AS keywords_total
    FROM shared.dfs_call_log
    WHERE created_at >= $1 AND created_at < $2
    GROUP BY location_code
    ORDER BY total_cost_usd DESC NULLS LAST
"""


@router.get("/dfs-usage/by-country", summary="AA-622 — DataForSEO spend grouped by target market (location_code)")
async def get_dfs_usage_by_country(
    request: Request, days: int = Query(30, ge=1, le=365),
    start: Optional[date] = None, end: Optional[date] = None,
):
    since, until = _resolve_window(days, start, end)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(_DFS_COUNTRY_SQL, since, until)
    out = []
    for r in rows:
        d = dict(r)
        code = d.get("location_code")
        d["country"] = _DFS_LOCATION_NAMES.get(code) or (str(code) if code is not None else "—")
        total = d["call_count"] or 0
        d["cache_hit_rate"] = (d["cache_hit_count"] / total) if total else None
        out.append(d)
    logger.info("admin_dfs_by_country_queried", days=days, row_count=len(out))
    return {**_window_meta(days, since, until), "countries": out}


# ── AA-622 — daily spend time-series (LLM + DFS) for the External Spend trend chart ─────────────
# The tree/dfs-usage endpoints GROUP BY dimension, not day, so they can't drive a trend line.
# One row per (day, source) where source is 'llm' or 'dfs' — the FE pivots this into a stacked
# area chart. gap-fill of zero-days is done client-side (simpler than a generate_series join here).
_DAILY_SPEND_SQL = """
    SELECT day::date AS day, source, cost_usd, call_count FROM (
        SELECT date_trunc('day', created_at) AS day, 'llm' AS source,
               COALESCE(SUM(cost_usd), 0)::float AS cost_usd, COUNT(*) AS call_count
          FROM shared.llm_call_log
         WHERE created_at >= $1 AND created_at < $2
         GROUP BY 1
        UNION ALL
        SELECT date_trunc('day', created_at) AS day, 'dfs' AS source,
               COALESCE(SUM(cost_usd), 0)::float AS cost_usd, COUNT(*) AS call_count
          FROM shared.dfs_call_log
         WHERE created_at >= $1 AND created_at < $2
         GROUP BY 1
    ) u
    ORDER BY day, source
"""


@router.get("/spend/daily", summary="AA-622 — daily LLM + DFS spend time-series for the trend chart")
async def get_spend_daily(
    request: Request, days: int = Query(30, ge=1, le=365),
    start: Optional[date] = None, end: Optional[date] = None,
):
    since, until = _resolve_window(days, start, end)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(_DAILY_SPEND_SQL, since, until)
    points = []
    for r in rows:
        d = dict(r)
        d["day"] = d["day"].isoformat() if d["day"] else None
        points.append(d)
    logger.info("admin_spend_daily_queried", days=days, point_count=len(points))
    return {**_window_meta(days, since, until), "points": points}


# ── AA-627 — DataForSEO account balance: daily check + low-balance alert ────────────────────────
# Two concerns, one pair of endpoints:
#   POST /admin/dfs-balance/check — the once-a-day job. Reads the FREE DFS balance, stores a
#       snapshot, and (if below threshold) raises a low-balance notification, throttled to at
#       most one unread alert per 24h so a persistently-low balance does not spam the bell.
#       Secret-gated because the EventBridge->Lambda scheduler supplies X-Admin-Secret (mirrors
#       the s4_trigger Lambda's INTERNAL_API_KEY convention).
#   GET  /admin/dfs-balance — the External Spend page reads the LATEST stored snapshot (never
#       calls DFS live per request, per the issue). Returns balance + threshold + low flag.

# One unread low-balance notification per 24h — a persistently-low balance keeps producing a
# snapshot every day, but we only surface a fresh alert if there isn't already an unread one.
_RECENT_LOW_ALERT_SQL = """
    SELECT 1 FROM shared.notifications
     WHERE event_type = $1
       AND is_read = FALSE
       AND created_at >= now() - interval '24 hours'
     LIMIT 1
"""
_INSERT_LOW_ALERT_SQL = """
    INSERT INTO shared.notifications
        (tenant_id, actor_type, event_type, entity_type, entity_id, payload, target_roles)
    VALUES ($1::uuid, 'system', $2, 'dfs_account', 'dataforseo', $3::jsonb, ARRAY['admin','content'])
"""


async def _maybe_alert_low_balance(conn, balance_usd: float, threshold: float) -> bool:
    """Insert a low-balance notification unless one is already unread within 24h. Returns True
    if a new alert was inserted. Best-effort: a notification failure must not fail the check
    (the snapshot + returned low flag are the source of truth; the bell is a bonus channel)."""
    try:
        existing = await conn.fetchval(_RECENT_LOW_ALERT_SQL, _DFS_BALANCE_LOW_EVENT)
        if existing:
            return False
        payload = json.dumps({
            "message": f"DataForSEO balance ${balance_usd:.2f} is below the ${threshold:.2f} alert threshold. "
                       f"Top up the account (thu@adventure.asia) to avoid SEO fetch failures (HTTP 402).",
            "balance_usd": balance_usd,
            "threshold_usd": threshold,
        })
        await conn.execute(_INSERT_LOW_ALERT_SQL, _PLATFORM_TENANT_ID, _DFS_BALANCE_LOW_EVENT, payload)
        return True
    except Exception as e:
        logger.warning("dfs_balance_alert_insert_failed", error=str(e))
        return False


@router.post("/dfs-balance/check", summary="AA-627 — daily DataForSEO balance read + low-balance alert")
async def check_dfs_balance(request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    from services.seo_intelligence.dataforseo_client import DataForSEOClient

    threshold = _DFS_BALANCE_ALERT_THRESHOLD_USD
    try:
        money = await DataForSEOClient().fetch_balance()
    except Exception as e:
        logger.error("dfs_balance_check_fetch_failed", error=str(e))
        raise HTTPException(status_code=502, detail=f"DataForSEO balance read failed: {e}")

    balance = money.get("balance")
    if balance is None:
        raise HTTPException(status_code=502, detail="DataForSEO balance read returned no balance")
    balance = float(balance)
    currency = money.get("currency")
    below = balance < threshold

    pool = request.app.state.pool
    snapshot = await record_balance_snapshot(
        pool, balance_usd=balance, currency=currency,
        below_threshold=below, threshold_usd=threshold, raw=money,
    )
    alerted = False
    if below:
        async with pool.acquire() as conn:
            alerted = await _maybe_alert_low_balance(conn, balance, threshold)

    logger.info("admin_dfs_balance_checked", balance=balance, threshold=threshold,
                below=below, alerted=alerted)
    return {
        "balance_usd": balance,
        "currency": currency,
        "threshold_usd": threshold,
        "below_threshold": below,
        "alerted": alerted,
        "fetched_at": snapshot.get("fetched_at"),
    }


@router.get("/dfs-balance", summary="AA-627 — latest stored DataForSEO balance (no live DFS call)")
async def get_dfs_balance(request: Request):
    pool = request.app.state.pool
    latest = await read_latest_balance(pool)
    threshold = _DFS_BALANCE_ALERT_THRESHOLD_USD
    if not latest:
        return {"balance_usd": None, "currency": None, "threshold_usd": threshold,
                "below_threshold": False, "fetched_at": None, "has_data": False}
    balance = latest.get("balance_usd")
    # Recompute the low flag against the CURRENT threshold (may differ from when it was stored).
    below = balance is not None and float(balance) < threshold
    return {
        "balance_usd": balance,
        "currency": latest.get("currency"),
        "threshold_usd": threshold,
        "below_threshold": below,
        "fetched_at": latest.get("fetched_at"),
        "has_data": True,
    }


# ── AA-623 — AWS Cost Explorer actual spend: daily check + latest-snapshot read ────────────────
# Same job-then-read split as the DFS balance pair above (AA-627), for the same reason: Cost
# Explorer's own pricing API costs ~$0.01/request, so the External Spend page must never call it
# live per page view.
#   POST /admin/cost-explorer/check — fetches acc1/acc2/acc3's own CE data for a window
#       (acc2 directly, acc1/acc3 via STS AssumeRole satellite roles -- see
#       shared/aws_client/cost_explorer.py's module docstring for why there is no single
#       consolidated call) and stores one row per (account, service, day). Secret-gated.
#       NOTE: no scheduler calls this yet (unlike /admin/dfs-balance/check) -- today it only runs
#       from the page's "Refresh from AWS" button. 3 CE requests (~$0.03) per call.
#   GET  /admin/cost-explorer — reads stored days in the same window as the rest of the page
#       (never calls CE live per request), Bedrock vs infra split per account.


def _ce_days(since: datetime, until: datetime) -> tuple[date, date]:
    """Window -> CE's day-granular, End-exclusive [start, end). A rolling window ending mid-day
    (now) rounds out to include today's partial day."""
    end_d = until.date() if until.time() == datetime.min.time() else until.date() + timedelta(days=1)
    return since.date(), end_d


@router.post("/cost-explorer/check", summary="AA-623 — fetch AWS Cost Explorer actual spend for a window")
async def check_cost_explorer(
    request: Request, x_admin_secret: str = Header(None),
    days: int = Query(7, ge=1, le=365), start: Optional[date] = None, end: Optional[date] = None,
):
    verify_admin_secret(x_admin_secret)

    start_d, end_d = _ce_days(*_resolve_window(days, start, end))
    try:
        result = fetch_all_accounts_cost(start_d.isoformat(), end_d.isoformat())
    except CostExplorerUnavailable as e:
        logger.error("cost_explorer_check_fetch_failed", error=str(e))
        raise HTTPException(status_code=502, detail=f"Cost Explorer read failed: {e}")

    pool = request.app.state.pool
    written = await record_snapshot(pool, result["rows"])

    logger.info("admin_cost_explorer_checked", row_count=written, errors=result["errors"])
    return {
        "row_count": written,
        "errors": result["errors"],
        "period_start": start_d.isoformat(),
        "period_end_exclusive": end_d.isoformat(),
    }


@router.get("/cost-explorer", summary="AA-623 — stored AWS Cost Explorer spend for a window (no live CE call)")
async def get_cost_explorer(
    request: Request, days: int = Query(7, ge=1, le=365),
    start: Optional[date] = None, end: Optional[date] = None,
):
    start_d, end_d = _ce_days(*_resolve_window(days, start, end))
    pool = request.app.state.pool
    data = await read_cost_range(pool, start_d, end_d)
    window = {"period_start": start_d.isoformat(), "period_end_exclusive": end_d.isoformat()}
    if not data:
        return {**window, "accounts": [], "services": [], "total_usd": None, "bedrock_usd": None,
                "fetched_at": None, "has_data": False}
    return {**window, **data, "has_data": True}


@router.get(
    "/unmapped-market-requests",
    summary="AA-629 Tier 2 — tenants waiting on a market outside DFS_LOCATION_MAP, grouped by country",
)
async def get_unmapped_market_requests(request: Request):
    """Read-only admin visibility (no admin-secret gate, same convention as GET /dfs-balance
    above — sits behind the FE's own requireAdmin() BFF layer). Empty list is the healthy/
    expected steady state (no tenant is currently waiting on an unsupported market). Tier 3
    (actually adding a market to DFS_LOCATION_MAP) stays a manual admin/code change — this
    endpoint only surfaces the queue, it does not act on it."""
    pool = request.app.state.pool
    requests = await list_unmapped_market_requests(pool)
    return {"requests": requests, "total_countries_waiting": len(requests)}


# ── AA-720 — Jev credit monitoring: daily canary + 402 sweep ───────────────────────────────────
# TypeSafe has no balance API (its OpenAPI exposes only the decide endpoint + a models list), so
# credit exhaustion only shows as a 402 billing_error on an actual call. decide() fails open, so a
# silent outage would otherwise go unnoticed (S209 China rerun). These two secret-gated endpoints
# are driven once a day by the same EventBridge->Lambda that runs /dfs-balance/check (AA-627); the
# Lambda just calls these extra paths with X-Admin-Secret. No scheduler lives in the app. The
# canary itself calls TypeSafe only through decide._call_jev (never a raw endpoint here), so the
# "only the gateway talks to TypeSafe" guard (test_aa660) still holds.
#   POST /admin/jev-canary/check — one tiny real decide-endpoint probe. On 402 the decide() breaker
#       trips and a throttled bell is raised even on a day with zero pipeline traffic. On success
#       the breaker is reset (credit is back).
#   POST /admin/jev/402-sweep    — counts decision_log rows that failed on a 402 in the last 24h
#       (catches a daytime outage that happened between canary runs) and raises the same throttled
#       bell if any are found.
_JEV_402_SWEEP_SQL = """
    SELECT count(*) AS n, max(created_at) AS last_seen
      FROM shared.decision_log
     WHERE zone = 'error'
       AND error LIKE '%402%'
       AND created_at >= now() - interval '24 hours'
"""


@router.post("/jev-canary/check", summary="AA-720 — daily Jev canary: one real decide-endpoint probe")
async def check_jev_canary(request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    from shared.llm_client import decide as decide_mod

    pool = request.app.state.pool
    # A minimal, cheap probe. We do not care about the verdict content — only whether the call is
    # billable (credit present) or raises JevBillingError (402).
    probe_questions = {"canary": {"type": "noul", "instructions": "Reply true.",
                                  "criteria": {"true_when": "always"}}}
    ok, billing_error, detail = True, False, None
    started = datetime.now(timezone.utc)
    try:
        await decide_mod._call_jev("Jev credit canary probe (AA-720).", probe_questions)
        decide_mod.reset_jev_breaker()        # a billable call proves credit is back
    except decide_mod.JevBillingError:
        ok, billing_error = False, True
        decide_mod._trip_jev_breaker()
    except Exception as exc:
        # Any other error (schema/timeout) still means the call was BILLABLE (it passed billing),
        # so credit is present — the canary only fails on billing_error.
        detail = str(exc)[:200]
    alerted = False
    if billing_error:
        async with pool.acquire() as conn:
            alerted = await decide_mod.maybe_alert_jev_credit(conn, source="canary")
    logger.info("admin_jev_canary_checked", credit_ok=not billing_error, alerted=alerted,
                breaker_open=decide_mod.jev_breaker_open())
    return {
        "credit_ok": not billing_error,
        "billing_error": billing_error,
        "alerted": alerted,
        "breaker_open": decide_mod.jev_breaker_open(),
        "detail": detail,
        "checked_at": started.isoformat(),
    }


@router.post("/jev/402-sweep", summary="AA-720 — daily sweep of decision_log for 402 billing errors")
async def sweep_jev_402(request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    from shared.llm_client import decide as decide_mod

    pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_JEV_402_SWEEP_SQL)
        n = int(row["n"]) if row and row["n"] is not None else 0
        last_seen = row["last_seen"] if row else None
        alerted = await decide_mod.maybe_alert_jev_credit(conn, source="402_sweep") if n > 0 else False
    logger.info("admin_jev_402_sweep", errors_24h=n, alerted=alerted)
    return {
        "errors_24h": n,
        "last_seen": last_seen.isoformat() if last_seen else None,
        "alerted": alerted,
    }
