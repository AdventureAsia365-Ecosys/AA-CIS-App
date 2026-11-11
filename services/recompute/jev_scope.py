"""AA-1083 — the Jev side of a recompute: which Segments Jev is asked about, a spend estimate before the run, a per-run
cap that holds a large run for staff, and the run's real Jev spend afterwards.

Jev (TypeSafe) is billed per input token with no batch tier, so its cost follows the number of NEW wordings a run asks
(decide() reuses a stored verdict of the same wording for 180 days). A platform recompute asks four A3 stages through
decide(): `a3_segment_type` (transit pick of a disputed moment), `a3_question_scope` and `a3_question_landing` (PAA
questions before they land) and `a3_demand` (does the Segment own a keyword's volume). Before AA-1083 the score stage
asked type + demand for EVERY Segment on every run, so a burst of newly bought keywords made one platform pass ask
about the whole catalogue (09/11/2026: 111k calls in a day).

Scope (services/recompute/stages.py `RecomputeScope.jev_changed_only`, switch env RECOMPUTE_JEV_SCOPE = `changed`
(default) | `all`): Jev is asked only about the Segments whose inputs changed, i.e. the Segment scope the trigger
declared (`segment_ids` of an a3_atomize / research / status change / sweep, the tour's Segments of a tour run) plus
the Segments `segment_catchup` touched. Every other Segment keeps its stored verdicts (atom_ranking.py: stored transit
pick, the demand of its current Score row, stored landing verdicts). A run without `segment_ids` asks about every
Segment, as before.

Estimate + cap: before the stages run, `plan_run()` counts the Jev calls the run would make that have no stored verdict
(the same pairs the stages build, read with decide.platform_verdicts, no Jev call) and prices them at the measured
unit cost per stage (mean `cost_usd` of answered TypeSafe calls over the last UNIT_COST_DAYS days, from
shared.llm_call_log). Above the cap (shared.spend_budget provider `jev`, scope `job:recompute`, `per_run_usd`; no row =
DEFAULT_CAP_USD with a hard stop; `hard_stop = false` = record only) the job ends `stopped_budget` with an error that
starts with JEV_CAP_HELD, the estimate in its result and one admin bell; `release_held()` (POST
/admin/job-runner/jobs/{id}/release-jev-cap) re-queues it with `payload.jev_release`, and a released run is not held.

The estimate is a planning figure, not a ceiling: it assumes the common outcome (a demand candidate with no stored
verdict is the one claimed; a country-scope pair with no stored verdict keeps its question) and leaves out the
segment_catchup stage's own Jev (a3_segment_match, at most SEGMENT_CATCHUP_MAX tours a pass). decide()'s daily Jev cap
(jev / global) still guards the provider as a whole.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional

import structlog

from services.recompute.errors import JEV_CAP_HELD

logger = structlog.get_logger()

SWITCH_ENV = "RECOMPUTE_JEV_SCOPE"
CAP_PROVIDER = "jev"
CAP_SCOPE = "job:recompute"
DEFAULT_CAP_USD = 0.50
# Unit cost of one Jev call when a stage has no answered call in the window (S277 ledger: 1.2e-5 to 1.8e-5 USD).
FALLBACK_UNIT_USD = 0.00002
UNIT_COST_DAYS = 14
JEV_PROVIDERS = ("typesafe", "jev")
HELD_EVENT = "recompute_jev_held"
# The four decide() stages a recompute asks (atom_ranking TYPE_STAGE, SCOPE_STAGE, LANDING_STAGE, DEMAND_STAGE).
STAGES = ("a3_segment_type", "a3_question_scope", "a3_question_landing", "a3_demand")

UNIT_COST_SQL = """
    SELECT stage, avg(cost_usd)::float AS unit_usd, count(*)::int AS calls
    FROM shared.llm_call_log
    WHERE stage = ANY($1::text[]) AND lower(provider) = ANY($2::text[])
      AND coalesce(status, 'ok') = 'ok' AND cost_usd > 0
      AND created_at > now() - make_interval(days => $3)
    GROUP BY stage
"""
CAP_SQL = """
    SELECT per_run_usd::float AS per_run_usd, hard_stop
    FROM shared.spend_budget WHERE provider = $1 AND scope = $2
"""
DEMAND_ROWS_SQL = """
    SELECT keyword, market, search_volume
    FROM acp_contract.search_demand WHERE search_volume IS NOT NULL
"""
# Same query as services/recompute/stages._THIS_TOUR_SEGMENTS_SQL (a tour run lands this tour's current Segments).
TOUR_SEGMENTS_SQL = """
    SELECT DISTINCT asm.segment_id
    FROM acp_contract.atom_segment_member asm
    JOIN acp_contract.tour_atoms ta ON ta.atom_id = asm.atom_id
    WHERE ta.tour_id = $1::uuid AND NOT ta.deleted AND NOT ta.is_empty_marker
"""
JOB_SPEND_SQL = """
    SELECT stage,
           count(*) FILTER (WHERE coalesce(status, 'ok') = 'ok')::int AS calls,
           count(*) FILTER (WHERE coalesce(status, 'ok') <> 'ok')::int AS refused,
           coalesce(sum(cost_usd), 0)::float AS usd
    FROM shared.llm_call_log
    WHERE job_id = $1::uuid AND lower(provider) = ANY($2::text[])
    GROUP BY stage ORDER BY stage
"""
BELL_SQL = """
    INSERT INTO shared.notifications (tenant_id, actor_type, event_type, entity_type, entity_id, payload, target_roles)
    VALUES ('00000000-0000-0000-0000-000000000001'::uuid, 'system', $1, 'job', $2, $3::jsonb, ARRAY['admin'])
"""
# A held recompute goes back to the queue with the release recorded in its payload (kept: the job's result keeps the
# held estimate until the released run writes its own). Guarded on kind, status and the error code, so a job held for
# another reason (or already re-queued) is never touched.
RELEASE_SQL = """
    UPDATE shared.job SET status = 'queued', attempt = 0, run_after = now(), error = NULL,
           cancel_requested = false, finished_at = NULL, updated_at = now(),
           payload = payload || jsonb_build_object('jev_release', jsonb_build_object(
               'by', $2::text, 'at', now(), 'estimate_usd', (result->'jev'->'estimate'->>'usd')::float))
    WHERE id = $1::uuid AND kind = 'recompute' AND status = 'stopped_budget' AND left(error, length($3)) = $3
    RETURNING id::text AS id, payload->'jev_release' AS jev_release
"""


def scope_enabled(environ=None) -> bool:
    """True unless RECOMPUTE_JEV_SCOPE asks for the old pass (`all`): Jev only for the Segments that changed."""
    env = os.environ if environ is None else environ
    return (env.get(SWITCH_ENV) or "changed").strip().lower() not in ("all", "off", "false", "0", "no")


@dataclass
class Estimate:
    """Jev calls with no stored verdict the run would make, per stage, and the unit cost of each stage."""

    new_calls: dict[str, int] = field(default_factory=dict)
    unit_usd: dict[str, float] = field(default_factory=dict)
    segments: Optional[int] = None          # Segments Jev may be asked about (None = every Segment)

    @property
    def calls(self) -> int:
        return sum(self.new_calls.values())

    @property
    def usd(self) -> float:
        return sum(n * self.unit_usd.get(stage, FALLBACK_UNIT_USD) for stage, n in self.new_calls.items())

    def as_dict(self) -> dict:
        return {
            "new_calls": self.calls, "usd": round(self.usd, 4), "segments": self.segments,
            "by_stage": {stage: {"calls": n, "unit_usd": round(self.unit_usd.get(stage, FALLBACK_UNIT_USD), 8),
                                 "usd": round(n * self.unit_usd.get(stage, FALLBACK_UNIT_USD), 4)}
                         for stage, n in self.new_calls.items()},
        }


def hold_reason(estimate: Estimate, cap_usd: Optional[float], hard_stop: bool, released: bool) -> Optional[str]:
    """The error a held run ends with, or None when it runs. Pure."""
    if released or cap_usd is None or not hard_stop or round(estimate.usd, 6) <= cap_usd:
        return None
    return (f"{JEV_CAP_HELD}: Jev estimate ${estimate.usd:.2f} ({estimate.calls} new questions) is above the "
            f"${cap_usd:.2f} cap per recompute run. Release the job on the Jobs page to run it.")


async def read_cap(pool) -> tuple[Optional[float], bool]:
    """(per-run cap in USD or None for no cap, hard stop). No row = DEFAULT_CAP_USD, hard."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(CAP_SQL, CAP_PROVIDER, CAP_SCOPE)
    if row is None:
        return DEFAULT_CAP_USD, True
    cap = row["per_run_usd"]
    return (float(cap) if cap is not None else None), bool(row["hard_stop"])


async def unit_costs(pool, days: int = UNIT_COST_DAYS) -> dict[str, float]:
    """Mean cost of one answered Jev call per recompute stage over the last `days` days; a stage with no answered
    call takes FALLBACK_UNIT_USD."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(UNIT_COST_SQL, list(STAGES), list(JEV_PROVIDERS), days)
    got = {r["stage"]: float(r["unit_usd"]) for r in rows if r["unit_usd"] is not None}
    return {stage: got.get(stage, FALLBACK_UNIT_USD) for stage in STAGES}


def _walk_demand(candidates: list[str], stored: dict) -> Optional[str]:
    """The first candidate key the demand walk would ASK about (no stored verdict), or None when the stored verdicts
    settle it: a stored enforced reject moves on, any other stored verdict claims. Mirrors resolve_demand()."""
    from services.acp_contract.atom_ranking import DEMAND_Q

    for key in candidates:
        dec = stored.get(key)
        if dec is None:
            return key
        if not dec.rejected(DEMAND_Q):
            return None
    return None


async def estimate(pool, *, segment_ids: Optional[set[str]], jev_changed_only: bool) -> Estimate:
    """The Jev calls a recompute with this Segment scope would make that no stored verdict answers (see the module
    docstring). `segment_ids` = the landing scope of the run (None = every Segment); `jev_changed_only` = the run asks
    only about those Segments (RecomputeScope.jev_changed_only)."""
    from services.acp_contract import atom_ranking as ar
    from services.seo_intelligence.seed_builder import DFS_LOCATION_MAP

    async with pool.acquire() as conn:
        rows = [dict(r) for r in await conn.fetch(ar.LANDING_SEGMENTS_SQL)]
        paa = ar.paa_tuples_of(await conn.fetch(ar.PAA_ROWS_SQL))
        demand_rows = [(r["keyword"], r["market"], r["search_volume"]) for r in await conn.fetch(DEMAND_ROWS_SQL)]
    jev_ids = set(segment_ids) if jev_changed_only and segment_ids is not None else None

    def asks(segment_id: str) -> bool:
        return jev_ids is None or segment_id in jev_ids

    new: dict[str, int] = {}
    # a3_segment_type — a disputed moment one asked Segment has
    _rule, disputed = ar.disputed_moments(rows)
    moments = [m for m, ids in disputed.items() if any(asks(i) for i in ids)]
    stored = await ar.stored_decisions(pool, [ar.TYPE_Q], [ar.type_subject_key(m) for m in moments])
    new[ar.TYPE_STAGE] = sum(1 for m in moments if stored.get(ar.type_subject_key(m)) is None)

    exclusions = await ar.resolve_exclusions(pool, rows, cached_only=True)
    rankable = [r for r in rows if not exclusions[r["segment_id"]]]
    # a3_question_scope + a3_question_landing — the landing pass's in-scope rows Jev is asked about
    landing_rows = [r for r in rankable
                    if (segment_ids is None or r["segment_id"] in segment_ids or r["questions_count"] is None)
                    and asks(r["segment_id"])]
    candidates = {r["segment_id"]: ar._capped_candidates(r["canonical_place"], r["canonical_action"], paa)
                  for r in landing_rows}
    scope_pairs = {(ar.country_scope(r.get("countries")), q) for r in landing_rows
                   for q in candidates[r["segment_id"]] if ar.country_scope(r.get("countries"))}
    stored = await ar.stored_decisions(pool, [ar.SCOPE_FOREIGN_Q, ar.SCOPE_HERE_Q],
                                       [ar.scope_subject_key(*p) for p in scope_pairs])
    new[ar.SCOPE_STAGE] = sum(1 for p in scope_pairs if stored.get(ar.scope_subject_key(*p)) is None)
    dropped = {p for p in scope_pairs
               if (dec := stored.get(ar.scope_subject_key(*p))) is not None and ar.scope_dropped(dec)}
    landing_pairs = set()
    for r in landing_rows:
        scope = ar.country_scope(r.get("countries"))
        moment = ar.landing_moment(r["canonical_place"] or "", r["canonical_action"] or "")
        landing_pairs.update((moment, q) for q in candidates[r["segment_id"]] if (scope, q) not in dropped)
    stored = await ar.stored_decisions(pool, [ar.LANDING_Q], [ar.landing_subject_key(*p) for p in landing_pairs])
    new[ar.LANDING_STAGE] = sum(1 for p in landing_pairs if stored.get(ar.landing_subject_key(*p)) is None)

    # a3_demand — per market, the Segments asked (in scope, or with no current Score row there)
    walks: list[list[str]] = []
    for market in DFS_LOCATION_MAP:
        previous = await ar.previous_demand(pool, market) if jev_ids is not None else {}
        for r in rankable:
            if jev_ids is not None and r["segment_id"] not in jev_ids and r["segment_id"] in previous:
                continue
            moment = ar.landing_moment(r["canonical_place"], r["canonical_action"])
            walks.append([ar.demand_subject_key(moment, kw) for kw, _v in ar.demand_candidates(
                r["canonical_place"], r["canonical_action"], demand_rows, market)[:ar.DEMAND_MAX_TRIES]])
    stored = await ar.stored_decisions(pool, [ar.DEMAND_Q], {k for w in walks for k in w})
    new[ar.DEMAND_STAGE] = len({k for w in walks if (k := _walk_demand(w, stored)) is not None})

    return Estimate(new_calls=new, unit_usd=await unit_costs(pool), segments=None if jev_ids is None else len(jev_ids))


async def tour_segment_scope(pool, tour_id: str) -> set[str]:
    async with pool.acquire() as conn:
        return {r["segment_id"] for r in await conn.fetch(TOUR_SEGMENTS_SQL, tour_id)}


async def plan_run(pool, *, scope: str, tour_id: Optional[str], segment_ids: Optional[list[str]],
                   released: Optional[dict]) -> dict:
    """The `jev` block of a recompute's result before its stages run: estimate, cap, and `held` (the error to stop
    with) when the estimate is above a hard cap and the run was not released. A failed estimate never blocks the run:
    it is recorded (`estimate_error`) and the run goes ahead (decide()'s daily Jev cap still applies)."""
    changed_only = scope_enabled()
    out: dict = {"scope": "changed" if changed_only else "all", "released": released or None}
    try:
        cap, hard = await read_cap(pool)
        out.update(cap_usd=cap, hard_stop=hard)
        if scope == "tour":
            landing = await tour_segment_scope(pool, tour_id)
        else:
            landing = set(segment_ids) if segment_ids is not None else None
        est = await estimate(pool, segment_ids=landing, jev_changed_only=changed_only)
    except Exception as exc:  # noqa: BLE001 — an estimate must never stop the recompute it plans
        logger.warning("recompute_jev_estimate_failed", error=f"{type(exc).__name__}: {str(exc)[:200]}")
        out["estimate_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        return out
    out["estimate"] = est.as_dict()
    reason = hold_reason(est, cap, hard, bool(released))
    if reason:
        out["held"] = reason
    logger.info("recompute_jev_plan", calls=est.calls, usd=round(est.usd, 4), cap_usd=cap, held=bool(reason),
                released=bool(released))
    return out


async def job_jev_spend(pool, job_id: str) -> dict:
    """What the run really spent on Jev: answered calls, refused / failed asks (0 USD) and USD, per stage, from the
    job's shared.llm_call_log rows (the A3 recompute stages carry the job id, AA-1058). Never raises."""
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(JOB_SPEND_SQL, job_id, list(JEV_PROVIDERS))
    except Exception as exc:  # noqa: BLE001 — observability only
        logger.warning("recompute_jev_spend_read_failed", job_id=job_id, error=str(exc)[:200])
        return {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    by_stage = {r["stage"]: {"calls": r["calls"], "refused": r["refused"], "usd": round(float(r["usd"]), 6)}
                for r in rows}
    return {"calls": sum(v["calls"] for v in by_stage.values()),
            "refused": sum(v["refused"] for v in by_stage.values()),
            "usd": round(sum(v["usd"] for v in by_stage.values()), 6), "by_stage": by_stage}


async def ring_held_bell(pool, job_id: str, message: str, jev: dict) -> None:
    """One admin bell for a held run (the job id is the entity, so each held job rings once). Best-effort."""
    try:
        payload = {"message": message, "job_id": job_id, "estimate": jev.get("estimate"), "cap_usd": jev.get("cap_usd")}
        async with pool.acquire() as conn:
            await conn.execute(BELL_SQL, HELD_EVENT, job_id, json.dumps(payload, default=str))
    except Exception as exc:  # noqa: BLE001 — the job's status, error and result still say held
        logger.warning("recompute_jev_bell_failed", job_id=job_id, error=str(exc)[:200])


async def release_held(pool, job_id: str, actor: str) -> Optional[dict]:
    """Re-queue a recompute the cap held, recording who released it. None when the job is not a held recompute."""
    row = await pool.fetchrow(RELEASE_SQL, job_id, actor[:64], JEV_CAP_HELD)
    if row is None:
        return None
    release = row["jev_release"]
    return {"job_id": row["id"], "status": "queued",
            "jev_release": json.loads(release) if isinstance(release, str) else release}


__all__ = ["Estimate", "scope_enabled", "hold_reason", "read_cap", "unit_costs", "estimate", "plan_run",
           "job_jev_spend", "ring_held_bell", "release_held", "JEV_CAP_HELD", "HELD_EVENT"]
