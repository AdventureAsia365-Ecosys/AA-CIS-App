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

import structlog

from services.content_generation.brand_fit import (
    _JUDGE_SEED, _JUDGE_TEMPERATURE, has_brand_signals, score_brand_fit,
)
from shared.llm_client.call_log import record_call_sync

logger = structlog.get_logger()

# Mirror graph.MIN_QUALITY (7.0). Defined locally to avoid a circular import (graph imports this node).
_MIN_QUALITY = 7.0
# A missing mission-hook caps the judge score just below the retry threshold so it forces at least
# one Bedrock retry instead of failing outright.
_MISSION_ABSENT_CAP = 6.0

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
TIE_Q = "a1_brand_fit"
_TIE_BAND = 0.5
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
    decision = decide_sync(TIE_STAGE, f"a1_judge:{tour_id}:{attempt}", _tie_state(state, generated), [TIE_Q])
    v = decision.verdicts.get(TIE_Q)
    new_score = tie_break_score(judge_score, decision)
    info = {"zone": v and v.zone, "p": v and v.probability, "mode": v and v.mode,
            "before": judge_score, "after": new_score}
    logger.info("judge_tiebreak", tour_id=tour_id, **info)
    return new_score, info


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

    try:
        result = score_brand_fit(state, generated, mission_absent_cap=_MISSION_ABSENT_CAP)
        judge_score = a1_judge_score(result) if not state.get("is_tenant_rewrite") else result.judge_score
        tiebreak = None
        if not state.get("is_tenant_rewrite") and in_tie_band(judge_score, validate_score):
            judge_score, tiebreak = _tie_break(state, generated, judge_score)   # AA-692 A1-4

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
                    validate_score=validate_score, new_score=new_score,
                    distinct_gates=bool(state.get("is_tenant_rewrite")))

        record_call_sync(
            stage="s1_judge", role="judge", model=result.model_used,
            tokens_in=result.input_tokens, tokens_out=result.output_tokens,
            cost_usd=result.cost_usd, tenant_id=None,
            quality_signal={
                "judge_score": judge_score, "brand_fit_score": result.brand_fit_score,
                "cross_brand_distinct": result.cross_brand_distinct,
                "mission_present": result.mission_present,
                "passed": new_score >= _MIN_QUALITY,
            },
            stop_reason=result.stop_reason,
            account=result.account,
            fallback_used=result.fallback_used,
        )
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
        return state
