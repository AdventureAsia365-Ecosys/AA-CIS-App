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

**Rank only until calibrated (AA-1079, S277).** The AA-741 phase 3 audit (root
`docs/audits/2026-11-10-S276-aa741-phase3/` §4.3) found Debate flagging 499 of 502 free candidates: the
`MAX_CUT_FRACTION` cap, not the judgement, decided what was dropped. `debate_mode()` (env `T7_DEBATE_MODE`,
`rank` when unset) now keeps every candidate: the flags ride on the Candidate (`debate_flags`,
`debate_severity`), `_choose_slate()` orders unflagged candidates before flagged ones (rank-sum order inside each
group) and the `t7_slate` job result records them (`debate_report()`). `cut` is the pre-AA-1079 behaviour, kept
for the day the brand-fit judge is calibrated (AA-1079 part 2).

**Advisory, never able to empty a Channel's list** — mirrors the origin's own safety rule
("any failure/malformed ruling and the rules-based list stands unchanged... Debate must never
be able to make the Slate empty by erroring") and its own numeric safety rail ("never cut more
than half a Channel's list in one run"). Both are implemented in `apply_debate()` below.
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Optional
from uuid import UUID

from services.content_generation.stages import T7_DEBATE_JUDGE

import structlog

from services.acp_contract.atom_ranking import CONTESTED_CUT_THRESHOLD, compute_contested
from services.content_generation.brand_fit import billed_calls, has_brand_signals, score_candidate_fit
from shared.llm_client.call_log import billed_fields, record_billed_failure, record_call
from shared.llm_client.decide import decide
from services.quality.registry import threshold

if TYPE_CHECKING:
    from services.acp_shared.slate import Candidate

logger = structlog.get_logger()

# AA-631 safety rail — the origin's own rule ("never cut more than half a Channel's list per
# run"), now needed for the hybrid design too: `contested` is a hard threshold rather than an
# LLM judgement with its own restraint, so a single bad threshold/data point could otherwise
# cut an unbounded fraction of a Channel's candidates in one `propose_slate()` call.
MAX_CUT_FRACTION = 0.5

# AA-1079 — what Debate does with its flags. `rank` (the default, also when the env var is unset or unknown): no
# candidate is dropped, flagged ones are ordered after unflagged ones. `cut`: the AA-631 cut under the cap above.
DEBATE_MODE_ENV = "T7_DEBATE_MODE"
DEBATE_MODE_RANK = "rank"
DEBATE_MODE_CUT = "cut"
DEBATE_MODES = (DEBATE_MODE_RANK, DEBATE_MODE_CUT)
FLAG_CONTESTED = "contested"
FLAG_BRAND_FIT = "brand_fit"
# Flagged candidates listed one by one in the job result (the counts always cover all of them).
REPORT_SIGNALS_MAX = 300
# Evidence shown to the brand-fit judge per candidate (distinct atom texts, each trimmed).
EVIDENCE_ITEMS = 12
EVIDENCE_CHARS = 240


# AA-1081 (S278) — how many brand-fit judge calls (cache misses) one Debate pass keeps in flight. One at a time a
# refresh with ~320 misses took 12-15 min (~2.2 s a call); the gateway's own limits still apply to every call.
DEBATE_CONCURRENCY_ENV = "T7_DEBATE_CONCURRENCY"
DEBATE_CONCURRENCY_DEFAULT = 8
DEBATE_CONCURRENCY_MAX = 32


def debate_concurrency() -> int:
    """`T7_DEBATE_CONCURRENCY` (read per call): an integer 1..32; unset, not a number or below 1 = the default 8,
    above 32 = 32. 1 is the old one-at-a-time run."""
    raw = (os.environ.get(DEBATE_CONCURRENCY_ENV) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEBATE_CONCURRENCY_DEFAULT
    if value < 1:
        return DEBATE_CONCURRENCY_DEFAULT
    return min(value, DEBATE_CONCURRENCY_MAX)


def debate_mode() -> str:
    """`rank` unless `T7_DEBATE_MODE` says `cut` (any other value, or none, is rank: the safe side)."""
    raw = (os.environ.get(DEBATE_MODE_ENV) or "").strip().lower()
    return raw if raw in DEBATE_MODES else DEBATE_MODE_RANK


@dataclass(frozen=True)
class DebateSignal:
    """What Debate found about one candidate. `flags` empty = passes; `severity` = "how bad" (higher cuts / sinks
    first: contested as stored, brand fit as 10 - judge_score); `brand_fit_source` = where the brand-fit score came
    from (cache, jev_accept, jev_reject, llm, cache_miss in a cache-only replay, error, skipped)."""

    flags: tuple[str, ...] = ()
    severity: Optional[float] = None
    judge_score: Optional[float] = None
    contested: Optional[float] = None
    brand_fit_source: Optional[str] = None


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
    """Debate runs BEFORE T2 rewrite — a candidate Segment/Route has no `generated_content` row
    yet (T2 creates it after a tenant picks it off the Slate). The only real content is its
    member atoms' raw `text` (acp_contract.tour_atoms), so `score_candidate_fit()` is given the
    topic (place + action) plus that text as evidence: deduped, capped, never dressed up as a
    tour page. (Until S203 the same joined text was pasted into summary, highlights and
    itineraries of a fake page and judged as a rewrite — the judge read it as corrupted.)"""
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
    evidence: list[str] = []
    seen: set[str] = set()
    for r in rows:
        text = " ".join((r["text"] or "").split())
        if not text or text.casefold() in seen:
            continue
        seen.add(text.casefold())
        evidence.append(text[:EVIDENCE_CHARS])
        if len(evidence) >= EVIDENCE_ITEMS:
            break
    return {
        "place": candidate.place or candidate.hub_name or "",
        "action": candidate.action or "",
        "evidence": evidence,
    }


_LATEST_CACHE_SEGMENT_SQL = """
    SELECT judge_score, feedback FROM acp_shared.debate_brand_fit_cache
    WHERE tenant_id = $1::uuid AND segment_id = $2
    ORDER BY brand_version DESC, computed_at DESC LIMIT 1
"""
_LATEST_CACHE_ROUTE_SQL = """
    SELECT judge_score, feedback FROM acp_shared.debate_brand_fit_cache
    WHERE tenant_id = $1::uuid AND route_id = $2
    ORDER BY brand_version DESC, computed_at DESC LIMIT 1
"""


async def _cached_brand_fit(conn, tenant_id: UUID, candidate: "Candidate", brand_version: int,
                            *, any_brand_version: bool = False) -> dict | None:
    if any_brand_version:
        # AA-1079 replay: the newest stored score of this candidate, whatever brand version it was judged under (the
        # phase 2 slates were judged under the brand version of that day).
        row = await conn.fetchrow(
            _LATEST_CACHE_SEGMENT_SQL if candidate.segment_id else _LATEST_CACHE_ROUTE_SQL,
            tenant_id, candidate.segment_id or candidate.route_id)
        return dict(row) if row else None
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
BRAND_FIT_CUT_THRESHOLD = threshold("t7.brand_fit_cut")  # services/quality/thresholds.yaml (AA-926)


# AA-700 T7-1 — Jev prefilter before the Luna brand-fit call (cache miss only). Enforced + confident:
# accept passes without Luna, reject cuts without Luna; grey (and every shadow verdict) → Luna as
# before. Tenant content → asked only for Jev allow-listed tenants (design C2).
T7_STAGE = "t7_debate"
T7_Q = "t7_topic_fits_brand"
_T7_JEV_REJECT_SCORE = BRAND_FIT_CUT_THRESHOLD - 0.5   # cut, but ordered after Luna's clearly bad ones
_T7_EVIDENCE_ITEMS = 8


def t7_state(brand_profile: dict, topic: dict) -> dict:
    return {
        "brand": {
            "core_idea": brand_profile.get("brand_core_idea") or "",
            "who_it_is_for": brand_profile.get("brand_customer_segment") or "",
            "what_they_want": brand_profile.get("brand_customer_mindset") or "",
            "voice_examples": [v for v in (brand_profile.get("brand_voice_examples") or []) if v][:3],
        },
        "topic": {
            "place": topic.get("place") or "",
            "action": topic.get("action") or "",
            "evidence": [str(e)[:600] for e in (topic.get("evidence") or [])][:_T7_EVIDENCE_ITEMS],
        },
    }


async def t7_prefilter(tenant_id: UUID, candidate: "Candidate", brand_profile: dict, topic: dict):
    """The Jev Decision for one candidate (never raises — decide() fails open)."""
    key = candidate.segment_id or candidate.route_id or f"{candidate.place}|{candidate.action}"
    return await decide(T7_STAGE, f"t7:{brand_profile.get('_version')}:{key}",
                        t7_state(brand_profile, topic), [T7_Q], tenant_id=str(tenant_id))


async def assess_candidates(
    tenant_id: UUID, candidates: list["Candidate"], conn, *, cache_only: bool = False,
    any_brand_version: bool = False, concurrency: Optional[int] = None,
) -> list[DebateSignal]:
    """AA-1079 — Debate's judgement of each candidate, one `DebateSignal` per candidate in input order; nothing is
    cut here (`apply_signals()` decides that). Any per-candidate failure counts that candidate as PASSING (never
    flagged on a failure). Brand fit is only asked when `contested` did not flag the candidate (same cost as before).

    AA-1081 (S278) — two passes. The first walks the candidates in order on `conn` (contested, cache read, evidence
    read for a cache miss). The second asks the Jev pre-filter and the brand-fit judge for the cache misses, at most
    `concurrency` at once (`debate_concurrency()`, env `T7_DEBATE_CONCURRENCY`, default 8): the judge call is
    synchronous, so each runs in a worker thread with a copy of the caller's context (job tenant, capacity line).
    The gateway still applies its own limits to every call (connection `max_concurrency` / `rpm_limit`, the TPM
    reservation, the tenant AI budget). The cache writes go back through `conn` one at a time. Each signal lands at
    its candidate's index, so the result (and the rank built on it) is the same as a one-at-a-time run.

    `cache_only=True` (the $0 replay, `scripts/quality/replay_aa1079_debate.py`): a brand-fit cache miss is reported
    as `cache_miss` and passes; no Jev decision, no LLM call, no cache write. `any_brand_version=True` reads the
    newest stored score of the candidate whatever brand version it was judged under."""
    if not candidates:
        return []

    brand_profile = None
    try:
        brand_profile = await _fetch_brand_profile(conn, tenant_id)
    except Exception as exc:
        logger.warning("debate_brand_profile_fetch_failed", tenant_id=str(tenant_id), error=str(exc))

    run_brand_fit = bool(brand_profile and has_brand_signals(brand_profile))
    signals: list[Optional[DebateSignal]] = [None] * len(candidates)
    to_judge: list[tuple[int, "Candidate", dict, Optional[float]]] = []

    for i, candidate in enumerate(candidates):
        contested = None
        # Standard #1 — contested (deterministic, always attempted regardless of brand profile).
        try:
            if candidate.segment_id:
                row = await conn.fetchrow(
                    "SELECT contested FROM acp_contract.atom_segment WHERE segment_id = $1",
                    candidate.segment_id,
                )
                contested = row["contested"] if row else None
            if contested is not None and contested >= CONTESTED_CUT_THRESHOLD:
                signals[i] = DebateSignal(flags=(FLAG_CONTESTED,), severity=float(contested),
                                          contested=float(contested))
                continue
        except Exception as exc:
            logger.warning("debate_contested_check_failed", segment_id=candidate.segment_id,
                            route_id=candidate.route_id, error=str(exc))

        contested_value = float(contested) if contested is not None else None
        if not run_brand_fit:
            signals[i] = DebateSignal(contested=contested_value, brand_fit_source="skipped")
            continue

        # Standard #4 — brand-fit (LLM, cached, only when the tenant has a real brand profile).
        try:
            cached = await _cached_brand_fit(conn, tenant_id, candidate, brand_profile["_version"],
                                             any_brand_version=any_brand_version)
            if cached is None and cache_only:
                signals[i] = DebateSignal(contested=contested_value, brand_fit_source="cache_miss")
                continue
            if cached is not None:
                signals[i] = _brand_fit_signal(cached["judge_score"], "cache", contested_value)
                continue
            to_judge.append((i, candidate, await _fetch_candidate_text(conn, candidate), contested_value))
        except Exception as exc:
            logger.warning("debate_brand_fit_check_failed", segment_id=candidate.segment_id,
                            route_id=candidate.route_id, error=str(exc))
            signals[i] = DebateSignal(contested=contested_value, brand_fit_source="error")

    if to_judge:
        width = max(1, min(concurrency or debate_concurrency(), len(to_judge)))
        gate = asyncio.Semaphore(width)
        conn_lock = asyncio.Lock()   # one asyncpg connection: one statement at a time
        executor = ThreadPoolExecutor(max_workers=width, thread_name_prefix="t7-debate-judge")
        try:
            async def _one(i: int, candidate: "Candidate", topic: dict, contested_value: Optional[float]) -> None:
                async with gate:
                    signals[i] = await _judge_brand_fit(tenant_id, candidate, brand_profile, topic,
                                                        contested_value, conn, conn_lock, executor)

            await asyncio.gather(*(_one(*item) for item in to_judge))
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        logger.info("debate_judged", tenant_id=str(tenant_id), candidates=len(candidates),
                    judged=len(to_judge), concurrency=width)
    return [s if s is not None else DebateSignal(brand_fit_source="error") for s in signals]


def _brand_fit_signal(judge_score, source: str, contested_value: Optional[float]) -> DebateSignal:
    judge_score = float(judge_score)
    if judge_score < BRAND_FIT_CUT_THRESHOLD:
        # Lower judge_score = worse fit = "how bad" — invert so a bigger number
        # means "cut this first", consistent with contested's own convention above.
        return DebateSignal(flags=(FLAG_BRAND_FIT,), severity=10.0 - judge_score, judge_score=judge_score,
                            contested=contested_value, brand_fit_source=source)
    return DebateSignal(judge_score=judge_score, contested=contested_value, brand_fit_source=source)


async def _judge_brand_fit(tenant_id: UUID, candidate: "Candidate", brand_profile: dict, topic: dict,
                           contested_value: Optional[float], conn, conn_lock: asyncio.Lock,
                           executor: ThreadPoolExecutor) -> DebateSignal:
    """AA-1081 — one cache miss: the Jev pre-filter, then (grey) the brand-fit judge in a worker thread, its
    `llm_call_log` row and its cache row. Never raises: a failure lets the candidate pass (`error`)."""
    try:
        jev = await t7_prefilter(tenant_id, candidate, brand_profile, topic)   # AA-700 T7-1
        if jev.accepted(T7_Q):
            return _brand_fit_signal(10.0, "jev_accept", contested_value)
        if jev.rejected(T7_Q):
            return _brand_fit_signal(_T7_JEV_REJECT_SCORE, "jev_reject", contested_value)
        # score_candidate_fit() is synchronous (the gateway call): it runs in a worker thread with a copy of this
        # task's context, so the job's tenant and capacity line bound on the context reach the gateway.
        # AA-763 (G4): own stage key, same route as s1_judge.
        ctx = contextvars.copy_context()
        call = functools.partial(score_candidate_fit, brand_profile, topic, stage=T7_DEBATE_JUDGE)
        result = await asyncio.get_running_loop().run_in_executor(executor, ctx.run, call)
        # AA-685 — this call used to write no llm_call_log row (judge_node.py logs its own use of the same helper).
        await record_call(
            stage=T7_DEBATE_JUDGE, role="judge", model=result.model_used,
            tokens_in=result.input_tokens, tokens_out=result.output_tokens,
            cost_usd=result.cost_usd, tenant_id=str(tenant_id),
            quality_signal={
                "source": "debate", "judge_score": result.judge_score,
                "passed": result.judge_score >= BRAND_FIT_CUT_THRESHOLD,
            },
            stop_reason=result.stop_reason, account=result.account,
            fallback_used=result.fallback_used, **billed_fields(result),
        )
        async with conn_lock:
            await _store_brand_fit_cache(conn, tenant_id, candidate, brand_profile["_version"], result)
        return _brand_fit_signal(result.judge_score, "llm", contested_value)
    except Exception as exc:
        logger.warning("debate_brand_fit_check_failed", segment_id=candidate.segment_id,
                       route_id=candidate.route_id, error=str(exc))
        # AA-927: a judge reply billed but unusable (malformed JSON) still gets its cost row. Only
        # score_candidate_fit attaches billed calls, and only when it raised (its own success row is written right
        # after it returns, above).
        try:
            await record_billed_failure(stage=T7_DEBATE_JUDGE, role="judge", billed=billed_calls(exc),
                                        error=str(exc), tenant_id=str(tenant_id))
        except Exception as log_exc:  # noqa: BLE001 — the cost row never fails the candidate
            logger.warning("debate_billed_failure_log_failed", error=str(log_exc)[:200])
        return DebateSignal(contested=contested_value, brand_fit_source="error")


def apply_signals(
    candidates: list["Candidate"], signals: list[DebateSignal], mode: str,
) -> list["Candidate"]:
    """Pure. `rank` (AA-1079): every candidate is kept, in input order; a flagged one carries its flags and severity
    (`Candidate.debate_flags` / `debate_severity`), which `_choose_slate()` orders after the unflagged ones. `cut`
    (AA-631): the flagged candidates are cut, worst first, up to `MAX_CUT_FRACTION` of the list; the rest are let
    through uncut, original relative order kept."""
    if len(signals) != len(candidates):
        raise ValueError("one DebateSignal per candidate")
    if mode != DEBATE_MODE_CUT:
        return [replace(c, debate_flags=sig.flags, debate_severity=sig.severity) if sig.flags else c
                for c, sig in zip(candidates, signals)]

    flagged = [(c, sig.severity) for c, sig in zip(candidates, signals) if sig.flags]
    if not flagged:
        return list(candidates)
    max_cuts = int(len(candidates) * MAX_CUT_FRACTION)
    flagged.sort(key=lambda pair: -pair[1])  # worst-first
    actually_cut = {id(c) for c, _ in flagged[:max_cuts]}
    if len(flagged) > max_cuts:
        logger.info("debate_safety_rail_engaged", flagged=len(flagged), max_cuts=max_cuts, total=len(candidates))
    # Preserve original relative order — surviving ∪ reprieved, filtered back through the
    # original candidates list rather than concatenated (which would reorder reprieved ones
    # to the end, unlike everything else in this pipeline's "strongest-first, stable" ordering).
    return [c for c in candidates if id(c) not in actually_cut]


def would_cut(signals: list[DebateSignal]) -> int:
    """How many candidates the AA-631 cut would drop from this list (the cap applied). Pure."""
    flagged = sum(1 for s in signals if s.flags)
    return min(flagged, int(len(signals) * MAX_CUT_FRACTION))


async def apply_debate(
    tenant_id: UUID, candidates: list["Candidate"], conn,
) -> list["Candidate"]:
    """AA-631 — Debate, applied per candidate pool (Segments, Routes) between candidate-fetch and `_choose_slate()`
    in `propose_slate()`. AA-1079: in `rank` mode (the default, `debate_mode()`) nothing is cut and the flags ride on
    the candidates; in `cut` mode the AA-631 cut applies (see `apply_signals()`). Never raises — a bug in Debate must
    never make `propose_slate()` itself fail."""
    if not candidates:
        return candidates
    signals = await assess_candidates(tenant_id, candidates, conn)
    return apply_signals(candidates, signals, debate_mode())


def _candidate_key(c: "Candidate") -> str:
    return c.segment_id or c.route_id or f"{c.place}|{c.action}"


def debate_report(mode: str, before: list["Candidate"], after: list["Candidate"]) -> dict:
    """AA-1079 — what Debate did to one candidate pool, for the `t7_slate` job result (admin only; the tenant Slate
    read never sees it). Pure. `flagged` counts the flags riding on the kept candidates (rank mode); in cut mode the
    flags of the dropped ones are gone, so it is the number dropped (`dropped`) that tells."""
    kept_flagged = [c for c in after if getattr(c, "debate_flags", ())]
    by_flag: dict[str, int] = {}
    for c in kept_flagged:
        for f in c.debate_flags:
            by_flag[f] = by_flag.get(f, 0) + 1
    worst = sorted(kept_flagged, key=lambda c: (-(c.debate_severity or 0.0), _candidate_key(c)))
    return {
        "mode": mode,
        "candidates": len(before),
        "kept": len(after),
        "dropped": len(before) - len(after),
        "flagged": len(kept_flagged),
        "flagged_by": dict(sorted(by_flag.items())),
        "signals": [{"id": _candidate_key(c), "flags": list(c.debate_flags),
                     "severity": round(c.debate_severity, 3) if c.debate_severity is not None else None}
                    for c in worst[:REPORT_SIGNALS_MAX]],
    }
