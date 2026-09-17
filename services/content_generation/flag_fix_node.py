"""AA-134: Targeted flag-fix node — rewrites only brand-flagged fields.
AA-132: write_lessons_log — persists audit lessons to shared.pipeline_lessons.
"""

import asyncio
import json
import structlog
import asyncpg

from shared.secrets import get_database_url

from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest
from shared.llm_client.call_log import record_call_sync
from .seo_meta_utils import (best_meta_candidate, meta_in_band, SEO_META_FORBIDDEN, SEO_META_MIN, SEO_META_MAX)
from .prompts import parse_source_day_word_counts
from .itinerary_utils import (
    ITINERARY_CLAMP_MIN, ITINERARY_CLAMP_MAX, nudge_itinerary_day,
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


def _repair_still_compressed_days(state: dict, itinerary_text: str):
    """AA-329c: dedicated per-day repair for ITINERARY_STILL_COMPRESSED — reuses AA-353's own
    nudge_itinerary_day with the correct target_word_count per violating day, instead of routing
    "itineraries" through the generic FIX_SYSTEM prompt below (which has no idea what any day's
    target length is). One extra attempt per violating day (same single-shot budget AA-353 already
    uses for its own nudge). Deterministic accept/reject guard, mirrors seo_meta's
    best_meta_candidate/meta_in_band: a day is only overwritten if the new attempt actually lands
    in [ITINERARY_CLAMP_MIN, ITINERARY_CLAMP_MAX] — otherwise the pre-fix day is kept untouched
    rather than being overwritten with something no better (or worse).

    Returns (new_itinerary_text, extra_cost_usd, applied: bool). applied=False when there was
    nothing to repair (no still-violating day, or a violating day couldn't be matched back to
    source text) — new_itinerary_text is the unchanged input in that case.
    """
    days = parse_canonical_itinerary_days(itinerary_text)
    if not days:
        return itinerary_text, 0.0, False

    tour = state.get("tour", {})
    itineraries_raw = tour.get("itineraries") or tour.get("itinerary") or ""
    source = parse_source_day_word_counts(itineraries_raw, tour.get("duration"))
    source_words_by_day = source["day_word_counts"]
    source_text_by_day = source["day_text"]

    still_violating = []
    for day_num, day in days.items():
        src_words = source_words_by_day.get(day_num)
        if not src_words:
            continue
        ratio = len(day["body"].split()) / src_words
        if not (ITINERARY_CLAMP_MIN <= ratio <= ITINERARY_CLAMP_MAX):
            still_violating.append(day_num)

    if not still_violating:
        return itinerary_text, 0.0, False

    client = LLMClient()
    extra_cost = 0.0
    applied = False
    for day_num in sorted(still_violating):
        target_words = source_words_by_day.get(day_num)
        source_text = source_text_by_day.get(day_num)
        if not target_words or not source_text:
            continue
        new_title, new_body, resp = nudge_itinerary_day(
            client, source_text, days[day_num]["title"], days[day_num]["body"], target_words,
        )
        extra_cost += resp.cost_usd
        new_ratio = len(new_body.split()) / target_words
        in_clamp = ITINERARY_CLAMP_MIN <= new_ratio <= ITINERARY_CLAMP_MAX
        record_call_sync(
            stage="s1_itinerary_nudge", role="writer", model=resp.model_used,
            tokens_in=getattr(resp, "input_tokens", None), tokens_out=getattr(resp, "output_tokens", None),
            cost_usd=resp.cost_usd, tenant_id=None,
            quality_signal={"landed_in_clamp": in_clamp, "ratio_after_nudge": round(new_ratio, 3)},
            stop_reason=getattr(resp, "stop_reason", None),
        )
        if in_clamp:
            days[day_num] = {"title": new_title, "body": new_body}
            applied = True
            logger.info("itinerary_day_repaired", day=day_num, new_ratio=round(new_ratio, 3))
        else:
            logger.warning("itinerary_day_repair_still_out_of_clamp", day=day_num,
                            new_ratio=round(new_ratio, 3))
    if not applied:
        return itinerary_text, extra_cost, False
    return serialize_itinerary_days(days), extra_cost, True


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

        # AA-329c: ITINERARY_STILL_COMPRESSED gets the dedicated per-day repair above instead of
        # the generic FIX_SYSTEM prompt below — pull "itineraries" out of fix_keys so the generic
        # call (if still needed for other fields) doesn't also try to rewrite it in the same pass.
        if "ITINERARY_STILL_COMPRESSED" in (state.get("failure_codes") or []) and "itineraries" in fix_keys:
            fix_keys = fix_keys - {"itineraries"}
            new_itinerary, itin_cost, itin_applied = _repair_still_compressed_days(
                state, current_content.get("itineraries", ""),
            )
            extra_cost += itin_cost
            if itin_applied:
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
        fixed_fields = json.loads(raw)
        record_call_sync(
            stage="s1_flag_fix", role="writer", model=resp.model_used,
            tokens_in=getattr(resp, "input_tokens", None), tokens_out=getattr(resp, "output_tokens", None),
            cost_usd=resp.cost_usd, tenant_id=None,
            quality_signal={"fields_fixed": len(fix_keys), "fields_requested": sorted(fix_keys)},
            stop_reason=getattr(resp, "stop_reason", None),
        )

        new_generated = dict(current_content)
        for k, v in fixed_fields.items():
            if k in fix_keys:
                new_generated[k] = v

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

        applied_fields |= fix_keys
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
