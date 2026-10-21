"""AA-133: LLM-as-Judge brand audit node for the CIS content pipeline."""

import json
import os
import re
import structlog
from services.content_generation.stages import stage_for
from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest
from shared.llm_client.call_log import record_call_sync, record_unusable_sync
from services.content_generation.brand_fit import itinerary_digest
from .brand_standards import AA_BRAND_IDENTITY_PROMPT, AA_COWORK_STRUCTURE_PROMPT

logger = structlog.get_logger()

# ── Deterministic pre-audit checks ───────────────────────────────────────────

CITY_LIST_SUBTITLE = re.compile(
    r'^[A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*(?:,\s*[A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*){2,}$')
WAYPOINT_SUBTITLE = re.compile(r'[→❧►]|Route:|^\w[\w\s]+\s*→')
META_ROBOTIC_OPENERS = [
    re.compile(r'^This is a\b', re.IGNORECASE),
    re.compile(r'^Discover\b',   re.IGNORECASE),
    re.compile(r'^Book\b',       re.IGNORECASE),
    re.compile(r'^Find\b',       re.IGNORECASE),
]
OPTIONAL_HIGHLIGHT = re.compile(r'^Optional\b', re.IGNORECASE)
ELEPHANT_RIDING = re.compile(
    r'elephant[-\s]?back|elephant\s+ride|elephant\s+trek', re.IGNORECASE
)
NAME_ALL_CAPS_RE = re.compile(r'^[A-Z\s\d&\-]+$')
NAME_SUPERLATIVES = ['the best of', 'ultimate', 'must-see', 'and fun', 'expenditures']
# AA-195 / AA-608: fabricated meal LOGISTICS or clock-times in itineraries = PRODUCT_TRUTH_RISK.
# AA-608 finding (H2 verify): the old rule r'\b(breakfast|lunch|dinner)\b' fired on ANY mention
# of a meal word, so ordinary editorial prose ("after breakfast, cycle to the next village")
# tripped it on ~26/30 tours — a false positive, not a product-truth risk. The AA-195 intent was
# to catch a FABRICATED meal-inclusion CLAIM the source never made (a meal presented as logistics:
# "Meals: Breakfast, Dinner", "(B, L, D)", "breakfast included/provided/served"), not a narrative
# reference to the time of day. Narrate freely; only a logistics-style meal claim is flagged.
# AA-803: also plural and future forms ("Lunch and dinner are included", "dinner will be served"), a meal noun
# a few words before its verb ("meals on board the junk are prepared by the onboard chef"), generic forms
# ("All meals are included.", "Evening meal included.", "full board") and the feast / BBQ / picnic / brunch
# nouns: with the Jev question off, a meal claim the regex does not see is never checked at all.
ITIN_MEAL_INVENTED = re.compile(
    r'(?:'
    r'\bmeals?\s*(?:included|provided)?\s*[:\-]'                 # "Meals:" / "Meal included -"
    r'|\((?:\s*[BLD](?:\s*(?:[,/&|\u00b7]|and)\s*[BLD]){0,2}\s*)\)'   # "(B, L, D)" meal codes
    r'|\b(?:breakfasts?|brunch(?:es)?|lunch(?:es|eons?)?|dinners?|suppers?|meals?|picnics?|feasts?|barbecues?'
    r'|bbqs?|banquets?)'
    r'(?:\s+(?!(?:not|no|never|optional|cannot|recommend\w*|suggest\w*|options?|tips?|ideas?|advice)\b)'
    r'[\w\'\u2019-]+){0,4}?'
    r'\s+(?:(?:is|are|was|were|will\s+be|can\s+be|gets?)\s+)?'
    r'(?:also\s+)?(?:included|provided|served|arranged|offered|supplied|prepared|hosted)\b'
    r'|\b(?:full|half)[\s-]+board\b|\ball\s+meals\b'
    r')',
    re.IGNORECASE,
)
# A specific clock time ("7:00 AM departure", "8am") is still a fabricated logistic when the
# source gave none — kept as-is (AA-195). Narrative "morning/afternoon/evening" is fine (not matched).
ITIN_CLOCK_TIME = re.compile(r'\b\d{1,2}(:\d{2})?\s?(am|pm)\b', re.IGNORECASE)
# AA-196: generic day titles that name no place/activity = ITINERARY_DAY_TITLE_GENERIC
ITIN_DAY_TITLE_GENERIC = re.compile(
    r"Day\s+\d+\s*--\s*(Exploration|Free Day|Arrival Day|Arrival|Departure|Transfer)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _norm_clock(text: str) -> str:
    return re.sub(r"\s+", "", text.lower())


def meal_time_claims_supported(itin_text: str, tour: dict, *, tenant_id: str | None = None,
                               is_tenant_rewrite: bool = False) -> bool:
    """AA-692 A1-3: True when every meal/clock-time hit in the rewrite is backed by the raw source.

    S205 measure: the regex fired on 41/125 master tours, and most hits were the source's own
    logistics (14/17 clock times appear verbatim in the source; 26/34 tours' sources state meals too).
    - clock time: supported when the same time is in the source (deterministic);
    - meal claim (AA-803): first the deterministic rule (``meal_claims``): the sentence is sourced when
      every meal it claims is named, not negated, on the same source day or in INCLUSIONS (meal words,
      B/L/D codes, "all meals" / full board), and it states no clock time the source lacks;
    - a meal sentence the rule does not clear is sent to the same enforced Jev Question grounding uses
      (a1_claim_supported, same source text and subject key, so it is normally a cache hit);
      supported only on a confident, enforced accept. Grey/shadow/error/off keep the code (fail-closed),
      so with the question off only the deterministic rule clears a meal claim."""
    from services.content_generation import grounding as g
    from services.content_generation import meal_claims
    source = g.source_text(tour)
    src_clocks = {_norm_clock(m.group(0)) for m in ITIN_CLOCK_TIME.finditer(source)}
    if any(_norm_clock(m.group(0)) not in src_clocks for m in ITIN_CLOCK_TIME.finditer(itin_text)):
        return False
    if not ITIN_MEAL_INVENTED.search(itin_text):
        return True
    # AA-803: every meal-claim sentence, including the short ones ("Lunch is included.") and day-title lines
    # ("Day 3 — Hue (B, L)") that grounding never asks Jev about (fewer than MIN_WORDS words / title lines).
    claims = [u for u in meal_claims.itinerary_sentences(itin_text) if ITIN_MEAL_INVENTED.search(u["sentence"])]
    if not claims:
        return False
    days = g._source_days(tour)
    out_days = meal_claims.output_day_numbers(meal_claims.itinerary_sentences(itin_text))
    unsourced = [u for u in claims if not meal_claims.unit_meal_sourced(u, tour, days, out_days)[0]]
    if not unsourced:
        return True             # every meal claim is in the source: no Jev call
    askable = {(u["sentence"], u["day"]) for u in g.sentence_units({"itineraries": itin_text})}
    units = [u for u in unsourced if (u["sentence"], u["day"]) in askable]
    if len(units) < len(unsourced):
        return False            # an unsourced short / title-line claim Jev is never asked about: keep the code
    if is_tenant_rewrite and not tenant_id:
        return False            # tenant text with no owner → never sent to Jev; keep the code
    # AA-741 pre-audit: judge_units takes {unit index: per-unit source} since #631; passing the
    # whole source string made every call fail open, so this code fired on every meal mention.
    # Per-unit source = the same subject key grounding uses → normally a cache hit.
    sources = {i: g.unit_source(tour, u) for i, u in enumerate(units)}
    verdicts = g.judge_units(units, sources, tenant_id=tenant_id if is_tenant_rewrite else None)
    return all(getattr(verdicts.get(i), "enforced", False) and getattr(verdicts.get(i), "zone", None) == "accept"
               for i in range(len(units)))


def pre_audit_checks(generated: dict, tour: dict | None = None, *, tenant_id: str | None = None,
                     is_tenant_rewrite: bool = False) -> list[str]:
    codes = []
    name = (generated.get("name") or "")
    subtitle = (generated.get("subtitle") or "")
    seo_meta = (generated.get("seo_meta") or "")
    highlights = generated.get("highlights") or []
    if isinstance(highlights, str):
        highlights = [highlights]

    if name and NAME_ALL_CAPS_RE.match(name.strip()):
        codes.append("NAME_ALL_CAPS")
    for sup in NAME_SUPERLATIVES:
        if sup in name.lower():
            codes.append("NAME_SUPERLATIVE")
            break
    if subtitle and CITY_LIST_SUBTITLE.match(subtitle.strip()):
        codes.append("SUBTITLE_CITY_LIST")
    if subtitle and WAYPOINT_SUBTITLE.search(subtitle):
        codes.append("SUBTITLE_WAYPOINT_FORMAT")
    for opener in META_ROBOTIC_OPENERS:
        if opener.search(seo_meta):
            codes.append("META_OPENER_ROBOTIC")
            break
    if "package" in seo_meta.lower():
        codes.append("META_PACKAGE_WORD")
    for h in highlights:
        if OPTIONAL_HIGHLIGHT.match(str(h)):
            codes.append("HIGHLIGHTS_OPTIONAL_LANGUAGE")
            break
    combined = " ".join(str(h) for h in highlights).lower()
    if ELEPHANT_RIDING.search(combined):
        codes.append("FACT_CHECK_MANUAL_CHECK")

    itineraries = generated.get("itineraries") or ""
    if isinstance(itineraries, (list, dict)):
        itineraries = json.dumps(itineraries)
    itin_text = str(itineraries)
    if ITIN_MEAL_INVENTED.search(itin_text) or ITIN_CLOCK_TIME.search(itin_text):
        # AA-692 A1-3: no code when the source itself states these logistics (needs the source tour).
        if not (tour and meal_time_claims_supported(itin_text, tour, tenant_id=tenant_id,
                                                     is_tenant_rewrite=is_tenant_rewrite)):
            codes.append("ITINERARY_MEAL_TIME_INVENTED")
    if ITIN_DAY_TITLE_GENERIC.search(itin_text):
        codes.append("ITINERARY_DAY_TITLE_GENERIC")

    return list(set(codes))


# ── GPT-4.1 strict JSON schema ────────────────────────────────────────────────

BRAND_AUDIT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["brand_audit"],
    "properties": {
        "brand_audit": {
            "type": "object",
            "additionalProperties": False,
            # AA-714: `status` is NOT required. Luna on Bedrock Converse drops it ~15% of the time
            # (Converse does not enforce the tool schema), and status is fully derivable from
            # failure_codes + publish_ready (derive_status). The model may still send it; we trust
            # our own derivation either way, so the audit no longer depends on the model emitting it.
            "required": [
                "publish_ready", "fields_to_fix",
                "failure_codes", "issues", "scores", "notes",
                "lessons_extracted"
            ],
            "properties": {
                "status":         {"type": "string", "enum": ["pass", "flagged", "manual_check"]},
                "publish_ready":  {"type": "boolean"},
                "fields_to_fix":  {"type": "array", "items": {"type": "string"}},
                "failure_codes":  {"type": "array", "items": {"type": "string"}},
                "issues":         {"type": "array", "items": {"type": "string"}},
                "scores": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["brand_fit", "human_read", "seo_fit", "trip_type_accuracy", "publish_readiness"],
                    "properties": {
                        "brand_fit":          {"type": "integer", "enum": [0, 1]},
                        "human_read":         {"type": "integer", "enum": [0, 1]},
                        "seo_fit":            {"type": "integer", "enum": [0, 1]},
                        "trip_type_accuracy": {"type": "integer", "enum": [0, 1]},
                        "publish_readiness":  {"type": "integer", "enum": [0, 1]},
                    },
                },
                "notes": {"type": "string"},
                "lessons_extracted": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["pattern", "failure_code", "field", "example_before", "severity"],
                        "properties": {
                            "pattern":        {"type": "string"},
                            "failure_code":   {"type": "string"},
                            "field":          {"type": "string"},
                            "example_before": {"type": "string"},
                            "severity":       {"type": "string", "enum": ["high", "medium", "low"]},
                        },
                    },
                },
            },
        }
    },
}


# ── Node ──────────────────────────────────────────────────────────────────────

# AA-714: set when the audit model call fails, so the reviewer sees why the tour is held.
BRAND_AUDIT_UNAVAILABLE = "BRAND_AUDIT_UNAVAILABLE"

# A code that always means "a human must look", regardless of how many codes there are.
_MANUAL_CHECK_CODES = {"FACT_CHECK_MANUAL_CHECK", "PRODUCT_TRUTH_RISK"}


def derive_status(failure_codes: list[str], publish_ready, model_status=None) -> str:
    """AA-714: compute brand_audit status from the evidence, not from a key the model may omit.

    Luna on Bedrock Converse drops the `status` key ~15% of the time, which used to fail the audit
    open. Status is deterministic from what the audit already produces:
      - any manual-check code (fact/product-truth)         -> manual_check (strongest, wins)
      - else any failure code, or publish_ready == False    -> flagged
      - else                                                -> pass
    `model_status` (when present) only breaks a tie toward the safer side — it never downgrades a
    status our codes imply."""
    codes = set(failure_codes or [])
    if codes & _MANUAL_CHECK_CODES or model_status == "manual_check":
        return "manual_check"
    if codes or publish_ready is False or model_status == "flagged":
        return "flagged"
    return "pass"

def _audit_from_judge(state: dict, generated: dict) -> dict:
    """AA-206: build the brand-audit result from the GPT-4.1 judge fields instead of a second LLM
    call. Deterministic pre-audit codes still fire (they drive flag_fix's field targeting) and the
    judge's feedback is surfaced as an issue so flag_fix has context for the repair pass."""
    pre_codes = pre_audit_checks(generated, state.get("tour"), tenant_id=state.get("tenant_id"),
                                 is_tenant_rewrite=bool(state.get("is_tenant_rewrite")))
    judge_feedback = (state.get("judge_feedback") or "").strip()
    issues = [judge_feedback] if judge_feedback else []
    # S218 audit: same status rule as the LLM audit path (derive_status). This path used its own
    # "flagged if any code", so FACT_CHECK_MANUAL_CHECK (elephant riding) read as a fixable brand
    # flag: flag_fix ran with nothing to fix and the reviewer saw the wrong reason.
    status = derive_status(pre_codes, None)
    logger.info("brand_audit_from_judge", status=status, codes=pre_codes,
                brand_fit=state.get("judge_brand_fit"),
                mission_present=state.get("judge_mission_present"))
    return {
        **state,
        "brand_audit_status": status,
        "brand_audit_codes":  pre_codes,
        "brand_audit_issues": issues,
        "brand_audit_fields": [],
        "lessons_extracted":  [],
    }


def _tenant_rubric(state: dict) -> str | None:
    """AA-792: the T2 audit identity — the tenant's own system_prompt when one was written, else the
    rubric compiled from its T0 fields (AA-771 brand_pack.compile_rubric) plus its style guide.
    None when the tenant has no brand to audit against."""
    from services.acp_shared.brand_pack import compile_rubric
    from .batch_prompt import brand_pack_from_state
    pack = brand_pack_from_state(state)
    rubric = pack.get("system_prompt") or compile_rubric(pack)
    if rubric and pack.get("style_guide"):
        rubric += "\nStyle guide:\n" + pack["style_guide"]
    return rubric


def _audit_deterministic_only(state: dict, generated: dict) -> dict:
    """AA-792: T2 with no tenant brand set up — no LLM audit (there is nothing to judge the voice
    against); the deterministic pre-audit codes still drive flag_fix, as in _audit_from_judge."""
    pre_codes = pre_audit_checks(generated, state.get("tour"), tenant_id=state.get("tenant_id"),
                                 is_tenant_rewrite=True)
    status = derive_status(pre_codes, None)
    logger.info("brand_audit_tenant_no_brand", status=status, codes=pre_codes)
    return {
        **state,
        "brand_audit_status": status,
        "brand_audit_codes":  pre_codes,
        "brand_audit_issues": [],
        "brand_audit_fields": [],
        "lessons_extracted":  [],
    }


def brand_audit_node(state: dict) -> dict:
    """AA-133: LLM-as-Judge brand audit. Runs after validate when score >= 7.0.

    AA-206: when the GPT-4.1 judge ran earlier in the graph (branded tours with a differentiation
    profile), reuse its result instead of making a second GPT call. Falls through to the standalone
    OpenAI audit only when the judge did not run (legacy/no-profile brands)."""
    generated = state.get("generated", {})
    if not generated:
        return {
            **state,
            "brand_audit_status": "pass",
            "brand_audit_codes":  [],
            "brand_audit_issues": [],
            "brand_audit_fields": [],
            "lessons_extracted":  [],
        }

    # AA-206: prefer the judge's result over a second LLM call (judge_node sets judge_brand_fit only
    # on a successful score). Keeps deterministic pre-audit codes for flag_fix; no double GPT cost.
    if "judge_brand_fit" in state:
        return _audit_from_judge(state, generated)

    # AA-792: a T2 tenant rewrite is never graded against Adventure Asia's own voice (calm, private,
    # no hype…) — that marked e.g. Trail Pulse's energetic copy off-brand. It is audited against the
    # tenant's own rubric (brand_pack.compile_rubric / its system_prompt), or, with no brand set up,
    # gets the deterministic pre-audit codes only.
    standard = "Adventure Asia brand standards"
    brand_fit_question = "calm, assured, selective, precise, private, no hype?"
    if state.get("is_tenant_rewrite"):
        rubric = _tenant_rubric(state)
        if rubric is None:
            return _audit_deterministic_only(state, generated)
        identity = rubric
        standard = "THIS client brand's standards"
        brand_fit_question = "does it read in the brand voice described above?"
    else:
        identity = AA_BRAND_IDENTITY_PROMPT

    resp, logged = None, False  # AA-927
    try:
        pre_codes = pre_audit_checks(generated, state.get("tour"), tenant_id=state.get("tenant_id"),
                                 is_tenant_rewrite=bool(state.get("is_tenant_rewrite")))

        system_prompt = identity + "\n\n" + AA_COWORK_STRUCTURE_PROMPT

        tour = state.get("tour", {})
        dfs = state.get("seo", {})
        highlights = generated.get("highlights") or []
        if isinstance(highlights, str):
            highlights = [highlights]
        highlights_text = "\n".join(f"- {h}" for h in highlights)
        dfs_brief = str(dfs.get("top_keywords", []))[:200]

        user_prompt = f"""Review this tour against {standard}.
Pre-audit deterministic checks found these codes (include in assessment): {pre_codes}

CONTENT TO REVIEW:
AA_NAME: {generated.get("name")}
AA_SUBTITLE: {generated.get("subtitle")}
AA_SUMMARY: {generated.get("summary")}
AA_HIGHLIGHTS:
{highlights_text}
AA_ITINERARIES: {itinerary_digest(generated.get("itineraries"), per_day=200, total=3000)}
SEO_TITLE: {generated.get("seo_title")}
SEO_META: {generated.get("seo_meta")}

TRIP TYPE: {generated.get("trip_type") or tour.get("trip_type", "unknown")}
SOURCE DURATION: {tour.get("duration", "unknown")}
DFS CONTEXT: {dfs_brief}

REVIEW ORDER:
1. Product truth: all fields align to same actual product?
2. Brand fit: {brand_fit_question}
3. Trip type accuracy: subtitle/summary/SEO match actual activity?
4. Highlights quality: specific moments, 4+ items, no "Optional"?
5. Human readability: no editorial language leak?
6. SEO quality: title 60 chars max, meta 140-155 chars, ends with period?
7. Publish readiness: ready without any changes?

Return JSON only per schema."""

        # AA-659 / ADR 0006: the model comes from the "s1_brand_audit" stage route through the
        # gateway (Bedrock first, OpenAI API as fallback). The strict schema is kept: OpenAI
        # json_schema, or a forced tool on Bedrock Converse.
        resp = LLMClient().generate(LLMRequest(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.1,
            stage=stage_for(state, "s1_brand_audit"),  # AA-763
            json_schema={"name": "brand_audit_result", "schema": BRAND_AUDIT_SCHEMA},
        ))
        result = json.loads(resp.content)["brand_audit"]

        # Merge deterministic pre-codes into LLM codes
        all_codes = list(dict.fromkeys(result["failure_codes"] + pre_codes))
        result["failure_codes"] = all_codes
        # AA-714: derive status ourselves (do not depend on the model emitting it — Luna drops it).
        status = derive_status(all_codes, result.get("publish_ready"), result.get("status"))
        result["status"] = status

        in_tok, out_tok = resp.input_tokens, resp.output_tokens
        cost = resp.cost_usd
        stop_reason = resp.stop_reason  # AA-493

        logger.info("brand_audit_done",
                    status=result["status"],
                    codes=result["failure_codes"], model=resp.model_used,
                    in_tokens=in_tok, out_tokens=out_tok, cost_usd=cost, stop_reason=stop_reason)

        record_call_sync(
            stage=stage_for(state, "s1_brand_audit"), role="judge", model=resp.model_used,
            tokens_in=in_tok, tokens_out=out_tok, cost_usd=cost,
            quality_signal={"status": result["status"], "failure_code_count": len(all_codes)},
            stop_reason=stop_reason, account=resp.satellite_account,
            fallback_used=resp.fallback_used, provider=resp.provider,
        )
        logged = True
        return {
            **state,
            "brand_audit_status": result["status"],
            "brand_audit_codes":  result["failure_codes"],
            "brand_audit_issues": result["issues"],
            "brand_audit_fields": result["fields_to_fix"],
            "lessons_extracted":  result["lessons_extracted"],
            "cost_usd": state.get("cost_usd", 0) + cost,
        }

    except Exception as e:
        # AA-714: fail closed. A model/schema error used to return "pass", so a tour the audit
        # never saw could publish (S200: Luna dropped `status`; S207: OpenAI out of credit).
        # manual_check blocks publish (_is_publishable) and sends the tour to the Review Queue.
        logger.warning("brand_audit_failed_manual_check", error=str(e)[:500])
        if not logged:
            # AA-927: a billed reply that broke the schema still gets its cost row (no `status` key).
            record_unusable_sync(resp, stage=stage_for(state, "s1_brand_audit"), role="judge", error=e)
        from services.quality import degraded  # AA-925: counted (it already fails closed)
        return {
            **state,
            "degraded": degraded.add(state.get("degraded"), degraded.BRAND_AUDIT_UNAVAILABLE,
                                     detector="brand_audit", detail=str(e)[:200]),
            "brand_audit_status": "manual_check",
            "brand_audit_codes":  [BRAND_AUDIT_UNAVAILABLE],
            "brand_audit_issues": [f"Brand audit could not run ({str(e)[:200]}). Check by hand or Regenerate."],
            "brand_audit_fields": [],
            "lessons_extracted":  [],
        }
