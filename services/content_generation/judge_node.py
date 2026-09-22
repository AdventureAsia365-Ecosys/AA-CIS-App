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

        # Stack the brand gate on top of validate's structural gate — never let high brand-fit mask a
        # structurally broken output, and vice-versa.
        new_score = min(validate_score, result.judge_score)

        # Merge judge feedback into the retry feedback only when we're below threshold (a retry will
        # actually fire). Preserve validate's feedback so Bedrock sees both signals.
        feedback = state.get("feedback", "") or ""
        if new_score < _MIN_QUALITY and result.feedback:
            feedback = f"{feedback}; {result.feedback}".strip("; ") if feedback else result.feedback

        logger.info("judge_done", brand_fit=result.brand_fit_score,
                    cross_brand_distinct=result.cross_brand_distinct,
                    mission_present=result.mission_present, judge_score=result.judge_score,
                    validate_score=validate_score, new_score=new_score)

        record_call_sync(
            stage="s1_judge", role="judge", model=result.model_used,
            tokens_in=result.input_tokens, tokens_out=result.output_tokens,
            cost_usd=result.cost_usd, tenant_id=None,
            quality_signal={
                "judge_score": result.judge_score, "brand_fit_score": result.brand_fit_score,
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
            "judge_score": result.judge_score,
            "cost_usd": state.get("cost_usd", 0) + result.cost_usd,
        }

    except Exception as e:
        # Non-blocking: keep validate's score so the pipeline continues unaffected by judge failure.
        logger.warning("judge_failed_graceful", error=str(e))
        return state
