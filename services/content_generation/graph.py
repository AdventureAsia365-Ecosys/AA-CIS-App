import json
import os
import re
import structlog

from services.content_generation.stages import stage_for
from json_repair import repair_json
from typing import TypedDict, Annotated, Optional
from langgraph.graph import StateGraph, END

from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest
from shared.llm_client.call_log import record_call_sync, record_unusable_sync
from services.quality.registry import threshold
from .prompts import parse_source_day_word_counts
from .batch_prompt import (
    build_brand_diff_block, build_s1_system_prompt, build_s1_user_prompt, prompt_version_of,
)
from .brand_audit_node import brand_audit_node
from .flag_fix_node import flag_fix_node
from .grounding import grounding_node, regrounding, repair_and_recheck
from .grounding import degraded_entries as grounding_degraded_entries, source_text as grounding_source_text
from .judge_node import judge_node
from .t2_repair import (
    increment_retry_t2, restore_best_node, revise_block, targeted_repair_enabled, track_best_node,
)
from .seo_meta_utils import (SEO_META_MIN, SEO_META_MAX, meta_complete_sentence, SEO_META_FORBIDDEN,
                             fit_seo_meta_final, fit_seo_title, meta_cut_signature, tour_days)
from services.quality import day_oracle, degraded, repair_contract
from .forbidden_words import (VALIDATE_FORBIDDEN, all_forbidden as all_forbidden_words, copy_text, has_word,
                              state_forbidden)
from .forbidden_strip import SKIP_FIELDS as _FORBIDDEN_SCAN_SKIP, strip_forbidden
from .itinerary_utils import (
    ITINERARY_CLAMP_MIN, ITINERARY_CLAMP_MAX, MAX_NUDGES_PER_TOUR, NUDGE_SKIP_REASON,
    clamp_distance, nudge_itinerary_day, nudge_voice_hint, generated_day_word_counts,
)
from .day_parse import CANONICAL_HEAD_NC, day_range

logger = structlog.get_logger()

MAX_RETRIES = 3
# AA-639: the tour writer's output ceiling. The LLMRequest default (4096) truncated long tours —
# live, an 18-day tour stopped at exactly 4096 output tokens (stop_reason=max_tokens) three times in
# a row, the cut JSON lost its trailing fields (MISSING_FIELD) and every truncation cost a full
# rewrite. Output tokens are billed as used, so a higher ceiling costs nothing for shorter tours.
GENERATE_MAX_TOKENS = 8192
MIN_QUALITY = threshold("a1.min_quality")  # services/quality/thresholds.yaml (AA-926)


def _process_itineraries(generated: dict, tour: dict, client: LLMClient,
                         nudge_budget: int = MAX_NUDGES_PER_TOUR, *, stage: str = "s1_itinerary_nudge",
                         voice_hint: str | None = None) -> dict:
    """AA-353: validate the model's structured ``itineraries`` array against its own PER-DAY
    SOURCE LENGTH target, auto-nudge (once, non-repeating) any day outside the hard clamp, then
    serialize the array back to the pre-existing canonical "Day N — Title\\nBody" string format —
    so every downstream reader (validate_node, judge_node, brand_audit_node, flag_fix_node,
    generated_content.aa_itineraries) sees exactly the string shape it already expects. No
    downstream code changes needed (confirmed by reading all of them — see implementation notes).

    Mutates ``generated["itineraries"]`` in place (array -> string). No-ops (leaves the field
    untouched, logs a warning) if the model didn't return the array contract at all — old string
    behavior, not a crash.

    AA-747: at most ``nudge_budget`` days are nudged (default MAX_NUDGES_PER_TOUR). When more days
    are out of clamp than the budget allows, the WORST days (largest distance outside the clamp
    band, clamp_distance()) are nudged first; the rest are recorded with ``nudged: False`` and
    ``skipped_reason: "nudge_cap"`` so the eval/regression nudge counters stay correct. The budget
    is counted across BOTH nudge call sites (this one and flag_fix_node's ITINERARY_STILL_COMPRESSED
    repair) via the caller threading the remaining budget through state.

    Returns {"day_ratios": [...], "extra_cost_usd": float, "extra_cache_read_tokens": int,
    "extra_cache_write_tokens": int, "nudges_used": int} for the caller to fold into generate_node's
    own totals and metadata (AA-353 STEP 0: generated_content.metadata JSONB, same place as AA-213's
    fallback_used/score_overall/batch_id).
    """
    itineraries = generated.get("itineraries")
    empty_result = {"day_ratios": [], "extra_cost_usd": 0.0,
                     "extra_cache_read_tokens": 0, "extra_cache_write_tokens": 0,
                     "nudges_used": 0}
    if not isinstance(itineraries, list):
        if itineraries:
            logger.warning("itinerary_contract_not_array", value_type=type(itineraries).__name__)
        return empty_result

    itineraries_raw = tour.get("itineraries") or tour.get("itinerary") or ""
    source = parse_source_day_word_counts(itineraries_raw, tour.get("duration"))
    source_by_day = source["day_word_counts"]
    source_text_by_day = source["day_text"]
    source_ordered = sorted(source_by_day.items())  # [(day_num, words), ...]

    # AA-747: first pass — resolve each day's source words/text and ratio, WITHOUT nudging yet, so
    # we can pick the worst out-of-clamp days once the whole tour is scored. Keyed by the item's
    # position so the serialize step below still emits days in their original order.
    day_meta = []  # list of dicts: position, day_item, source_words, source_text, ratio, record
    for position, day_item in enumerate(itineraries):
        if not isinstance(day_item, dict):
            continue
        day_num = day_item.get("day")
        body = str(day_item.get("body") or "")

        source_words = source_by_day.get(day_num)
        source_text = source_text_by_day.get(day_num)
        if source_words is None and position < len(source_ordered):
            # AA-353: model's day numbering didn't line up with the source parse — fall back to
            # positional matching (i-th returned day vs i-th source day) rather than dropping the
            # ratio check for that day entirely.
            source_words = source_ordered[position][1]
            source_text = source_text_by_day.get(source_ordered[position][0])

        actual_words = len(body.split())
        record = {"day": day_num, "source_words": source_words, "actual_words": actual_words,
                   "nudged": False}
        ratio = (actual_words / source_words) if source_words else None
        if ratio is not None:
            record["ratio"] = round(ratio, 3)
        day_meta.append({"position": position, "day_item": day_item, "source_words": source_words,
                         "source_text": source_text, "ratio": ratio, "record": record})

    # AA-747: out-of-clamp days, worst (largest distance from the band) first; break ties by day
    # order so the choice is deterministic.
    out_of_clamp = [
        m for m in day_meta
        if m["ratio"] is not None and (m["ratio"] < ITINERARY_CLAMP_MIN or m["ratio"] > ITINERARY_CLAMP_MAX)
    ]
    out_of_clamp.sort(key=lambda m: (-clamp_distance(m["ratio"]), m["position"]))
    to_nudge = {id(m) for m in out_of_clamp[:max(0, nudge_budget)]}

    extra_cost = 0.0
    extra_read = 0
    extra_write = 0
    nudges_used = 0

    for m in out_of_clamp:
        record = m["record"]
        day_num = record["day"]
        if id(m) not in to_nudge:
            # AA-747: budget spent — leave the day as written, record WHY it wasn't nudged so the
            # eval/regression counters distinguish "skipped for budget" from "nudge was attempted".
            record["skipped_reason"] = NUDGE_SKIP_REASON
            logger.info("itinerary_day_nudge_skipped_cap", day=day_num, ratio=record["ratio"])
            continue
        day_item = m["day_item"]
        source_words = m["source_words"]
        new_title, new_body, resp = nudge_itinerary_day(
            client, m["source_text"] or itineraries_raw,
            str(day_item.get("title") or ""), str(day_item.get("body") or ""), source_words,
            stage=stage, voice_hint=voice_hint,  # AA-792: T2 keeps the tenant voice; None for A1
        )
        try:
            extra_cost += resp.cost_usd
            extra_read += resp.cache_read_tokens
            extra_write += resp.cache_write_tokens
            nudges_used += 1
            day_item["title"], day_item["body"] = new_title, new_body
            actual_words_after = len(new_body.split())
            record["nudged"] = True
            record["actual_words_after_nudge"] = actual_words_after
            record["ratio_after_nudge"] = round(actual_words_after / source_words, 3)
        except Exception as e:
            # AA-927: the nudge was billed — its row is written before the error goes on up.
            record_unusable_sync(resp, stage=stage, role="writer", error=e)
            raise
        # AA-505 — quality_signal = did the nudge actually land inside clamp? Real,
        # computed right here, not a fabricated placeholder.
        record_call_sync(
            stage=stage, role="writer", model=resp.model_used,
            tokens_in=getattr(resp, "input_tokens", None), tokens_out=getattr(resp, "output_tokens", None),
            cost_usd=resp.cost_usd,
            quality_signal={
                "landed_in_clamp": ITINERARY_CLAMP_MIN <= record["ratio_after_nudge"] <= ITINERARY_CLAMP_MAX,
                "ratio_after_nudge": record["ratio_after_nudge"],
            },
            stop_reason=getattr(resp, "stop_reason", None),
            account=getattr(resp, "satellite_account", None),
            fallback_used=getattr(resp, "fallback_used", None),
        )
        logger.info("itinerary_day_nudged", day=day_num, ratio_before=record["ratio"],
                    ratio_after=record["ratio_after_nudge"])

    day_ratios = [m["record"] for m in day_meta]

    generated["itineraries"] = "\n\n".join(
        f"Day {d.get('day')} — {d.get('title') or ''}\n{d.get('body') or ''}".strip()
        for d in itineraries if isinstance(d, dict)
    )
    return {"day_ratios": day_ratios, "extra_cost_usd": extra_cost,
            "extra_cache_read_tokens": extra_read, "extra_cache_write_tokens": extra_write,
            "nudges_used": nudges_used}
# AA-216: structural failure (empty/missing field) caps quality below the gate regardless of
# other sub-scores. Empty content scores brand/quality=10 (no rule to violate) which the
# 4-bucket average can't pull under 7 — mirror judge's _MISSION_ABSENT_CAP pattern.
MISSING_FIELD_CAP = threshold("a1.missing_field_cap")
# AA-234: failure codes that HARD-BLOCK approve of a human-edited version even when the
# overall score clears MIN_QUALITY. These are rule violations that must never reach gold
# (SEO length/floor, brand deny-list, forbidden words, missing fields) — the exact class
# the Review Queue exists to catch. Soft codes (HIGHLIGHTS_*, *_GENERIC, SUMMARY_OFF_BRAND)
# are surfaced to the reviewer but do NOT block.
_HARD_BLOCK_CODES = frozenset({
    "META_TOO_SHORT", "SEO_META_TOO_LONG", "SEO_TITLE_TOO_LONG",
    "META_INCOMPLETE_SENTENCE", "BRAND_SEO_META_VIOLATION",
    "FORBIDDEN_WORD", "MISSING_FIELD",
    # AA-925: a hard fail-open detector (Jev, grounding) did not run on this output — set by ONE helper,
    # degraded.apply_a1_hold, at the end of the graph. Not a rewrite-class code: a retry cannot fix an outage.
    degraded.HOLD_CODE,
}) | day_oracle.enforced_codes()  # AA-925: a day-oracle code in enforce mode blocks A1 Master (none yet)

# S218 audit (lesson: "a hard code needs a repair path"): every code that blocks Master must name
# the step that can clear it. AA-736 made the length codes hard with no deterministic fix and 15
# tours scoring >= 7 sat in review until AA-740. tests/unit/test_s218_hard_code_repair.py fails if a
# code is added to _HARD_BLOCK_CODES without an entry here. "deterministic:" = no LLM, "llm:" =
# a model retry that may still fail (then review).
HARD_CODE_REPAIR = {
    "META_TOO_SHORT":           "deterministic: seo_meta_utils.fit_seo_meta_final (fact clause)",
    # AA-925: no clause/word cut any more (it left "...a 20-kilometre."): a sentence-boundary salvage, else the
    # flag_fix LLM seo_meta repair + its _rerepair_meta band guard; what is left goes to review.
    "SEO_META_TOO_LONG":        "llm: flag_fix seo_meta repair + _rerepair_meta after sentence-boundary salvage",
    "META_INCOMPLETE_SENTENCE": "deterministic: seo_meta_utils.fit_seo_meta_final (close sentence)",
    "SEO_TITLE_TOO_LONG":       "deterministic: seo_meta_utils.fit_seo_title",
    "FORBIDDEN_WORD":           "deterministic: forbidden_strip.strip_forbidden",
    "BRAND_SEO_META_VIOLATION": "llm: flag_fix seo_meta repair, then one haiku->sonnet retry (AA-736)",
    "MISSING_FIELD":            "llm: one haiku->sonnet retry (AA-736)",
    # AA-925: an enforced day-oracle code (struct.day_structure) — the writer rewrites the itinerary.
    **{code: "llm: one haiku->sonnet rewrite (AA-736), else review" for code in day_oracle.enforced_codes()},
    # AA-925: no model can clear an outage; the reviewer re-runs the tour once the detector is healthy.
    degraded.HOLD_CODE:         "review: re-run the tour once the detector is healthy",
    # AA-645 (S249): hard on an A1 writer-v2 draft only (V2_HARD_BLOCK_CODES). flag_fix can no longer cause it
    # (repair contract invariant days_kept); a v2 writer is already on Sonnet, so AA-736's haiku->sonnet retry
    # does not apply.
    "ITINERARY_DAY_COUNT_MISMATCH": ("review: regenerate the tour (a writer-v2 draft dropped a source day), or "
                                     "promote the stored page back"),
}

# AA-645 (S249, S1 go/no-go memo §3.1b option 1a): on an A1 writer-v2 draft a source day missing from the output
# blocks Master. v2 days are planned by code from the same source "Day N" markers validate compares against, so a
# missing source day is a lost day, never a planning choice: in the S249 shadow 49/50 pages match their plan and the
# 50th (India "Rajasthan Adventure", 15 -> 10 days) scored 9.75 and was publishable, because the code only takes 1.0
# off structure. Same code, same detector, no new gate: for v1 drafts and tenant (T2) rewrites it stays the soft
# structure deduction it was. Readers use ``hard_block_codes(state)``, never this set alone.
V2_HARD_BLOCK_CODES = frozenset({"ITINERARY_DAY_COUNT_MISMATCH"})


DAY_COUNT_CODE = "ITINERARY_DAY_COUNT_MISMATCH"


def itinerary_day_gap(tour: dict, itinerary) -> Optional[dict]:
    """AA-329a, the one source-vs-output day check (validate_node and final_day_guard): the source itinerary's
    "Day N" blocks (``parse_source_day_word_counts``, the splitter the writer v2 day plan and the day oracle read)
    against the written itinerary's canonical "Day N — Title" blocks. None when the source has no reliable day
    markers (the splitter fell back to an even split): nothing trustworthy to compare. Otherwise
    {"source_day_words", "generated_day_words", "missing": sorted source days absent from the output}."""
    src_raw = (tour or {}).get('itineraries') or (tour or {}).get('itinerary') or ""
    parsed = parse_source_day_word_counts(src_raw, (tour or {}).get('duration'))
    if parsed["used_fallback"]:
        return None
    source_day_words = parsed["day_word_counts"]
    # AA-990 (S253): both sides count a ranged heading as every day it covers — the source's "Days 12-15:" marker
    # (day_word_counts keys a block by its first day) and the output's "Day 12-15 —" block
    # (generated_day_word_counts expands it). Before, the source range counted as day 12 only, so an output that
    # folded days 13-15 away was not missing anything. Only TRUSTED ranges count (``day_ranges``,
    # day_parse.trusted_day_ranges: a separator after the range end, cut at the next marker and the duration):
    # this is a hard block on writer v2, and "Day 3 - 5 temples of Hue" in a 3-day source is day 3, not days 3-5.
    source_days = sorted({d for first, last in (parsed.get("day_ranges") or {}).items()
                          for d in range(first, last + 1)} | set(source_day_words))
    generated_day_words = generated_day_word_counts(itinerary if isinstance(itinerary, str) else str(itinerary or ""))
    missing = sorted(set(source_days) - set(generated_day_words))
    return {"source_day_words": source_day_words, "source_days": source_days,
            "generated_day_words": generated_day_words, "missing": missing}


def day_gap_issue(gap: dict) -> str:
    missing = gap["missing"]
    n_source = len(gap.get("source_days") or gap["source_day_words"])
    return (f"Itinerary missing {len(missing)} day(s) vs source "
            f"({n_source} source days -> {len(gap['generated_day_words'])} output days): "
            f"day {'/'.join(str(d) for d in missing)}")


def final_day_guard(state: dict) -> dict:
    """AA-990 N3 (S252) — the day-structure check on the FINAL text of an A1 run, after every edit.

    validate_node checks the days once per draft; the grounding repair, flag_fix and the post-revalidate sentence
    re-repair edit ``generated`` after it (each under the repair contract's ``days_kept``, but a revalidate
    passthrough re-runs no validate at all). This re-runs the same check (``itinerary_day_gap``) on the text that
    is persisted, as the last step of the graph. A missing source day puts ITINERARY_DAY_COUNT_MISMATCH on
    failure_codes (the code validate fires, so the publish gate, the review queue reason and HARD_CODE_REPAIR stay
    the existing ones); when the code is hard for this run (``hard_block_codes``: an A1 writer-v2 draft), the run
    reports ``revalidate_passed = False`` — a blocked page never says its re-validation passed. A tenant rewrite
    is left to T3 (lesson AA-800). A detector error leaves the state as it is (logged): validate's own run of the
    same function already scored the draft."""
    if state.get("is_tenant_rewrite"):
        return state
    generated = state.get("generated") or {}
    if not isinstance(generated, dict) or not generated:
        return state
    try:
        gap = itinerary_day_gap(state.get("tour") or {}, generated.get("itineraries") or "")
    except Exception as exc:  # never break the rewrite on a detector bug
        logger.warning("final_day_guard_failed", tour_id=state.get("tour_id"), error=str(exc)[:200])
        return state
    codes = list(state.get("failure_codes") or [])
    out = state
    if gap is not None and gap["missing"]:
        out = {**state, "day_guard_missing": gap["missing"]}
        if DAY_COUNT_CODE not in codes:
            codes.append(DAY_COUNT_CODE)
            out["failure_codes"] = codes
            out["feedback"] = "; ".join(x for x in (state.get("feedback") or "", day_gap_issue(gap)) if x)
            logger.warning("final_day_guard_mismatch", tour_id=state.get("tour_id"), missing=gap["missing"],
                           source_days=len(gap["source_days"]), output_days=len(gap["generated_day_words"]))
    if DAY_COUNT_CODE in codes and DAY_COUNT_CODE in hard_block_codes(state) and out.get("revalidate_ran"):
        out = {**out, "revalidate_passed": False}
    return out


def hard_block_codes(state: Optional[dict] = None) -> frozenset:
    """The hard-block codes for this run: ``_HARD_BLOCK_CODES``, plus ``V2_HARD_BLOCK_CODES`` when the draft was
    written by the S1 writer v2 on A1 (``s1_writer_v2_live.is_v2``: never a tenant rewrite)."""
    from .s1_writer_v2_live import is_v2   # lazy, like validate_node's import (keeps graph import-light)

    if state and is_v2(state):
        return _HARD_BLOCK_CODES | V2_HARD_BLOCK_CODES
    return _HARD_BLOCK_CODES

class ContentState(TypedDict):
    tour:                   dict
    seo:                    dict
    few_shots:              list
    generated:              dict
    quality_score:          float
    retry_count:            int
    feedback:               str
    error:                  str
    cost_usd:               float
    model_used:             str
    brand_system_prompt:    str
    brand_style_guide:      str
    brand_forbidden_words:  list
    rewrite_language:       str
    # brand differentiation fields (AA-202)
    brand_core_idea:        str
    brand_customer_segment: str
    brand_customer_mindset: str
    brand_voice_examples:   list
    brand_good_examples:    str
    # AA-792: T2 brand pack fields (services/acp_shared/brand_pack, AA-771) for the CLIENT BRAND block.
    brand_name:             str
    brand_type:             str
    brand_default_cta:      str
    brand_signature_words:  list   # AA-800: custom_validators.signature_words (T2 only)
    model_tier:             str
    fallback_used:          bool   # AA-213: True khi Sonnet T1 fell back to Haiku T2
    satellite_account:      Optional[str]  # AA-296/397: "acc3" nếu qua satellite, None nếu không
    prompt_version:         str    # AA-289: sha256[:8] của system prompt cuối cùng gửi cho LLM
    cache_read_tokens:      int    # AA-288: Bedrock prompt-cache tokens read (0 nếu không cache)
    cache_write_tokens:     int    # AA-288: Bedrock prompt-cache tokens written (0 nếu không cache)
    subtitle_focus:         str
    is_tenant_rewrite:      bool
    is_branded:             bool
    failure_codes:          list
    sub_scores:             dict
    passed_count:           int
    failed_count:           int
    seo_mode:               str    # "dataforseo" | "custom_keywords" | "disabled"
    # brand audit fields (AA-133)
    brand_audit_status:     str    # "pass" | "flagged" | "manual_check" | ""
    brand_audit_codes:      list
    brand_audit_issues:     list
    brand_audit_fields:     list
    lessons_extracted:      list
    # flag fix tracking (AA-134)
    fix_pass_applied:       bool
    fix_pass_fields:        list
    # AA-206: GPT-4.1 two-model judge fields (set by judge_node when a brand profile is present)
    judge_brand_fit:            float
    judge_cross_brand_distinct: float
    judge_mission_present:      bool
    judge_feedback:             str
    # AA-209: judge_score must be declared too, else LangGraph strips it from the final state and it
    # never reaches _rewrite_tour → _build_generated_metadata (metadata.judge.judge_score stayed null).
    judge_score:                float
    # AA-645 (S249): rubric-v2 verdict details (brand_fit_v2); absent on a v1 verdict. Declared so LangGraph keeps
    # them (AA-209 gotcha) for generated_content.metadata.judge.
    judge_rubric:               str
    judge_unsupported:          list
    judge_caps:                 list
    # AA-215: revalidate-after-flag_fix verification (POST-fix). revalidate_ran=True chi khi
    # fix_pass_applied; revalidate_passed phan anh ket qua re-validate+re-judge tren content da sua.
    revalidate_ran:         bool
    revalidate_passed:      bool
    # AA-353: per-day actual/source word-count ratios from the structured itineraries array,
    # set by generate_node's _process_itineraries. Must be declared here or LangGraph strips it
    # from node_output before _rewrite_tour ever sees it (same gotcha as judge_score, AA-209).
    itinerary_day_ratios:   list
    # AA-747: nudge LLM calls already spent on this tour by generate_node's _process_itineraries,
    # so flag_fix_node's ITINERARY_STILL_COMPRESSED repair stays under MAX_NUDGES_PER_TOUR across
    # BOTH call sites. Declared here or LangGraph strips it before flag_fix reads it (AA-209 gotcha).
    itinerary_nudges_used:  int
    # AA-606: raw writer text from Bedrock Batch attempt-1, consumed by seed_generated_node
    # (build_graph_from_generated). Declared here so LangGraph doesn't strip it before the seed
    # node reads it. Absent/None on the normal synchronous build_graph() path.
    batch_text:             Optional[str]
    # AA-620: real tenant UUID for a T2 rewrite so record_call logs the right tenant instead of
    # NULL (A1 admin leaves this None — its writes stay tenant_id NULL by design).
    tenant_id:              Optional[str]
    # AA-620: which llm_role_config stage the WRITE node resolves — "t2_generate" for a tenant
    # (T2) rewrite, "s1_generate" for A1 admin. None -> falls back to s1_generate. AA-763: the
    # other shared-graph calls derive their t2_* twin from is_tenant_rewrite (stages.stage_for).
    generate_stage:         Optional[str]
    # AA-748: per-day source facts (source_facts.extract_day_facts) threaded into the writer prompt
    # behind S1_STRUCTURED_FACTS. generate_node extracts them once (one s1_source_facts LLM call)
    # and stores them here before building the prompt. Declared so LangGraph keeps the field; absent
    # with the flag off (and on the Bedrock Batch path) -> prompt byte-identical to today.
    structured_facts:       Optional[dict]
    # AA-691: A1 grounding against the raw source (grounding.py). Declared so LangGraph keeps them.
    grounding_ran:              bool
    grounding_found:            list   # violations before the sentence repair
    grounding_repaired_fields:  list
    grounding_violations:       list   # still unsupported after repair (→ manual_check in revalidate)
    grounding_notes:            list   # low-probability shadow/grey Jev verdicts, for the reviewer
    # AA-793: T2 best draft wins + targeted repair (t2_repair.py). Declared so LangGraph keeps them
    # (AA-209 gotcha). best_draft = snapshot of the highest-scoring judged draft; draft_scores = one
    # {attempt, score, judge_score, revision} per judged draft; previous_draft = the draft the next
    # writer call revises (T2 retry, or a T3 repair round); best_draft_restored = the hitl exit put
    # an earlier, better draft back.
    best_draft:             Optional[dict]
    draft_scores:           list
    previous_draft:         Optional[dict]
    best_draft_restored:    bool
    # AA-993 G2: the graph-state keys a writer node changed, saved by an s1_rewrite job (s1_checkpoint) and merged
    # back by resume_writer_node (build_graph_resumed) when a released job is re-claimed. Declared so LangGraph keeps it
    # until the entry node reads it.
    resume_state:           Optional[dict]
    # AA-925 (Quality System v1): validate_node's shadow findings (day oracle, S2 cut signature) — recorded
    # in generated_content.metadata, no score or verdict change — and the fail-open paths that ran
    # (services/quality/degraded.py). Declared so LangGraph keeps them (AA-209 gotcha).
    quality_shadow:         list
    degraded:               list
    # AA-645 step 0: "v2" when the draft came from the S1 writer v2 (s1_writer_v2_live, flag S1_WRITER_V2, A1 only);
    # validate and the judge read it (report-only codes / brand-fit). tour_agency = raw_tours group_size /
    # best_time_to_go, which only the v2 fact sheet reads. Declared so LangGraph keeps them (AA-209 gotcha).
    writer_version:         Optional[str]
    tour_agency:            Optional[dict]
    # AA-919 (WM-18 / WM-20): the writing-memory view the run resolved (services/writing_memory/insertion.resolve;
    # None / absent = memory off) and the stamp of the writer that consumed it (the A1 v2 writer). Declared so
    # LangGraph keeps them (AA-209 gotcha).
    writing_memory:         Optional[dict]
    writing_release:        Optional[dict]

# code → (dimension, deduction)
_FAILURE_MAP: dict[str, tuple[str, float]] = {
    "MISSING_FIELD":             ("structure", 1.5),
    "HIGHLIGHTS_NOT_LIST":       ("structure", 1.0),
    "HIGHLIGHTS_TOO_FEW":        ("structure", 0.5),
    "SUBTITLE_GENERIC":          ("brand",     1.0),
    "SUMMARY_OFF_BRAND":         ("brand",     1.0),
    "BRAND_SEO_META_VIOLATION":  ("brand",     1.5),
    "FORBIDDEN_WORD":            ("quality",   0.5),
    "HIGHLIGHTS_TOO_GENERIC":    ("quality",   0.5),
    "SEO_TITLE_TOO_LONG":        ("seo",       0.5),
    "SEO_META_TOO_LONG":         ("seo",       0.5),
    "META_INCOMPLETE_SENTENCE":  ("seo",       1.0),
    "META_TOO_SHORT":            ("seo",       0.5),
    "ITINERARY_STRUCTURE_WEAK":  ("structure", 1.0),
    "DFS_INTENT_UNDERUSED":      ("seo",       1.0),
    # AA-329a: source day dropped entirely from the output — same deduction as
    # ITINERARY_STRUCTURE_WEAK (task explicitly said not to invent a new number).
    "ITINERARY_DAY_COUNT_MISMATCH": ("structure", 1.0),
    # AA-329c: AA-353's own one-shot nudge already ran and the day is STILL outside clamp —
    # heavier than the other structure codes since content this far off is reaching gold today
    # (still confirmed to never alone drop quality_score below MIN_QUALITY — see AA-329
    # implementation notes — so it can't divert the run away from flag_fix_node).
    "ITINERARY_STILL_COMPRESSED":   ("structure", 1.5),
    # AA-925: the day oracle's codes, deducted only when a code is enforced (A1); shadow costs nothing.
    **{code: ("structure", 1.0) for code in day_oracle.DAY_ORACLE_MODES},
}

# AA-240: canonical validate-node forbidden list (single source; validate_node + review-queue
# handler both consume so the re-derived per-field reason can never drift from what fired).
# AA-641: the list now lives in forbidden_words.py so flag_fix_node can use it too (circular import).
_VALIDATE_FORBIDDEN = VALIDATE_FORBIDDEN

# AA-240: failure code -> editable gc field (column) so the review UI marks which field failed.
# Multi-field codes (FORBIDDEN_WORD, MISSING_FIELD) are resolved by re-scanning live content in
# the handler, so they are intentionally NOT in this 1:1 map.
_CODE_FIELD_MAP = {
    "SUBTITLE_GENERIC":          "aa_subtitle",
    "SUMMARY_OFF_BRAND":         "aa_summary",
    "HIGHLIGHTS_NOT_LIST":       "aa_highlights",
    "HIGHLIGHTS_TOO_FEW":        "aa_highlights",
    "HIGHLIGHTS_TOO_GENERIC":    "aa_highlights",
    "ITINERARY_STRUCTURE_WEAK":  "aa_itineraries",
    "ITINERARY_DAY_COUNT_MISMATCH": "aa_itineraries",
    "ITINERARY_STILL_COMPRESSED":   "aa_itineraries",
    "SEO_TITLE_TOO_LONG":        "seo_title",
    "SEO_META_TOO_LONG":         "seo_meta",
    "META_TOO_SHORT":            "seo_meta",
    "META_INCOMPLETE_SENTENCE":  "seo_meta",
    "BRAND_SEO_META_VIOLATION":  "seo_meta",
    "DFS_INTENT_UNDERUSED":      "seo_meta",
}

def _build_brand_diff_block(state: ContentState) -> str:
    """AA-202: brand-differentiation system block. AA-606: moved to batch_prompt.build_brand_diff_block
    (single source of truth) so the Bedrock Batch manifest and the synchronous generate_node produce
    byte-identical system prompts. Thin re-export kept so existing importers/tests still resolve here."""
    return build_brand_diff_block(state)

def _writer_sampling() -> dict:
    """S224 (AA-748 follow-up): the S1 writer has always run at the provider default temperature
    (1.0 for Claude on Bedrock), the likely source of the large run-to-run variance in grounding
    hits and judge scores. `S1_WRITER_TEMPERATURE` (env, unset by default) sets it for an A/B;
    unset or invalid -> no temperature field, so the request is exactly as before."""
    raw = os.environ.get("S1_WRITER_TEMPERATURE", "").strip()
    if not raw:
        return {}
    try:
        value = float(raw)
    except ValueError:
        return {}
    return {"temperature": value} if 0.0 <= value <= 1.0 else {}


def generate_node(state: ContentState) -> ContentState:
    """Node 1: Generate content via LLMClient."""
    client = LLMClient()

    # AA-620: the WRITE stage — "t2_generate" for a tenant (T2) rewrite so admin can tune/price
    # the tenant writer independently, "s1_generate" for A1 admin. Set by the caller's
    # initial_state; None (any older caller not threaded through) -> s1_generate, unchanged.
    gen_stage = state.get("generate_stage") or "s1_generate"
    # AA-620: real tenant for a T2 rewrite, None for A1 admin (its s1_generate writes stay NULL).
    tenant_id = state.get("tenant_id")

    # AA-645 step 0: the S1 writer v2 behind S1_WRITER_V2 (default off; A1 only, never T2 or a batch seed).
    from . import s1_writer_v2_live
    if s1_writer_v2_live.applies(state):
        return _generate_node_v2(state, client)

    # AA-748: behind S1_STRUCTURED_FACTS (default OFF, tenant-flag override), extract per-day source
    # facts once (one s1_source_facts LLM call) and thread them into the writer prompt so numbers/
    # meals are constrained to what the source lists. Fails open: no facts -> prompt as today. Only
    # the A1 admin path extracts here; the Bedrock Batch materializer stays I/O-free (facts absent).
    from .source_facts import extract_day_facts, s1_structured_facts_enabled
    if "structured_facts" not in state and s1_structured_facts_enabled(state.get("tenant_flags")):
        state = {**state, "structured_facts": extract_day_facts(state.get("tour", {}), client=client)}

    # AA-606: attempt-1 (system, user) prompt is materialized by the SAME builder the Bedrock Batch
    # manifest uses (services/content_generation/batch_prompt), so batch and sync writes never drift.
    system = build_s1_system_prompt(state)
    prompt = build_s1_user_prompt(state)

    # Inject feedback nếu đang retry — batch attempt-1 never has feedback, so this stays sync-only.
    # AA-793: a T2 retry / T3 repair round revises the previous (best) draft instead of starting over.
    revising = bool(state.get("is_tenant_rewrite") and state.get("previous_draft")
                    and targeted_repair_enabled())
    if revising:
        prompt += revise_block(state.get("previous_draft"))
    if state.get("feedback"):
        prompt += f"\n\nPREVIOUS ATTEMPT FEEDBACK:\n{state['feedback']}\nPlease fix these issues."

    brand_sp = state.get("brand_system_prompt", "") or ""

    # AA-289: hash the exact system-prompt string sent to the LLM this call — the same string
    # _call_bedrock's use_cache=True path marks with cache_control, so a prompt_version change
    # here is also a cache-key change (a genuinely different prompt SHOULD miss the old cache).
    # AA-606: via prompt_version_of (batch_prompt) so sync + batch compute the version identically.
    prompt_version = prompt_version_of(system)

    is_branded = bool(brand_sp)
    if state.get("is_tenant_rewrite") and not is_branded:
        # AA-792: a T2 brand is the T0 pack (system_prompt is empty for form/.docx tenants), so
        # "branded" means the CLIENT BRAND block carries T0 data — not only a written system_prompt.
        from services.acp_shared.brand_pack import has_brand
        from .batch_prompt import brand_pack_from_state
        is_branded = has_brand(brand_pack_from_state(state))
    prompt_len = len(system)
    # AA-318: log the FULL prompt text sent to the LLM this call, not just its length — the
    # original gap (verifying AA-314's Highlights-leak fix) needed to read the actual user
    # prompt content (tour data, including Highlights) in CloudWatch and found nothing to read;
    # this log line used to measure only `len(system)` (the system prompt) and never touched
    # `prompt` (the user prompt) at all, length or content. Confirmed safe to log in full:
    # neither `system` nor `prompt` is ever built from any secret/credential/env var (grepped
    # prompts.py + this file) — both are pure static instructions + brand rules + tour/SEO data,
    # all of which is either public tour content or the tenant's own brand brief (already
    # visible to that tenant elsewhere in the product). `prompt_version` (AA-289) is included
    # alongside so a specific logged prompt can be tied back to `generated_content.metadata.
    # prompt_version` / the Rewrite History UI without a separate correlation step.
    logger.info("llm_prompt_built", prompt_len=prompt_len, is_branded=is_branded,
                retry=state.get("retry_count", 0), prompt_version=prompt_version,
                system_prompt_text=system, user_prompt_text=prompt)
    if not is_branded:
        logger.warning("unbranded_generation", tour_name=state.get("tour", {}).get("name", ""))

    request = LLMRequest(
        system_prompt=system,
        user_prompt=prompt,
        # AA-518: no more hardcoded "haiku" fallback here — state.get("model_tier") is None
        # whenever the caller (admin_pipeline.py/v1_pipeline.py's own request field) didn't
        # explicitly choose a tier, in which case LLMClient.generate() now falls back to the
        # admin's "s1_generate" stage config instead. An explicit tier (including AA-237's
        # opt-in sonnet re-run) still overrides, unchanged.
        model_tier=state.get("model_tier"),
        stage=gen_stage,  # AA-620: t2_generate for tenant, s1_generate for A1 admin
        max_tokens=GENERATE_MAX_TOKENS,  # AA-639
        **_writer_sampling(),
    )

    resp = None
    logged = False  # AA-927: the writer row of `resp` is written (see the except branch)
    try:
        resp = client.generate(request)
        # Strip markdown fences nếu LLM wrap JSON trong ```json ... ```
        raw = resp.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        # AA-217: deterministic json-repair salvage on malformed output (no LLM re-ask).
        # AA-606: shared parse helper (parse_generated_json) so the batch seed path decodes identically.
        generated = parse_generated_json(raw)
        if not generated:
            logger.warning("json_parse_failed",
                           raw_len=len(raw),
                           model_used=resp.model_used if resp else None,
                           fallback_used=resp.fallback_used if resp else None,
                           retry_count=state.get("retry_count"))
            record_call_sync(
                stage=gen_stage, role="writer", model=resp.model_used,  # AA-620
                tokens_in=getattr(resp, "input_tokens", None), tokens_out=getattr(resp, "output_tokens", None),
                cost_usd=resp.cost_usd, tenant_id=tenant_id,  # AA-620: real tenant for T2, None for A1
                quality_signal={"json_parsed": False, "fallback_used": resp.fallback_used},
                stop_reason=getattr(resp, "stop_reason", None),
                account=getattr(resp, "satellite_account", None),
                fallback_used=getattr(resp, "fallback_used", None),
            )
            logged = True
            return {**state, "generated": {}, "is_branded": is_branded,
                    "cost_usd": state.get("cost_usd", 0) + resp.cost_usd,
                    "prompt_version": prompt_version,
                    "cache_read_tokens": state.get("cache_read_tokens", 0) + resp.cache_read_tokens,
                    "cache_write_tokens": state.get("cache_write_tokens", 0) + resp.cache_write_tokens,
                    "error": "JSON parse error"}
        logger.info("content_generated", retry=state.get("retry_count", 0),
                    model=resp.model_used, cost=resp.cost_usd)
        # AA-225: persist keywords thực sự inject vào prompt (khớp prompts.py:61 + validate_node normalize)
        _apply_seo_keywords_used(generated, state)
        # AA-724: deterministically fit an over-long seo_meta before validate scores it (mirrors the
        # T2 path's AA-641 fit) so SEO_META_TOO_LONG doesn't loop the gate on a count the LLM missed.
        _apply_seo_meta_fit(generated, state)
        # AA-353: clamp/nudge the structured itineraries array against its own per-day source
        # length target, then serialize back to the plain string every downstream node expects.
        _itin_result = _process_itineraries(generated, state.get("tour", {}), client,
                                            stage=stage_for(state, "s1_itinerary_nudge"),  # AA-763
                                            voice_hint=nudge_voice_hint(state))  # AA-792
        # AA-738: strip forbidden words deterministically before validate scores them — after the
        # itinerary nudge, so its output is covered too.
        _apply_forbidden_strip(generated, state)
        # AA-620 — resolves the AA-505/AA-434 gap this comment used to flag: ContentState now
        # carries tenant_id + generate_stage, so a T2 tenant rewrite logs stage="t2_generate" with
        # the real tenant_id, while an A1 admin rewrite logs stage="s1_generate" with tenant_id
        # NULL (A1 leaves both unset) — the two are finally distinguishable in llm_call_log.
        record_call_sync(
            stage=gen_stage, role="writer", model=resp.model_used,  # AA-620
            tokens_in=getattr(resp, "input_tokens", None), tokens_out=getattr(resp, "output_tokens", None),
            cost_usd=resp.cost_usd, tenant_id=tenant_id,  # AA-620: real tenant for T2, None for A1
            quality_signal={"json_parsed": True, "fallback_used": resp.fallback_used,
                            **({"revision": True} if revising else {})},  # AA-793
            stop_reason=getattr(resp, "stop_reason", None),
            account=getattr(resp, "satellite_account", None),
            fallback_used=getattr(resp, "fallback_used", None),
        )
        logged = True
        return {
            **state,
            "generated":  generated,
            "cost_usd":   state.get("cost_usd", 0) + resp.cost_usd + _itin_result["extra_cost_usd"],
            "model_used": resp.model_used,
            "is_branded": is_branded,
            "error":      "",
            "fallback_used": resp.fallback_used,
            "satellite_account": resp.satellite_account,
            "prompt_version": prompt_version,
            "cache_read_tokens": (state.get("cache_read_tokens", 0) + resp.cache_read_tokens
                                   + _itin_result["extra_cache_read_tokens"]),
            "cache_write_tokens": (state.get("cache_write_tokens", 0) + resp.cache_write_tokens
                                    + _itin_result["extra_cache_write_tokens"]),
            "itinerary_day_ratios": _itin_result["day_ratios"],
            "itinerary_nudges_used": _itin_result["nudges_used"],
        }
    except Exception as e:
        logger.error("generation_failed", error=str(e))
        if resp is not None and not logged:
            # AA-927: billed, then the post-call code raised — a billed_unusable row (no json_parsed).
            record_unusable_sync(resp, stage=gen_stage, role="writer", error=e, tenant_id=tenant_id)
        return {**state, "generated": {}, "is_branded": is_branded,
                "prompt_version": prompt_version,
                "cost_usd": state.get("cost_usd", 0) + (resp.cost_usd if resp else 0),
                "error": str(e)}

def _generate_node_v2(state: ContentState, client: LLMClient) -> ContentState:
    """AA-645 step 0 — one S1 writer v2 draft (s1_writer_v2_live.write: writer + meta + one meta retry, each logged).
    No itinerary nudge and no seo_meta fit (v2 never cuts a meta; validate / flag_fix see what it wrote); the
    deterministic forbidden strip runs as on the v1 path (AA-738)."""
    from . import s1_writer_v2_live
    out = s1_writer_v2_live.write(state, client)
    generated = out["generated"]
    if generated:
        _apply_forbidden_strip(generated, state)
    return {
        **state,
        "generated": generated,
        "writer_version": s1_writer_v2_live.WRITER_VERSION,
        "cost_usd": state.get("cost_usd", 0) + out["cost_usd"],
        "model_used": out.get("model_used") or state.get("model_used", ""),
        "is_branded": True,
        "error": out["error"] if not generated else "",
        "fallback_used": bool(out.get("fallback_used")),
        "satellite_account": out.get("satellite_account"),
        "prompt_version": out["prompt_version"],
        "writing_release": out.get("writing_release"),   # AA-919: the release the v2 draft was written with
        "cache_read_tokens": state.get("cache_read_tokens", 0) + out["cache_read_tokens"],
        "cache_write_tokens": state.get("cache_write_tokens", 0) + out["cache_write_tokens"],
        "itinerary_day_ratios": [],
        "itinerary_nudges_used": 0,
    }


# AA-251 (ADR-2026-021, hướng 4): generic tour-marketing filler that shouldn't count
# toward a DFS_INTENT_UNDERUSED keyword match — a keyword matching only on "tours"
# is not evidence the content reflects real search intent.
_DFS_INTENT_STOPWORDS = frozenset({
    "tour", "tours", "travel", "trip", "trips", "package", "packages",
    "best", "top", "in", "the", "a", "of", "and", "for", "to", "with",
})
_DFS_INTENT_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Fraction of a keyword's significant tokens that must show up in seo_title+seo_meta
# for the keyword to count as reflected. 0.5 tolerates word-order/synonym drift
# without accepting a single incidental token match on an unrelated keyword.
_DFS_INTENT_OVERLAP_THRESHOLD = 0.5


def _dfs_intent_tokens(text: str) -> set[str]:
    return {
        t for t in _DFS_INTENT_TOKEN_RE.findall(text.lower())
        if t not in _DFS_INTENT_STOPWORDS and len(t) > 2
    }


def _keyword_intent_matched(kw_texts: list[str], field_text: str) -> bool:
    """Token-overlap match: a keyword counts as reflected once >= threshold of its
    significant tokens appear in seo_title+seo_meta — not the whole phrase verbatim."""
    field_tokens = _dfs_intent_tokens(field_text)
    if not field_tokens:
        return False
    for kw in kw_texts:
        kw_tokens = _dfs_intent_tokens(kw)
        if not kw_tokens:
            continue
        overlap = kw_tokens & field_tokens
        if len(overlap) / len(kw_tokens) >= _DFS_INTENT_OVERLAP_THRESHOLD:
            return True
    return False


def parse_generated_json(raw: str) -> Optional[dict]:
    """AA-606: parse a writer LLM's raw text into the ``generated`` dict, with the SAME markdown-
    fence strip + AA-217 json-repair salvage generate_node uses inline. Returns None when even the
    salvage can't produce a dict with a ``name`` (the sync path's own "parse failed" condition).

    Factored out so the Bedrock Batch ingest path (seed_generated_node) decodes batch output byte-
    identically to the synchronous generate_node — no second, drifting JSON parser."""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        salvaged = repair_json(raw, return_objects=True)
        if isinstance(salvaged, dict) and salvaged.get("name"):
            return salvaged
        return None


def _apply_seo_keywords_used(generated: dict, state: ContentState) -> None:
    """AA-225: record the keywords actually injected into the prompt onto the generated dict —
    shared by generate_node and the batch seed node so both persist the same seo_keywords_used."""
    _seo_used = state.get("seo", {})
    _kws_raw = _seo_used.get("top_keywords", []) or _seo_used.get("keywords", {}).get("top_keywords", [])
    _kws_norm = [
        (kw["keyword"] if isinstance(kw, dict) else str(kw))
        for kw in _kws_raw[:5] if kw
    ]
    if isinstance(generated, dict):
        generated["seo_keywords_used"] = _kws_norm


def _apply_seo_meta_fit(generated: dict, state: ContentState) -> bool:
    """AA-724: deterministically fit an out-of-band seo_meta BEFORE validate scores it, the same
    discipline AA-641 gave the T2 path. Runs in generate_node and the batch seed node (live + Bedrock
    Batch), and AA-740 again in revalidate_node after the last LLM repair.

    AA-740: since AA-736 the length codes block Master, but the salvage alone left a one-sentence
    meta > 155 or a meta < 140 unchanged (S218: 15 tours scoring >= 7 stuck in review). Now
    fit_seo_meta_final also appends a clause built from the tour's own facts (days, country); seo_title
    gets fit_seo_title (AA-639). AA-925: it no longer cuts a too-long meta mid-clause — flag_fix's LLM
    meta repair does. Returns True if anything changed. What still misses the band is left for the hard
    code."""
    if not isinstance(generated, dict):
        return False
    changed = False
    tour = state.get("tour") or {}
    meta = generated.get("seo_meta")
    if isinstance(meta, str):
        # AA-794: name too, so a multi-country tour never gets an "in <one country>" tail.
        new = fit_seo_meta_final(meta, {"duration": tour.get("duration"), "country": tour.get("country"),
                                        "name": tour.get("name")},
                                 state.get("brand_forbidden_words"))
        if new != meta:
            generated["seo_meta"], changed = new, True
    title = generated.get("seo_title")
    if isinstance(title, str) and len(title) > 60:
        new = fit_seo_title(title)
        if new != title:
            generated["seo_title"], changed = new, True
    return changed


def _apply_forbidden_strip(generated: dict, state: ContentState) -> bool:
    """AA-738: replace/drop forbidden words (AA list + the brand's list) in the writer output, in
    place, before validate_node scores it. Asking the model to avoid the list (prompt) and retrying
    on Sonnet (AA-736) both left ordinary brand words like "explore"/"package" in ~10% of tours.
    What cannot be cleaned is left as is: validate fires FORBIDDEN_WORD and the AA-736 gate holds."""
    if not isinstance(generated, dict) or not generated:
        return False
    # AA-800: a T2 tenant rewrite strips the tenant's layered list (its signature words stay; stock
    # superlatives only when there are more than one); A1 strips the AA list + brand list as before.
    words = state_forbidden(state, generated) if state.get("is_tenant_rewrite") else None
    cleaned, report = strip_forbidden(generated, state.get("brand_forbidden_words"), words=words)
    changed = bool(report["replaced"] or report["dropped"])
    if changed or report["unresolved"]:
        generated.clear()
        generated.update(cleaned)
        logger.info("forbidden_strip_applied", tour=(state.get("tour") or {}).get("name"),
                    replaced=len(report["replaced"]), dropped=len(report["dropped"]),
                    unresolved=sorted({f"{f}:{w}" for f, w in report["unresolved"]}))
    return changed


def seed_generated_node(state: ContentState) -> ContentState:
    """AA-606: entry node for build_graph_from_generated — nạp text writer đã sinh SẴN bởi Bedrock
    Batch (attempt-1) vào ``generated``, chạy đúng hậu xử lý generate_node làm (seo_keywords_used +
    _process_itineraries), rồi để graph chạy tiếp validate → judge → gate như thường.

    state phải chứa ``batch_text`` (raw model text cho tour này). Parse fail → generated={} + error
    (giống nhánh json_parse_failed của generate_node) → gate sẽ route xuống retry (generate on-demand)
    hoặc hitl, không crash. model_used/satellite_account lấy từ batch metadata state đã set sẵn."""
    raw = state.get("batch_text", "") or ""
    generated = parse_generated_json(raw)
    if not generated:
        logger.warning("s1_batch_seed_parse_failed", tour_id=state.get("tour_id"), raw_len=len(raw))
        return {**state, "generated": {}, "error": "batch JSON parse error"}

    _apply_seo_keywords_used(generated, state)
    _apply_seo_meta_fit(generated, state)  # AA-724: same deterministic seo_meta fit as the live path
    # Reuse the exact itinerary clamp/nudge + serialize the sync path runs. LLMClient() is only used
    # by _process_itineraries for the optional per-day nudge (on-demand, small) — batch doesn't cover it.
    _itin_result = _process_itineraries(generated, state.get("tour", {}), LLMClient(),
                                        stage=stage_for(state, "s1_itinerary_nudge"),  # AA-763
                                        voice_hint=nudge_voice_hint(state))  # AA-792
    _apply_forbidden_strip(generated, state)  # AA-738: same deterministic strip as the live path
    logger.info("s1_batch_seeded", tour_id=state.get("tour_id"),
                model=state.get("model_used", "batch-haiku"))
    return {
        **state,
        "generated": generated,
        "is_branded": bool(state.get("brand_system_prompt")),
        "error": "",
        "cost_usd": state.get("cost_usd", 0) + _itin_result["extra_cost_usd"],
        "cache_read_tokens": state.get("cache_read_tokens", 0) + _itin_result["extra_cache_read_tokens"],
        "cache_write_tokens": state.get("cache_write_tokens", 0) + _itin_result["extra_cache_write_tokens"],
        "itinerary_day_ratios": _itin_result["day_ratios"],
        "itinerary_nudges_used": _itin_result["nudges_used"],
    }


# AA-228 day-title separator normaliser ("Day N -- title", "Day N – title" -> "Day N — title"). AA-990 (S253): a ranged
# heading keeps its range — "Day 12-15 — Beach" was rewritten to "Day 12 — 15 — Beach", which every reader took as
# Day 12 alone (days 13-15 lost without a day-count finding: Sri Lanka b45db4c8).
# AA-990 review: a range (and the plural "Days") is only read at the START of a line, and only when the range end is
# followed by whitespace and a dash — so a number joined to a word ("Day 2 – 4-hour trek", "Day 2 - 10-km walk")
# stays the title, and prose ("from Day 3 to 5 — the hardest stretch") is left alone. Group 4 = the range end; a range
# that day_parse.day_range rejects (backwards, over two weeks) keeps the old single-day rewrite.
_DAY_HEAD_DASH_RE = re.compile(
    r"(?<![^\n])([ \t]*)Day(s?)[ \t]+(\d+)(?:[ \t]*(?:[-–]|to)[ \t]*(\d{1,3})(?=[ \t]+[-–—]))?\s*[-–—]+\s*")
# anywhere else: the AA-228 single-day rewrite, as before — except over a range heading the line-start pass wrote
_DAY_DASH_RE = re.compile(r"\bDay\s+(\d+)(?!-\d{1,3} —)\s*[-–—]+\s*")
# a canonical heading for the "title | prose" and blank-line normalisers: a single "Day N —" anywhere (AA-228), a
# ranged / plural one only at a line start
_SINGLE_HEAD = r"Day\s+\d+\s+—"
_HEAD_ANY = r"(?:(?<![^\n])[ \t]*" + CANONICAL_HEAD_NC + r"|" + _SINGLE_HEAD + r")"


def _canonical_day_dash(m: "re.Match") -> str:
    lead, plural, first = m.group(1), m.group(2), int(m.group(3))
    if m.group(4):
        last = day_range(first, int(m.group(4)))[1]
        if last > first:
            return f"{lead}Day {first}-{last} — "
        return f"{lead}Day{plural} {first} — {m.group(4)} — "
    return f"{lead}Day{plural} {first} — "


def normalise_day_dashes(it: str) -> str:
    """AA-228 + AA-990: every day-heading dash spelling -> the canonical "Day N — " / "Day A-B — "."""
    it = _DAY_HEAD_DASH_RE.sub(_canonical_day_dash, it)       # line starts: ranges and "Days" kept (AA-990)
    return _DAY_DASH_RE.sub(r"Day \1 — ", it)                # anywhere: single day, as before


def validate_node(state: ContentState) -> ContentState:
    """Node 2: Quality check — structured failure codes, 4 sub-dimensions, score 0-10."""
    generated = state.get("generated", {})
    if generated.get("itineraries"):
        import re as _re
        it = generated["itineraries"]
        if isinstance(it, str):
            it = _re.sub(r"\*\*([^*]+)\*\*", r"\1", it)
            it = it.replace("**", "")
            # AA-228: normalize day-title separator to a single canonical form across model tiers.
            # Haiku emits "Day N -- title | prose ... || Day N+1", Sonnet/GPT emit "Day N — title".
            it = it.replace("||", "\n\n")                         # inter-day inline sep -> blank line
            it = normalise_day_dashes(it)                          # any dash -> em-dash, ranges kept (AA-990)
            it = _re.sub(r"(" + _HEAD_ANY + r"\s+[^\n|]+?)\s*\|\s*", r"\1\n", it)  # "title | prose" -> newline
            # ensure a blank line before each "Day N" that isn't already at start/after blank line; a ranged or
            # plural heading only where it already starts a line (AA-990 review: never split prose out as a day)
            it = _re.sub(r"(?<=\S)\n?(?=" + _SINGLE_HEAD + r")", "\n\n", it.lstrip())
            it = _re.sub(r"(?<=\S)\n(?=[ \t]*" + CANONICAL_HEAD_NC + r")", "\n\n", it)
            it = _re.sub(r"[ \t]+\n", "\n", it)                   # strip trailing spaces before newline
            it = _re.sub(r"\n[ \t]+", "\n", it)                   # strip leading spaces after newline
            it = _re.sub(r"\n{3,}", "\n\n", it)                   # collapse extra blank lines
        elif isinstance(it, dict):
            parts = [f"{k}: {v}" for k, v in it.items()]
            it = "\n\n".join(parts)
        elif isinstance(it, list):
            parts = []
            for item in it:
                if isinstance(item, dict):
                    day   = item.get("day", "")
                    title = item.get("title", "")
                    desc  = item.get("description", "")
                    acts  = item.get("activities", [])
                    day_str = f"Day {day}"
                    if title:
                        day_str += f" — {title}"
                    if desc:
                        day_str += f"\n{desc}"
                    if acts:
                        act_list = ", ".join(str(a) for a in acts) if isinstance(acts, list) else str(acts)
                        day_str += f"\n*Activities: {act_list}*"
                    parts.append(day_str)
                else:
                    parts.append(str(item))
            it = "\n\n---\n\n".join(parts)
        else:
            it = str(it)
        generated = {**generated, "itineraries": str(it).strip()}
    tour      = state.get("tour", {})
    issues: list[str] = []
    fired:  list[str] = []
    score = 10.0

    # Required fields
    for field in ["name", "subtitle", "summary", "highlights", "itineraries",
                  "seo_title", "seo_meta"]:
        if not generated.get(field):
            issues.append(f"Missing field: {field}")
            fired.append("MISSING_FIELD")
            score -= 1.5

    # highlights must be list with 3+ items
    highlights = generated.get("highlights", [])
    if not isinstance(highlights, list):
        issues.append("highlights must be a list")
        fired.append("HIGHLIGHTS_NOT_LIST")
        score -= 1.0
    elif len(highlights) < 3:
        issues.append(f"highlights has only {len(highlights)} items, need 3+")
        fired.append("HIGHLIGHTS_TOO_FEW")
        score -= 0.5

    # Subtitle must not be generic
    subtitle = generated.get("subtitle", "").lower()
    generic_subtitle_flags = [
        "kingdom of happiness", "land of thunder dragon",
        "last shangri-la", "pristine wilderness",
    ]
    for flag in generic_subtitle_flags:
        if flag in subtitle:
            issues.append(f"Generic subtitle phrase: '{flag}'")
            fired.append("SUBTITLE_GENERIC")
            score -= 1.0

    # Forbidden words — AA core list + tenant custom list
    # P3-S3: AA core list + the tenant's own words (AA-641: shared with flag_fix_node)
    # AA-800: a T2 tenant rewrite is checked against its layered list (state_forbidden): platform truth,
    # the positioning words it did not declare, its own list, and the stock superlatives once there is more
    # than one. A1 is unchanged (all_forbidden).
    if state.get("is_tenant_rewrite"):
        all_forbidden = state_forbidden(state, generated)
    else:
        all_forbidden = all_forbidden_words(state.get("brand_forbidden_words"))

    # AA-738: scan the copy only. seo_keywords_used holds DFS keywords (e.g. "cheap summer getaways",
    # or the source title "Explore Amazing Five ...") — data the writer did not author and no repair
    # can change, so a hit there blocked Master permanently (S218: 13/538 latest versions).
    # Plain text, not json.dumps — the dump's escapes hide a word after "\n" or "—" (copy_text).
    content_text = copy_text({k: v for k, v in generated.items()
                              if k not in _FORBIDDEN_SCAN_SKIP}).lower()
    for word in all_forbidden:
        if has_word(content_text, word):
            issues.append(f"Forbidden word: '{word}'")
            fired.append("FORBIDDEN_WORD")
            score -= 0.5
    if state.get("is_tenant_rewrite"):
        # AA-800: one stock superlative in a tenant tour is a warning (soft code, no deduction); a second
        # one made them all FORBIDDEN_WORD above.
        from services.acp_shared.signature_vocabulary import superlative_hits
        _sup = superlative_hits(content_text)
        if len(_sup) == 1:
            issues.append(f"Stock superlative (allowed once per tour, prefer a concrete detail): '{_sup[0]}'")
            fired.append("STOCK_SUPERLATIVE")

    # Highlights specificity — must not be pure generic
    generic_highlight_patterns = [
        "panoramic views", "beautiful landscapes", "stunning scenery",
        "breathtaking views", "pristine nature", "gross national happiness",
    ]
    highlights_text = " ".join(highlights).lower() if isinstance(highlights, list) else ""
    for pattern in generic_highlight_patterns:
        if pattern in highlights_text:
            issues.append(f"Generic highlight phrase: '{pattern}'")
            fired.append("HIGHLIGHTS_TOO_GENERIC")
            score -= 0.5

    # seo_meta must not contain budget/accommodation language (AA audience = $250k+)
    _seo_meta_forbidden = SEO_META_FORBIDDEN  # AA-238/D4: canonical deny-list
    seo_meta_lower = (generated.get("seo_meta") or "").lower().replace("-", " ")  # AA-238: catch hyphen variants
    for term in _seo_meta_forbidden:
        if has_word(seo_meta_lower, term.replace("-", " ")):
            issues.append(f"Budget language in seo_meta: '{term}'")
            fired.append("BRAND_SEO_META_VIOLATION")
            score -= 1.5
            break  # one violation is enough

    # SEO field lengths
    if len(generated.get("seo_title", "")) > 60:
        issues.append("seo_title exceeds 60 chars")
        fired.append("SEO_TITLE_TOO_LONG")
        score -= 0.5
    if len(generated.get("seo_meta", "")) > SEO_META_MAX:
        issues.append("seo_meta exceeds 155 chars")
        fired.append("SEO_META_TOO_LONG")
        score -= 0.5
    # AA-201: enforce lower band — no under-length meta
    _meta_len = generated.get("seo_meta", "").strip()
    if _meta_len and len(_meta_len) < SEO_META_MIN:
        issues.append("seo_meta under 140 chars")
        fired.append("META_TOO_SHORT")
        score -= 0.5

    # Summary generic opener check
    summary = generated.get("summary", "")
    generic_openers = [
        "journey into", "journey along", "embark on", "discover the magic",
        "experience the wonder", "this refined", "this carefully",
        "this curated", "designed for discerning",
    ]
    for opener in generic_openers:
        if summary.lower().startswith(opener):
            issues.append(f"Generic summary opener: '{opener}'")
            fired.append("SUMMARY_OFF_BRAND")
            score -= 1.0
            break

    # META_INCOMPLETE_SENTENCE: seo_meta must read as a complete sentence (AA-201)
    meta = generated.get("seo_meta", "").strip()
    if meta and not meta_complete_sentence(meta):
        issues.append("seo_meta is not a complete sentence")
        fired.append("META_INCOMPLETE_SENTENCE")
        score -= 1.0

    # AA-645 step 0: a v2 draft's length-ratio and DFS-keyword codes are report-only (recorded in quality_shadow).
    from .s1_writer_v2_live import is_v2 as _is_v2
    _v2_draft = _is_v2(state)
    _v2_report: list[dict] = []

    # ITINERARY_STRUCTURE_WEAK: must have day markers and be substantive
    itinerary = generated.get("itineraries", "") or ""
    has_day_marker = bool(re.search(r'\bday\s*[1-9one two three]\b', itinerary.lower()))
    if not has_day_marker or len(itinerary) < 80:
        issues.append("Itinerary lacks day structure or is too short")
        fired.append("ITINERARY_STRUCTURE_WEAK")
        score -= 1.0

    # AA-329(a)/(c): compare against the SOURCE itinerary's own day markers/lengths, computed
    # FRESH every call (not from state["itinerary_day_ratios"], which only reflects generate_node's
    # one-shot AA-353 nudge and would go stale once flag_fix_node repairs a day — see AA-329
    # implementation notes). Skipped entirely when the source has no reliable day markers
    # (parse_source_day_word_counts fell back to an even split) — nothing trustworthy to compare.
    _src_raw = tour.get('itineraries') or tour.get('itinerary') or ""
    # AA-990 N3: one function for this check and the final day guard (final_day_guard), which re-runs it on the
    # text that is persisted after every later edit.
    _gap = itinerary_day_gap(tour, itinerary)
    if _gap is not None:
        _source_day_words = _gap["source_day_words"]
        _source_days = set(_source_day_words)
        _generated_day_words = _gap["generated_day_words"]
        _generated_days = set(_generated_day_words)

        # AA-329a: whole days present in the source but absent from the output.
        if _gap["missing"]:
            issues.append(day_gap_issue(_gap))
            fired.append("ITINERARY_DAY_COUNT_MISMATCH")
            score -= 1.0

        # AA-329c: days present in both, but the output is still outside the AA-353 clamp
        # relative to its own source day — i.e. generate_node's one-shot nudge (_process_itineraries)
        # already ran on this content and didn't bring the day back in range.
        _still_compressed = []
        for _day in sorted(_source_days & _generated_days):
            _src_words = _source_day_words[_day]
            _gen_words = _generated_day_words[_day]
            if not _src_words:
                continue
            _ratio = _gen_words / _src_words
            if not (ITINERARY_CLAMP_MIN <= _ratio <= ITINERARY_CLAMP_MAX):
                _still_compressed.append((_day, _gen_words, _src_words, round(_ratio, 3)))
        if _still_compressed:
            _detail = "; ".join(
                f"day {d}: {gw} words vs source {sw} words (ratio {r})"
                for d, gw, sw, r in _still_compressed
            )
            if _v2_draft:
                # AA-645 step 0: v2 has no per-day length target and no nudge ("length follows the source"), so
                # the ratio is recorded, not scored.
                _v2_report.append({"code": "ITINERARY_STILL_COMPRESSED", "detector": "s1_writer_v2",
                                   "mode": "report", "detail": _detail})
            else:
                issues.append(f"Itinerary still compressed/expanded vs source after nudge — {_detail}")
                fired.append("ITINERARY_STILL_COMPRESSED")
                score -= 1.5

    # AA-925 (Q-2): the day oracle (struct.day_structure) on the written itinerary against the source, and the
    # S2 cut signature on the meta (struct.truncated). Shadow findings go to quality_shadow only (no score, no
    # verdict); an enforced day code blocks A1 Master — never a tenant rewrite, whose structure T3 checks
    # through this same function and whose writer reads the master text (lesson AA-800).
    shadow: list[dict] = []
    _day_degraded: list[dict] = []
    try:
        _day_findings = day_oracle.check(_src_raw, tour.get('duration'), itinerary)
    except Exception as exc:  # a detector bug must not break validate; it is counted as degraded
        logger.warning("day_oracle_failed", error=str(exc)[:200])
        _day_findings = []
        _day_degraded.append({"name": degraded.DAY_CHECK_SKIPPED, "count": 1, "detail": f"day_oracle: {exc}"})
    if any(_f["code"] == day_oracle.DAY_CHECK_SKIPPED for _f in _day_findings):
        _day_degraded.append({"name": degraded.DAY_CHECK_SKIPPED, "count": 1,
                              "detail": "no source day markers and no usable duration"})
    for _f in _day_findings:
        if _f["mode"] == "enforce" and not state.get("is_tenant_rewrite"):
            issues.append(f"{_f['code']}: {_f['detail']}")
            fired.append(_f["code"])
            score -= _FAILURE_MAP[_f["code"]][1]
        else:
            shadow.append({**_f, "mode": "shadow"})
    _cut = meta_cut_signature(meta) if meta else None
    if _cut:
        shadow.append({"code": "META_CUT_SIGNATURE", "class_id": "struct.truncated",
                       "detector": "meta_cut_signature", "mode": "shadow", "detail": _cut})

    # DFS_INTENT_UNDERUSED: if SEO keywords exist, at least one should appear in title+meta
    # B1: handle both flat {"top_keywords": [...]} and nested {"keywords": {"top_keywords": [...]}}
    _seo    = state.get("seo", {})
    seo_kws = _seo.get("top_keywords", []) or _seo.get("keywords", {}).get("top_keywords", [])
    if seo_kws:
        kw_texts = [
            (kw["keyword"] if isinstance(kw, dict) else str(kw)).lower()
            for kw in seo_kws if kw
        ]
        seo_field_text = (
            (generated.get("seo_title", "") or "") + " " +
            (generated.get("seo_meta", "") or "")
        ).lower()
        if kw_texts and not _keyword_intent_matched(kw_texts, seo_field_text):
            if _v2_draft:
                # AA-645 step 0: the v2 meta uses a source-covered keyword only when it fits (never forced, S233).
                _v2_report.append({"code": "DFS_INTENT_UNDERUSED", "detector": "s1_writer_v2", "mode": "report",
                                   "detail": "no DFS keyword in seo_title / seo_meta"})
            else:
                issues.append("SEO keywords not reflected in seo_title or seo_meta")
                fired.append("DFS_INTENT_UNDERUSED")
                score -= 1.0

    # Sub-score computation — each dimension starts at 10, deducts its own codes
    score = max(0.0, score)
    sub_scores = {
        dim: max(0.0, 10.0 - sum(
            _FAILURE_MAP[code][1] for code in fired
            if code in _FAILURE_MAP and _FAILURE_MAP[code][0] == dim
        ))
        for dim in ("brand", "seo", "structure", "quality")
    }
    # AA-216: a MISSING_FIELD means the generation is structurally incomplete (often a JSON-parse
    # failure → empty generated). Cap below MIN_QUALITY so should_retry never returns "done" and
    # _is_publishable (AA-211) blocks it — empty content must route to retry/HITL, never gold.
    _structural_fail = ("MISSING_FIELD" in fired) or (not generated.get("name"))
    quality_score = sum(sub_scores.values()) / 4
    if _structural_fail:
        quality_score = min(quality_score, MISSING_FIELD_CAP)
    failure_codes = list(dict.fromkeys(fired))   # unique, first-seen order
    passed_count  = sum(1 for d in sub_scores.values() if d == 10.0)
    failed_count  = len(failure_codes)
    feedback      = "; ".join(issues) if issues else ""

    logger.info("validation_done", score=quality_score, issues=len(issues),
                retry=state.get("retry_count", 0),
                failure_codes=failure_codes, sub_scores=sub_scores,
                quality_shadow=sorted({f["code"] for f in shadow}))

    return {
        **state,
        "generated":     generated,
        "quality_score": quality_score,
        "feedback":      feedback,
        "failure_codes": failure_codes,
        "sub_scores":    sub_scores,
        "passed_count":  passed_count,
        "failed_count":  failed_count,
        "quality_shadow": shadow + _v2_report,  # AA-925: recomputed on every validate (the latest content's findings)
        # AA-925: the day check that could not run, for the latest content (replaced on every validate)
        "degraded": degraded.replace_detector(state.get("degraded"), day_oracle.DETECTOR, _day_degraded),
    }

def should_retry(state: ContentState) -> str:
    """Edge: decide retry / done / hitl."""
    retry_count = state.get("retry_count", 0)
    score       = state.get("quality_score", 0)

    if score >= MIN_QUALITY:
        return "done"
    if retry_count < MAX_RETRIES - 1:
        return "retry"
    return "hitl"

def increment_retry(state: ContentState) -> ContentState:
    return {**state, "retry_count": state.get("retry_count", 0) + 1}

def _apply_grounding_recheck(state: ContentState) -> ContentState:
    """AA-691: after flag_fix, re-check grounding on the final content (numbers are free, unchanged
    sentences hit the Jev Verdict cache). Anything still unsupported → manual_check (HITL), its codes
    back on failure_codes (validate_node rebuilt them without grounding). ADR 0007: never publish an
    unsupported claim, never delete.

    AA-925: the last step of the A1 graph, so it is where the one degraded hold is applied
    (degraded.apply_a1_hold: a hard fail-open detector → DEGRADED_DETECTOR + manual_check).

    AA-990 N3: and where the day structure of the final text is checked (final_day_guard), after the sentence
    re-repair below — the last edit of the graph."""
    return degraded.apply_a1_hold(final_day_guard(_grounding_recheck(state)))


def _grounding_recheck(state: ContentState) -> ContentState:
    if not state.get("grounding_ran"):
        return state
    violations = state.get("grounding_violations") or []
    if state.get("fix_pass_applied"):
        res = regrounding(state)
        if res is not None:
            violations = res["violations"]
            state = {**state, "degraded": degraded.replace_detector(
                state.get("degraded"), "grounding", grounding_degraded_entries(res))}
        else:   # regrounding_failed: the repaired content was never checked
            state = {**state, "degraded": degraded.replace_detector(
                state.get("degraded"), "grounding",
                [{"name": degraded.GROUNDING_NOT_RUN, "count": 1, "detail": "regrounding_failed"}])}
        if violations:
            # flag_fix (e.g. the per-day itinerary nudge) can re-introduce a figure the grounding node
            # had removed — one more sentence repair before escalating to HITL (S204 smoke, Bhutan).
            rr = repair_and_recheck(state.get("generated", {}), violations, state.get("tour", {}),
                                    model_tier=state.get("model_tier"),
                                    brand_forbidden_words=state.get("brand_forbidden_words"))
            violations = rr["violations"]
            lst = state.get("degraded")
            if rr.get("check") is not None:
                lst = degraded.replace_detector(lst, "grounding", grounding_degraded_entries(rr["check"]))
            if rr.get("contract") is not None and not rr["contract"].ok:
                lst = degraded.add(lst, degraded.REPAIR_CONTRACT, detector="grounding_repair",
                                   count=len(rr["contract"].fields()), detail=", ".join(rr["contract"].invariants()))
            state = {**state, "generated": rr["generated"], "cost_usd": state.get("cost_usd", 0) + rr["cost_usd"],
                     "grounding_repaired_fields": sorted(set(state.get("grounding_repaired_fields") or [])
                                                         | set(rr["fields"])),
                     "degraded": lst}
    if not violations:
        return {**state, "grounding_violations": []}
    codes = list(dict.fromkeys(v["code"] for v in violations))
    logger.info("grounding_still_unsupported", violations=len(violations), codes=codes)
    out = {
        **state,
        "grounding_violations": violations,
        "failure_codes": list(dict.fromkeys((state.get("failure_codes") or []) + codes)),
        "brand_audit_status": "manual_check",
    }
    if state.get("revalidate_ran"):
        out["revalidate_passed"] = False
    return out


def _contract_after_edit(state: ContentState, before: dict, *, detector: str) -> ContentState:
    """AA-925: ``repair_contract.enforce_fields`` over ``state["generated"]`` against ``before`` (in place on the
    dict, as the strip / fit edit it), plus a soft ``degraded: repair_contract`` entry on a revert."""
    after = state.get("generated")
    if not isinstance(after, dict):
        return state
    tour = state.get("tour") or {}
    source = grounding_source_text(tour)
    # The fit's fact tail states the day count tour_days() derives ("8 nights" -> "over 9 days", "2 weeks" ->
    # 14): a derived fact of the source, not a new specific (review of PR 3).
    days = tour_days(tour.get("duration"))
    if days:
        source += f"\n{days} days"
    kept, res = repair_contract.enforce_fields(before, after, source=source, names=False)
    if res.ok:
        return state
    after.clear()
    after.update(kept)
    logger.warning("repair_contract_revert", detector=detector, fields=res.fields(), invariants=res.invariants())
    return {**state, "degraded": degraded.add(state.get("degraded"), degraded.REPAIR_CONTRACT, detector=detector,
                                              count=len(res.fields()), detail=", ".join(res.invariants()))}


def revalidate_node(state: ContentState) -> ContentState:
    """AA-215: verify the fix pass actually worked before publish.

    Only re-checks tours that flag_fix repaired (fix_pass_applied=True). For those, re-run the
    SAME validate + judge nodes on the repaired content and rewrite brand_audit_status to the
    POST-fix state (was stale/PRE-fix). Passthrough for everything else:
      - clean pass (no fix): already validated/judged, still publishable — untouched.
      - flagged/manual_check but NOT fixed: already blocked by _is_publishable (AA-211) — untouched.
    Re-validate (rule-based, free) catches structural/SEO regressions from the fix; re-judge
    (GPT-4.1) re-confirms brand-fit. On fail -> manual_check so the existing export gate blocks it
    and _enqueue_review routes it to HITL.
    """
    # AA-738: strip again after every LLM edit (grounding repair, flag_fix, the seo_meta re-repair
    # loop) — S218 Sri Lanka: 8 tours lost Master because flag_fix rewrote seo_meta back to
    # "Explore …" after the write-time strip. In place, before the re-validate below scores it.
    _pre = dict(state.get("generated") or {})
    stripped = _apply_forbidden_strip(state.get("generated"), state)
    # AA-740: same position for the seo_meta/seo_title length fit (repairs can push them out again).
    fitted = _apply_seo_meta_fit(state.get("generated"), state)
    if stripped or fitted:
        # AA-925 repair contract on the two deterministic edits: no added figure, no sentence cut short, no
        # broken bracket. A violating field keeps its pre-edit text (its code stays and validate sees it).
        state = _contract_after_edit(state, _pre, detector="revalidate_fit")

    # A deterministic edit here must be re-validated even without a fix pass, or validate's old
    # codes (FORBIDDEN_WORD / SEO_META_TOO_LONG …) would keep blocking content that is now clean.
    if not state.get("fix_pass_applied") and not (stripped or fitted):
        return _apply_grounding_recheck({**state, "revalidate_ran": False})

    # Re-run in the same order as the main graph: validate sets quality_score that judge then
    # min()'s against. Calling judge alone would min against the stale pre-fix validate score.
    s = validate_node(state)
    s = judge_node(s)

    score = s.get("quality_score", 0.0)
    audit_status = s.get("brand_audit_status", "")
    # "clean" POST-fix = score gate passed AND the post-fix audit isn't itself manual_check.
    # (brand_audit_node ran pre-fix; we don't re-run it here — judge is the post-fix brand gate.)
    passed = score >= MIN_QUALITY and audit_status != "manual_check"

    new_status = "fixed" if passed else "manual_check"
    logger.info("revalidate_done", passed=passed, post_fix_score=score,
                new_brand_audit_status=new_status,
                fix_fields=state.get("fix_pass_fields", []))
    return _apply_grounding_recheck({
        **s,
        "brand_audit_status": new_status,
        "revalidate_ran":     True,
        "revalidate_passed":  passed,
    })

def human_edit_gate_node(state: ContentState) -> ContentState:
    """AA-234: gate node for the re-validation graph (human-edited content).

    Unlike revalidate_node (which only verifies an automated flag_fix pass and no-ops when
    fix_pass_applied is False), this runs on content a reviewer edited by hand. flag_fix is
    deliberately NOT in this graph — the human edit is final, the system only re-scores and
    gates. Pass = validate+judge score clears MIN_QUALITY AND brand_audit isn't manual_check.
    On fail the reviewer must fix and re-validate again before approve is allowed.
    """
    score = state.get("quality_score", 0.0)
    audit_status = state.get("brand_audit_status", "")
    codes = state.get("failure_codes", []) or []
    hard_hits = sorted(set(codes) & hard_block_codes(state))   # AA-645: v2 drafts add V2_HARD_BLOCK_CODES
    passed = (
        score >= MIN_QUALITY
        and audit_status != "manual_check"
        and not hard_hits
    )
    logger.info("human_edit_revalidate_done", passed=passed, score=score,
                brand_audit_status=audit_status, hard_block_hits=hard_hits,
                failure_codes=codes)
    return {
        **state,
        "revalidate_ran":    True,
        "revalidate_passed": passed,
    }


def build_revalidation_graph() -> StateGraph:
    """AA-234: re-score a human-edited generated_content version IN PLACE.

    validate -> judge -> brand_audit -> human_edit_gate -> END. NO generate (content is the
    reviewer's edit, not LLM-generated), NO retry loop, NO flag_fix (the human edit is final).
    Compiled over the SAME ContentState so no field is stripped from the final state.
    """
    graph = StateGraph(ContentState)
    graph.add_node("validate", validate_node)
    graph.add_node("llm_judge", judge_node)
    graph.add_node("brand_audit", brand_audit_node)
    graph.add_node("human_edit_gate", human_edit_gate_node)

    graph.set_entry_point("validate")
    graph.add_edge("validate", "llm_judge")
    graph.add_edge("llm_judge", "brand_audit")
    graph.add_edge("brand_audit", "human_edit_gate")
    graph.add_edge("human_edit_gate", END)

    return graph.compile()


def build_graph(t2_drafts: bool = False) -> StateGraph:
    """The S1/T2 content graph. ``t2_drafts=True`` (AA-793, tenant rewrites only — chosen by
    v1_pipeline._rewrite_tour) adds draft tracking after the judge: the ``hitl`` exit restores the
    best-scoring draft instead of keeping the last one, and a retry revises that draft (see
    t2_repair.py). The default graph (A1, and T2 with both AA-793 switches off) is unchanged."""
    graph = StateGraph(ContentState)

    graph.add_node("generate", generate_node)
    graph.add_node("validate", validate_node)
    graph.add_node("llm_judge", judge_node)
    graph.add_node("increment_retry", increment_retry_t2 if t2_drafts else increment_retry)
    graph.add_node("brand_audit", brand_audit_node)
    graph.add_node("grounding", grounding_node)  # AA-691
    graph.add_node("flag_fix", flag_fix_node)
    graph.add_node("revalidate", revalidate_node)

    graph.set_entry_point("generate")
    graph.add_edge("generate", "validate")
    # AA-206: GPT-4.1 judge sits between validate and the retry decision so should_retry routes on
    # the judge-adjusted quality_score (Bedrock fixes against judge feedback via the retry loop).
    graph.add_edge("validate", "llm_judge")
    if t2_drafts:
        graph.add_node("track_best", track_best_node)
        graph.add_node("restore_best", restore_best_node)
        graph.add_edge("llm_judge", "track_best")
        graph.add_conditional_edges("track_best", should_retry, {
            "done":  "brand_audit",
            "retry": "increment_retry",
            "hitl":  "restore_best",
        })
        graph.add_edge("restore_best", END)
    else:
        graph.add_conditional_edges("llm_judge", should_retry, {
            "done":  "brand_audit",
            "retry": "increment_retry",
            "hitl":  END,
        })
    graph.add_edge("brand_audit", "grounding")
    graph.add_edge("grounding", "flag_fix")
    graph.add_edge("flag_fix", "revalidate")
    graph.add_edge("revalidate", END)
    graph.add_edge("increment_retry", "generate")

    return graph.compile()


def resume_writer_node(state: ContentState) -> ContentState:
    """AA-993 G2: entry node of build_graph_resumed — puts back the state the last writer node of a released
    s1_rewrite job produced (its draft, cost, model, writer_version stamp, retry_count, feedback ...), so the graph
    goes on at validate without a new writer call. No LLM call here."""
    saved = state.get("resume_state") or {}
    logger.info("s1_writer_state_resumed", retry_count=saved.get("retry_count", state.get("retry_count", 0)),
                writer_version=saved.get("writer_version"))
    return {**state, **saved, "resume_state": None}


def build_graph_resumed() -> StateGraph:
    """AA-993 G2: the A1 graph of build_graph(), entered after the writer. Every node and edge downstream is the same
    (validate → judge → gate → brand_audit → grounding → flag_fix → revalidate, and the retry loop back to the live
    generate_node), so a resumed run decides exactly as the run that was released would have. initial_state must
    carry ``resume_state`` (s1_checkpoint.writer_state_delta of the last writer node). Unlike the Bedrock Batch seed
    (build_graph_from_generated), the draft keeps its writer stamp (writer_version), so a v2 draft stays v2 in
    validate, the judge and the retries."""
    graph = StateGraph(ContentState)

    graph.add_node("resume_writer", resume_writer_node)
    graph.add_node("generate", generate_node)  # only reached via the retry loop
    graph.add_node("validate", validate_node)
    graph.add_node("llm_judge", judge_node)
    graph.add_node("increment_retry", increment_retry)
    graph.add_node("brand_audit", brand_audit_node)
    graph.add_node("grounding", grounding_node)
    graph.add_node("flag_fix", flag_fix_node)
    graph.add_node("revalidate", revalidate_node)

    graph.set_entry_point("resume_writer")
    graph.add_edge("resume_writer", "validate")
    graph.add_edge("generate", "validate")
    graph.add_edge("validate", "llm_judge")
    graph.add_conditional_edges("llm_judge", should_retry, {
        "done":  "brand_audit",
        "retry": "increment_retry",
        "hitl":  END,
    })
    graph.add_edge("brand_audit", "grounding")
    graph.add_edge("grounding", "flag_fix")
    graph.add_edge("flag_fix", "revalidate")
    graph.add_edge("revalidate", END)
    graph.add_edge("increment_retry", "generate")

    return graph.compile()


def build_graph_from_generated() -> StateGraph:
    """AA-606: same S1 graph, but entry is seed_generated (nạp Bedrock Batch attempt-1 output)
    instead of a live generate call. Everything downstream is byte-identical to build_graph():
    validate → judge → gate → brand_audit → flag_fix → revalidate, and the retry loop still routes
    a gate failure back through the ON-DEMAND generate_node (attempt-2/3) — so a tour the batch
    write couldn't get past the gate is repaired exactly as the synchronous path would, no batch
    round-2. initial_state must carry ``batch_text`` (the raw writer text for this tour)."""
    graph = StateGraph(ContentState)

    graph.add_node("seed_generated", seed_generated_node)
    graph.add_node("generate", generate_node)  # only reached via the retry loop (attempt-2/3)
    graph.add_node("validate", validate_node)
    graph.add_node("llm_judge", judge_node)
    graph.add_node("increment_retry", increment_retry)
    graph.add_node("brand_audit", brand_audit_node)
    graph.add_node("grounding", grounding_node)  # AA-691
    graph.add_node("flag_fix", flag_fix_node)
    graph.add_node("revalidate", revalidate_node)

    graph.set_entry_point("seed_generated")
    graph.add_edge("seed_generated", "validate")
    graph.add_edge("validate", "llm_judge")
    graph.add_conditional_edges("llm_judge", should_retry, {
        "done":  "brand_audit",
        "retry": "increment_retry",
        "hitl":  END,
    })
    graph.add_edge("brand_audit", "grounding")
    graph.add_edge("grounding", "flag_fix")
    graph.add_edge("flag_fix", "revalidate")
    graph.add_edge("revalidate", END)
    graph.add_edge("increment_retry", "generate")

    return graph.compile()
