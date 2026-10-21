"""AA-206 [AA-193·F1]: GPT-4.1 brand-fit judge for the S1 content graph.

Two-model generate–judge: Bedrock writes the content (generate_node); GPT-4.1 scores it for
brand fit and cross-brand distinctiveness here. The judge does NOT edit content — it only sets
``quality_score`` + ``feedback`` so the existing retry loop (should_retry → increment_retry →
generate) lets Bedrock fix the content against the judge's feedback.

Pure scoring node. All failure modes are non-blocking: any GPT error or parse failure logs and
leaves validate's ``quality_score`` untouched so the pipeline never stalls on the judge.

AA-631 — the actual GPT-4.1 brand-fit call (system prompt, prompt-building, score-combining)
now lives in `services/content_generation/brand_fit.py::score_brand_fit()`, shared with
Debate's slate.py brand-fit cut (AA-631's own design decision: reuse the EXISTING judge rather
than build a second, parallel LLM mechanism). This node keeps everything T2-graph-specific:
the retry-threshold feedback merge, `record_call_sync` LLM cost logging, and the non-blocking
try/except around the whole thing — none of which Debate needs or wants.
"""

import os

import structlog

from services.content_generation.stages import stage_for
from services.content_generation.brand_fit import (
    _JUDGE_SEED, _JUDGE_TEMPERATURE, billed_calls, has_brand_signals, run_billing, runs_signal,
    score_brand_fit,
)
from shared.llm_client.call_log import (
    billed_unusable_rows, record_billed_failure_sync, record_call_sync, record_calls_sync,
)
from services.quality.registry import threshold

logger = structlog.get_logger()

# The judge pass line (thresholds.yaml a1.judge_pass, the same pass_line as graph.MIN_QUALITY; AA-926).
_MIN_QUALITY = threshold("a1.judge_pass")
# A missing mission-hook caps the judge score just below the retry threshold so it forces at least
# one Bedrock retry instead of failing outright. A1 never uses it (AA-698); T2 uses it under the
# mission gates below, and only when the tenant has a mission (core idea) to look for.
_MISSION_ABSENT_CAP = threshold("judge.mission_absent_cap")

# AA-792 — what the T2 (tenant rewrite) judge gates on. Nghiệp chose option B (Gate 1, 14/10/2026):
# keep the mission as a gate, but the mission is the TENANT's own — its brand-pack core_idea
# (tenant_brand_rules.core_idea), named verbatim in the judge prompt — never Adventure Asia's.
#   "brand_fit_mission"  (default) brand_fit, capped at _MISSION_ABSENT_CAP when the tenant's
#                        mission is absent from the itineraries;
#   "brand_fit"          brand_fit_score alone, the A1 rule (AA-698);
#   "combined"           pre-AA-792 rule: min(brand_fit, cross_brand_distinct), mission-capped.
# A tenant with no core_idea has no mission: the cap never applies (any gate) and quality_signal
# logs mission_source="none". S234: mission was judged absent on 72/104 T2 calls (0 of those passed)
# when the judge had to guess what "mission-hook" meant. Flip with the env var (worker task def).
T2_JUDGE_GATE_ENV = "T2_JUDGE_GATE"
T2_JUDGE_GATES = ("brand_fit_mission", "brand_fit", "combined")
T2_JUDGE_GATE_DEFAULT = "brand_fit_mission"


def t2_judge_gate() -> str:
    mode = os.environ.get(T2_JUDGE_GATE_ENV, "").strip().lower()
    return mode if mode in T2_JUDGE_GATES else T2_JUDGE_GATE_DEFAULT


# AA-801 — how many judge calls make one T2 verdict. S235: the same draft scored brand_fit 4..8 across
# calls and mission_present flipped, so one call decided save-at-6 vs save-at-8 and drove retries.
#   "single"   (default) one call, plus the AA-714 median-of-3 only when it lands in 6..8 — prod as
#              before; stays the default until the AA-801 measurement (5 runs x 10 versions) is in;
#   "median3"  three parallel calls on every draft: median brand_fit/distinct, majority mission;
#              the three raw scores go into llm_call_log.quality_signal.judge_runs.
# Flip with the env var (worker task def), like T2_JUDGE_GATE.
T2_JUDGE_REPEAT_ENV = "T2_JUDGE_REPEAT"
T2_JUDGE_REPEATS = {"single": 1, "median3": 3}
T2_JUDGE_REPEAT_DEFAULT = "single"


def t2_judge_repeat() -> str:
    mode = os.environ.get(T2_JUDGE_REPEAT_ENV, "").strip().lower()
    return mode if mode in T2_JUDGE_REPEATS else T2_JUDGE_REPEAT_DEFAULT


# AA-801 option (b) — rubric anchors in the T2 judge prompt: what a 4 / 6 / 7 / 8 / 10 looks like for THIS
# tenant (built from its T0 brand pack) and a yes/no mission test that must quote the itinerary
# sentence proving it (brand_fit.build_rubric_anchors). Off by default; "on" / "1" / "true" turns it
# on (worker task def), like T2_JUDGE_REPEAT. Combines with median3. A1 is never affected.
T2_JUDGE_ANCHORS_ENV = "T2_JUDGE_ANCHORS"
_ON_VALUES = ("1", "true", "on", "yes")


def t2_judge_anchors() -> bool:
    return os.environ.get(T2_JUDGE_ANCHORS_ENV, "").strip().lower() in _ON_VALUES


def t2_mission(state: dict) -> str:
    """AA-792: the T2 tenant's own mission = its brand-pack core_idea ('' when it has none)."""
    from services.content_generation.batch_prompt import brand_pack_from_state
    return (brand_pack_from_state(state).get("core_idea") or "").strip()


def t2_judge_score(result, gate: str | None = None, *, has_mission: bool = True) -> float:
    """AA-792: the score a T2 tenant rewrite is gated on (see T2_JUDGE_GATE). `has_mission=False`
    (tenant without a core_idea) disables the mission cap under every gate."""
    gate = gate or t2_judge_gate()
    if gate == "combined":
        if has_mission:
            return result.judge_score   # min(brand_fit, distinct), mission-capped in brand_fit.py
        return min(result.brand_fit_score, result.cross_brand_distinct)
    score = result.brand_fit_score
    if gate == "brand_fit_mission" and has_mission and not result.mission_present:
        score = min(score, _MISSION_ABSENT_CAP)
    return score

# AA-631 — re-exported from brand_fit.py (the module that now actually uses them) so existing
# `from services.content_generation.judge_node import judge_node, _JUDGE_SEED, _JUDGE_TEMPERATURE`
# call sites (test_aa209_judge_determinism.py) keep working unchanged after the extraction.
__all__ = ["judge_node", "_JUDGE_SEED", "_JUDGE_TEMPERATURE"]


def a1_judge_score(result) -> float:
    """AA-698: the A1 master rewrite is brand-neutral by design (AA-535) — distinctiveness from
    other brands and a brand's mission-hook are T2's job, not A1's. So for A1 the gate is brand fit
    alone; cross_brand_distinct and mission_present are logged, not gated. Measured S204 on identical
    inputs: GPT-5.6 Luna scored cross_brand_distinct 2–6 where GPT-4.1 gave 8–9, and after that fix
    still returned mission_present=False on 2 of 3 smoke tours (it sees only the first 600 chars of
    the itinerary) — each sent the tour to HITL at 6.0 while validate scored 9.6–9.9."""
    return result.brand_fit_score


# AA-692 A1-4 — Jev tie-break near the retry line. Luna scores A1 brand fit 7–8 (S204), right on
# _MIN_QUALITY, so a one-point wobble decides retry/HITL. Inside ±_TIE_BAND of the line, and only when
# the judge (not validate) is what decides, the Noul a1_brand_fit settles it; enforce + confident only
# (ADR 0007). The judge still writes the feedback.
TIE_STAGE = "s1_judge_tiebreak"
# AA-699 T2-1 — the same Noul settles a tenant rewrite's tie (judge = combined brand score there),
# logged under its own stage and the tenant's id so the allow-list guard applies (design C2).
T2_TIE_STAGE = "t2_judge_tiebreak"
TIE_Q = "a1_brand_fit"
_TIE_BAND = threshold("judge.tie_band")
_BELOW_LINE = _MIN_QUALITY - 0.1


def in_tie_band(judge_score: float, validate_score: float) -> bool:
    return validate_score >= _MIN_QUALITY and abs(judge_score - _MIN_QUALITY) <= _TIE_BAND


def tie_break_score(judge_score: float, decision) -> float:
    """The judge score after Jev's verdict: accept lifts it to the line, reject drops it just below."""
    if decision.accepted(TIE_Q):
        return max(judge_score, _MIN_QUALITY)
    if decision.rejected(TIE_Q):
        return min(judge_score, _BELOW_LINE)
    return judge_score


def _tie_state(brand_profile: dict, generated: dict) -> dict:
    highlights = generated.get("highlights") or []
    if isinstance(highlights, str):
        highlights = [highlights]
    return {
        "brand": {
            "core_idea": brand_profile.get("brand_core_idea") or "",
            "who_it_is_for": brand_profile.get("brand_customer_segment") or "",
            "what_they_want": brand_profile.get("brand_customer_mindset") or "",
            "voice_examples": [v for v in (brand_profile.get("brand_voice_examples") or []) if v][:3],
        },
        "content": {
            "name": generated.get("name") or "",
            "subtitle": generated.get("subtitle") or "",
            "summary": str(generated.get("summary") or "")[:1200],
            "highlights": [str(h) for h in highlights][:8],
        },
    }


def _tie_break(state: dict, generated: dict, judge_score: float) -> tuple[float, dict | None]:
    from shared.llm_client.decide import decide_sync
    tour_id = str((state.get("tour") or {}).get("tour_id") or "")
    attempt = state.get("retry_count", 0)
    if state.get("is_tenant_rewrite"):
        tenant_id = state.get("tenant_id")
        decision = decide_sync(T2_TIE_STAGE, f"t2_judge:{tenant_id}:{tour_id}:{attempt}",
                               _tie_state(state, generated), [TIE_Q], tenant_id=tenant_id)
    else:
        decision = decide_sync(TIE_STAGE, f"a1_judge:{tour_id}:{attempt}", _tie_state(state, generated), [TIE_Q])
    v = decision.verdicts.get(TIE_Q)
    new_score = tie_break_score(judge_score, decision)
    info = {"zone": v and v.zone, "p": v and v.probability, "mode": v and v.mode,
            "before": judge_score, "after": new_score}
    logger.info("judge_tiebreak", tour_id=tour_id, **info)
    return new_score, info


def _judge_rows(stage: str, result, verdict_signal: dict) -> list[dict]:
    """AA-927: the llm_call_log rows of one verdict — one per billed judge call.

    A single call: one row, the verdict, with the result's own billing (shape unchanged). Several calls
    (AA-714 near-band repeat, AA-801 median3): the verdict row carries `verdict_signal` + `judge_calls`
    with run 1's billing only; runs 2..n carry `{judge_run, of, verdict_row: false, brand_fit, distinct,
    mission}` (no ok-rate key); calls billed then dropped (median3 malformed reply) are billed_unusable
    rows. Row costs sum to `result.cost_usd`."""
    runs = [r for r in (getattr(result, "runs", None) or ()) if isinstance(r, dict) and "brand_fit" in r]
    failed = list(getattr(result, "billed_failed", None) or ())
    n = len(runs) + len(failed)
    if runs:
        first = runs[0]
        billing = {k: first.get(k) for k in ("model", "tokens_in", "tokens_out", "cost_usd", "stop_reason",
                                             "account", "fallback_used")}
    else:
        billing = {"model": result.model_used, "tokens_in": result.input_tokens,
                   "tokens_out": result.output_tokens, "cost_usd": result.cost_usd,
                   "stop_reason": result.stop_reason, "account": result.account,
                   "fallback_used": result.fallback_used}
    rows = [dict(stage=stage, role="judge", **billing,
                 quality_signal={**verdict_signal, **({"judge_calls": n} if n > 1 else {})})]
    for i, r in enumerate(runs[1:], start=2):
        rows.append(dict(
            stage=stage, role="judge", model=r.get("model"), tokens_in=r.get("tokens_in"),
            tokens_out=r.get("tokens_out"), cost_usd=r.get("cost_usd"), stop_reason=r.get("stop_reason"),
            account=r.get("account"), fallback_used=r.get("fallback_used"),
            quality_signal={"judge_run": i, "of": n, "verdict_row": False, "brand_fit": r.get("brand_fit"),
                            "distinct": r.get("distinct"), "mission": r.get("mission")}))
    rows += billed_unusable_rows(stage=stage, role="judge", billed=failed, error="judge reply unusable",
                                 quality_signal_extra={"judge_run_failed": True, "of": n})
    return rows


def _write_judge_rows(rows: list[dict]) -> None:
    """One row → `record_call_sync` (unchanged path); several → one connection, one transaction."""
    if len(rows) == 1:
        record_call_sync(**rows[0])
    elif rows:
        record_calls_sync(rows)


def _billed_rows_of(stage: str, result, error: str) -> list[dict]:
    """AA-927: every billed call behind `result` as billed_unusable rows (the verdict was lost after the
    calls returned: tie-break / feedback code raised)."""
    runs = [r for r in (getattr(result, "runs", None) or ()) if isinstance(r, dict) and "brand_fit" in r]
    billed = [{k: r.get(k) for k in ("model", "tokens_in", "tokens_out", "cost_usd", "stop_reason", "account",
                                     "fallback_used")} for r in runs] or [run_billing(result)]
    billed += list(getattr(result, "billed_failed", None) or ())
    return billed_unusable_rows(stage=stage, role="judge", billed=billed, error=error,
                                quality_signal_extra={"post_call_error": True,
                                                      "brand_fit_score": getattr(result, "brand_fit_score", None)})


def judge_node(state: dict) -> dict:
    """AA-206: GPT-4.1 brand-fit judge. Runs after validate, before should_retry.

    Sets ``quality_score`` = min(validate score, judge score) so the brand gate stacks on top of
    validate's structural gate, and merges judge ``feedback`` into ``feedback`` for the retry.
    Any failure is non-blocking: keeps validate's score and does not raise.
    """
    validate_score = state.get("quality_score", 0.0)
    generated = state.get("generated", {})

    # Skip judging when there is no brand differentiation profile (legacy/default brands): scoring
    # cross-brand distinctiveness against an empty profile is meaningless and would burn GPT cost.
    if not generated or not has_brand_signals(state):
        logger.info("judge_skipped", reason="no_generated" if not generated else "no_brand_profile")
        return state

    # AA-927: one llm_call_log row per billed judge call, written even when the code after the calls
    # raises (`logged` guards the except branch below).
    result = None
    logged = False
    log_stage = stage_for(state, "s1_judge")
    try:
        is_t2 = bool(state.get("is_tenant_rewrite"))
        if is_t2:
            mission = t2_mission(state)          # AA-792: the tenant's own core_idea, never AA's
            repeat_mode = t2_judge_repeat()      # AA-801
            anchors = t2_judge_anchors()         # AA-801 option (b)
            result = score_brand_fit(state, generated, mission_absent_cap=_MISSION_ABSENT_CAP,
                                     stage=stage_for(state, "s1_judge"), mission=mission or None,
                                     **({"repeat": T2_JUDGE_REPEATS[repeat_mode]}
                                        if repeat_mode != T2_JUDGE_REPEAT_DEFAULT else {}),
                                     **({"anchors": True} if anchors else {}))
            gate = t2_judge_gate()
            judge_score = t2_judge_score(result, gate, has_mission=bool(mission))
            mission_source = "tenant_core_idea" if mission else "none"
            if not mission and gate != "brand_fit":
                logger.info("judge_mission_not_gated", reason="tenant_has_no_core_idea", gate=gate,
                            tenant_id=state.get("tenant_id"))
        else:
            result = score_brand_fit(state, generated, mission_absent_cap=_MISSION_ABSENT_CAP,
                                     stage=stage_for(state, "s1_judge"))  # AA-763
            gate = "brand_fit"
            judge_score = a1_judge_score(result)
            mission_source = None
            repeat_mode = None
            anchors = False
        tiebreak = None
        if in_tie_band(judge_score, validate_score):
            judge_score, tiebreak = _tie_break(state, generated, judge_score)   # AA-692 A1-4, AA-699 T2-1

        # Stack the brand gate on top of validate's structural gate — never let high brand-fit mask a
        # structurally broken output, and vice-versa.
        new_score = min(validate_score, judge_score)

        # Merge judge feedback into the retry feedback only when we're below threshold (a retry will
        # actually fire). Preserve validate's feedback so Bedrock sees both signals.
        feedback = state.get("feedback", "") or ""
        if new_score < _MIN_QUALITY and result.feedback:
            feedback = f"{feedback}; {result.feedback}".strip("; ") if feedback else result.feedback

        logger.info("judge_done", brand_fit=result.brand_fit_score,
                    cross_brand_distinct=result.cross_brand_distinct,
                    mission_present=result.mission_present, judge_score=judge_score,
                    validate_score=validate_score, new_score=new_score, gate=gate,
                    distinct_gates=gate == "combined")

        _write_judge_rows(_judge_rows(log_stage, result, {
                "judge_score": judge_score, "brand_fit_score": result.brand_fit_score,
                "cross_brand_distinct": result.cross_brand_distinct,
                "mission_present": result.mission_present,
                "passed": new_score >= _MIN_QUALITY,
                "gate": gate,  # AA-792: what judge_score was gated on
                # AA-792: T2 only — "tenant_core_idea" (mission named in the prompt, may cap) or
                # "none" (tenant has no core_idea: mission never caps). Absent for A1 (unchanged).
                **({"mission_source": mission_source,
                    "mission_gated": mission_source == "tenant_core_idea" and gate != "brand_fit"}
                   if is_t2 else {}),
                # AA-793: which graph attempt this verdict scored and whether that draft revised an
                # earlier one — per-attempt judge rows prove "no saved draft below an earlier pass".
                **({"attempt": state.get("retry_count", 0),
                    "revision": bool(state.get("previous_draft"))} if is_t2 else {}),
                # AA-801: the raw calls behind a median3 verdict (absent in the default mode).
                **({"judge_runs": runs_signal(result, repeat_mode)}
                   if repeat_mode and repeat_mode != T2_JUDGE_REPEAT_DEFAULT else {}),
                # AA-801 (b): anchored prompt + the quoted mission sentence (absent when the flag is off).
                **({"judge_anchors": True,
                    "mission_evidence": (getattr(result, "mission_evidence", "") or "")[:300],
                    "evidence_verified": getattr(result, "evidence_verified", None)}
                   if anchors else {}),
            }))
        logged = True
        return {
            **state,
            "quality_score": new_score,
            "feedback": feedback,
            "judge_brand_fit": result.brand_fit_score,
            "judge_cross_brand_distinct": result.cross_brand_distinct,
            "judge_mission_present": result.mission_present,
            "judge_feedback": result.feedback,
            # AA-209: expose the capped judge score (the value min()'d against validate) so the
            # persist path can record exactly what drove score_overall, not just the inputs.
            "judge_score": judge_score,
            "judge_tiebreak": tiebreak,
            "cost_usd": state.get("cost_usd", 0) + result.cost_usd,
        }

    except Exception as e:
        # Non-blocking: keep validate's score so the pipeline continues unaffected by judge failure.
        logger.warning("judge_failed_graceful", error=str(e))
        if result is None:
            # AA-927: the judge raised after billed calls (malformed reply, a repeat call failing).
            record_billed_failure_sync(stage=log_stage, role="judge", billed=billed_calls(e),
                                       error=str(e))
        elif not logged:
            # AA-927: the calls returned, then the tie-break / feedback code raised.
            rows = _billed_rows_of(log_stage, result, str(e))
            if rows:
                record_calls_sync(rows)
        return state
