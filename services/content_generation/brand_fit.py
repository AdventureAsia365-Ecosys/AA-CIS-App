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

import contextvars
import dataclasses
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

import structlog

from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest

logger = structlog.get_logger()

# AA-209: fixed seed + low temperature make the judge reproducible. A reasoning model (Luna) ignores
# both, so AA-714 adds a repeat-and-take-the-median layer for it instead (see score_brand_fit).
_JUDGE_TEMPERATURE = 0.1
_JUDGE_SEED = 42

# AA-714 layer 2 — a reasoning judge (no seed) wobbles ±1 around MIN_QUALITY=7; a lone call then
# decides retry/HITL on noise. When the first score lands in this band AND the model does not take a
# seed, score two more times and keep the median. A model that honours the seed (GPT-4.1) is already
# reproducible, so it is scored once. Layer 1 = the single call; layer 3 = the Jev tie-break in
# judge_node, which only fires when a score is still exactly on the line after this.
_REPEAT_BAND = (6.0, 8.0)          # inclusive; MIN_QUALITY (7.0) sits in the middle
_REPEAT_EXTRA = 2                  # 2 more calls -> median of 3


def _median(values: list[float]) -> float:
    """Statistical median: the middle value for an odd count, the mean of the two middle values for
    an even count (only reached when a median3 call failed and two results remain)."""
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2

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
    # AA-801: the individual judge calls behind an aggregated verdict (median3 mode); () for a
    # single call or for the AA-714 near-band repeat (its log line is unchanged).
    runs: tuple = ()


def aggregate_runs(results: list[BrandFitResult], *, mission_absent_cap: Optional[float],
                   failed: int = 0, failed_cost_usd: float = 0.0) -> BrandFitResult:
    """AA-801 / AA-714: one verdict from several judge calls on the same draft.

    brand_fit and cross_brand_distinct = median (`_median`); mission_present = strict majority, a
    tie counts as absent (conservative: it can cap, it never lifts); judge_score = min(brand_fit,
    distinct), mission-capped like a single call. Feedback comes from the run whose brand_fit is
    closest to the median (the first one on a tie). Cost and tokens are summed: every call is billed,
    including `failed_cost_usd` = the known spend of calls that were billed but then failed (e.g. a
    malformed JSON reply). Model/account/stop_reason come from the first run. Raises ValueError on
    an empty list."""
    if not results:
        raise ValueError("aggregate_runs needs at least one result")
    brand_fits = [r.brand_fit_score for r in results]
    distincts = [r.cross_brand_distinct for r in results]
    missions = [bool(r.mission_present) for r in results]
    brand_fit = _median(brand_fits)
    distinct = _median(distincts)
    mission = sum(missions) > len(results) / 2
    judge_score = min(brand_fit, distinct)
    if mission_absent_cap is not None and not mission:
        judge_score = min(judge_score, mission_absent_cap)
    picked = min(results, key=lambda r: abs(r.brand_fit_score - brand_fit))
    first = results[0]
    runs = tuple(
        {"brand_fit": r.brand_fit_score, "distinct": r.cross_brand_distinct,
         "mission": bool(r.mission_present), "cost_usd": r.cost_usd, "model": r.model_used}
        for r in results)
    return BrandFitResult(
        brand_fit_score=brand_fit, cross_brand_distinct=distinct, mission_present=mission,
        feedback=picked.feedback, judge_score=judge_score, model_used=first.model_used,
        cost_usd=sum((r.cost_usd or 0.0) for r in results) + (failed_cost_usd or 0.0),
        input_tokens=sum((r.input_tokens or 0) for r in results),
        output_tokens=sum((r.output_tokens or 0) for r in results),
        stop_reason=first.stop_reason, account=first.account, fallback_used=first.fallback_used,
        runs=runs + ((_failed_entry(failed, failed_cost_usd),) if failed else ()),
    )


def _failed_entry(failed: int, failed_cost_usd: float) -> dict:
    entry = {"failed": failed}
    if failed_cost_usd:
        entry["failed_cost_usd"] = failed_cost_usd
    return entry


def billed_cost(exc: BaseException) -> float:
    """AA-801: the spend `_judge` attached to an exception raised AFTER a billed LLM reply (0.0 when
    the call failed before anything was billed, or the exception came from elsewhere)."""
    return float(getattr(exc, "billed_cost_usd", 0.0) or 0.0)


def runs_signal(result: BrandFitResult, mode: str) -> dict:
    """AA-801: the quality_signal block that shows the spread behind an aggregated verdict."""
    runs = [r for r in result.runs if "brand_fit" in r]
    failed = sum(r.get("failed", 0) for r in result.runs)
    failed_cost = sum(r.get("failed_cost_usd", 0.0) for r in result.runs)
    fits = [r["brand_fit"] for r in runs]
    return {
        "mode": mode, "n": len(runs), "failed": failed,
        "brand_fits": fits, "distincts": [r["distinct"] for r in runs],
        "missions": [r["mission"] for r in runs],
        "chosen_brand_fit": result.brand_fit_score, "chosen_distinct": result.cross_brand_distinct,
        "chosen_mission": result.mission_present,
        "brand_fit_spread": (max(fits) - min(fits)) if fits else None,
        **({"failed_cost_usd": failed_cost} if failed_cost else {}),
    }


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


_MISSION_AXIS = """- mission_present (true/false): does this brand's mission-hook actually surface in the ITINERARIES
  (each day's framing), not only in the summary?"""


def _tenant_mission_axis(mission: str) -> str:
    """AA-792 (Nghiệp, Gate 1 option B, 14/10/2026): the T2 mission is the TENANT's own mission —
    its core idea from the brand pack — never Adventure Asia's. Named verbatim so the judge does not
    have to guess what "mission-hook" means (S234: absent on 72/104 T2 calls). Pilot fix: the same
    definition the T2 writer's CORE IDEA lens uses (batch_prompt._core_idea_lens_block) — the core
    idea frames the overview and most days, not quoted, not every day (pilot: absent 27/27)."""
    return f"""- mission_present (true/false): does THIS brand's own mission — its core idea: "{mission}" —
  clearly shape how the trip is framed? true when the overview AND most itinerary days reflect it in
  what they emphasise; it does not have to be quoted or named, and not every single day must carry
  it. false when it appears only in the summary, or not at all. Judge only against this brand's
  mission above, never Adventure Asia's or a generic travel mission."""


def _build_judge_prompt(brand_profile: dict, generated: dict, mission: Optional[str] = None) -> str:
    """Assemble the judge user-prompt: this brand's profile + the content to score. Verbatim
    port of judge_node.py's own `_build_judge_prompt()` (same field names, same prompt text) —
    unchanged so an existing T2 rewrite call and a new Debate call score identically for the
    same inputs, which is the whole point of sharing this module.

    AA-792: `mission` (T2 only, the tenant's core idea) names the mission the mission_present axis
    asks about. None (A1, every other caller) leaves the prompt byte-identical."""
    mission_axis = _tenant_mission_axis(mission) if mission else _MISSION_AXIS
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
{mission_axis}
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


def score_candidate_fit(brand_profile: dict, candidate: dict, *, stage: str = "s1_judge") -> BrandFitResult:
    """Debate's brand-fit call (AA-631): same stage route, temperature/seed and result shape as
    `score_brand_fit()`, but scores a candidate topic from raw evidence instead of finished copy.
    `candidate` = {"place", "action", "evidence": [str, ...]}. There is no mission axis — nothing
    has been written yet — so `mission_present` is always True and never caps the score.
    Raises on any failure, like `score_brand_fit()`."""
    return _judge(CANDIDATE_JUDGE_SYSTEM, _build_candidate_prompt(brand_profile, candidate),
                  mission_absent_cap=None, stage=stage)


def score_brand_fit(
    brand_profile: dict, generated: dict, *, mission_absent_cap: float = 6.0, stage: str = "s1_judge",
    mission: Optional[str] = None, repeat: int = 1,
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

    AA-714 layer 2: when a reasoning judge's first score lands near MIN_QUALITY, re-score and keep
    the median so one noisy call does not flip retry/HITL. See `_REPEAT_BAND`.

    AA-792: `mission` — the T2 tenant's own core idea, named in the mission_present axis (see
    `_build_judge_prompt`). Prompt text only; scoring, cap and median are unchanged.

    AA-801: `repeat` > 1 (T2 with `T2_JUDGE_REPEAT=median3`) runs that many calls in parallel on
    every draft and aggregates them (`aggregate_runs`); the AA-714 near-band layer is then skipped.
    The default 1 keeps the behaviour above exactly.
    """
    system_prompt = JUDGE_SYSTEM
    user_prompt = _build_judge_prompt(brand_profile, generated, mission)

    if repeat > 1:
        return _judge_parallel(system_prompt, user_prompt, n=repeat,
                               mission_absent_cap=mission_absent_cap, stage=stage)

    first = _judge(system_prompt, user_prompt, mission_absent_cap=mission_absent_cap, stage=stage)

    lo, hi = _REPEAT_BAND
    if _seeded_model(first.model_used) or not (lo <= first.brand_fit_score <= hi):
        return first

    results = [first] + [_judge(system_prompt, user_prompt, mission_absent_cap=mission_absent_cap, stage=stage)
                         for _ in range(_REPEAT_EXTRA)]
    out = aggregate_runs(results, mission_absent_cap=mission_absent_cap)
    logger.info("judge_repeat_median", model=first.model_used,
                brand_fits=[r.brand_fit_score for r in results],
                median_brand_fit=out.brand_fit_score, median_distinct=out.cross_brand_distinct)
    return dataclasses.replace(out, runs=())   # AA-714 path: unchanged result shape


def _judge_parallel(system_prompt: str, user_prompt: str, *, n: int, mission_absent_cap: Optional[float],
                    stage: str) -> BrandFitResult:
    """AA-801 option (a): `n` judge calls at once, aggregated by `aggregate_runs`. Each call runs in
    its own copy of the caller's contextvars (live-progress sink, tenant/job attribution). A failed
    call is dropped from the verdict but its known billed spend (`billed_cost`) is kept in cost_usd;
    when every call fails the first error is raised (the caller degrades) carrying the summed
    `billed_cost_usd` of all of them.

    Each call is a full `LLMClient.generate`, so a stage-route shadow (sampled per call) can fire on
    each of the n calls: with a shadow configured on the route, median3 samples up to n shadows per
    judgement instead of one (documented in AA-801 implementation notes / CONTEXT.md)."""
    def one():
        return _judge(system_prompt, user_prompt, mission_absent_cap=mission_absent_cap, stage=stage)

    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(contextvars.copy_context().run, one) for _ in range(n)]
    results, errors = [], []
    for f in futures:
        try:
            results.append(f.result())
        except Exception as e:  # noqa: BLE001 — one noisy call must not drop the verdict
            errors.append(e)
    failed_cost = sum(billed_cost(e) for e in errors)
    if not results:
        _attach_cost(errors[0], failed_cost)
        raise errors[0]
    if errors:
        logger.warning("judge_median_partial", failed=len(errors), ok=len(results), error=str(errors[0]),
                       failed_cost_usd=failed_cost)
    out = aggregate_runs(results, mission_absent_cap=mission_absent_cap, failed=len(errors),
                         failed_cost_usd=failed_cost)
    logger.info("judge_median", n=n, brand_fits=[r.brand_fit_score for r in results],
                median_brand_fit=out.brand_fit_score, mission=out.mission_present)
    return out


def _attach_cost(exc: BaseException, cost_usd: float) -> None:
    try:
        exc.billed_cost_usd = cost_usd
    except (AttributeError, TypeError):   # an exception type without __dict__: the cost is lost
        pass


def _seeded_model(model_used: Optional[str]) -> bool:
    """True when the model honours the fixed seed (so one call is already reproducible). Reads the
    catalog's `supports_temperature`; unknown/legacy models (gpt-4.1) are treated as seeded."""
    if not model_used:
        return True
    from shared.llm_client.catalog import get_model_sync
    key = model_used.replace("satellite-", "")
    m = get_model_sync(key)
    return True if m is None else bool(m.supports_temperature)


def _judge(system_prompt: str, user_prompt: str, *, mission_absent_cap: float | None,
           stage: str = "s1_judge") -> BrandFitResult:
    request = LLMRequest(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        # AA-659 / ADR 0006: the model comes from the "s1_judge" stage route. The judge-vendor ≠
        # writer-vendor rule (ADR-2026-014/027) is enforced when the route is saved in admin.
        # AA-763: T2 passes "t2_judge", Debate "t7_debate_judge" (same route, own key).
        stage=stage,
        temperature=_JUDGE_TEMPERATURE,
        seed=_JUDGE_SEED,
    )
    client = LLMClient()
    resp = client.generate(request)

    try:
        raw = resp.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        result = json.loads(raw)
    except Exception as e:
        # AA-801: the reply was billed even though it is unusable — keep its cost on the exception
        # so median3 (and ad-hoc scripts) can count it.
        _attach_cost(e, float(getattr(resp, "cost_usd", 0.0) or 0.0))
        raise

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
