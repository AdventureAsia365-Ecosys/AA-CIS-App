import json
import re
import structlog
from json_repair import repair_json
from typing import TypedDict, Annotated, Optional
from langgraph.graph import StateGraph, END

from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest
from shared.llm_client.call_log import record_call_sync
from .prompts import parse_source_day_word_counts
from .batch_prompt import (
    build_brand_diff_block, build_s1_system_prompt, build_s1_user_prompt, prompt_version_of,
)
from .brand_audit_node import brand_audit_node
from .flag_fix_node import flag_fix_node
from .judge_node import judge_node
from .seo_meta_utils import SEO_META_MIN, SEO_META_MAX, meta_complete_sentence, SEO_META_FORBIDDEN
from .itinerary_utils import (
    ITINERARY_CLAMP_MIN, ITINERARY_CLAMP_MAX, nudge_itinerary_day,
    generated_day_word_counts,
)

logger = structlog.get_logger()

MAX_RETRIES = 3
MIN_QUALITY = 7.0


def _process_itineraries(generated: dict, tour: dict, client: LLMClient) -> dict:
    """AA-353: validate the model's structured ``itineraries`` array against its own PER-DAY
    SOURCE LENGTH target, auto-nudge (once, non-repeating) any day outside the hard clamp, then
    serialize the array back to the pre-existing canonical "Day N — Title\\nBody" string format —
    so every downstream reader (validate_node, judge_node, brand_audit_node, flag_fix_node,
    generated_content.aa_itineraries) sees exactly the string shape it already expects. No
    downstream code changes needed (confirmed by reading all of them — see implementation notes).

    Mutates ``generated["itineraries"]`` in place (array -> string). No-ops (leaves the field
    untouched, logs a warning) if the model didn't return the array contract at all — old string
    behavior, not a crash.

    Returns {"day_ratios": [...], "extra_cost_usd": float, "extra_cache_read_tokens": int,
    "extra_cache_write_tokens": int} for the caller to fold into generate_node's own totals and
    metadata (AA-353 STEP 0: generated_content.metadata JSONB, same place as AA-213's
    fallback_used/score_overall/batch_id).
    """
    itineraries = generated.get("itineraries")
    empty_result = {"day_ratios": [], "extra_cost_usd": 0.0,
                     "extra_cache_read_tokens": 0, "extra_cache_write_tokens": 0}
    if not isinstance(itineraries, list):
        if itineraries:
            logger.warning("itinerary_contract_not_array", value_type=type(itineraries).__name__)
        return empty_result

    itineraries_raw = tour.get("itineraries") or tour.get("itinerary") or ""
    source = parse_source_day_word_counts(itineraries_raw, tour.get("duration"))
    source_by_day = source["day_word_counts"]
    source_text_by_day = source["day_text"]
    source_ordered = sorted(source_by_day.items())  # [(day_num, words), ...]

    day_ratios = []
    extra_cost = 0.0
    extra_read = 0
    extra_write = 0

    for position, day_item in enumerate(itineraries):
        if not isinstance(day_item, dict):
            continue
        day_num = day_item.get("day")
        title = str(day_item.get("title") or "")
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

        if source_words:
            ratio = actual_words / source_words
            record["ratio"] = round(ratio, 3)
            if ratio < ITINERARY_CLAMP_MIN or ratio > ITINERARY_CLAMP_MAX:
                new_title, new_body, resp = nudge_itinerary_day(
                    client, source_text or itineraries_raw, title, body, source_words,
                )
                extra_cost += resp.cost_usd
                extra_read += resp.cache_read_tokens
                extra_write += resp.cache_write_tokens
                title, body = new_title, new_body
                day_item["title"], day_item["body"] = new_title, new_body
                actual_words_after = len(body.split())
                record["nudged"] = True
                record["actual_words_after_nudge"] = actual_words_after
                record["ratio_after_nudge"] = round(actual_words_after / source_words, 3)
                # AA-505 — quality_signal = did the nudge actually land inside clamp? Real,
                # computed right here, not a fabricated placeholder.
                record_call_sync(
                    stage="s1_itinerary_nudge", role="writer", model=resp.model_used,
                    tokens_in=getattr(resp, "input_tokens", None), tokens_out=getattr(resp, "output_tokens", None),
                    cost_usd=resp.cost_usd, tenant_id=None,
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
        day_ratios.append(record)

    generated["itineraries"] = "\n\n".join(
        f"Day {d.get('day')} — {d.get('title') or ''}\n{d.get('body') or ''}".strip()
        for d in itineraries if isinstance(d, dict)
    )
    return {"day_ratios": day_ratios, "extra_cost_usd": extra_cost,
            "extra_cache_read_tokens": extra_read, "extra_cache_write_tokens": extra_write}
# AA-216: structural failure (empty/missing field) caps quality below the gate regardless of
# other sub-scores. Empty content scores brand/quality=10 (no rule to violate) which the
# 4-bucket average can't pull under 7 — mirror judge's _MISSION_ABSENT_CAP pattern.
MISSING_FIELD_CAP = 4.0
# AA-234: failure codes that HARD-BLOCK approve of a human-edited version even when the
# overall score clears MIN_QUALITY. These are rule violations that must never reach gold
# (SEO length/floor, brand deny-list, forbidden words, missing fields) — the exact class
# the Review Queue exists to catch. Soft codes (HIGHLIGHTS_*, *_GENERIC, SUMMARY_OFF_BRAND)
# are surfaced to the reviewer but do NOT block.
_HARD_BLOCK_CODES = frozenset({
    "META_TOO_SHORT", "SEO_META_TOO_LONG", "SEO_TITLE_TOO_LONG",
    "META_INCOMPLETE_SENTENCE", "BRAND_SEO_META_VIOLATION",
    "FORBIDDEN_WORD", "MISSING_FIELD",
})

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
    model_tier:             str
    fallback_used:          bool   # AA-213: True khi Sonnet T1 fell back to Haiku T2
    satellite_account:      Optional[str]  # AA-296/397: "acc1"/"acc3" nếu qua satellite, None nếu không
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
    # AA-215: revalidate-after-flag_fix verification (POST-fix). revalidate_ran=True chi khi
    # fix_pass_applied; revalidate_passed phan anh ket qua re-validate+re-judge tren content da sua.
    revalidate_ran:         bool
    revalidate_passed:      bool
    # AA-353: per-day actual/source word-count ratios from the structured itineraries array,
    # set by generate_node's _process_itineraries. Must be declared here or LangGraph strips it
    # from node_output before _rewrite_tour ever sees it (same gotcha as judge_score, AA-209).
    itinerary_day_ratios:   list
    # AA-606: raw writer text from Bedrock Batch attempt-1, consumed by seed_generated_node
    # (build_graph_from_generated). Declared here so LangGraph doesn't strip it before the seed
    # node reads it. Absent/None on the normal synchronous build_graph() path.
    batch_text:             Optional[str]
    # AA-620: real tenant UUID for a T2 rewrite so record_call logs the right tenant instead of
    # NULL (A1 admin leaves this None — its writes stay tenant_id NULL by design).
    tenant_id:              Optional[str]
    # AA-620: which llm_role_config stage the WRITE node resolves — "t2_generate" for a tenant
    # (T2) rewrite, "s1_generate" for A1 admin. Only the generate node reads it; s1_flag_fix /
    # s1_itinerary_nudge stay shared (not split — see AA-620). None -> falls back to s1_generate.
    generate_stage:         Optional[str]

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
}

# AA-240: canonical validate-node forbidden list (single source; validate_node + review-queue
# handler both consume so the re-derived per-field reason can never drift from what fired).
_VALIDATE_FORBIDDEN = [
    "curated", "pristine", "refined", "tailored", "bespoke",
    "stunning", "breathtaking", "magical", "paradise",
    "cheap", "deal", "book now", "instant booking", "discount",
]

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

def generate_node(state: ContentState) -> ContentState:
    """Node 1: Generate content via LLMClient."""
    client = LLMClient()

    # AA-620: the WRITE stage — "t2_generate" for a tenant (T2) rewrite so admin can tune/price
    # the tenant writer independently, "s1_generate" for A1 admin. Set by the caller's
    # initial_state; None (any older caller not threaded through) -> s1_generate, unchanged.
    gen_stage = state.get("generate_stage") or "s1_generate"
    # AA-620: real tenant for a T2 rewrite, None for A1 admin (its s1_generate writes stay NULL).
    tenant_id = state.get("tenant_id")

    # AA-606: attempt-1 (system, user) prompt is materialized by the SAME builder the Bedrock Batch
    # manifest uses (services/content_generation/batch_prompt), so batch and sync writes never drift.
    system = build_s1_system_prompt(state)
    prompt = build_s1_user_prompt(state)

    # Inject feedback nếu đang retry — batch attempt-1 never has feedback, so this stays sync-only.
    if state.get("feedback"):
        prompt += f"\n\nPREVIOUS ATTEMPT FEEDBACK:\n{state['feedback']}\nPlease fix these issues."

    brand_sp = state.get("brand_system_prompt", "") or ""

    # AA-289: hash the exact system-prompt string sent to the LLM this call — the same string
    # _call_bedrock's use_cache=True path marks with cache_control, so a prompt_version change
    # here is also a cache-key change (a genuinely different prompt SHOULD miss the old cache).
    # AA-606: via prompt_version_of (batch_prompt) so sync + batch compute the version identically.
    prompt_version = prompt_version_of(system)

    is_branded = bool(brand_sp)
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
    )

    resp = None
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
        # AA-353: clamp/nudge the structured itineraries array against its own per-day source
        # length target, then serialize back to the plain string every downstream node expects.
        _itin_result = _process_itineraries(generated, state.get("tour", {}), client)
        # AA-620 — resolves the AA-505/AA-434 gap this comment used to flag: ContentState now
        # carries tenant_id + generate_stage, so a T2 tenant rewrite logs stage="t2_generate" with
        # the real tenant_id, while an A1 admin rewrite logs stage="s1_generate" with tenant_id
        # NULL (A1 leaves both unset) — the two are finally distinguishable in llm_call_log.
        record_call_sync(
            stage=gen_stage, role="writer", model=resp.model_used,  # AA-620
            tokens_in=getattr(resp, "input_tokens", None), tokens_out=getattr(resp, "output_tokens", None),
            cost_usd=resp.cost_usd, tenant_id=tenant_id,  # AA-620: real tenant for T2, None for A1
            quality_signal={"json_parsed": True, "fallback_used": resp.fallback_used},
            stop_reason=getattr(resp, "stop_reason", None),
            account=getattr(resp, "satellite_account", None),
            fallback_used=getattr(resp, "fallback_used", None),
        )
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
        }
    except Exception as e:
        logger.error("generation_failed", error=str(e))
        return {**state, "generated": {}, "is_branded": is_branded,
                "prompt_version": prompt_version,
                "cost_usd": state.get("cost_usd", 0) + (resp.cost_usd if resp else 0),
                "error": str(e)}

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
    # Reuse the exact itinerary clamp/nudge + serialize the sync path runs. LLMClient() is only used
    # by _process_itineraries for the optional per-day nudge (on-demand, small) — batch doesn't cover it.
    _itin_result = _process_itineraries(generated, state.get("tour", {}), LLMClient())
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
    }


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
            it = _re.sub(r"\bDay\s+(\d+)\s*[-–—]+\s*", r"Day \1 — ", it)  # any dash -> em-dash
            it = _re.sub(r"(Day\s+\d+\s+—\s+[^\n|]+?)\s*\|\s*", r"\1\n", it)  # "title | prose" -> newline
            # ensure a blank line before each "Day N" that isn't already at start/after blank line
            it = _re.sub(r"(?<=\S)\n?(?=Day\s+\d+\s+—)", "\n\n", it.lstrip())
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
    forbidden = list(_VALIDATE_FORBIDDEN)  # AA-240: shared const
    # P3-S3: Merge tenant forbidden_words (lowercase, deduplicated)
    tenant_forbidden = [w.lower().strip() for w in (state.get("brand_forbidden_words") or []) if w]
    all_forbidden = list(dict.fromkeys(forbidden + tenant_forbidden))  # preserve order, dedupe

    content_text = json.dumps(generated).lower()
    for word in all_forbidden:
        if word in content_text:
            issues.append(f"Forbidden word: '{word}'")
            fired.append("FORBIDDEN_WORD")
            score -= 0.5

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
        if term in seo_meta_lower:
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
    _src_parsed = parse_source_day_word_counts(_src_raw, tour.get('duration'))
    if not _src_parsed["used_fallback"]:
        _source_day_words = _src_parsed["day_word_counts"]
        _source_days = set(_source_day_words)
        _generated_day_words = generated_day_word_counts(itinerary)
        _generated_days = set(_generated_day_words)

        # AA-329a: whole days present in the source but absent from the output.
        _missing_days = sorted(_source_days - _generated_days)
        if _missing_days:
            issues.append(
                f"Itinerary missing {len(_missing_days)} day(s) vs source "
                f"({len(_source_days)} source days -> {len(_generated_days)} output days): "
                f"day {'/'.join(str(d) for d in _missing_days)}"
            )
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
            issues.append(f"Itinerary still compressed/expanded vs source after nudge — {_detail}")
            fired.append("ITINERARY_STILL_COMPRESSED")
            score -= 1.5

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
                failure_codes=failure_codes, sub_scores=sub_scores)

    return {
        **state,
        "generated":     generated,
        "quality_score": quality_score,
        "feedback":      feedback,
        "failure_codes": failure_codes,
        "sub_scores":    sub_scores,
        "passed_count":  passed_count,
        "failed_count":  failed_count,
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
    if not state.get("fix_pass_applied"):
        return {**state, "revalidate_ran": False}

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
    return {
        **s,
        "brand_audit_status": new_status,
        "revalidate_ran":     True,
        "revalidate_passed":  passed,
    }

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
    hard_hits = sorted(set(codes) & _HARD_BLOCK_CODES)
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


def build_graph() -> StateGraph:
    graph = StateGraph(ContentState)

    graph.add_node("generate", generate_node)
    graph.add_node("validate", validate_node)
    graph.add_node("llm_judge", judge_node)
    graph.add_node("increment_retry", increment_retry)
    graph.add_node("brand_audit", brand_audit_node)
    graph.add_node("flag_fix", flag_fix_node)
    graph.add_node("revalidate", revalidate_node)

    graph.set_entry_point("generate")
    graph.add_edge("generate", "validate")
    # AA-206: GPT-4.1 judge sits between validate and the retry decision so should_retry routes on
    # the judge-adjusted quality_score (Bedrock fixes against judge feedback via the retry loop).
    graph.add_edge("validate", "llm_judge")
    graph.add_conditional_edges("llm_judge", should_retry, {
        "done":  "brand_audit",
        "retry": "increment_retry",
        "hitl":  END,
    })
    graph.add_edge("brand_audit", "flag_fix")
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
    graph.add_edge("brand_audit", "flag_fix")
    graph.add_edge("flag_fix", "revalidate")
    graph.add_edge("revalidate", END)
    graph.add_edge("increment_retry", "generate")

    return graph.compile()
