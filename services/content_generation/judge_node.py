"""AA-206 [AA-193·F1]: GPT-4.1 brand-fit judge for the S1 content graph.

Two-model generate–judge: Bedrock writes the content (generate_node); GPT-4.1 scores it for
brand fit and cross-brand distinctiveness here. The judge does NOT edit content — it only sets
``quality_score`` + ``feedback`` so the existing retry loop (should_retry → increment_retry →
generate) lets Bedrock fix the content against the judge's feedback.

Pure scoring node. All failure modes are non-blocking: any GPT error or parse failure logs and
leaves validate's ``quality_score`` untouched so the pipeline never stalls on the judge.
"""

import json
import structlog

from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest
from shared.llm_client.call_log import record_call_sync

logger = structlog.get_logger()

# Mirror graph.MIN_QUALITY (7.0). Defined locally to avoid a circular import (graph imports this node).
_MIN_QUALITY = 7.0
# A missing mission-hook caps the judge score just below the retry threshold so it forces at least
# one Bedrock retry instead of failing outright.
_MISSION_ABSENT_CAP = 6.0
# AA-209: fixed seed + low temperature make the judge reproducible — same content no longer yields
# different score_overall across versions (root cause of v4=7.0 vs v5=9.0 on identical sub-scores).
_JUDGE_TEMPERATURE = 0.1
_JUDGE_SEED = 42

JUDGE_SYSTEM = """You are a brand-fit judge for Adventure Asia's B2B content pipeline.
You do NOT rewrite content. You score how well a tour rewrite reflects ONE specific client brand's
distinct angle, then give concrete, actionable feedback the writer can use to make it more on-brand.
Be strict: generic travel copy that would fit any brand must score low. Return JSON only."""


def _coerce_score(value, default: float = 0.0) -> float:
    """Coerce a judge score to a float in [0, 10]; fall back to ``default`` on bad input."""
    try:
        return max(0.0, min(10.0, float(value)))
    except (TypeError, ValueError):
        return default


def _build_judge_prompt(state: dict) -> str:
    """Assemble the judge user-prompt: this brand's profile + the generated content to score."""
    generated = state.get("generated", {})
    voice_ex = [v for v in (state.get("brand_voice_examples") or []) if v]

    highlights = generated.get("highlights") or []
    if isinstance(highlights, str):
        highlights = [highlights]
    highlights_text = "\n".join(f"- {h}" for h in highlights)

    return f"""Score this tour rewrite against THIS client brand's distinct angle.

BRAND PROFILE (the rewrite must reflect THIS, not generic travel copy):
- Core idea: {state.get("brand_core_idea", "") or "(none)"}
- Who this is for: {state.get("brand_customer_segment", "") or "(none)"}
- What this traveller wants: {state.get("brand_customer_mindset", "") or "(none)"}
- Voice (tone words): {", ".join(voice_ex) or "(none)"}
- Example of this brand's voice on one moment: {state.get("brand_good_examples", "") or "(none)"}

GENERATED CONTENT TO JUDGE:
NAME: {generated.get("name")}
SUBTITLE: {generated.get("subtitle")}
SUMMARY: {generated.get("summary")}
HIGHLIGHTS:
{highlights_text}
ITINERARIES: {str(generated.get("itineraries") or "")[:600]}
SEO_TITLE: {generated.get("seo_title")}
SEO_META: {generated.get("seo_meta")}

SCORE on these axes:
- brand_fit_score (1-10): does the content reflect THIS brand's specific angle and the traveller
  mindset above — not a generic register that fits any travel brand?
- cross_brand_distinct (1-10): if the SAME tour were rewritten for a DIFFERENT brand, how clearly
  would this version differ? A synonym swap of a generic description scores low.
- mission_present (true/false): does this brand's mission-hook actually surface in the ITINERARIES
  (each day's framing), not only in the summary?
- feedback (string): specific, concrete changes needed to make the content read more distinctly as
  THIS brand. If everything is on-brand, return an empty string.

Return JSON ONLY, no markdown, exactly:
{{"brand_fit_score": <int>, "cross_brand_distinct": <int>, "mission_present": <bool>, "feedback": "<string>"}}"""


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
    # Same signal guard as graph._build_brand_diff_block (inlined to avoid a circular import).
    has_brand_signals = bool(
        (state.get("brand_core_idea") or "")
        or (state.get("brand_customer_mindset") or "")
        or [v for v in (state.get("brand_voice_examples") or []) if v]
    )
    if not generated or not has_brand_signals:
        logger.info("judge_skipped", reason="no_generated" if not generated else "no_brand_profile")
        return state

    try:
        request = LLMRequest(
            system_prompt=JUDGE_SYSTEM,
            user_prompt=_build_judge_prompt(state),
            # AA-518: "s1_judge" stage config is seeded to gpt-4.1 today, matching this prior
            # hardcoded literal — kept as an explicit model_tier too (not just stage) since this
            # judge must NEVER silently drift onto a Bedrock tier if the stage config is ever
            # misconfigured (ADR-2026-014/027: judge must stay a different vendor than the
            # writer). stage= is passed for logging/attribution even though it isn't consulted
            # here (model_tier already pins the tier).
            model_tier="gpt-4.1",
            stage="s1_judge",
            temperature=_JUDGE_TEMPERATURE,
            seed=_JUDGE_SEED,
        )
        client = LLMClient()
        resp = client.generate(request)

        raw = resp.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        result = json.loads(raw)

        brand_fit = _coerce_score(result.get("brand_fit_score"))
        distinct = _coerce_score(result.get("cross_brand_distinct"))
        mission_present = bool(result.get("mission_present", True))
        judge_feedback = (result.get("feedback") or "").strip()

        # Judge score = the weaker of brand-fit and distinctiveness; a missing mission-hook caps it
        # below the retry threshold to force at least one Bedrock retry.
        judge_score = min(brand_fit, distinct)
        if not mission_present:
            judge_score = min(judge_score, _MISSION_ABSENT_CAP)

        # Stack the brand gate on top of validate's structural gate — never let high brand-fit mask a
        # structurally broken output, and vice-versa.
        new_score = min(validate_score, judge_score)

        # Merge judge feedback into the retry feedback only when we're below threshold (a retry will
        # actually fire). Preserve validate's feedback so Bedrock sees both signals.
        feedback = state.get("feedback", "") or ""
        if new_score < _MIN_QUALITY and judge_feedback:
            feedback = f"{feedback}; {judge_feedback}".strip("; ") if feedback else judge_feedback

        logger.info("judge_done", brand_fit=brand_fit, cross_brand_distinct=distinct,
                    mission_present=mission_present, judge_score=judge_score,
                    validate_score=validate_score, new_score=new_score)

        record_call_sync(
            stage="s1_judge", role="judge", model=resp.model_used,
            tokens_in=getattr(resp, "input_tokens", None), tokens_out=getattr(resp, "output_tokens", None),
            cost_usd=resp.cost_usd, tenant_id=None,
            quality_signal={
                "judge_score": judge_score, "brand_fit_score": brand_fit,
                "cross_brand_distinct": distinct, "mission_present": mission_present,
                "passed": new_score >= _MIN_QUALITY,
            },
            stop_reason=getattr(resp, "stop_reason", None),
            account=getattr(resp, "satellite_account", None),
            fallback_used=getattr(resp, "fallback_used", None),
        )
        return {
            **state,
            "quality_score": new_score,
            "feedback": feedback,
            "judge_brand_fit": brand_fit,
            "judge_cross_brand_distinct": distinct,
            "judge_mission_present": mission_present,
            "judge_feedback": judge_feedback,
            # AA-209: expose the capped judge score (the value min()'d against validate) so the
            # persist path can record exactly what drove score_overall, not just the inputs.
            "judge_score": judge_score,
            "cost_usd": state.get("cost_usd", 0) + resp.cost_usd,
        }

    except Exception as e:
        # Non-blocking: keep validate's score so the pipeline continues unaffected by judge failure.
        logger.warning("judge_failed_graceful", error=str(e))
        return state
