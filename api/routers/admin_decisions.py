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

import asyncio
import json
from datetime import date
from typing import Literal, Optional

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from api.core.staff_authz import staff_policy
from api.routers.admin import verify_admin_secret
from services.acp_shared.audit_log import write_audit_log
from shared import jev_credit as jev_credit_mod   # the module; `jev_credit` is the GET route below
from shared.llm_client.decide import JEV_CREDIT_EXHAUSTED_EVENT, PLATFORM_TENANT_ID

logger = structlog.get_logger()

# AA-882 (GĐ1): the PUT/POST/DELETE need ai.admin / spend.write (api/core/staff_authz.py POLICY).
router = APIRouter(prefix="/admin/decisions", tags=["admin-decisions"], dependencies=[Depends(staff_policy)])

_ZONES = ("accept", "grey", "reject", "error", "skipped")

# count(l.created_at), not count(l.id): created_at is an index key, id is not, so the per-question
# aggregate stays an index-only scan on decision_log_question_created_cov_idx (migration 205).
_SUMMARY_SQL = """
    SELECT q.question_key, q.stage, q.kind, q.instructions, q.criteria, q.mode,
           q.accept_floor::float AS accept_floor, q.reject_ceiling::float AS reject_ceiling,
           q.threshold_version, q.calibration_ref, q.notes, q.updated_at, q.updated_by,
           count(l.created_at)::int AS verdicts,
           count(l.created_at) FILTER (WHERE l.zone = 'accept')::int  AS accept,
           count(l.created_at) FILTER (WHERE l.zone = 'grey')::int    AS grey,
           count(l.created_at) FILTER (WHERE l.zone = 'reject')::int  AS reject,
           count(l.created_at) FILTER (WHERE l.zone = 'error')::int   AS error,
           count(l.created_at) FILTER (WHERE l.zone = 'skipped')::int AS skipped,
           count(l.created_at) FILTER (WHERE l.zone IN ('accept', 'reject') AND l.mode = 'enforce')::int AS acted,
           count(l.created_at) FILTER (WHERE l.cached)::int AS cached,
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
           count(*) FILTER (WHERE cached)::int AS cached,
           coalesce(sum(cost_usd), 0)::float AS cost_usd,
           avg(latency_ms) FILTER (WHERE zone NOT IN ('skipped', 'error') AND NOT cached)::float AS avg_latency_ms,
           max(created_at) AS last_used_at
    FROM shared.decision_log
    WHERE created_at >= now() - make_interval(days => $1)
    GROUP BY stage ORDER BY stage
"""

# AA-742: verdicts served from the decide() cache no longer get a ledger row each — they are counted
# per day/stage/question/mode/zone. Added back into the counts above so the page reads the same.
_HITS_SQL = """
    SELECT day, stage, question_key, mode, zone, sum(hits)::bigint AS hits, max(updated_at) AS last_used_at
    FROM shared.decision_cache_hits_daily
    WHERE day >= (now() - make_interval(days => $1))::date
    GROUP BY day, stage, question_key, mode, zone
"""

# The exact billed amount per stage (one llm_call_log row per Jev call).
_CALLS_SQL = """
    SELECT stage, count(*)::int AS calls, coalesce(sum(cost_usd), 0)::float AS cost_usd,
           coalesce(sum(tokens_in), 0)::bigint AS tokens_in
    FROM shared.llm_call_log
    WHERE provider = 'typesafe' AND created_at >= now() - make_interval(days => $1)
    GROUP BY stage ORDER BY stage
"""

# Tenants whose content is sent to Jev (the allow-list), shown on the overview.
_ALLOWLIST_SQL = """
    SELECT a.tenant_id::text, t.slug, t.name, a.reason
    FROM shared.jev_tenant_allowlist a
    JOIN shared.tenants t USING (tenant_id)
    ORDER BY t.slug
"""


# Every aggregate the FE sums (reduce) must carry each numeric field as a real number — never a
# missing key or NULL. A stage that only appears in the cache-hit rollup (_HITS_SQL) or in
# _CALLS_SQL, or a ledger row where a FILTERed count/sum is NULL, would otherwise reach the FE as
# `undefined`/`null`; `reduce((n, s) => n + s.acted)` then yields NaN (the live "Acted on" bug:
# s1_judge_tiebreak had acted: null). Starting every aggregate from a zeroed template, and
# coalescing the DB rows, makes one bad row impossible.
_STAGE_NUM_FIELDS = ("questions", "verdicts", "acted", "errors", "skipped", "cached", "cost_usd")
_QUESTION_NUM_FIELDS = ("verdicts", "accept", "grey", "reject", "error", "skipped", "acted",
                        "cached", "cost_usd")
_DAY_NUM_FIELDS = ("verdicts", "accept", "grey", "reject", "error", "skipped", "cost_usd")


def _new_stage(stage: str) -> dict:
    d = {k: 0 for k in _STAGE_NUM_FIELDS}
    d.update(stage=stage, avg_latency_ms=None, last_used_at=None)
    return d


def _new_day(day) -> dict:
    d = {k: 0 for k in _DAY_NUM_FIELDS}
    d["day"] = day
    return d


def _coalesce_nums(target: dict, fields) -> None:
    """A FILTERed count is 0 (never NULL), but sum()/avg() can be NULL on an empty group — zero the
    numeric counters so the FE never sums a NULL."""
    for k in fields:
        if target.get(k) is None:
            target[k] = 0


def _add_hits(target: dict, h: dict) -> None:
    """Fold one rollup row of cache hits into a question / stage / day aggregate."""
    n = int(h["hits"])
    target["verdicts"] = (target.get("verdicts") or 0) + n
    target["cached"] = (target.get("cached") or 0) + n
    if h["zone"] in ("accept", "grey", "reject"):
        target[h["zone"]] = (target.get(h["zone"]) or 0) + n
    if h["zone"] in ("accept", "reject") and h["mode"] == "enforce":
        target["acted"] = (target.get("acted") or 0) + n
    last = h.get("last_used_at")
    if last is not None and (target.get("last_used_at") is None or last > target["last_used_at"]):
        target["last_used_at"] = last


def _iso(row: dict, *keys: str) -> dict:
    for k in keys:
        if row.get(k) is not None:
            row[k] = row[k].isoformat()
    return row


@router.get("/summary", summary="Jev questions per stage: mode, floors, verdicts, cost")
async def summary(request: Request, days: int = Query(7, ge=1, le=90), x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    # The window holds ~100k ledger rows a day during a wave; run the five reads side by side
    # (each on its own pooled connection) so the page stays under the 29 s API Gateway limit.
    q_rows, orphan_rows, call_rows, daily_rows, stage_rows, allow_rows, hit_rows = await asyncio.gather(
        pool.fetch(_SUMMARY_SQL, days),
        pool.fetch(_ORPHAN_SQL, days),
        pool.fetch(_CALLS_SQL, days),
        pool.fetch(_DAILY_SQL, days),
        pool.fetch(_STAGE_SQL, days),
        pool.fetch(_ALLOWLIST_SQL),
        pool.fetch(_HITS_SQL, days),
    )
    hits = [dict(h) for h in hit_rows]
    q_by_key = {r["question_key"]: dict(r) for r in q_rows}
    stage_by_key = {r["stage"]: dict(r) for r in stage_rows}
    day_by_key = {r["day"]: dict(r) for r in daily_rows}
    # Zero every numeric field a DB sum() could have left NULL (an empty group) so the FE sums only
    # real numbers.
    for q in q_by_key.values():
        _coalesce_nums(q, _QUESTION_NUM_FIELDS)
    for s in stage_by_key.values():
        _coalesce_nums(s, _STAGE_NUM_FIELDS)
    for d in day_by_key.values():
        _coalesce_nums(d, _DAY_NUM_FIELDS)
    for h in hits:
        if h["question_key"] in q_by_key:
            _add_hits(q_by_key[h["question_key"]], h)
        # A stage seen only in the cache-hit rollup starts from a fully-zeroed template (not just
        # {"stage": ...}), so acted/errors/skipped/cost_usd are 0, never missing.
        _add_hits(stage_by_key.setdefault(h["stage"], _new_stage(h["stage"])), h)
        day_row = day_by_key.setdefault(h["day"], _new_day(h["day"]))
        _add_hits(day_row, h)
        day_row.pop("last_used_at", None)
    # A stage that billed Jev calls (_CALLS_SQL) but has no ledger/hit rows must still appear with
    # every numeric field present, not be absent from `stages`.
    for c in call_rows:
        stage_by_key.setdefault(c["stage"], _new_stage(c["stage"]))
    questions = []
    for d in q_by_key.values():
        d = _iso(d, "updated_at", "last_used_at", "first_used_at")
        if isinstance(d.get("criteria"), str):
            d["criteria"] = json.loads(d["criteria"])
        questions.append(d)
    orphans = [_iso(dict(r), "last_used_at") for r in orphan_rows]
    calls = [dict(r) for r in call_rows]
    daily = [{**r, "day": r["day"].isoformat()} for _, r in sorted(day_by_key.items())]
    stages = [_iso(r, "last_used_at") for _, r in sorted(stage_by_key.items())]
    allowlist = [dict(r) for r in allow_rows]
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


# AA-756 — per question, how Jev's zone compared with the human-established truth now stored in
# decision_log.outcome. `outcome` is a TEXT column holding a JSON object (see
# shared/llm_client/decide.py::record_outcome); `outcome::jsonb->>'truth'` reads the stored bool.
# Agreement is judged on the confident zones only:
#   agree    = (accept & truth) OR (reject & !truth)   — Jev was confident and right
#   disagree = (accept & !truth) OR (reject & truth)   — Jev was confident and wrong
#   grey     = the zone was grey (Jev was not confident)   — neither agree nor disagree
# accept/reject with truth=NULL (an unparseable outcome) count only in `with_outcome`.
_OUTCOMES_SUMMARY_SQL = """
    WITH o AS (
        SELECT l.question_key, l.stage, l.zone,
               (l.outcome::jsonb ->> 'truth')::boolean AS truth,
               l.outcome::jsonb ->> 'source' AS src
        FROM shared.decision_log l
        WHERE l.outcome IS NOT NULL
          AND l.created_at >= now() - make_interval(days => $1)
    )
    SELECT q.question_key, q.stage, q.mode,
           count(o.*)::int AS with_outcome,
           count(o.*) FILTER (WHERE o.truth IS TRUE)::int  AS truth_true,
           count(o.*) FILTER (WHERE o.truth IS FALSE)::int AS truth_false,
           count(o.*) FILTER (WHERE o.zone = 'grey')::int AS grey,
           count(o.*) FILTER (WHERE
               (o.zone = 'accept' AND o.truth IS TRUE) OR (o.zone = 'reject' AND o.truth IS FALSE)
           )::int AS agree,
           count(o.*) FILTER (WHERE
               (o.zone = 'accept' AND o.truth IS FALSE) OR (o.zone = 'reject' AND o.truth IS TRUE)
           )::int AS disagree,
           count(o.*) FILTER (WHERE o.zone = 'accept')::int AS accept,
           count(o.*) FILTER (WHERE o.zone = 'reject')::int AS reject
    FROM shared.decision_question q
    LEFT JOIN o ON o.question_key = q.question_key
    GROUP BY q.question_key, q.stage, q.mode
    HAVING count(o.*) > 0
    ORDER BY q.stage, q.question_key
"""

# Outcomes written against a question_key that is not (or no longer) in decision_question.
_OUTCOMES_ORPHAN_SQL = """
    SELECT l.question_key, l.stage, count(*)::int AS with_outcome, max(l.created_at) AS last_at
    FROM shared.decision_log l
    LEFT JOIN shared.decision_question q ON q.question_key = l.question_key
    WHERE l.outcome IS NOT NULL
      AND l.created_at >= now() - make_interval(days => $1)
      AND q.question_key IS NULL
    GROUP BY l.question_key, l.stage
    ORDER BY with_outcome DESC
"""


@router.get("/outcomes/summary", summary="Per question, Jev zone vs. human-established truth")
async def outcomes_summary(request: Request, days: int = Query(90, ge=1, le=365),
                           x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    rows, orphan_rows = await asyncio.gather(
        pool.fetch(_OUTCOMES_SUMMARY_SQL, days),
        pool.fetch(_OUTCOMES_ORPHAN_SQL, days),
    )
    questions = []
    for r in rows:
        d = dict(r)
        decided = d["agree"] + d["disagree"]
        # Precision among the confident verdicts that have a human truth (grey excluded).
        d["precision"] = round(d["agree"] / decided, 4) if decided else None
        questions.append(d)
    return {
        "days": days,
        "questions": questions,
        "unregistered": [_iso(dict(r), "last_at") for r in orphan_rows],
        "total_with_outcome": sum(q["with_outcome"] for q in questions),
    }


# ── AA-845 — stratified sample of a1_claim_supported sentences for a labelling panel ──────────────
# decision_log keeps only the subject key, so the sentence and the source Jev read are rebuilt from
# the latest master version of each sampled tour (grounding.review_units — the same key builder the
# S1 run, the approve hook and the reviewer label use). Read-only, no Jev/LLM call ($0). The sample is
# reproducible: tours and sentences are ordered by md5(seed || key).
_CLAIM_SAMPLE_TOURS_SQL = """
    SELECT x.* FROM (
        SELECT DISTINCT ON (gc.tour_id)
               gc.tour_id::text AS tour_id, gc.id::text AS generated_content_id, gc.version_num,
               gc.aa_subtitle, gc.aa_summary, gc.aa_highlights, gc.aa_itineraries,
               rt.src_name, rt.src_subtitle, rt.src_summary, rt.src_description, rt.src_highlights,
               rt.src_itineraries, rt.country, rt.duration, rt.price_raw, rt.inclusions, rt.exclusions
          FROM silver_aa_internal.generated_content gc
          JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = gc.tour_id
         WHERE gc.tenant_id = $1::uuid
           AND rt.source_status = 'active' AND rt.deleted_at IS NULL
           AND ($2::text IS NULL OR rt.country = $2)
         ORDER BY gc.tour_id, gc.version_num DESC
    ) x
    ORDER BY md5($3 || x.tour_id)
    LIMIT $4
"""
_MASTER_TENANT_ID = "00000000-0000-0000-0000-000000000001"


def _md5(text: str) -> str:
    import hashlib
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def pick_stratum(candidates: list[dict], n: int, seed: str, per_tour: int,
                 taken_per_tour: dict[str, int]) -> list[dict]:
    """Up to n candidates in md5(seed || subject_key) order, at most `per_tour` per tour overall
    (`taken_per_tour` is shared across strata and updated in place)."""
    out = []
    for c in sorted(candidates, key=lambda c: _md5(seed + c["subject_key"])):
        if len(out) >= n:
            break
        if taken_per_tour.get(c["tour_id"], 0) >= per_tour:
            continue
        taken_per_tour[c["tour_id"]] = taken_per_tour.get(c["tour_id"], 0) + 1
        out.append(c)
    return out


def build_claim_candidates(rows: list) -> list[dict]:
    """One candidate per distinct subject key of the sampled tours' current master versions."""
    from services.content_generation import grounding as gr

    seen: set[str] = set()
    out = []
    for r in rows:
        tour = gr.tour_from_row(r)
        for u in gr.review_units(tour, gr.generated_from_row(r)):
            if u["subject_key"] in seen:
                continue
            seen.add(u["subject_key"])
            out.append({"tour_id": r["tour_id"], "generated_content_id": r["generated_content_id"],
                        "tour_name": r["src_name"], "country": r["country"], "field": u["field"],
                        "day": u["day"], "sentence": u["sentence"], "subject_key": u["subject_key"],
                        "_tour": tour, "_unit": u})
    return out


@router.get("/claim-sample",
            summary="Stratified a1_claim_supported sentences (+ source) for a labelling panel")
async def claim_sample(request: Request,
                       n: int = Query(100, ge=1, le=300),
                       low_share: float = Query(0.6, ge=0, le=1),
                       low_max: float = Query(0.20, ge=0, le=1),
                       high_max: float = Query(0.85, ge=0, le=1),
                       tours: int = Query(150, ge=1, le=400),
                       per_tour: int = Query(3, ge=1, le=20),
                       seed: str = Query("aa845", min_length=1, max_length=40),
                       country: Optional[str] = None,
                       x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    if low_max >= high_max:
        raise HTTPException(status_code=422, detail="low_max must be below high_max")
    from services.acp_shared.grounding import find_novel_numeric_claims
    from services.content_generation import grounding as gr

    pool = request.app.state.pool
    rows = await pool.fetch(_CLAIM_SAMPLE_TOURS_SQL, _MASTER_TENANT_ID, country, seed, tours)
    candidates = await asyncio.to_thread(build_claim_candidates, rows)
    verdicts = await gr.latest_verdicts(pool, [c["subject_key"] for c in candidates],
                                        (gr.QUESTION, gr.PROMISE_QUESTION))
    low, mid = [], []
    with_verdict = 0
    for c in candidates:
        v = verdicts.get((gr.QUESTION, c["subject_key"]))
        if v is None or v["probability"] is None or v["zone"] in ("error", "skipped"):
            continue
        with_verdict += 1
        p = v["probability"]
        c.update(p=p, zone=v["zone"], mode=v["mode"],
                 promise_p=(verdicts.get((gr.PROMISE_QUESTION, c["subject_key"])) or {}).get("probability"),
                 outcome=v["outcome"])
        if p <= low_max:
            low.append({**c, "stratum": "low"})
        elif p < high_max:
            mid.append({**c, "stratum": "mid"})
    n_low = round(n * low_share)
    taken: dict[str, int] = {}
    picked = pick_stratum(low, n_low, seed, per_tour, taken) + pick_stratum(mid, n - n_low, seed, per_tour, taken)
    items = []
    for i, c in enumerate(picked, start=1):
        tour, unit = c.pop("_tour"), c.pop("_unit")
        c["numeric_hit"] = bool(find_novel_numeric_claims(c["sentence"], gr.source_number_parts(tour)))
        c["source"] = gr.unit_source(tour, unit)
        items.append({"id": i, **c})
    return {
        "question_key": gr.QUESTION,
        "params": {"n": n, "low_share": low_share, "low_max": low_max, "high_max": high_max,
                   "tours": tours, "per_tour": per_tour, "seed": seed, "country": country},
        "population": {"tours": len(rows), "units": len(candidates), "with_verdict": with_verdict,
                       "low": len(low), "mid": len(mid)},
        "items": items,
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


@router.get("/log", summary="Jev verdicts: filter, sort, page")
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
               l.model, l.latency_ms, l.cost_usd::float AS cost_usd, l.error, l.outcome, l.cached
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
                                detail="enforce needs a calibration record (calibration_ref)")
        if body.accept_floor is None and body.reject_ceiling is None:
            raise HTTPException(status_code=422, detail="enforce needs at least one floor")


@router.put("/questions/{question_key}", summary="Set a question's mode / floors")
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


# ── AA-756 — Jev (TypeSafe) credit top-ups + estimated balance ─────────────────────────────────
# TypeSafe has no balance API; admins record each manual top-up (Ms. Thư tops up Jev by hand) so
# Settings can show an estimate. Spend = sum(cost_usd) of shared.llm_call_log where provider =
# 'typesafe' and created_at >= the first top-up date (includes the reconcile_s224 backfill rows,
# see shared.llm_call_log quality_signal->>'source' = 'reconcile_s224'). Empty table → nulls, never
# an error. Alerts: the exhausted alert (AA-720, on a 402) and, since AA-845, the low-credit alert when
# the estimate drops below shared/jev_credit.LOW_CREDIT_THRESHOLD_USD (bell + operator e-mail).

_TOPUPS_SQL = """
    SELECT id, topped_up_on, amount_usd::float AS amount_usd, note, created_by, created_at
    FROM shared.jev_credit_topup
    ORDER BY topped_up_on DESC, id DESC
"""

# Jev spend since the earliest top-up date (one query for the card and the AA-845 low-credit check).
_JEV_SPEND_SQL = jev_credit_mod.JEV_SPEND_SQL

# The most recent exhausted alert (AA-720), shown next to the estimate so an admin sees the signal. AA-845: a
# test bell ("Send test alerts") is not an alert, as in jev_credit.LAST_LOW_ALERT_SQL.
_LAST_EXHAUSTED_SQL = """
    SELECT max(created_at) AS last_at FROM shared.notifications
     WHERE event_type = $1 AND coalesce(payload->>'test', 'false') <> 'true'
"""


@router.get("/jev-credit", summary="Jev credit top-ups + estimated balance")
async def jev_credit(request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    topup_rows = await pool.fetch(_TOPUPS_SQL)
    topups = [_iso(dict(r), "created_at") for r in topup_rows]
    for t in topups:
        if t.get("topped_up_on") is not None:
            t["topped_up_on"] = t["topped_up_on"].isoformat()
    total = sum(t["amount_usd"] for t in topups) if topups else None
    first_on = topup_rows[-1]["topped_up_on"] if topup_rows else None
    spent_row, last_row, low_row = await asyncio.gather(
        pool.fetchrow(_JEV_SPEND_SQL, first_on),
        pool.fetchrow(_LAST_EXHAUSTED_SQL, JEV_CREDIT_EXHAUSTED_EVENT),
        pool.fetchrow(jev_credit_mod.LAST_LOW_ALERT_SQL, jev_credit_mod.JEV_CREDIT_LOW_EVENT),
    )
    spent = spent_row["spent"] if first_on is not None else None
    estimated_left = (total - spent) if (total is not None and spent is not None) else None
    last_alert = last_row["last_at"] if last_row else None
    last_low = low_row["last_at"] if low_row else None
    return {
        "topups": topups,
        "total_topped_up_usd": total,
        "first_topup_on": first_on.isoformat() if first_on else None,
        "spent_since_first_topup_usd": spent,
        "estimated_left_usd": estimated_left,
        "last_exhausted_alert_at": last_alert.isoformat() if last_alert else None,
        # AA-845 — the low-credit alert threshold and state, for the Settings card.
        "low_threshold_usd": jev_credit_mod.LOW_CREDIT_THRESHOLD_USD,
        "below_low_threshold": jev_credit_mod.is_below(estimated_left, jev_credit_mod.LOW_CREDIT_THRESHOLD_USD),
        "last_low_alert_at": last_low.isoformat() if last_low else None,
    }


class JevCreditCheck(BaseModel):
    """AA-845 — body of POST /admin/decisions/jev-credit/check. Both fields are for live verification only:
    an explicit threshold makes it a TEST check (bell + e-mail marked test, never counted by the real
    throttle); test_exhausted_alert also sends one test exhausted alert through the AA-720 path."""
    threshold_usd: Optional[float] = Field(None, gt=0, le=jev_credit_mod.MAX_TEST_THRESHOLD_USD)
    test_exhausted_alert: bool = False


@router.post("/jev-credit/check", summary="Jev low-credit check (bell + e-mail below the threshold)")
async def check_jev_credit(request: Request, body: Optional[JevCreditCheck] = None,
                           x_admin_secret: str = Header(None), x_admin_user_id: Optional[str] = Header(None)):
    verify_admin_secret(x_admin_secret)
    from shared.llm_client import decide as decide_mod

    body = body or JevCreditCheck()
    pool = request.app.state.pool
    source = f"admin:{x_admin_user_id or 'unknown'}"
    out = await jev_credit_mod.check_low_credit(pool, source=source, threshold_usd=body.threshold_usd)
    exhausted_test = False
    if body.test_exhausted_alert:
        async with pool.acquire() as conn:
            exhausted_test = await decide_mod.maybe_alert_jev_credit(conn, source=source, test=True)
    logger.info("admin_jev_credit_checked", admin_user=x_admin_user_id, below=out["below_threshold"],
                alerted=out["alerted"], test=out["test"], exhausted_test=exhausted_test)
    return {**out, "exhausted_test_alerted": exhausted_test}


class TopupCreate(BaseModel):
    topped_up_on: date
    amount_usd: float = Field(..., gt=0)
    note: Optional[str] = Field(None, max_length=2000)


@router.post("/jev-credit/topups", summary="Record a Jev credit top-up")
async def create_jev_topup(body: TopupCreate, request: Request, x_admin_secret: str = Header(None),
                           x_admin_user_id: Optional[str] = Header(None)):
    verify_admin_secret(x_admin_secret)
    actor = f"admin:{x_admin_user_id or 'unknown'}"
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO shared.jev_credit_topup (topped_up_on, amount_usd, note, created_by)
                VALUES ($1, $2, $3, $4)
                RETURNING id, topped_up_on, amount_usd::float AS amount_usd, note, created_by, created_at
                """,
                body.topped_up_on, body.amount_usd, body.note, actor,
            )
            await write_audit_log(
                conn, tenant_id=PLATFORM_TENANT_ID, actor=actor, action="jev_credit.topup_added",
                resource_type="jev_credit_topup", resource_id=str(row["id"]),
                details={"topped_up_on": body.topped_up_on.isoformat(), "amount_usd": body.amount_usd,
                         "note": body.note},
            )
    logger.info("jev_credit_topup_added", topup_id=row["id"], amount_usd=body.amount_usd,
                admin_user=x_admin_user_id)
    out = _iso(dict(row), "created_at")
    out["topped_up_on"] = out["topped_up_on"].isoformat()
    return out


@router.delete("/jev-credit/topups/{topup_id}", summary="Delete a Jev credit top-up")
async def delete_jev_topup(topup_id: int, request: Request, x_admin_secret: str = Header(None),
                           x_admin_user_id: Optional[str] = Header(None)):
    verify_admin_secret(x_admin_secret)
    actor = f"admin:{x_admin_user_id or 'unknown'}"
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            deleted = await conn.fetchrow(
                "DELETE FROM shared.jev_credit_topup WHERE id = $1 "
                "RETURNING id, topped_up_on, amount_usd::float AS amount_usd",
                topup_id,
            )
            if deleted is None:
                raise HTTPException(status_code=404, detail="unknown top-up")
            await write_audit_log(
                conn, tenant_id=PLATFORM_TENANT_ID, actor=actor, action="jev_credit.topup_deleted",
                resource_type="jev_credit_topup", resource_id=str(topup_id),
                details={"topped_up_on": deleted["topped_up_on"].isoformat(),
                         "amount_usd": deleted["amount_usd"]},
            )
    logger.info("jev_credit_topup_deleted", topup_id=topup_id, admin_user=x_admin_user_id)
    return {"id": topup_id, "deleted": True}
