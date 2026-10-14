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
    # AA-801 option (b): the verbatim itinerary sentence the anchored mission test quoted ("" when
    # the mission is absent or the prompt had no anchors).
    mission_evidence: str = ""


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
    evidence = next((r.mission_evidence for r in results if r.mission_present and r.mission_evidence), "") \
        if mission else ""
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
        mission_evidence=evidence,
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


# AA-801 option (b) — rubric anchors. S236 measurement: the same T2 draft scored brand_fit 6..8 and
# mission flipped across calls with median-of-3 as noisy as one call, so the noise is in the rubric
# ("does it reflect the brand?" leaves the judge to invent its own scale), not in sampling. The anchors
# say what a 4 / 6 / 8 / 10 looks like FOR THIS TENANT, built deterministically from its T0 brand pack,
# and the mission becomes a yes/no test that must quote the itinerary sentence that proves it.
_ANCHOR_EXAMPLE_CHARS = 400
_ANCHOR_MAX_VOICE = 6
_ANCHOR_MAX_FORBIDDEN = 15


def _clip(text: str, limit: int) -> str:
    """Whitespace-normalised `text`, cut at a word boundary to at most `limit` characters."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:")
    return cut + " …"


def build_rubric_anchors(brand_profile: dict, mission: Optional[str] = None) -> str:
    """AA-801 option (b): the SCORE ANCHORS block of the T2 judge prompt, built only from the graph
    state's brand_* fields (the tenant's T0 brand pack) — same inputs, same text, no LLM. Fields the
    tenant has not filled are left out of the anchors rather than shown as "(none)"."""
    name = (brand_profile.get("brand_name") or "").strip() or "this brand"
    core = (mission or brand_profile.get("brand_core_idea") or "").strip()
    segment = (brand_profile.get("brand_customer_segment") or "").strip()
    mindset = (brand_profile.get("brand_customer_mindset") or "").strip()
    voice = [str(v).strip() for v in (brand_profile.get("brand_voice_examples") or []) if str(v).strip()]
    example = _clip(brand_profile.get("brand_good_examples") or "", _ANCHOR_EXAMPLE_CHARS)
    forbidden = [str(w).strip() for w in (brand_profile.get("brand_forbidden_words") or []) if str(w).strip()]

    ten = [f"reads unmistakably as {name}"]
    if core:
        ten.append(f'the overview and most itinerary days are framed by its core idea ("{core}")')
    if segment or mindset:
        who = segment or "its travellers"
        ten.append(f"it speaks to {who}" + (f" (what they want: {mindset})" if mindset else ""))
    if voice:
        ten.append("in its voice (" + ", ".join(voice[:_ANCHOR_MAX_VOICE]) + ")")
    if example:
        ten.append(f'as close in voice to this on-brand example as the facts allow: "{example}"')
    lines = [
        "SCORE ANCHORS for THIS brand (pick the anchor the copy matches best; use 5, 7 or 9 only when",
        "the copy fully meets the lower anchor and only part of the higher one):",
        "brand_fit_score:",
        "- 10: " + "; ".join(ten) + "; no generic filler anywhere.",
        "- 8: this brand's angle is clear in the overview and most days and the voice mostly matches; a",
        "  few passages read as generic travel copy.",
        "- 6: the brand angle shows only in the summary/overview, or brand words are bolted onto otherwise",
        "  generic copy; most days read like a standard tour description.",
        "- 4: generic travel copy that would fit any brand; the core idea and the voice are absent.",
    ]
    if forbidden:
        lines.append("- Any of this brand's forbidden words or phrases in the copy caps brand_fit_score at 6: "
                     + ", ".join(forbidden[:_ANCHOR_MAX_FORBIDDEN]) + ".")
    lines += [
        "cross_brand_distinct:",
        "- 10: the choice of details, the framing of each day and the voice would all change for another brand.",
        "- 8: most days emphasise different details than a neutral version would, and the voice is distinct.",
        "- 6: the same structure and details as a neutral version, with brand-specific wording in places.",
        "- 4: a synonym swap of a generic description.",
    ]
    return "\n".join(lines)


def _anchored_mission_axis(mission: str) -> str:
    """AA-801 option (b): the mission as a crisp yes/no test with a required verbatim quote."""
    return f"""- mission_present (true/false) — MISSION TEST, answer yes or no: does the copy express this
  brand's core idea: "{mission}"? Answer true ONLY when you can copy one sentence VERBATIM from the
  ITINERARIES (a day, not the summary) that expresses it, and put that sentence in mission_evidence.
  No such sentence → false and mission_evidence "". Judge only against this brand's core idea above,
  never Adventure Asia's or a generic travel mission.
- mission_evidence (string): the verbatim itinerary sentence, or "" when mission_present is false."""


def _build_judge_prompt(brand_profile: dict, generated: dict, mission: Optional[str] = None,
                        anchors: bool = False) -> str:
    """Assemble the judge user-prompt: this brand's profile + the content to score. Verbatim
    port of judge_node.py's own `_build_judge_prompt()` (same field names, same prompt text) —
    unchanged so an existing T2 rewrite call and a new Debate call score identically for the
    same inputs, which is the whole point of sharing this module.

    AA-792: `mission` (T2 only, the tenant's core idea) names the mission the mission_present axis
    asks about. None (A1, every other caller) leaves the prompt byte-identical.

    AA-801: `anchors=True` (T2 with `T2_JUDGE_ANCHORS` on) adds the tenant's SCORE ANCHORS block and,
    when there is a mission, turns mission_present into the quoted yes/no test; the reply gains an
    optional `mission_evidence` key (old parsers ignore it). False leaves the prompt byte-identical."""
    if anchors and mission:
        mission_axis = _anchored_mission_axis(mission)
    else:
        mission_axis = _tenant_mission_axis(mission) if mission else _MISSION_AXIS
    voice_ex = [v for v in (brand_profile.get("brand_voice_examples") or []) if v]

    highlights = generated.get("highlights") or []
    if isinstance(highlights, str):
        highlights = [highlights]
    highlights_text = "\n".join(f"- {h}" for h in highlights)
    anchors_block = ("\n" + build_rubric_anchors(brand_profile, mission) + "\n") if anchors else ""
    if anchors and mission:
        reply_shape = ('{"brand_fit_score": <int>, "cross_brand_distinct": <int>, "mission_present": <bool>, '
                       '"mission_evidence": "<verbatim itinerary sentence or empty>", "feedback": "<string>"}')
    else:
        reply_shape = ('{"brand_fit_score": <int>, "cross_brand_distinct": <int>, "mission_present": <bool>, '
                       '"feedback": "<string>"}')

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
{anchors_block}
SCORE on these axes:
- brand_fit_score (1-10): does the content reflect THIS brand's specific angle and the traveller
  mindset above — not a generic register that fits any travel brand?
- cross_brand_distinct (1-10): if the SAME tour were rewritten for a DIFFERENT brand, how clearly
  would this version differ? A synonym swap of a generic description scores low.
{mission_axis}
- feedback (string): specific, concrete changes needed to make the content read more distinctly as
  THIS brand. If everything is on-brand, return an empty string.

Return JSON ONLY, no markdown, exactly:
{reply_shape}"""


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
    mission: Optional[str] = None, repeat: int = 1, anchors: bool = False,
    model_override: Optional[str] = None,
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

    AA-801 option (b): `anchors=True` (T2 with `T2_JUDGE_ANCHORS` on) builds the anchored prompt and,
    when there is a mission, counts mission_present only with a quoted `mission_evidence`.

    AA-801 option (c): `model_override` — a catalog Model Key sent as the request's `model_tier` (the
    gateway's existing per-request override: one model, no route fallback, no shadow). Measurement
    only: allowed on an `adhoc_*` stage, never on a production stage route (ValueError), so app code
    cannot pin a judge model past `shared.llm_role_config`.
    """
    if model_override and not (stage or "").startswith("adhoc_"):
        raise ValueError(f"model_override is for adhoc_* measurement stages only, not {stage!r}; "
                         "change the stage route in shared.llm_role_config instead")
    system_prompt = JUDGE_SYSTEM
    user_prompt = (_build_judge_prompt(brand_profile, generated, mission, anchors=True) if anchors
                   else _build_judge_prompt(brand_profile, generated, mission))
    extra = {}
    if anchors and mission:
        extra["require_evidence"] = True
    if model_override:
        extra["model_tier"] = model_override

    if repeat > 1:
        return _judge_parallel(system_prompt, user_prompt, n=repeat,
                               mission_absent_cap=mission_absent_cap, stage=stage, **extra)

    first = _judge(system_prompt, user_prompt, mission_absent_cap=mission_absent_cap, stage=stage, **extra)

    lo, hi = _REPEAT_BAND
    if _seeded_model(first.model_used) or not (lo <= first.brand_fit_score <= hi):
        return first

    results = [first] + [_judge(system_prompt, user_prompt, mission_absent_cap=mission_absent_cap, stage=stage,
                                **extra)
                         for _ in range(_REPEAT_EXTRA)]
    out = aggregate_runs(results, mission_absent_cap=mission_absent_cap)
    logger.info("judge_repeat_median", model=first.model_used,
                brand_fits=[r.brand_fit_score for r in results],
                median_brand_fit=out.brand_fit_score, median_distinct=out.cross_brand_distinct)
    return dataclasses.replace(out, runs=())   # AA-714 path: unchanged result shape


def _judge_parallel(system_prompt: str, user_prompt: str, *, n: int, mission_absent_cap: Optional[float],
                    stage: str, **extra) -> BrandFitResult:
    """AA-801 option (a): `n` judge calls at once, aggregated by `aggregate_runs`. Each call runs in
    its own copy of the caller's contextvars (live-progress sink, tenant/job attribution). A failed
    call is dropped from the verdict but its known billed spend (`billed_cost`) is kept in cost_usd;
    when every call fails the first error is raised (the caller degrades) carrying the summed
    `billed_cost_usd` of all of them.

    Each call is a full `LLMClient.generate`, so a stage-route shadow (sampled per call) can fire on
    each of the n calls: with a shadow configured on the route, median3 samples up to n shadows per
    judgement instead of one (documented in AA-801 implementation notes / CONTEXT.md)."""
    def one():
        return _judge(system_prompt, user_prompt, mission_absent_cap=mission_absent_cap, stage=stage, **extra)

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
           stage: str = "s1_judge", require_evidence: bool = False,
           model_tier: Optional[str] = None) -> BrandFitResult:
    """One judge call. AA-801: `require_evidence` (anchored mission test) counts mission_present only
    when the reply quotes a non-empty `mission_evidence`; `model_tier` is the gateway's per-request
    model override (adhoc measurement only — `score_brand_fit` guards the stage)."""
    request = LLMRequest(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        # AA-659 / ADR 0006: the model comes from the "s1_judge" stage route. The judge-vendor ≠
        # writer-vendor rule (ADR-2026-014/027) is enforced when the route is saved in admin.
        # AA-763: T2 passes "t2_judge", Debate "t7_debate_judge" (same route, own key).
        stage=stage,
        temperature=_JUDGE_TEMPERATURE,
        seed=_JUDGE_SEED,
        **({"model_tier": model_tier} if model_tier else {}),
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
    evidence = str(result.get("mission_evidence") or "").strip()
    if require_evidence and mission_present and not evidence:
        mission_present = False          # AA-801 (b): an unquoted "yes" does not count

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
        mission_evidence=evidence if mission_present else "",
    )
