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
# AA-803 (S247): also plural, future and generic claims. On main only "<meal> [is] included/provided/served/arranged"
# was read, so "Lunch and dinner are included", "dinner will be served", "meals on board the junk are prepared",
# "The price includes all meals" and "full board" were never checked at all. A gap word may not be a negation, a
# modal or an advice noun: "Lunch is not included", "Dinner can be arranged" (the traveller's option) and "Dinner
# recommendations are provided" are not inclusion claims.
_MEAL_NOUN = (r"(?:(?:breakfast|lunch|dinner|supper)[\s-]?times?|breakfasts?|brunch(?:es)?|lunch(?:eons?|es)?"
              r"|dinners?|suppers?|meals?|picnics?|feasts?|banquets?|barbecues?|bbqs?)")
_MEAL_GAP = (r"(?!(?:not|no|never|optional|cannot|can|could|may|might|should|would|recommend\w*|suggest\w*"
             r"|options?|tips?|ideas?|advice)\b|\w+n['\u2019]t\b)[\w'\u2019-]+")
_MEAL_VERB = r"(?:included|provided|served|arranged|offered|supplied|prepared|hosted|covered)"
_MEAL_PLACEHOLDER = "[BLDx\u00d7\\-\u2013\u2014]"
ITIN_MEAL_INVENTED = re.compile(
    r'(?:'
    r'\bmeals?\s*(?:included|provided)?\s*[:\-\u2013\u2014]'      # "Meals:" / "Meal included -"
    # "(B, L, D)" meal codes, also with a placeholder for a meal not given ("(B/-/D)", "(B/x/D)")
    r'|\((?=[^()]*[BLD])\s*' + _MEAL_PLACEHOLDER + r'(?:\s*(?:[,/&|\u00b7\u2022+]|and)\s*'
    + _MEAL_PLACEHOLDER + r'){0,2}\s*\)'
    # "<meal> [up to 4 words] [is/are/will be] [also] included|provided|served|..."
    r'|\b' + _MEAL_NOUN + r'(?:\s+' + _MEAL_GAP + r'){0,4}?\s+(?:(?:is|are|was|were|will\s+be|being|gets?)\s+)?'
    r'(?:also\s+)?' + _MEAL_VERB + r'\b'
    # "includes / including [all|a|daily ...] <meal>"
    r'|\binclud(?:es|ing|ed)\s+(?:(?:a|an|the|daily|all|full|three|two|packed|picnic|buffet|set|welcome|farewell'
    r'|local|traditional)\s+){0,2}' + _MEAL_NOUN + r'\b'
    r'|\b(?:full|half)[\s-]+board\b|\b(?:all|three|daily|every|each)\s+meals?\b'
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


def itinerary_claim_sentences(itin_text: str) -> list[dict]:
    """AA-803: every sentence of a written itinerary as {field, sentence, day}, with the same line / sentence split
    and the same day rule as ``grounding.sentence_units``, but without its ``MIN_WORDS`` floor and keeping the
    day-title lines ("Day 3 — Hue (B, L)", one unit each). ``sentence_units`` drops both, so a short meal claim
    ("Dinner included.") or a title-line code list was never checked."""
    from services.content_generation import grounding as g
    out: list[dict] = []
    day: int | None = None
    for ln in str(itin_text or "").splitlines():
        if not ln.strip():
            continue
        m = g._GEN_DAY_NUM_RE.match(ln)
        if m:
            day = int(m.group(1))
            if g._DAY_TITLE_RE.match(ln):
                out.append({"field": "itineraries", "sentence": ln.strip(), "day": day})
                continue
        for c in g._SENT_SPLIT_RE.split(ln):
            s = c.strip()
            if s:
                out.append({"field": "itineraries", "sentence": s, "day": day})
    return out


def meal_time_claims_supported(itin_text: str, tour: dict, *, tenant_id: str | None = None,
                               is_tenant_rewrite: bool = False) -> bool:
    """AA-692 A1-3: True when every meal/clock-time hit in the rewrite is backed by the raw source.

    S205 measure: the regex fired on 41/125 master tours, and most hits were the source's own
    logistics (14/17 clock times appear verbatim in the source; 26/34 tours' sources state meals too).
    - clock time: supported when the same time is in the source (deterministic);
    - meal claim: every itinerary sentence that makes one (AA-803: short sentences and day-title lines too) is
      sent to the same enforced Jev Question grounding uses (a1_claim_supported, same source text and subject
      key, so it is normally a cache hit); supported only on a confident, enforced accept of every one.
      Grey/shadow/error/off keep the code (fail-closed), and so does a claim sentence Jev is never asked about."""
    from services.content_generation import grounding as g
    source = g.source_text(tour)
    src_clocks = {_norm_clock(m.group(0)) for m in ITIN_CLOCK_TIME.finditer(source)}
    if any(_norm_clock(m.group(0)) not in src_clocks for m in ITIN_CLOCK_TIME.finditer(itin_text)):
        return False
    if not ITIN_MEAL_INVENTED.search(itin_text):
        return True
    claims = [u for u in itinerary_claim_sentences(itin_text) if ITIN_MEAL_INVENTED.search(u["sentence"])]
    if not claims:
        return False
    if not is_tenant_rewrite:
        # AA-645 (S249): an A1 meal line that copies the same source day's own meal codes is not a claim to check.
        blocks = source_day_meal_blocks(tour, itin_text)
        claims = [c for c in claims if not source_backed_meal_line(c["sentence"], blocks.get(c["day"]))]
        if not claims:
            return True
    return _jev_clears_meal_claims(claims, itin_text, tour, tenant_id=tenant_id,
                                   is_tenant_rewrite=is_tenant_rewrite)


# ── AA-645 (S249): a meal fact line copied from the source day is source-backed ──────────────────
# The S1 writer v2 ends a day with one fact line holding only what that source day states (rule 7; it reads the day's
# verbatim source block), e.g. "Overnight: Jaipur. Meals: dinner." from "Meals included: Dinner" or "(L+D)".
# The "Meals:" branch of ITIN_MEAL_INVENTED matched every such line and only Jev could clear it, so 36/50 v2 drafts of
# the S249 shadow were flagged and 34 went through the generic flag_fix pass that rewrites the WHOLE itinerary — the
# rewrite that dropped Days 11–15 of India "Rajasthan Adventure". A meal line is source-backed when (1) every meal it
# includes is one the SAME source day lists in its own meal line or code, and (2) every other word of it occurs in that
# source day's text. Anything else — a meal the day does not list, a word the day does not have, a second meal claim
# in the sentence, a day the source has no block for — still goes to Jev, exactly as before.
_MEAL_LINE_RE = re.compile(r"\bmeals?\s*(?:included|provided)?\s*[:\-–—]\s*(?P<v>[^.;\n]*)", re.IGNORECASE)
_PAGE_MEAL_CODE_RE = re.compile(r"\(\s*(?P<v>(?:B|L|D)(?:\s*[,/&+]\s*(?:B|L|D)){0,2})\s*\)")
_MEAL_TOKEN = {"breakfast": "B", "breakfasts": "B", "b": "B", "bb": "B", "lunch": "L", "lunches": "L", "l": "L",
               "dinner": "D", "dinners": "D", "supper": "D", "suppers": "D", "d": "D"}
_BOARD = {"full": {"B", "L", "D"}, "half": {"B", "D"}}
_BOARD_CODES = {"hb": {"B", "D"}, "fb": {"B", "L", "D"}}
# A clause that does not include the meal: "lunch not included", "dinner optional, payable directly", "no meals".
_NOT_INCLUDED_RE = re.compile(r"\b(?:not|no|none|nil|excluded?|own|optional|payable|extra|request|self)\b",
                              re.IGNORECASE)
_MEAL_LIST_WORDS = frozenset({"and", "or", "included", "provided", "only", "meal", "meals", "board", "with"})
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def meal_set(value: str) -> set[str]:
    """The meals ("B", "L", "D") a meal-line value includes: "breakfast, lunch" / "B, L, D" / "B/L/D" / "full board"
    / "HB". A clause that says the meal is not included (``_NOT_INCLUDED_RE``) adds nothing."""
    out: set[str] = set()
    for clause in re.split(r"[,;/|]", str(value or "").lower()):    # "lunch and dinner not included" = one clause
        if _NOT_INCLUDED_RE.search(clause):
            continue
        toks = _TOKEN_RE.findall(clause)
        for i, t in enumerate(toks):
            if t in _MEAL_TOKEN:
                out.add(_MEAL_TOKEN[t])
            elif t in _BOARD_CODES:
                out |= _BOARD_CODES[t]
            elif t in _BOARD and i + 1 < len(toks) and toks[i + 1] == "board":
                out |= _BOARD[t]
    return out


def source_day_meal_blocks(tour: dict, itin_text: str) -> dict:
    """{page day: that day's source text} for the meal-line check. Source "Day N" blocks (ranges expanded, the first
    block of a repeated day kept: s1_writer_v2.source_day_blocks, the blocks the v2 plan is built from). A source with
    no day markers maps to a ONE-day page only (day 1, and None for claims above any day heading); a multi-day page
    over an unmarked source has no per-day block, so its meal lines are left to Jev."""
    from services.content_generation.s1_writer_v2 import source_day_blocks
    from services.quality.s1_facts import field_text

    src = field_text((tour or {}).get("itineraries") or (tour or {}).get("itinerary"))
    out: dict = {}
    for first, last, text in source_day_blocks(src):
        for n in range(first, last + 1):
            out.setdefault(n, text)
    if not out and src.strip():
        from services.content_generation import grounding as g
        page_days = {int(m.group(1)) for m in (g._GEN_DAY_NUM_RE.match(ln) for ln in str(itin_text or "").splitlines())
                     if m}
        if len(page_days) <= 1:
            out = {1: src, None: src}
    return out


# The source day's own meal list: a meal-label line ("Meals: B, L", "Meals  L D", "Meals included: Breakfast & Dinner")
# or a meal-code group anywhere on a line ("(B, L)", "(L+D)", "(B/-/D)"). Codes are upper-case only, so an "(a) … (b)"
# enumeration is never read as breakfast.
_SRC_MEAL_LABEL_RE = re.compile(r"^\s*[-\u2022*]?\s*meals?(?:\s+included)?\b\s*[:\-\u2013\u2014]?\s*(?P<v>\S.*)$",
                                re.IGNORECASE)
_SRC_MEAL_HEADER_RE = re.compile(r"^\s*meals?(?:\s+included)?\s*:?\s*$", re.IGNORECASE)
_SRC_BULLET_RE = re.compile(r"^\s*[-\u2022*\u00b7]\s*\S")
_SRC_CODE = r"(?:BB|HB|FB|B|L|D|[-\u2013\u2014x\u00d7])"
_SRC_MEAL_CODE_RE = re.compile(r"\((?=[^()]*[BLDHF])\s*(?P<v>" + _SRC_CODE + r"(?:\s*(?:[,/&+|\u00b7]|and)\s*"
                               + _SRC_CODE + r"){0,3})\s*\)")


def _source_day_meals(block: str) -> set[str]:
    """Meals the source day lists in its own meal line(s) or meal-code group(s) — never a meal its prose mentions
    ("after breakfast, drive to …" is not a meal list)."""
    out: set[str] = set()
    lines = (block or "").splitlines()
    for i, line in enumerate(lines):
        if _SRC_MEAL_HEADER_RE.match(line):            # "Meals Included:" with the list as bullets below it
            for nxt in lines[i + 1:i + 4]:
                if not _SRC_BULLET_RE.match(nxt):
                    break
                out |= meal_set(nxt)
        else:
            m = _SRC_MEAL_LABEL_RE.match(line)
            if m and len(line) <= 200:
                out |= meal_set(m.group("v"))
        for c in _SRC_MEAL_CODE_RE.finditer(line):
            out |= meal_set(c.group("v"))
    return out


def source_backed_meal_line(sentence: str, block: str | None) -> bool:
    """True when ``sentence``'s meal claim is a copy of the source day's meal line (see the comment block above)."""
    if not block or not block.strip():
        return False
    m = _MEAL_LINE_RE.search(sentence) or _PAGE_MEAL_CODE_RE.search(sentence)
    if not m:
        return False
    rest = sentence[:m.start()] + " " + sentence[m.end():]
    if ITIN_MEAL_INVENTED.search(rest):
        return False                                   # a second meal claim in the same sentence: Jev decides
    value = m.group("v")
    page_meals, src_meals = meal_set(value), _source_day_meals(block)
    if not page_meals <= src_meals:
        return False                                   # a meal the source day does not list
    allowed = set(_MEAL_TOKEN) | _MEAL_LIST_WORDS | set(_BOARD) | set(_BOARD_CODES)
    if not page_meals and not src_meals:
        allowed |= {"none", "no", "not"}               # "Meals: none included." on a day that lists no meal
    src_words = set(_TOKEN_RE.findall(block.lower()))
    for w in _TOKEN_RE.findall(value.lower()):
        if w not in allowed and w not in src_words:
            return False                               # "buffet", "welcome", "3": only when the source day says it
    return True


def _jev_clears_meal_claims(claims: list[dict], itin_text: str, tour: dict, *, tenant_id: str | None,
                            is_tenant_rewrite: bool) -> bool:
    """True only when Jev gives an enforced accept on every meal-claim sentence in ``claims``."""
    from services.content_generation import grounding as g
    askable = g.sentence_units({"itineraries": itin_text})
    asked = {(u["sentence"], u["day"]) for u in askable}
    # AA-803: a claim grounding never builds a unit for (under MIN_WORDS, a title line) cannot get a verdict, so it
    # keeps the code; Jev is not called, since it could not clear the itinerary anyway.
    if any((c["sentence"], c["day"]) not in asked for c in claims):
        return False
    wanted = {(c["sentence"], c["day"]) for c in claims}
    units = [u for u in askable if (u["sentence"], u["day"]) in wanted]
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

def _is_v2_draft(state: dict) -> bool:
    from services.content_generation.s1_writer_v2_live import is_v2
    return is_v2(state)


# AA-645 step 0 (d02): on an A1 draft of the S1 writer v2 the AA voice rubric of the LLM audit is report-only, like
# brand fit in the judge. What still gates: the deterministic pre-audit codes (editorial format, meal/clock-time
# claims, elephant riding = FACT_CHECK_MANUAL_CHECK), the product-truth codes (_MANUAL_CHECK_CODES) and a model
# "manual_check" verdict (a human must look: product truth, not voice). FORBIDDEN_WORD is validate's hard code and is
# not touched here. The other LLM codes and a bare publish_ready=false / "flagged" go to quality_shadow.
V2_BRAND_AUDIT_DETECTOR = "s1_writer_v2_brand_audit"


def _v2_report_only_voice(all_codes: list[str], pre_codes: list[str], model_status,
                          publish_ready=None) -> tuple[list, str, list]:
    hard = [c for c in all_codes if c in pre_codes or c in _MANUAL_CHECK_CODES]
    voice = [c for c in all_codes if c not in hard]
    status = derive_status(hard, None, "manual_check" if model_status == "manual_check" else None)
    report = [{"code": c, "detector": V2_BRAND_AUDIT_DETECTOR, "mode": "report",
               "detail": "AA voice rubric (report-only on a v2 draft, AA-645 d02)"} for c in voice]
    if not voice and status == "pass" and (model_status == "flagged" or publish_ready is False):
        report.append({"code": "BRAND_AUDIT_FLAGGED", "detector": V2_BRAND_AUDIT_DETECTOR, "mode": "report",
                       "detail": "the audit model flagged the draft without a code (report-only on v2)"})
    return hard, status, report


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
    # AA-645 (S248): the A1 page is a neutral, white-label catalogue page agencies resell unchanged — the judge is
    # framed like the A1 fixer (flag_fix_node.FIX_SYSTEM), not as "Adventure Asia brand standards". The voice rubric
    # in the system prompt (AA_BRAND_IDENTITY_PROMPT) is unchanged: step-5 cleanup.
    standard = ("the editorial standard above, for a neutral white-label catalogue page that agencies resell "
                "unchanged (no brand, operator or supplier name on the page)")
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
        model_status = result.get("status")
        # AA-714: derive status ourselves (do not depend on the model emitting it — Luna drops it).
        status = derive_status(all_codes, result.get("publish_ready"), model_status)
        voice_report: list[dict] = []
        if _is_v2_draft(state):
            all_codes, status, voice_report = _v2_report_only_voice(all_codes, pre_codes, model_status,
                                                                    result.get("publish_ready"))
        result["failure_codes"] = all_codes
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
        out = {
            **state,
            "brand_audit_status": result["status"],
            "brand_audit_codes":  result["failure_codes"],
            "brand_audit_issues": result["issues"],
            "brand_audit_fields": result["fields_to_fix"],
            "lessons_extracted":  result["lessons_extracted"],
            "cost_usd": state.get("cost_usd", 0) + cost,
        }
        if voice_report:
            out["quality_shadow"] = list(state.get("quality_shadow") or []) + voice_report
        return out

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
