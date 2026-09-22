"""services/acp_shared/debate.py — AA-631: the Debate stage.

Ported/adapted from Ms. Thư's aa-social-media `src/aa_social/stages/slate_debate.py`
(ADR-0025) — a cut applied BEFORE a tenant sees a candidate Segment/Route, AFTER rank-sum
has already ranked it (AA-610). Full design decisions and evidence trail: the AA-631 Linear
issue itself (contested data source, everywhere_domains scope, brand-fit reuse, threshold,
safety rail — all 5 approved by Nghiệp 22/09/2026).

**Hybrid architecture, not the origin's literal Critic+Judge** — `propose_slate()` runs on
EVERY `GET /v1/slate`, so repeating the origin's 2-LLM-calls-per-run mechanism would be the
exact performance mistake AA-610 Sub 2 made before its own scope fix. Split by whether the
signal is measurable or a judgement call:

- `contested` — deterministic, no LLM (`atom_ranking.py::compute_contested()`/
  `everywhere_domains()`), cached on `atom_segment.contested` (migration 165), same pattern as
  `questions_count` (migration 163).
- brand-fit — LLM, but the EXISTING judge (`services/content_generation/brand_fit.py`,
  AA-206), not a new mechanism. Cached per (tenant, candidate, brand_version) in
  `acp_shared.debate_brand_fit_cache` (migration 166) so a repeat `GET /v1/slate` for the same
  tenant/candidate/brand_version never re-calls the LLM.

**Advisory, never able to empty a Channel's list** — mirrors the origin's own safety rule
("any failure/malformed ruling and the rules-based list stands unchanged... Debate must never
be able to make the Slate empty by erroring") and its own numeric safety rail ("never cut more
than half a Channel's list in one run"). Both are implemented in `apply_debate()` below.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import UUID

import structlog

from services.acp_contract.atom_ranking import CONTESTED_CUT_THRESHOLD, compute_contested
from services.content_generation.brand_fit import has_brand_signals, score_brand_fit

if TYPE_CHECKING:
    from services.acp_shared.slate import Candidate

logger = structlog.get_logger()

# AA-631 safety rail — the origin's own rule ("never cut more than half a Channel's list per
# run"), now needed for the hybrid design too: `contested` is a hard threshold rather than an
# LLM judgement with its own restraint, so a single bad threshold/data point could otherwise
# cut an unbounded fraction of a Channel's candidates in one `propose_slate()` call.
MAX_CUT_FRACTION = 0.5


async def _fetch_brand_profile(conn, tenant_id: UUID) -> dict | None:
    """The 5 brand_* fields + version, same shape judge_node.py's callers already build
    (services/content_generation/s1_batch.py's own core_idea -> brand_core_idea rename) —
    reused here via the same resolver (`admin_pipeline._resolve_brand_rule`) rather than a
    second, parallel brand-rule query. None when the tenant has no active brand row at all
    (distinct from "has a row but no brand-diff signals", which `has_brand_signals()` below
    still correctly treats as "skip the LLM call")."""
    from api.routers.admin_pipeline import _resolve_brand_rule
    row = await _resolve_brand_rule(conn, tenant_id, None, None)
    if row is None:
        return None
    voice = row["voice_examples"]
    return {
        "brand_core_idea": row.get("core_idea") or "",
        "brand_customer_segment": row.get("customer_segment") or "",
        "brand_customer_mindset": row.get("customer_mindset") or "",
        "brand_voice_examples": (
            list(voice) if isinstance(voice, list) else json.loads(voice or "[]")
        ),
        "brand_good_examples": row.get("good_examples") or "",
        "_version": row["version"],
    }


async def _fetch_candidate_text(conn, candidate: "Candidate") -> dict:
    """Debate runs BEFORE T2 rewrite — a candidate Segment/Route has no `generated_content`
    row yet (that is created by T2, AFTER a tenant picks it off the Slate). `brand_fit.
    score_brand_fit()` needs a `generated`-shaped dict (name/subtitle/summary/highlights/
    itineraries/seo_title/seo_meta) to judge — synthesized here from the candidate's own member
    atoms' raw `text` (acp_contract.tour_atoms), the only real content that exists at this
    point in the pipeline. Deliberately thin (not a rewrite-quality judgement — Debate's own
    brand-fit standard is "does this read as this brand's angle", answerable from raw atom text
    same as the origin's own Critic reads a Segment/Route's raw evidence, not a written page)."""
    if candidate.segment_id:
        rows = await conn.fetch("""
            SELECT ta.text FROM acp_contract.atom_segment_member asm
            JOIN acp_contract.tour_atoms ta ON ta.atom_id = asm.atom_id
            WHERE asm.segment_id = $1 AND NOT ta.deleted
        """, candidate.segment_id)
    else:
        rows = await conn.fetch("""
            SELECT ta.text
            FROM acp_contract.route r
            JOIN acp_contract.atom_segment_member asm
                ON asm.segment_id = ANY(SELECT jsonb_array_elements_text(r.ordered_segment_ids))
            JOIN acp_contract.tour_atoms ta ON ta.atom_id = asm.atom_id
            WHERE r.route_id = $1 AND NOT ta.deleted
        """, candidate.route_id)
    texts = [r["text"] for r in rows if r["text"]]
    return {
        "name": candidate.place or candidate.hub_name or "",
        "subtitle": candidate.action or "",
        "summary": " ".join(texts)[:800],
        "highlights": texts[:5],
        "itineraries": " ".join(texts)[:600],
        "seo_title": "", "seo_meta": "",
    }


async def _cached_brand_fit(conn, tenant_id: UUID, candidate: "Candidate", brand_version: int) -> dict | None:
    if candidate.segment_id:
        row = await conn.fetchrow("""
            SELECT judge_score, feedback FROM acp_shared.debate_brand_fit_cache
            WHERE tenant_id = $1::uuid AND segment_id = $2 AND brand_version = $3
        """, tenant_id, candidate.segment_id, brand_version)
    else:
        row = await conn.fetchrow("""
            SELECT judge_score, feedback FROM acp_shared.debate_brand_fit_cache
            WHERE tenant_id = $1::uuid AND route_id = $2 AND brand_version = $3
        """, tenant_id, candidate.route_id, brand_version)
    return dict(row) if row else None


async def _store_brand_fit_cache(
    conn, tenant_id: UUID, candidate: "Candidate", brand_version: int, result,
) -> None:
    conflict_target = (
        "(tenant_id, segment_id, brand_version) WHERE segment_id IS NOT NULL"
        if candidate.segment_id else
        "(tenant_id, route_id, brand_version) WHERE route_id IS NOT NULL"
    )
    await conn.execute(f"""
        INSERT INTO acp_shared.debate_brand_fit_cache
            (tenant_id, segment_id, route_id, brand_version, judge_score,
             brand_fit_score, cross_brand_distinct, mission_present, feedback)
        VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9)
        ON CONFLICT {conflict_target}
        DO UPDATE SET judge_score = excluded.judge_score, feedback = excluded.feedback,
                      computed_at = now()
    """, tenant_id, candidate.segment_id, candidate.route_id, brand_version,
        result.judge_score, result.brand_fit_score, result.cross_brand_distinct,
        result.mission_present, result.feedback,
    )


# Brand-fit "fails" a candidate below this judge_score — mirrors judge_node.py's own MIN_QUALITY
# gate (7.0) rather than inventing a second number: brand-fit that would not pass the T2 rewrite
# gate anyway should not be recommended to a tenant, whether or not they ever pick it.
BRAND_FIT_CUT_THRESHOLD = 7.0


async def apply_debate(
    tenant_id: UUID, candidates: list["Candidate"], conn,
) -> list["Candidate"]:
    """AA-631 — the Debate cut itself, applied per Channel's candidate list (same grain
    `_choose()` already operates at) between candidate-fetch and `_choose()` in
    `propose_slate()`. Returns the SURVIVING candidates, in the same relative order.

    Advisory + safety-railed, exactly like the origin: any per-candidate failure (contested
    read error, brand-fit LLM/cache error) counts that candidate as PASSING (never cut on a
    failure), and the total cut across this call is capped at `MAX_CUT_FRACTION` of the input
    — if more candidates would fail than the cap allows, the WEAKEST-failing (lowest judge_score
    among brand-fit failures, highest contested among contested failures — whichever standard
    flagged them) are the ones actually cut, up to the cap, and the rest are let through
    uncut. Never raises — a bug in Debate must never make `propose_slate()` itself fail.
    """
    if not candidates:
        return candidates

    brand_profile = None
    try:
        brand_profile = await _fetch_brand_profile(conn, tenant_id)
    except Exception as exc:
        logger.warning("debate_brand_profile_fetch_failed", tenant_id=str(tenant_id), error=str(exc))

    run_brand_fit = bool(brand_profile and has_brand_signals(brand_profile))

    flagged: list[tuple["Candidate", float]] = []  # (candidate, "how bad" — higher cuts first)
    surviving: list["Candidate"] = []

    for candidate in candidates:
        cut_score = None  # None = passes; else the "how bad" value used for cap ordering

        # Standard #1 — contested (deterministic, always attempted regardless of brand profile).
        try:
            contested = None
            if candidate.segment_id:
                row = await conn.fetchrow(
                    "SELECT contested FROM acp_contract.atom_segment WHERE segment_id = $1",
                    candidate.segment_id,
                )
                contested = row["contested"] if row else None
            if contested is not None and contested >= CONTESTED_CUT_THRESHOLD:
                cut_score = contested
        except Exception as exc:
            logger.warning("debate_contested_check_failed", segment_id=candidate.segment_id,
                            route_id=candidate.route_id, error=str(exc))

        # Standard #4 — brand-fit (LLM, cached, only when the tenant has a real brand profile).
        if cut_score is None and run_brand_fit:
            try:
                cached = await _cached_brand_fit(conn, tenant_id, candidate, brand_profile["_version"])
                if cached is not None:
                    judge_score = cached["judge_score"]
                else:
                    generated = await _fetch_candidate_text(conn, candidate)
                    # NOTE: score_brand_fit() is synchronous (same as judge_node.py's own call
                    # site in the T2 LangGraph) — a real LLM call here blocks this task's event
                    # loop for its duration. Same pre-existing characteristic every judge_node.py
                    # call already has platform-wide, not a new regression introduced by Debate;
                    # a cache HIT (the expected steady state after brand_version stabilizes) never
                    # reaches this line at all.
                    result = score_brand_fit(brand_profile, generated)
                    await _store_brand_fit_cache(conn, tenant_id, candidate, brand_profile["_version"], result)
                    judge_score = result.judge_score
                if judge_score < BRAND_FIT_CUT_THRESHOLD:
                    # Lower judge_score = worse fit = "how bad" — invert so a bigger number
                    # means "cut this first", consistent with contested's own convention above.
                    cut_score = 10.0 - judge_score
            except Exception as exc:
                logger.warning("debate_brand_fit_check_failed", segment_id=candidate.segment_id,
                                route_id=candidate.route_id, error=str(exc))

        if cut_score is None:
            surviving.append(candidate)
        else:
            flagged.append((candidate, cut_score))

    if not flagged:
        return surviving

    max_cuts = int(len(candidates) * MAX_CUT_FRACTION)
    flagged.sort(key=lambda pair: -pair[1])  # worst-first
    actually_cut = {id(c) for c, _ in flagged[:max_cuts]}
    reprieved = [c for c, _ in flagged[max_cuts:]]

    if len(flagged) > max_cuts:
        logger.info("debate_safety_rail_engaged", tenant_id=str(tenant_id),
                    flagged=len(flagged), max_cuts=max_cuts, total=len(candidates))

    result_list = [c for c in candidates if id(c) not in actually_cut]
    # Preserve original relative order — surviving ∪ reprieved, filtered back through the
    # original candidates list rather than concatenated (which would reorder reprieved ones
    # to the end, unlike everything else in this pipeline's "strongest-first, stable" ordering).
    return result_list
