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
import re
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


_DAY_SPLIT = re.compile(r"\n\s*\n(?=\s*(?:\*\*)?\s*Day\s*\d)", re.IGNORECASE)


def itinerary_digest(itineraries, per_day: int = 350, total: int = 6000) -> str:
    """Every day of the itinerary, each cut to `per_day` characters (header kept).

    S207 Korea wave: the judge got `itineraries[:600]` — only Day 1 of a 9-15 day tour — said
    "only Day 1 is shown / Day 3 is cut off", set mission_present=false and the mission cap put 9 of
    30 tours at 6.0 (HITL). Each day's framing is what `mission_present` asks about, so each day
    must be visible; cutting per day keeps the prompt bounded."""
    if isinstance(itineraries, str):
        try:
            parsed = json.loads(itineraries)
            if isinstance(parsed, list):
                itineraries = parsed
        except ValueError:
            pass
    if isinstance(itineraries, list):
        blocks = [f"Day {d.get('day')} — {d.get('title') or ''}\n{d.get('body') or d.get('description') or ''}".strip()
                  if isinstance(d, dict) else str(d) for d in itineraries]
    else:
        blocks = [b.strip() for b in _DAY_SPLIT.split(str(itineraries or "")) if b.strip()]
    cut = [b if len(b) <= per_day else b[:per_day].rstrip() + " …" for b in blocks]
    out = "\n\n".join(cut)
    return out if len(out) <= total else out[:total].rstrip() + " … [later days omitted for length]"


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
ITINERARIES (every day, each shortened):
{itinerary_digest(generated.get("itineraries"))}
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


CANDIDATE_JUDGE_SYSTEM = """You are a brand-fit judge for Adventure Asia's B2B content pipeline.
You do NOT write content. Before anything is written, you decide whether a candidate TOPIC (a place
and what travellers do there, shown as raw catalogue evidence) is worth proposing to ONE specific
client brand. Judge the topic, not the wording: the evidence is unedited source text and is expected
to be plain and repetitive. Return JSON only."""


def _build_candidate_prompt(brand_profile: dict, candidate: dict) -> str:
    """Debate (AA-631) prompt: does this TOPIC suit this brand? The rewrite prompt above asks
    whether finished copy reads on-brand and whether the brand's mission surfaces in each day's
    itinerary — raw atom text can never pass that, so every fresh Debate candidate scored ≤ 6
    (28/09/2026: 2 of 2 scored 1.0 with feedback about "repeated placeholder text")."""
    voice_ex = [v for v in (brand_profile.get("brand_voice_examples") or []) if v]
    evidence = "\n".join(f"- {e}" for e in candidate.get("evidence") or []) or "- (none)"
    return f"""Decide whether this candidate topic is a good fit for THIS client brand's content.

BRAND PROFILE:
- Core idea: {brand_profile.get("brand_core_idea", "") or "(none)"}
- Who this is for: {brand_profile.get("brand_customer_segment", "") or "(none)"}
- What this traveller wants: {brand_profile.get("brand_customer_mindset", "") or "(none)"}
- Voice (tone words): {", ".join(voice_ex) or "(none)"}

CANDIDATE TOPIC:
- Place: {candidate.get("place") or "(unknown)"}
- What travellers do: {candidate.get("action") or "(unspecified)"}
RAW EVIDENCE (catalogue source text, unedited):
{evidence}

SCORE on these axes:
- brand_fit_score (1-10): would this brand's travellers genuinely want this experience? A topic
  that clashes with who they are or what they want scores low; a natural match scores high.
- cross_brand_distinct (1-10): does the evidence give this brand real material for its own angle
  (specific detail its travellers care about), rather than something only generic copy could cover?
- feedback (string): one or two sentences on why the topic fits or does not fit this brand.

Return JSON ONLY, no markdown, exactly:
{{"brand_fit_score": <int>, "cross_brand_distinct": <int>, "feedback": "<string>"}}"""


def score_candidate_fit(brand_profile: dict, candidate: dict) -> BrandFitResult:
    """Debate's brand-fit call (AA-631): same stage route, temperature/seed and result shape as
    `score_brand_fit()`, but scores a candidate topic from raw evidence instead of finished copy.
    `candidate` = {"place", "action", "evidence": [str, ...]}. There is no mission axis — nothing
    has been written yet — so `mission_present` is always True and never caps the score.
    Raises on any failure, like `score_brand_fit()`."""
    return _judge(CANDIDATE_JUDGE_SYSTEM, _build_candidate_prompt(brand_profile, candidate),
                  mission_absent_cap=None)


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
    return _judge(JUDGE_SYSTEM, _build_judge_prompt(brand_profile, generated),
                  mission_absent_cap=mission_absent_cap)


def _judge(system_prompt: str, user_prompt: str, *, mission_absent_cap: float | None) -> BrandFitResult:
    request = LLMRequest(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        # AA-659 / ADR 0006: the model comes from the "s1_judge" stage route. The judge-vendor ≠
        # writer-vendor rule (ADR-2026-014/027) is enforced when the route is saved in admin.
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
    if mission_absent_cap is not None and not mission_present:
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
