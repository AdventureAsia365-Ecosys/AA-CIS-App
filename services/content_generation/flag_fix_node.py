"""AA-134: Targeted flag-fix node — rewrites only brand-flagged fields.
AA-132: write_lessons_log — persists audit lessons to shared.pipeline_lessons.
"""

import asyncio
import json
import structlog
import asyncpg
from json_repair import repair_json

from shared.secrets import get_database_url

from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest
from shared.llm_client.call_log import record_call_sync
from .seo_meta_utils import (best_meta_candidate, meta_in_band, meta_complete_sentence,
                             meta_has_forbidden, fit_seo_meta_final, fit_seo_title,
                             SEO_META_FORBIDDEN, SEO_META_MIN, SEO_META_MAX, SEO_TITLE_MAX)
from .forbidden_words import all_forbidden, forbidden_in
from .prompts import parse_source_day_word_counts
from .itinerary_utils import (
    ITINERARY_CLAMP_MIN, ITINERARY_CLAMP_MAX, MAX_NUDGES_PER_TOUR, NUDGE_SKIP_REASON,
    clamp_distance, nudge_itinerary_day,
    parse_canonical_itinerary_days, serialize_itinerary_days,
)

logger = structlog.get_logger()

# Failure code → generated dict key (keys match generate_node output, no "aa_" prefix)
STAGE2_FIX_MAPPING = {
    "BRAND_SEO_META_VIOLATION": "seo_meta",  # AA-238: forbidden word in meta -> repair
    "SUBTITLE_TRIP_TYPE_MISMATCH":  "subtitle",
    "SUBTITLE_CITY_LIST":           "subtitle",
    "SUBTITLE_WAYPOINT_FORMAT":     "subtitle",
    "SUMMARY_OFF_BRAND":            "summary",
    "SUMMARY_HONEYMOON_LANGUAGE":   "summary",
    "SUMMARY_SELF_REFERENTIAL":     "summary",
    "GENERIC_AI_WORDING":           "summary",
    "HIGHLIGHTS_TOO_GENERIC":       "highlights",
    "HIGHLIGHTS_ORDERING_WRONG":    "highlights",
    "HIGHLIGHTS_OPTIONAL_LANGUAGE": "highlights",
    "ITINERARY_STRUCTURE_WEAK":     "itineraries",
    "ITINERARY_MEAL_TIME_INVENTED": "itineraries",
    "ITINERARY_DAY_TITLE_GENERIC":  "itineraries",
    "ITINERARY_STILL_COMPRESSED":   "itineraries",  # AA-329c: dedicated per-day repair, see below

    "SEO_TITLE_WEAK":               "seo_title",
    "SEO_TITLE_WRONG_ACTIVITY":     "seo_title",
    "META_INCOMPLETE_SENTENCE":     "seo_meta",
    "META_TOO_SHORT":               "seo_meta",
    "SEO_META_TOO_LONG":            "seo_meta",   # AA-204: over-length now routes to repair
    "META_OPENER_ROBOTIC":          "seo_meta",
    "META_PACKAGE_WORD":            "seo_meta",
    "META_DFS_VERBATIM":            "seo_meta",
    "DFS_INTENT_UNDERUSED":         "seo_meta",
    "NAME_ALL_CAPS":                "name",
    "NAME_SUPERLATIVE":             "name",
}

# AA-204: deterministic SEO length/sentence codes raised by validate_node. These must drive a
# repair pass independently of the (non-deterministic) brand_audit "flagged" status.
# AA-329c: ITINERARY_STILL_COMPRESSED added here too — same reasoning (deterministic, validate_node-
# raised, must force a fix pass regardless of what the LLM brand audit thinks of the content).
_DETERMINISTIC_SEO_CODES = {
    "SEO_META_TOO_LONG", "META_TOO_SHORT", "META_INCOMPLETE_SENTENCE",
    "BRAND_SEO_META_VIOLATION",  # AA-238: forbidden meta forces a repair pass
    "ITINERARY_STILL_COMPRESSED",  # AA-329c
}


def _should_fix(state: dict) -> bool:
    """AA-204: run the fix pass when the brand audit flagged OR when a deterministic SEO
    length/sentence code fired in validate. A fully-clean pass (neither) still skips."""
    if state.get("brand_audit_status", "pass") == "flagged":
        return True
    return any(c in _DETERMINISTIC_SEO_CODES for c in state.get("failure_codes", []))


def _build_fix_keys(state: dict) -> set:
    """Collect generated-dict keys to repair: brand-audit codes/fields (AA-134) plus AA-204
    deterministic SEO codes carried from validate's failure_codes."""
    fix_keys: set[str] = set()
    for code in state.get("brand_audit_codes", []):
        mapped = STAGE2_FIX_MAPPING.get(code)
        if mapped:
            fix_keys.add(mapped)
    for f in state.get("brand_audit_fields", []):
        key = f.lower().replace("aa_", "")
        if key in STAGE2_FIX_MAPPING.values():
            fix_keys.add(key)
    for code in state.get("failure_codes", []):
        if code in _DETERMINISTIC_SEO_CODES:
            mapped = STAGE2_FIX_MAPPING.get(code)
            if mapped:
                fix_keys.add(mapped)
    return fix_keys


# AA-747: SEO codes that the deterministic fit (fit_seo_meta_final / fit_seo_title, AA-740) can
# resolve WITHOUT an LLM call — they are re-checkable on the fitted value (length / incomplete
# sentence / budget-word removal). _seo_code_fires() re-runs the exact validate_node check for
# each on the fitted value; a code that no longer fires is dropped from the LLM request.
_FITTABLE_SEO_CODES = {
    "SEO_META_TOO_LONG", "META_TOO_SHORT", "META_INCOMPLETE_SENTENCE",
    "BRAND_SEO_META_VIOLATION", "SEO_TITLE_TOO_LONG",
}
# SEO codes mapped to seo_meta / seo_title that are NOT deterministically re-checkable here
# (brand/intent judgments from brand_audit) — they stay with the LLM unless the fit removes the
# offending text, which only the fittable codes above can detect. Listed for clarity / tests.
_CONTENT_SEO_CODES = {
    "META_OPENER_ROBOTIC", "META_PACKAGE_WORD", "META_DFS_VERBATIM", "DFS_INTENT_UNDERUSED",
    "SEO_TITLE_WEAK", "SEO_TITLE_WRONG_ACTIVITY",
}


def _seo_code_fires(code: str, meta: str, title: str, forbidden) -> bool:
    """AA-747: re-run validate_node's own deterministic check for one SEO code against a value.
    True = the code still fires (same meaning validate_node gives it); only the fittable length /
    sentence / forbidden codes are re-checked here — content codes are never passed in."""
    m = (meta or "").strip()
    if code == "SEO_META_TOO_LONG":
        return len(m) > SEO_META_MAX
    if code == "META_TOO_SHORT":
        return bool(m) and len(m) < SEO_META_MIN
    if code == "META_INCOMPLETE_SENTENCE":
        return bool(m) and not meta_complete_sentence(m)
    if code == "BRAND_SEO_META_VIOLATION":
        return meta_has_forbidden(m, forbidden)
    if code == "SEO_TITLE_TOO_LONG":
        return len(title or "") > SEO_TITLE_MAX
    return True  # unknown / content code — treat as still firing (keep with the LLM)


def _apply_seo_fit_before_llm(state: dict, content: dict, fix_keys: set) -> tuple[set, set, dict]:
    """AA-747: before building the LLM fix request, apply the deterministic fit to seo_meta /
    seo_title (fit_seo_meta_final / fit_seo_title, AA-740) and re-check that field's codes on the
    fitted value. A fittable length/sentence/forbidden code that no longer fires is dropped; a
    field left with NO firing code is dropped from fix_keys entirely (no LLM call for it). Content
    codes (opener-robotic, dfs-verbatim, …) stay with the LLM unless the fit truly resolves them —
    the fit only trims/extends/removes-clause, so it can only clear the fittable codes.

    Mutates ``content`` in place with the fitted value when the field is being dropped from the LLM
    request (so the deterministic fix is kept). Returns (new_fix_keys, dropped_codes, changed) where
    ``changed`` maps field -> fitted value actually written to content."""
    tenant_forbidden = state.get("brand_forbidden_words")
    forbidden = set(SEO_META_FORBIDDEN) | {w.lower().strip() for w in (tenant_forbidden or []) if w and w.strip()}
    tour = state.get("tour", {}) or {}
    facts = {"duration": tour.get("duration"), "country": tour.get("country")}

    # The SEO codes this fix pass is acting on, per field (brand_audit + deterministic validate).
    acting_codes = set(state.get("brand_audit_codes", []) or [])
    acting_codes |= {c for c in (state.get("failure_codes", []) or []) if c in _DETERMINISTIC_SEO_CODES}

    dropped_codes: set[str] = set()
    changed: dict[str, str] = {}
    new_fix_keys = set(fix_keys)

    for field, fit_fn in (("seo_meta", lambda v: fit_seo_meta_final(v, facts, tenant_forbidden)),
                          ("seo_title", lambda v: fit_seo_title(v))):
        if field not in fix_keys:
            continue
        orig = content.get(field)
        if not isinstance(orig, str) or not orig.strip():
            continue
        fitted = fit_fn(orig)
        # Codes mapped to THIS field that this fix pass is acting on.
        field_codes = {c for c in acting_codes if STAGE2_FIX_MAPPING.get(c) == field}
        if not field_codes:
            continue
        meta_val = fitted if field == "seo_meta" else (content.get("seo_meta") or "")
        title_val = fitted if field == "seo_title" else (content.get("seo_title") or "")
        remaining = set()
        for code in field_codes:
            if code in _FITTABLE_SEO_CODES:
                if _seo_code_fires(code, meta_val, title_val, forbidden):
                    remaining.add(code)   # fit didn't resolve it → still needs the LLM
                else:
                    dropped_codes.add(code)
            else:
                remaining.add(code)       # content code — keep with the LLM
        if not remaining:
            # Every code for this field is resolved by the fit — write the fitted value and drop
            # the field from the LLM request (no LLM call for it).
            if fitted != orig:
                content[field] = fitted
                changed[field] = fitted
            new_fix_keys.discard(field)
            logger.info("flag_fix_seo_fitted_no_llm", field=field,
                        dropped_codes=sorted(c for c in field_codes if c in dropped_codes))
    return new_fix_keys, dropped_codes, changed

FIX_SYSTEM = """You are Adventure Asia's editorial fixer.
Fix ONLY the specified fields. Keep all other fields exactly as-is.
Preserve all product facts. Return strict JSON only."""


# AA-608 (H3 finding): SEO meta length was the single biggest reason ~94% of failed
# tours landed in the review queue — the meta simply would not fall inside the 140-155
# band after one repair attempt, and the code then escalated it to manual_check (a hard
# block). A length miss is a format problem, not a truth problem, so the fix is to (a) try
# harder to land it in band (a bounded retry loop, mirroring Ms.Thu's aa_batch_rewrite_v6
# `repair_seo_fields` which retries up to a max), and (b) NOT escalate to manual_check when
# it still misses — keep the best candidate and let it flag, never hard-block on length.
_META_REREPAIR_MAX_ATTEMPTS = 3


def _rerepair_meta(post: str, tour: dict, content: dict, model_tier: str, intent_clue: str = "", forbidden=None) -> str:
    """AA-205 / AA-608: bounded RETRY loop that rewrites seo_meta until it lands in
    [SEO_META_MIN, SEO_META_MAX] as a complete sentence, or the attempt budget is spent.

    Each attempt feeds back the previous candidate's exact length and whether it was too
    short / too long, so the model converges instead of guessing blind. After every attempt
    the raw candidate is run through best_meta_candidate (salvage a complete-sentence prefix
    in band) before the band check, so a slightly-over candidate is recovered rather than
    thrown away. Returns the first in-band result; if none lands, returns the best salvaged
    candidate seen (never worse than the incoming `post`). Never raises — LLM/parse errors
    fall through to the best candidate so far (graceful, same contract as before)."""
    from .seo_meta_utils import meta_in_band as _in_band, best_meta_candidate as _best, \
        SEO_META_MIN as _MIN, SEO_META_MAX as _MAX
    best = (post or "").strip()
    cur = best
    for attempt in range(1, _META_REREPAIR_MAX_ATTEMPTS + 1):
        try:
            too = "too short" if len(cur) < _MIN else ("too long" if len(cur) > _MAX else "out of band")
            prompt = (
                "Rewrite this SEO meta description to be " + str(_MIN) + "-" + str(_MAX)
                + " characters and a COMPLETE sentence ending in a period.\n\n"
                + "Current (" + str(len(cur)) + " chars, " + too + "): " + json.dumps(cur) + "\n"
                + "Tour: " + str(content.get("name")) + " - " + str(tour.get("country", "")) + "\n\n"
                + "Rules:\n- MUST be " + str(_MIN) + "-" + str(_MAX) + " characters "
                + "(current is " + too + " — "
                + ("add one concrete, source-true detail" if len(cur) < _MIN else "trim, do not truncate mid-word")
                + ").\n"
                + "- MUST end with a period and read as one complete sentence "
                + "(no trailing preposition/conjunction).\n"
                + "- Do NOT pad with filler; every word must be true to the tour.\n"
                + (("- Avoid these words entirely: " + ", ".join(sorted(forbidden)) + ".\n")
                   if forbidden else "")
                + (("- Weave in this real search-intent clue naturally (do not quote verbatim): "
                   + json.dumps(intent_clue) + "\n") if intent_clue else "")
                + "- Return JSON: {\"seo_meta\": \"...\"}"
            )
            resp = LLMClient().generate(LLMRequest(
                system_prompt=FIX_SYSTEM, user_prompt=prompt, model_tier=model_tier, stage="s1_flag_fix",
            ))
            raw = resp.content.strip()
            fence = chr(96) * 3
            if raw[:3] == fence:
                raw = raw.split(fence)[1]
                if raw[:4] == "json":
                    raw = raw[4:]
                raw = raw.strip()
            candidate = (json.loads(raw).get("seo_meta") or "").strip()
            # Salvage a complete-sentence prefix in band before judging (recovers a slight over).
            guarded = _best(candidate, best, forbidden=forbidden)
            landed = _in_band(guarded, forbidden)
            record_call_sync(
                stage="s1_flag_fix", role="writer", model=resp.model_used,
                tokens_in=getattr(resp, "input_tokens", None), tokens_out=getattr(resp, "output_tokens", None),
                cost_usd=resp.cost_usd, tenant_id=None,
                quality_signal={"meta_landed_in_band": landed, "candidate_len": len(guarded),
                                "attempt": attempt},
                stop_reason=getattr(resp, "stop_reason", None),
                account=getattr(resp, "satellite_account", None),
                fallback_used=getattr(resp, "fallback_used", None),
            )
            if landed:
                logger.info("meta_rerepair_landed", attempt=attempt,
                            before_len=len(best), after_len=len(guarded))
                return guarded
            # Keep the better of the two as the carry-forward candidate for the next attempt.
            if candidate:
                cur = candidate
            best = guarded or best
        except Exception as e:
            logger.warning("meta_rerepair_attempt_failed_graceful", attempt=attempt, error=str(e))
            break
    logger.info("meta_rerepair_exhausted", attempts=_META_REREPAIR_MAX_ATTEMPTS,
                final_len=len(best))
    return best


def _mark_day_ratio_skipped(state: dict, day_num) -> None:
    """AA-747: record on the tour's per-day ratio record (set by generate_node's
    _process_itineraries) that this out-of-clamp day was NOT nudged because the per-tour nudge
    budget was spent — so services/eval/regression.py's nudged_days / nudge_rate counters stay
    correct (they read these records, not the LLM-call log)."""
    for r in state.get("itinerary_day_ratios") or []:
        if r.get("day") == day_num:
            if r.get("nudged"):
                return  # generate_node already nudged this day — keep that record intact
            r["nudged"] = False
            r["skipped_reason"] = NUDGE_SKIP_REASON
            return


def _repair_still_compressed_days(state: dict, itinerary_text: str, nudge_budget: int):
    """AA-329c: dedicated per-day repair for ITINERARY_STILL_COMPRESSED — reuses AA-353's own
    nudge_itinerary_day with the correct target_word_count per violating day, instead of routing
    "itineraries" through the generic FIX_SYSTEM prompt below (which has no idea what any day's
    target length is). One extra attempt per violating day (same single-shot budget AA-353 already
    uses for its own nudge). Deterministic accept/reject guard, mirrors seo_meta's
    best_meta_candidate/meta_in_band: a day is only overwritten if the new attempt actually lands
    in [ITINERARY_CLAMP_MIN, ITINERARY_CLAMP_MAX] — otherwise the pre-fix day is kept untouched
    rather than being overwritten with something no better (or worse).

    AA-747: at most ``nudge_budget`` days are nudged — the REMAINING per-tour budget after
    generate_node already spent some (MAX_NUDGES_PER_TOUR across both call sites). The WORST days
    (largest distance outside the clamp band) are repaired first; the rest are recorded on the
    tour's per-day ratio record as nudged=false / skipped_reason="nudge_cap" (regression counters).

    Returns (new_itinerary_text, extra_cost_usd, applied: bool, nudges_used: int). applied=False
    when nothing was repaired (no still-violating day, no budget, or a violating day couldn't be
    matched back to source text) — new_itinerary_text is the unchanged input in that case.
    """
    days = parse_canonical_itinerary_days(itinerary_text)
    if not days:
        return itinerary_text, 0.0, False, 0

    tour = state.get("tour", {})
    itineraries_raw = tour.get("itineraries") or tour.get("itinerary") or ""
    source = parse_source_day_word_counts(itineraries_raw, tour.get("duration"))
    source_words_by_day = source["day_word_counts"]
    source_text_by_day = source["day_text"]

    still_violating = []  # (clamp_distance, day_num, ratio)
    for day_num, day in days.items():
        src_words = source_words_by_day.get(day_num)
        if not src_words:
            continue
        ratio = len(day["body"].split()) / src_words
        if not (ITINERARY_CLAMP_MIN <= ratio <= ITINERARY_CLAMP_MAX):
            still_violating.append((clamp_distance(ratio), day_num, ratio))

    if not still_violating:
        return itinerary_text, 0.0, False, 0

    # AA-747: worst (largest distance outside the band) first; break ties by day order.
    still_violating.sort(key=lambda t: (-t[0], t[1]))
    budget = max(0, nudge_budget)

    client = LLMClient()
    extra_cost = 0.0
    applied = False
    nudges_used = 0
    for _dist, day_num, _ratio in still_violating:
        if nudges_used >= budget:
            # AA-747: budget spent — leave the day untouched, mark it skipped on the ratio record.
            _mark_day_ratio_skipped(state, day_num)
            logger.info("itinerary_day_repair_skipped_cap", day=day_num)
            continue
        target_words = source_words_by_day.get(day_num)
        source_text = source_text_by_day.get(day_num)
        if not target_words or not source_text:
            continue
        new_title, new_body, resp = nudge_itinerary_day(
            client, source_text, days[day_num]["title"], days[day_num]["body"], target_words,
        )
        extra_cost += resp.cost_usd
        nudges_used += 1
        new_ratio = len(new_body.split()) / target_words
        in_clamp = ITINERARY_CLAMP_MIN <= new_ratio <= ITINERARY_CLAMP_MAX
        record_call_sync(
            stage="s1_itinerary_nudge", role="writer", model=resp.model_used,
            tokens_in=getattr(resp, "input_tokens", None), tokens_out=getattr(resp, "output_tokens", None),
            cost_usd=resp.cost_usd, tenant_id=None,
            quality_signal={"landed_in_clamp": in_clamp, "ratio_after_nudge": round(new_ratio, 3)},
            stop_reason=getattr(resp, "stop_reason", None),
            account=getattr(resp, "satellite_account", None),
            fallback_used=getattr(resp, "fallback_used", None),
        )
        if in_clamp:
            days[day_num] = {"title": new_title, "body": new_body}
            applied = True
            logger.info("itinerary_day_repaired", day=day_num, new_ratio=round(new_ratio, 3))
        else:
            logger.warning("itinerary_day_repair_still_out_of_clamp", day=day_num,
                            new_ratio=round(new_ratio, 3))
    if not applied:
        return itinerary_text, extra_cost, False, nudges_used
    return serialize_itinerary_days(days), extra_cost, True, nudges_used


def _revert_introduced_forbidden(before: dict, after: dict, keys, forbidden) -> dict:
    """AA-641: restore, per field, the pre-repair value of any field whose repair ADDED a forbidden
    word it did not have before. Mutates `after`; returns {field: [words added]}. A field that
    already contained the word keeps its repair (the repair did not make it worse)."""
    reverted = {}
    for k in keys:
        if k not in after or k not in before:
            continue
        added = forbidden_in(after[k], forbidden) - forbidden_in(before[k], forbidden)
        if added:
            after[k] = before[k]
            reverted[k] = sorted(added)
    return reverted


def flag_fix_node(state: dict) -> dict:
    """AA-134: Fix only the flagged fields identified by brand_audit_node."""
    # AA-204: run if brand-flagged OR a deterministic SEO length/sentence code fired.
    # Pass-through a fully-clean pass ("pass"/"manual_check" with no det SEO codes) unchanged.
    if not _should_fix(state):
        return {
            **state,
            "fix_pass_applied": False,
            "fix_pass_fields":  [],
        }

    try:
        fix_keys = _build_fix_keys(state)

        if not fix_keys:
            return {
                **state,
                "fix_pass_applied": False,
                "fix_pass_fields":  [],
            }

        current_content = dict(state.get("generated", {}))
        extra_cost = 0.0
        applied_fields: set = set()

        # AA-747: apply the deterministic SEO fit (fit_seo_meta_final / fit_seo_title, AA-740) and
        # re-check each seo_meta/seo_title code on the fitted value BEFORE any LLM request. A length
        # / incomplete-sentence / budget-word code the fit resolves is dropped; a field left with no
        # firing code is pulled out of fix_keys so no LLM call is made for it (content codes stay).
        # When the fit resolves every field, the post-itinerary `if not fix_keys` block below returns
        # fix_pass_applied=True with the fitted content (no LLM call), or False if nothing changed.
        fix_keys, _seo_dropped, _seo_fitted = _apply_seo_fit_before_llm(state, current_content, fix_keys)
        applied_fields |= set(_seo_fitted.keys())

        # AA-329c: ITINERARY_STILL_COMPRESSED gets the dedicated per-day repair above instead of
        # the generic FIX_SYSTEM prompt below — pull "itineraries" out of fix_keys so the generic
        # call (if still needed for other fields) doesn't also try to rewrite it in the same pass.
        if "ITINERARY_STILL_COMPRESSED" in (state.get("failure_codes") or []) and "itineraries" in fix_keys:
            fix_keys = fix_keys - {"itineraries"}
            # AA-747: budget left for this tour's nudges after generate_node already used some
            # (MAX_NUDGES_PER_TOUR counted across both nudge call sites).
            _nudge_budget = max(0, MAX_NUDGES_PER_TOUR - (state.get("itinerary_nudges_used") or 0))
            new_itinerary, itin_cost, itin_applied, _itin_nudges = _repair_still_compressed_days(
                state, current_content.get("itineraries", ""), _nudge_budget,
            )
            extra_cost += itin_cost
            if itin_applied:
                # AA-641: same forbidden-word guard as the generic pass below.
                itin_after = {"itineraries": new_itinerary}
                itin_reverted = _revert_introduced_forbidden(
                    current_content, itin_after, {"itineraries"},
                    all_forbidden(state.get("brand_forbidden_words")),
                )
                if itin_reverted:
                    logger.warning("flag_fix_introduced_forbidden", fields=itin_reverted,
                                   tour=current_content.get("name"))
                else:
                    current_content["itineraries"] = new_itinerary
                    applied_fields.add("itineraries")

        if not fix_keys:
            # Nothing left for the generic FIX_SYSTEM pass — either the itinerary repair above was
            # the only thing needed, or it ran and found nothing to change.
            if not applied_fields:
                return {
                    **state,
                    "fix_pass_applied": False,
                    "fix_pass_fields":  [],
                }
            logger.info("flag_fix_done", fixed_keys=sorted(applied_fields), cost=extra_cost)
            lessons = state.get("lessons_extracted", [])
            if lessons:
                _write_lessons_safe(lessons, state)
            return {
                **state,
                "generated":        current_content,
                "cost_usd":         state.get("cost_usd", 0) + extra_cost,
                "fix_pass_applied": True,
                "fix_pass_fields":  sorted(applied_fields),
            }

        issues_text = "\n".join(state.get("brand_audit_issues", []))
        fields_display = "\n".join(
            f"- {k}: {json.dumps(current_content.get(k))}"
            for k in fix_keys if k in current_content
        )
        tour = state.get("tour", {})
        # AA-641: the words validate_node fires FORBIDDEN_WORD on (AA list + tenant list). A repair
        # that adds one of them turns a soft/length problem into a hard code and a full rewrite.
        forbidden = all_forbidden(state.get("brand_forbidden_words"))

        # AA-201/AA-204: seo_meta repair-to-band rules (port of v5 repair_seo_fields)
        meta_rules = ""
        if "seo_meta" in fix_keys:
            _cur_meta = current_content.get("seo_meta") or ""
            meta_rules = f"""

SEO_META RULES:
- SEO_META MUST be 140-155 characters and a COMPLETE sentence (ends with a period, \
not ending on a preposition/conjunction, contains a clear verb).
- Include the DFS primary topic + one intent clue + one practical reassurance when available.
- Do NOT pad with filler to reach length; rewrite naturally to land in band.
- NEVER return meta under 140 chars.
- If the current SEO_META exceeds 155 chars, SHORTEN it to land within 140-155 as a COMPLETE \
sentence ending in a period — do NOT truncate mid-phrase.
Current SEO_META ({len(_cur_meta)} chars): {json.dumps(_cur_meta)}"""

        user_prompt = f"""Fix these fields for Adventure Asia brand standards.

FIELDS TO FIX:
{fields_display}

AUDIT ISSUES FOUND:
{issues_text}

TOUR CONTEXT:
Name: {current_content.get("name")}
Trip type: {current_content.get("trip_type") or tour.get("trip_type")}
Duration: {tour.get("duration")}{meta_rules}

NEVER USE these words or phrases anywhere in your output: {", ".join(forbidden)}.

Return JSON with ONLY these keys: {list(fix_keys)}
Keep all other fields unchanged."""

        llm_client = LLMClient()
        request = LLMRequest(
            system_prompt=FIX_SYSTEM,
            user_prompt=user_prompt,
            # AA-518: was state.get("model_tier","haiku") — the "haiku" literal masked the
            # "s1_flag_fix" stage config the same way graph.py's generate_node did (see that
            # file's own AA-518 comment). No explicit tier here (this is always a repair pass,
            # never AA-237's auto-upgrade target), so config now drives it directly.
            model_tier=state.get("model_tier"),
            stage="s1_flag_fix",
        )
        resp = llm_client.generate(request)

        raw = resp.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        # AA-608: json-repair salvage on malformed fix-pass output, same as generate_node (AA-217).
        # A single malformed character used to throw JSONDecodeError, drop into the outer except,
        # and return fix_pass_applied=False — the whole repair silently lost (observed on flagged
        # tours that never got their seo_meta shortened, so they stayed flagged out-of-band). Try
        # a clean parse first, then repair_json, so a nearly-valid object is still applied.
        try:
            fixed_fields = json.loads(raw)
        except json.JSONDecodeError:
            salvaged = repair_json(raw, return_objects=True)
            if isinstance(salvaged, dict) and salvaged:
                fixed_fields = salvaged
                logger.info("flag_fix_json_repair_salvaged", raw_len=len(raw),
                            keys=sorted(fixed_fields.keys()))
            else:
                logger.warning("flag_fix_json_unrecoverable", raw_len=len(raw))
                raise
        record_call_sync(
            # AA-620: s1_flag_fix stays SHARED between A1 and T2 (not split — Haiku is fine for
            # both, see AA-620). Only tenant_id is threaded so a T2 fix-pass logs its real tenant.
            stage="s1_flag_fix", role="writer", model=resp.model_used,
            tokens_in=getattr(resp, "input_tokens", None), tokens_out=getattr(resp, "output_tokens", None),
            cost_usd=resp.cost_usd, tenant_id=state.get("tenant_id"),  # AA-620
            quality_signal={"fields_fixed": len(fix_keys), "fields_requested": sorted(fix_keys)},
            stop_reason=getattr(resp, "stop_reason", None),
            account=getattr(resp, "satellite_account", None),
            fallback_used=getattr(resp, "fallback_used", None),
        )

        new_generated = dict(current_content)
        for k, v in fixed_fields.items():
            if k in fix_keys:
                new_generated[k] = v
        reverted = _revert_introduced_forbidden(current_content, new_generated, fix_keys, forbidden)
        if reverted:
            logger.warning("flag_fix_introduced_forbidden", fields=reverted, tour=current_content.get("name"))

        # AA-205: deterministic post-repair band guard for seo_meta (no pad, no escalate).
        # LLM repair can overshoot under SEO_META_MIN (e.g. 132). AA-215 revalidate re-runs
        # validate and fires META_TOO_SHORT but that is only -0.5 sub-score, so the under-band
        # meta still clears the 7.0 gate and reaches gold. Enforce the band here at the source.
        if "seo_meta" in fix_keys:
            _pre_meta = current_content.get("seo_meta", "") or ""
            _post_meta = new_generated.get("seo_meta", "") or ""
            _tf = {w.lower().strip() for w in (state.get("brand_forbidden_words") or []) if w}
            _meta_forbidden = set(SEO_META_FORBIDDEN) | _tf  # AA-238/D4 unified
            _guarded = best_meta_candidate(_post_meta, _pre_meta, forbidden=_meta_forbidden)
            if not meta_in_band(_guarded, _meta_forbidden):
                # AA-226: pull a real intent clue from seo context (PAA first, then related)
                _seo_ctx = state.get("seo", {}) or {}
                _paa = _seo_ctx.get("people_also_ask", []) or []
                _rel = (_seo_ctx.get("related_searches", [])
                        or _seo_ctx.get("related_keywords", []) or [])
                _clue = ""
                if _paa:
                    _clue = _paa[0] if isinstance(_paa[0], str) else str(_paa[0])
                elif _rel:
                    _clue = _rel[0] if isinstance(_rel[0], str) else str(_rel[0])
                _guarded = _rerepair_meta(
                    _post_meta, tour, new_generated, state.get("model_tier"),
                    intent_clue=_clue, forbidden=_meta_forbidden,
                )
            # AA-608 (H3 finding): if STILL out of band after the bounded re-repair loop,
            # keep the best candidate but DO NOT escalate to manual_check. A meta length
            # miss is a format problem, not a product-truth problem — hard-blocking on it
            # was sending ~94% of failed tours to the review queue for a fixable cosmetic
            # issue. validate/revalidate still fires META_TOO_SHORT / SEO_META_TOO_LONG as a
            # -0.5 sub-score (surfaced to the reviewer, never silently gold), which does not
            # by itself drop the 7.0 gate. Only genuine product-truth (manual_check from the
            # brand audit) hard-blocks now.
            if not meta_in_band(_guarded, _meta_forbidden):
                logger.warning("meta_band_unrecoverable_flagging_not_blocking",
                               final_len=len(_guarded), tour=current_content.get("name"))
            new_generated["seo_meta"] = _guarded

        logger.info("flag_fix_done", fixed_keys=list(fix_keys), cost=resp.cost_usd)

        # AA-132: write lessons back to shared.pipeline_lessons
        lessons = state.get("lessons_extracted", [])
        if lessons:
            _write_lessons_safe(lessons, state)

        applied_fields |= set(fix_keys) - set(reverted)
        return {
            **state,
            "generated":       new_generated,
            "cost_usd":        state.get("cost_usd", 0) + resp.cost_usd + extra_cost,
            "fix_pass_applied": True,
            "fix_pass_fields":  sorted(applied_fields),
            # AA-608: meta length no longer escalates to manual_check (see band guard above).
        }

    except Exception as e:
        logger.warning("flag_fix_failed_graceful", error=str(e))
        return {
            **state,
            "fix_pass_applied": False,
            "fix_pass_fields":  [],
        }


def _write_lessons_safe(lessons: list, state: dict) -> None:
    """Fire-and-forget lessons write-back — swallows all errors."""
    try:
        batch = state.get("tour", {}).get("batch_name", "")
        country = state.get("tour", {}).get("country", "")
        loop = asyncio.get_event_loop()
        new_count = loop.run_until_complete(
            write_lessons_log(lessons, batch=batch, country=country)
        )
        logger.info("lessons_writeback", new_count=new_count)
    except Exception as e:
        logger.warning("lessons_writeback_failed", error=str(e))


# ── AA-132: lessons write-back ────────────────────────────────────────────────

async def write_lessons_log(
    lessons: list[dict],
    batch: str = "",
    country: str = "",
    min_frequency: int = 2,
) -> int:
    """
    Insert new lessons into shared.pipeline_lessons.
    Deduplicates by (stage, field, pattern). Only inserts patterns that appear
    >= min_frequency times within the provided lessons batch.
    Returns count inserted.
    """
    if not lessons:
        return 0

    from collections import Counter

    def freq_key(les):
        return (
            f"{les.get('failure_code', '')}:{les.get('field', '')}:"
            f"{les.get('pattern', '')[:50].lower().strip()}"
        )
    freq = Counter(freq_key(les) for les in lessons)

    eligible = [les for les in lessons if freq[freq_key(les)] >= min_frequency]
    if not eligible:
        return 0

    seen: set = set()
    unique_lessons = []
    for les in eligible:
        k = freq_key(les)
        if k not in seen:
            seen.add(k)
            unique_lessons.append(les)

    conn = await asyncpg.connect(get_database_url())
    inserted = 0
    try:
        for lesson in unique_lessons:
            stage = "audit"
            field = lesson.get("field", "")
            pattern = lesson.get("pattern", "")
            existing = await conn.fetchval(
                "SELECT id FROM shared.pipeline_lessons WHERE stage=$1 AND field=$2 AND pattern=$3 LIMIT 1",
                stage, field, pattern,
            )
            if existing:
                continue
            await conn.execute(
                """INSERT INTO shared.pipeline_lessons
                   (batch, country, stage, field, pattern, why_it_matters,
                    what_to_do, example_before, is_active)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,true)""",
                batch,
                country,
                stage,
                field,
                pattern,
                f"Failure code: {lesson.get('failure_code', '')} — severity: {lesson.get('severity', '')}",
                f"Fix field {field} — failure code {lesson.get('failure_code', '')}",
                lesson.get("example_before", ""),
            )
            inserted += 1
    finally:
        await conn.close()
    return inserted
