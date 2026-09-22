"""AA-631 — brand-fit scoring, extracted from judge_node.py (AA-206) so it has ONE caller-
agnostic implementation instead of two independent copies drifting apart.

judge_node.py (T2 rewrite's post-generate gate) and slate.py's Debate stage (AA-631, a
pre-Slate cut BEFORE a tenant even picks a candidate) both need "does this content read as
THIS brand's distinct angle" — the issue's own design decision (see AA-631) is that Debate
reuses judge_node's EXISTING brand-fit judge rather than building a second, parallel LLM
mechanism: "a Segment judged brand-fit by Debate but then rejected by judge_node after writing
(or vice versa) would be a real inconsistency a second, parallel LLM judgement invites."

Nothing in this module is T2-graph-specific — no `ContentState`, no LangGraph, no retry/
feedback-merge logic (that stays in judge_node.py, which is a genuinely different concern:
"decide whether to make Bedrock retry" is not "score brand fit"). Callers pass a plain
`brand_profile` dict (the 5 `brand_*` fields) and a `generated` dict (the content to score) and
get a `BrandFitResult` back — same shape judge_node.py already built internally, now shared.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

import structlog

from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest

logger = structlog.get_logger()

# AA-209: fixed seed + low temperature make the judge reproducible.
_JUDGE_TEMPERATURE = 0.1
_JUDGE_SEED = 42

JUDGE_SYSTEM = """You are a brand-fit judge for Adventure Asia's B2B content pipeline.
You do NOT rewrite content. You score how well a tour rewrite reflects ONE specific client brand's
distinct angle, then give concrete, actionable feedback the writer can use to make it more on-brand.
Be strict: generic travel copy that would fit any brand must score low. Return JSON only."""


@dataclass(frozen=True)
class BrandFitResult:
    """Everything judge_node.py's try block used to compute, now the shared return shape."""
    brand_fit_score: float
    cross_brand_distinct: float
    mission_present: bool
    feedback: str
    judge_score: float          # min(brand_fit_score, cross_brand_distinct), capped if no mission
    model_used: str
    cost_usd: float
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    stop_reason: Optional[str]
    account: Optional[str]
    fallback_used: Optional[bool]


def _coerce_score(value, default: float = 0.0) -> float:
    """Coerce a judge score to a float in [0, 10]; fall back to ``default`` on bad input."""
    try:
        return max(0.0, min(10.0, float(value)))
    except (TypeError, ValueError):
        return default


def has_brand_signals(brand_profile: dict) -> bool:
    """The exact guard judge_node.py used to inline (and graph.py's `_build_brand_diff_block`
    duplicated separately, per judge_node.py's own comment "inlined to avoid a circular
    import") — scoring against an empty brand profile is meaningless (and, for the LLM path,
    burns cost for nothing). Checks only `brand_core_idea`/`brand_customer_mindset`/
    `brand_voice_examples` — NOT `brand_customer_segment`/`brand_good_examples` — preserved
    exactly as-is rather than "fixed", since callers already depend on this exact behavior and
    widening it is a separate decision, not part of this extraction."""
    return bool(
        (brand_profile.get("brand_core_idea") or "")
        or (brand_profile.get("brand_customer_mindset") or "")
        or [v for v in (brand_profile.get("brand_voice_examples") or []) if v]
    )


def _build_judge_prompt(brand_profile: dict, generated: dict) -> str:
    """Assemble the judge user-prompt: this brand's profile + the content to score. Verbatim
    port of judge_node.py's own `_build_judge_prompt()` (same field names, same prompt text) —
    unchanged so an existing T2 rewrite call and a new Debate call score identically for the
    same inputs, which is the whole point of sharing this module."""
    voice_ex = [v for v in (brand_profile.get("brand_voice_examples") or []) if v]

    highlights = generated.get("highlights") or []
    if isinstance(highlights, str):
        highlights = [highlights]
    highlights_text = "\n".join(f"- {h}" for h in highlights)

    return f"""Score this tour rewrite against THIS client brand's distinct angle.

BRAND PROFILE (the rewrite must reflect THIS, not generic travel copy):
- Core idea: {brand_profile.get("brand_core_idea", "") or "(none)"}
- Who this is for: {brand_profile.get("brand_customer_segment", "") or "(none)"}
- What this traveller wants: {brand_profile.get("brand_customer_mindset", "") or "(none)"}
- Voice (tone words): {", ".join(voice_ex) or "(none)"}
- Example of this brand's voice on one moment: {brand_profile.get("brand_good_examples", "") or "(none)"}

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


def score_brand_fit(
    brand_profile: dict, generated: dict, *, mission_absent_cap: float = 6.0,
) -> BrandFitResult:
    """The GPT-4.1 brand-fit call itself — extracted verbatim from judge_node.py's try block
    (same system prompt, same model_tier pin, same temperature/seed, same score-combining
    rule). Raises on any failure (LLM error, JSON parse failure) — callers decide how to
    degrade: judge_node.py catches and returns validate's score unchanged (non-blocking gate);
    Debate (slate.py) is expected to do the same ("any failure/malformed ruling and the
    rules-based list stands unchanged" — the origin's own safety rule, AA-631's issue).

    `mission_absent_cap` — judge_node.py hardcodes 6.0 (`_MISSION_ABSENT_CAP`, "just below the
    retry threshold" MIN_QUALITY=7.0). Debate has no retry-threshold concept of its own, so this
    is a parameter here rather than a second hardcoded constant — judge_node.py passes its own
    6.0 explicitly to keep its exact existing behavior unchanged by this extraction.
    """
    request = LLMRequest(
        system_prompt=JUDGE_SYSTEM,
        user_prompt=_build_judge_prompt(brand_profile, generated),
        # AA-518: "s1_judge" stage config is seeded to gpt-4.1 today — kept as an explicit
        # model_tier too (not just stage) since this judge must NEVER silently drift onto a
        # Bedrock tier (ADR-2026-014/027: judge must stay a different vendor than the writer).
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
    feedback = (result.get("feedback") or "").strip()

    judge_score = min(brand_fit, distinct)
    if not mission_present:
        judge_score = min(judge_score, mission_absent_cap)

    return BrandFitResult(
        brand_fit_score=brand_fit,
        cross_brand_distinct=distinct,
        mission_present=mission_present,
        feedback=feedback,
        judge_score=judge_score,
        model_used=resp.model_used,
        cost_usd=resp.cost_usd,
        input_tokens=getattr(resp, "input_tokens", None),
        output_tokens=getattr(resp, "output_tokens", None),
        stop_reason=getattr(resp, "stop_reason", None),
        account=getattr(resp, "satellite_account", None),
        fallback_used=getattr(resp, "fallback_used", None),
    )
