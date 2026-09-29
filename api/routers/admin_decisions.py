"""AA-660 — admin view of the Jev (TypeSafe) decision layer: which stage asks which question, in what
mode, with what floors, and what it costs — the Jev counterpart of the per-stage LLM model page.

  GET  /admin/decisions/summary?days=7     per question: stage, mode, floors, calibration, and over
                                           the window: calls, zone counts, errors, latency, cost, last use
  GET  /admin/decisions/log                recent verdicts, filter by stage / question / zone / subject
  PUT  /admin/decisions/questions/{key}    mode / floors / calibration_ref (enforce needs a calibration)

Cost also flows into External Spend like any model: every Jev call writes one shared.llm_call_log
row (provider 'typesafe', role 'validate'), see shared/llm_client/decide.py.
"""
from __future__ import annotations

import json
from typing import Literal, Optional

import structlog
from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from api.routers.admin import verify_admin_secret

logger = structlog.get_logger()

router = APIRouter(prefix="/admin/decisions", tags=["admin-decisions"])

_ZONES = ("accept", "grey", "reject", "error", "skipped")

_SUMMARY_SQL = """
    SELECT q.question_key, q.stage, q.kind, q.instructions, q.criteria, q.mode,
           q.accept_floor::float AS accept_floor, q.reject_ceiling::float AS reject_ceiling,
           q.threshold_version, q.calibration_ref, q.notes, q.updated_at, q.updated_by,
           count(l.id)::int AS verdicts,
           count(l.id) FILTER (WHERE l.zone = 'accept')::int  AS accept,
           count(l.id) FILTER (WHERE l.zone = 'grey')::int    AS grey,
           count(l.id) FILTER (WHERE l.zone = 'reject')::int  AS reject,
           count(l.id) FILTER (WHERE l.zone = 'error')::int   AS error,
           count(l.id) FILTER (WHERE l.zone = 'skipped')::int AS skipped,
           count(l.id) FILTER (WHERE l.zone IN ('accept', 'reject') AND l.mode = 'enforce')::int AS acted,
           coalesce(sum(l.cost_usd), 0)::float AS cost_usd,
           avg(l.latency_ms) FILTER (WHERE l.zone NOT IN ('skipped', 'error'))::float AS avg_latency_ms,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY l.latency_ms)
               FILTER (WHERE l.zone NOT IN ('skipped', 'error'))::float AS p50_latency_ms,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY l.latency_ms)
               FILTER (WHERE l.zone NOT IN ('skipped', 'error'))::float AS p95_latency_ms,
           avg(l.probability) FILTER (WHERE l.probability IS NOT NULL)::float AS avg_probability,
           min(l.created_at) AS first_used_at,
           max(l.created_at) AS last_used_at
    FROM shared.decision_question q
    LEFT JOIN shared.decision_log l
           ON l.question_key = q.question_key AND l.created_at >= now() - make_interval(days => $1)
    GROUP BY q.question_key
    ORDER BY q.stage, q.question_key
"""

# Stages that asked Jev without a question row (ad-hoc scripts, removed questions) still cost money.
_ORPHAN_SQL = """
    SELECT l.stage, l.question_key, count(*)::int AS verdicts, coalesce(sum(l.cost_usd), 0)::float AS cost_usd,
           max(l.created_at) AS last_used_at
    FROM shared.decision_log l
    LEFT JOIN shared.decision_question q ON q.question_key = l.question_key AND q.stage = l.stage
    WHERE l.created_at >= now() - make_interval(days => $1) AND q.question_key IS NULL
    GROUP BY l.stage, l.question_key
    ORDER BY cost_usd DESC
"""

# One row per day: verdict counts per zone, and cost (for the trend chart).
_DAILY_SQL = """
    SELECT date_trunc('day', created_at)::date AS day, count(*)::int AS verdicts,
           count(*) FILTER (WHERE zone = 'accept')::int AS accept,
           count(*) FILTER (WHERE zone = 'grey')::int AS grey,
           count(*) FILTER (WHERE zone = 'reject')::int AS reject,
           count(*) FILTER (WHERE zone = 'error')::int AS error,
           count(*) FILTER (WHERE zone = 'skipped')::int AS skipped,
           coalesce(sum(cost_usd), 0)::float AS cost_usd
    FROM shared.decision_log
    WHERE created_at >= now() - make_interval(days => $1)
    GROUP BY 1 ORDER BY 1
"""

# Per stage, from the ledger (what was asked and how it fell), joined to the billed calls below.
_STAGE_SQL = """
    SELECT stage, count(DISTINCT question_key)::int AS questions, count(*)::int AS verdicts,
           count(*) FILTER (WHERE zone IN ('accept', 'reject') AND mode = 'enforce')::int AS acted,
           count(*) FILTER (WHERE zone = 'error')::int AS errors,
           count(*) FILTER (WHERE zone = 'skipped')::int AS skipped,
           coalesce(sum(cost_usd), 0)::float AS cost_usd,
           avg(latency_ms) FILTER (WHERE zone NOT IN ('skipped', 'error'))::float AS avg_latency_ms,
           max(created_at) AS last_used_at
    FROM shared.decision_log
    WHERE created_at >= now() - make_interval(days => $1)
    GROUP BY stage ORDER BY stage
"""

# The exact billed amount per stage (one llm_call_log row per Jev call).
_CALLS_SQL = """
    SELECT stage, count(*)::int AS calls, coalesce(sum(cost_usd), 0)::float AS cost_usd,
           coalesce(sum(tokens_in), 0)::bigint AS tokens_in
    FROM shared.llm_call_log
    WHERE provider = 'typesafe' AND created_at >= now() - make_interval(days => $1)
    GROUP BY stage ORDER BY stage
"""


def _iso(row: dict, *keys: str) -> dict:
    for k in keys:
        if row.get(k) is not None:
            row[k] = row[k].isoformat()
    return row


@router.get("/summary", summary="AA-660 — Jev questions per stage: mode, floors, verdicts, cost")
async def summary(request: Request, days: int = Query(7, ge=1, le=90), x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    questions = []
    for r in await pool.fetch(_SUMMARY_SQL, days):
        d = _iso(dict(r), "updated_at", "last_used_at", "first_used_at")
        if isinstance(d.get("criteria"), str):
            d["criteria"] = json.loads(d["criteria"])
        questions.append(d)
    orphans = [_iso(dict(r), "last_used_at") for r in await pool.fetch(_ORPHAN_SQL, days)]
    calls = [dict(r) for r in await pool.fetch(_CALLS_SQL, days)]
    daily = [{**dict(r), "day": r["day"].isoformat()} for r in await pool.fetch(_DAILY_SQL, days)]
    stages = [_iso(dict(r), "last_used_at") for r in await pool.fetch(_STAGE_SQL, days)]
    allowlist = [dict(r) for r in await pool.fetch(
        "SELECT a.tenant_id::text, t.slug, t.name, a.reason FROM shared.jev_tenant_allowlist a "
        "JOIN shared.tenants t USING (tenant_id) ORDER BY t.slug")]
    return {
        "days": days,
        "questions": questions,
        "unregistered": orphans,
        "calls_by_stage": calls,
        "daily": daily,
        "stages": stages,
        "total_cost_usd": sum(c["cost_usd"] for c in calls),
        "total_calls": sum(c["calls"] for c in calls),
        "tenant_allowlist": allowlist,
    }


_SORTS = {"created_at": "l.created_at", "probability": "l.probability", "latency_ms": "l.latency_ms",
          "cost_usd": "l.cost_usd"}
_LOG_WHERE = """
        WHERE ($1::text IS NULL OR l.stage = $1)
          AND ($2::text IS NULL OR l.question_key = $2)
          AND ($3::text IS NULL OR l.zone = $3)
          AND ($4::text IS NULL OR l.subject_key ILIKE '%' || $4 || '%')
          AND ($5::text IS NULL OR l.mode = $5)
          AND ($6::text IS NULL OR coalesce(t.slug, 'platform') = $6)
          AND l.created_at >= now() - make_interval(days => $7)
"""


@router.get("/log", summary="AA-660 — Jev verdicts: filter, sort, page")
async def log(request: Request, stage: Optional[str] = None, question_key: Optional[str] = None,
              zone: Optional[str] = Query(None, description="|".join(_ZONES)),
              subject: Optional[str] = Query(None, description="substring of subject_key"),
              mode: Optional[Literal["off", "shadow", "enforce"]] = None,
              tenant: Optional[str] = Query(None, description="tenant slug, or 'platform'"),
              days: int = Query(30, ge=1, le=365),
              sort: Literal["created_at", "probability", "latency_ms", "cost_usd"] = "created_at",
              direction: Literal["asc", "desc"] = "desc",
              limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
              x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    if zone is not None and zone not in _ZONES:
        raise HTTPException(status_code=422, detail=f"zone must be one of {_ZONES}")
    args = (stage, question_key, zone, subject, mode, tenant, days)
    pool = request.app.state.pool
    total = await pool.fetchval(
        "SELECT count(*) FROM shared.decision_log l LEFT JOIN shared.tenants t ON t.tenant_id = l.tenant_id"
        + _LOG_WHERE, *args)
    order = f"{_SORTS[sort]} {'ASC' if direction == 'asc' else 'DESC'} NULLS LAST, l.id DESC"
    rows = await pool.fetch(
        """
        SELECT l.id, l.created_at, l.stage, l.question_key, l.subject_key, l.tenant_id::text AS tenant_id,
               t.slug AS tenant_slug, l.job_id::text AS job_id, l.mode, l.zone,
               l.probability::float AS probability, l.choice, l.probabilities, l.threshold_version,
               l.model, l.latency_ms, l.cost_usd::float AS cost_usd, l.error, l.outcome
        FROM shared.decision_log l
        LEFT JOIN shared.tenants t ON t.tenant_id = l.tenant_id
        """ + _LOG_WHERE + f" ORDER BY {order} LIMIT $8 OFFSET $9",
        *args, limit, offset,
    )
    out = []
    for r in rows:
        d = _iso(dict(r), "created_at")
        if isinstance(d.get("probabilities"), str):
            d["probabilities"] = json.loads(d["probabilities"])
        out.append(d)
    return {"decisions": out, "total": total, "limit": limit, "offset": offset}


class QuestionUpdate(BaseModel):
    mode: Literal["off", "shadow", "enforce"]
    accept_floor: Optional[float] = Field(None, ge=0, le=1)
    reject_ceiling: Optional[float] = Field(None, ge=0, le=1)
    calibration_ref: Optional[str] = Field(None, max_length=300)
    notes: Optional[str] = Field(None, max_length=2000)


def validate_update(body: QuestionUpdate) -> None:
    """Same rules as the DB CHECKs, as a readable 422 instead of a constraint error."""
    if body.accept_floor is not None and body.reject_ceiling is not None \
            and body.reject_ceiling >= body.accept_floor:
        raise HTTPException(status_code=422, detail="reject_ceiling must be below accept_floor")
    if body.mode == "enforce":
        if not body.calibration_ref:
            raise HTTPException(status_code=422,
                                detail="enforce needs a calibration record (calibration_ref, AA-661)")
        if body.accept_floor is None and body.reject_ceiling is None:
            raise HTTPException(status_code=422, detail="enforce needs at least one floor")


@router.put("/questions/{question_key}", summary="AA-660 — set a question's mode / floors")
async def update_question(question_key: str, body: QuestionUpdate, request: Request,
                          x_admin_secret: str = Header(None), x_admin_user_id: Optional[str] = Header(None)):
    verify_admin_secret(x_admin_secret)
    validate_update(body)
    row = await request.app.state.pool.fetchrow(
        """
        UPDATE shared.decision_question SET
            mode = $2, accept_floor = $3, reject_ceiling = $4, calibration_ref = $5,
            notes = coalesce($6, notes),
            threshold_version = threshold_version
                + CASE WHEN accept_floor IS DISTINCT FROM $3 OR reject_ceiling IS DISTINCT FROM $4 THEN 1 ELSE 0 END,
            updated_at = now(), updated_by = $7
        WHERE question_key = $1
        RETURNING question_key, mode, accept_floor::float AS accept_floor,
                  reject_ceiling::float AS reject_ceiling, threshold_version, calibration_ref
        """,
        question_key, body.mode, body.accept_floor, body.reject_ceiling, body.calibration_ref, body.notes,
        f"admin:{x_admin_user_id or 'unknown'}",
    )
    if row is None:
        raise HTTPException(status_code=404, detail="unknown question")
    logger.info("admin_decision_question_updated", question_key=question_key, admin_user=x_admin_user_id,
                mode=body.mode, accept_floor=body.accept_floor, reject_ceiling=body.reject_ceiling)
    return dict(row)
