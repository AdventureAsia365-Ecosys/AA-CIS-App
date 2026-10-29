"""AA-606: standalone S1-rewrite prompt materialization for Bedrock Batch Inference.

The synchronous S1 graph (graph.generate_node) builds the writer's (system, user) prompt inline
and calls the LLM one tour at a time. Bedrock Batch (CreateModelInvocationJob) needs the SAME
attempt-1 prompt pre-materialized into a JSONL manifest (one record per tour) BEFORE any LLM call,
so this module factors that prompt assembly out of generate_node into pure, importable functions.

Contract kept identical to generate_node's attempt-1 path (retry_count=0, feedback=""):
  - USER prompt  = build_rewrite_prompt(tour, seo, few_shots, subtitle_focus)  (prompts.py, pure)
  - SYSTEM prompt = SYSTEM_PROMPT + language + brand append + brand-diff block + forbidden words
    (was inlined in generate_node; graph.generate_node now calls build_s1_system_prompt so the two
    can never drift).

There is NO feedback branch here on purpose: the retry loop's `PREVIOUS ATTEMPT FEEDBACK` append
depends on the judge output of a prior attempt, which does not exist for a batch attempt-1. Tours
that fail the gate after batch attempt-1 fall back to the existing synchronous retry loop, which
still adds feedback exactly as before.
"""
from __future__ import annotations

import hashlib
from typing import TypedDict

from .prompts import SYSTEM_PROMPT, build_rewrite_prompt, build_t2_base_prompt, t2_dashless


class S1PromptInputs(TypedDict, total=False):
    """The subset of graph ContentState fields the writer prompt is built from — all available
    before any LLM call (tour data + resolved brand rule + SEO), none depending on graph output."""
    tour: dict
    seo: dict
    few_shots: list
    subtitle_focus: str
    rewrite_language: str
    brand_system_prompt: str
    brand_style_guide: str
    brand_forbidden_words: list
    brand_core_idea: str
    brand_customer_segment: str
    brand_customer_mindset: str
    brand_voice_examples: list
    brand_good_examples: str
    is_tenant_rewrite: bool   # AA-792: True → the T2 tenant-voice builders below
    brand_name: str           # AA-792: from the AA-771 brand pack (T2 only)
    brand_type: str
    brand_default_cta: str
    brand_signature_words: list  # AA-800: custom_validators.signature_words (T2 only)


def build_brand_diff_block(state: dict) -> str:
    """AA-202 brand-differentiation system block. Copied verbatim from graph._build_brand_diff_block
    so materialized batch prompts are byte-identical to the synchronous path. graph._build_brand_diff_block
    now delegates here (single source of truth)."""
    core_idea    = state.get("brand_core_idea", "") or ""
    cust_segment = state.get("brand_customer_segment", "") or ""
    cust_mindset = state.get("brand_customer_mindset", "") or ""
    voice_ex     = [v for v in (state.get("brand_voice_examples") or []) if v]
    good_ex      = state.get("brand_good_examples", "") or ""

    if not (core_idea or cust_mindset or voice_ex):
        return ""

    diff_block = "\n\nBRAND DIFFERENTIATION PROFILE (this client's distinct angle — the rewrite MUST reflect it):"
    if core_idea:
        diff_block += f"\n- Core idea: {core_idea}"
    if cust_segment:
        diff_block += f"\n- Who this is for: {cust_segment}"
    if cust_mindset:
        diff_block += f"\n- What this traveller wants: {cust_mindset}"
    if voice_ex:
        diff_block += f"\n- Voice (tone words): {', '.join(voice_ex)}"
    if good_ex:
        diff_block += f"\n- Example of this brand's voice on a single moment: {good_ex}"
    return diff_block + _brand_contrast_rules()


def _brand_contrast_rules() -> str:
    """AA-202 contrast + generic-phrasing rules (the tail of build_brand_diff_block). AA-792: split
    out so the T2 prompt can keep them after the CLIENT BRAND block without repeating the profile."""
    return (
        "\n\nCONTRAST REQUIREMENT: The summary, highlights, itineraries (including each day-title), "
        "and the overall framing MUST be written from THIS brand's specific angle and mindset above. "
        "Do NOT produce generic copy that would fit any travel brand. If the same tour were rewritten "
        "for a different brand, the wording, emphasis, and framing must be clearly distinct — not a "
        "synonym swap. Lead with what makes THIS brand's take different."
        "\n\nGENERIC PHRASING TO AVOID (these read identically for any brand — do NOT write like this): "
        "'connects the country's primary cultural regions', 'moves through layered geography', "
        "'a journey through diverse landscapes', 'experience the best of' — they describe a route, not "
        "THIS brand's mission. Instead, lead every field with THIS brand's specific mission angle and "
        "let it shape what you foreground; do not merely synonym-swap a generic description."
    )


def brand_pack_from_state(state: dict) -> dict:
    """AA-792: rebuild the AA-771 brand pack (services/acp_shared/brand_pack.BrandPack) from the
    graph state's brand_* fields, which t2_rewrite_job loads with brand_pack.fetch_brand_pack_conn —
    so the T2 writer renders the brand with the same render_brand_block as T8/T9."""
    return {
        "brand_name": state.get("brand_name") or None,
        "brand_type": state.get("brand_type") or None,
        "core_idea": state.get("brand_core_idea") or None,
        "customer_segment": state.get("brand_customer_segment") or None,
        "customer_mindset": state.get("brand_customer_mindset") or None,
        "voice": [v for v in (state.get("brand_voice_examples") or []) if v],
        "style_guide": state.get("brand_style_guide") or None,
        "forbidden_words": [w for w in (state.get("brand_forbidden_words") or []) if w],
        "good_examples": state.get("brand_good_examples") or None,
        "default_cta": state.get("brand_default_cta") or None,
        "system_prompt": state.get("brand_system_prompt") or None,
        "signature_words": [w for w in (state.get("brand_signature_words") or []) if w],  # AA-800
    }


def build_t2_system_prompt(state: dict) -> str:
    """AA-792: the T2 tenant-rewrite writer SYSTEM prompt — written AS the client brand.

    T2_SYSTEM_PROMPT (brand-voice base, same truth/structure rules as A1) + language + the CLIENT
    BRAND block (render_brand_block of the AA-771 pack: name, type, core idea, audience, voice,
    style guide, forbidden words, on-brand example) + the brand's own system_prompt as brief, when
    one was written + the AA-202 contrast rules + the AA core + brand forbidden-words constraint.
    No "append only" and no master-catalogue framing: the brand is the point of T2."""
    from services.acp_shared.brand_pack import render_brand_block
    from .brand_fit import has_brand_signals
    brand_sp = state.get("brand_system_prompt", "") or ""
    pack = brand_pack_from_state(state)
    # AA-800: rule 1 is built per tenant from the word layers (signature words allowed).
    system = build_t2_base_prompt(pack) + _language_line(state)
    system += "\n\n" + render_brand_block(pack, heading="CLIENT BRAND")
    if brand_sp:
        system += f"\n\nCLIENT BRAND BRIEF (the brand's own words, part of the brand above):\n{brand_sp}"
    system += _core_idea_lens_block(pack.get("core_idea"))
    if has_brand_signals(state):
        system += _brand_contrast_rules()
    # AA-919 (WM-18 / WM-20): the writing-memory slot `t2_block` of the release this run resolved; "" for release 0
    # and without memory (byte-identical prompt).
    from services.writing_memory.insertion import t2_block
    # AA-778: the contrast rules and the forbidden-words block are shared with A1 (pinned); T2 reads them
    # without the em-dash.
    return t2_dashless(system + _forbidden_words_block(state) + t2_block(state.get("writing_memory")))


def _core_idea_lens_block(core_idea) -> str:
    """AA-792 (pilot fix): the T2 judge gates on mission_present — does the tenant's core idea shape
    the overview and the itinerary days. The writer was never asked for that (and the CLIENT BRAND
    block's "at most one sentence about the brand itself" kept the brand out of the days), so the
    pilot scored mission absent on 27/27 calls. Same definition on both sides: the core idea is the
    lens for the trip, not brand self-reference. '' when the tenant has no core idea."""
    core_idea = (core_idea or "").strip()
    if not core_idea:
        return ""
    return (
        "\n\nCORE IDEA AS THE LENS FOR THE WHOLE TRIP: this brand's core idea"
        f" (\"{core_idea}\") is the lens for the whole trip. The overview and each itinerary day's"
        " framing (what the day emphasises and why it matters to this brand's travellers) should"
        " reflect it naturally, in the brand's voice, using only facts from the source. This is not"
        " brand self-reference: no slogans, no naming the brand in every day, no quoting the core"
        " idea word for word, and no invented claims or activities to make a day fit it."
    )


def build_s1_system_prompt(state: dict) -> str:
    """Assemble the S1 writer SYSTEM prompt exactly as graph.generate_node's attempt-1 does.

    base SYSTEM_PROMPT + language convention + tenant brand append + AA-202 brand-diff block +
    forbidden words. Pure string assembly, no I/O — reused by both generate_node (sync) and the
    batch manifest builder so they never diverge.

    AA-792: a T2 tenant rewrite (``state["is_tenant_rewrite"]``) gets its own prompt
    (build_t2_system_prompt). The A1 branch below is byte-identical to before (golden test).
    """
    if state.get("is_tenant_rewrite"):
        return build_t2_system_prompt(state)
    brand_sp    = state.get("brand_system_prompt", "") or ""

    system = SYSTEM_PROMPT + _language_line(state)
    if brand_sp:
        system += f"\n\nCLIENT BRAND CONTEXT (append only — do not override the base rules above):\n{brand_sp}"
    system += build_brand_diff_block(state)
    return system + _forbidden_words_block(state)


def _language_line(state: dict) -> str:
    language = state.get("rewrite_language", "en-US") or "en-US"
    if language == "en-GB":
        return (
            "\n\nLANGUAGE: Use British English spelling and conventions "
            "(e.g. 'colour', 'travelling', 'organised')."
        )
    return "\n\nLANGUAGE: Use American English spelling and conventions."


def _forbidden_words_block(state: dict) -> str:
    system = ""
    # S207: the writer is told the SAME list validate_node checks (AA core + brand). It used to see
    # only the brand's words, so core words (curated, refined, tailored, deal…) were written and
    # then fired FORBIDDEN_WORD → a flag_fix repair call for something the prompt never forbade.
    # AA-800: a T2 tenant rewrite lists the words validate refuses for THIS tenant (state_forbidden: platform
    # truth, undeclared positioning words, its own list); A1 is all_forbidden as before (golden test).
    from .forbidden_words import state_forbidden
    fw = state_forbidden(state)
    # AA-738: a bare list was ignored for ordinary words ("explore", "package", "nestled") on both
    # Haiku and Sonnet. State it as a hard constraint covering every field, with plain alternatives
    # for the words actually measured on the S217 Nepal wave.
    if fw:
        system += (
            "\n\nHARD CONSTRAINT — FORBIDDEN WORDS. The output is rejected automatically if any of these"
            " words or phrases (any capitalization, singular or plural) appears in ANY field — name,"
            " subtitle, summary, description, every highlight, every itinerary day, seo_title, seo_meta:\n"
            + ", ".join(fw)
            + "\nUse plain alternatives instead, e.g. 'visit' / 'walk around' / 'wander' for 'explore',"
            " 'set in' for 'nestled in', 'trip' for 'package', 'lively' for 'vibrant'."
            " Before returning the JSON, re-read every field and replace any forbidden word."
        )
    return system


def build_s1_user_prompt(state: dict) -> str:
    """The S1 writer USER prompt for attempt-1 (no retry feedback). style_guide is appended the
    same way generate_node does (onto the user prompt, not the system prompt).

    AA-792: a T2 tenant rewrite gets the tenant-voice variant (voice="tenant") and no appended
    style guide — it is already in the system prompt's CLIENT BRAND block."""
    from .prompts import s1_per_day_targets_enabled
    from .t2_facts import FACTS_KEY
    is_t2 = bool(state.get("is_tenant_rewrite"))
    prompt = build_rewrite_prompt(
        state["tour"],
        state.get("seo", {}),
        state.get("few_shots", []),
        subtitle_focus=state.get("subtitle_focus", "standard"),
        # AA-747: OFF by default; a tenant flag wins over the S1_PER_DAY_TARGETS env var.
        per_day_targets=s1_per_day_targets_enabled(state.get("tenant_flags")),
        # AA-748: per-day source facts, threaded in behind S1_STRUCTURED_FACTS by generate_node.
        # None on the Bedrock Batch path (no live extraction) -> prompt as today, byte-identical.
        structured_facts=state.get("structured_facts"),
        voice="tenant" if is_t2 else "neutral",
        # AA-814 follow-up (T2_FACTS): the Facts Entry lines the t2_rewrite job put on the T2 source; absent with the
        # switch off and on every A1 tour -> no FACTS block, prompt byte-identical.
        facts=(state["tour"].get(FACTS_KEY) or "") if is_t2 else "",
    )
    style_guide = state.get("brand_style_guide", "") or ""
    if style_guide and not is_t2:
        prompt += f"\n\nSTYLE GUIDE FOR THIS CLIENT:\n{style_guide}"
    return prompt


def prompt_version_of(system: str) -> str:
    """AA-289: 8-char sha256 of the exact system prompt string — same key generate_node records in
    generated_content.metadata.prompt_version, so a batch-written version is traceable identically."""
    return hashlib.sha256(system.encode("utf-8")).hexdigest()[:8]


def materialize_s1_prompt(state: dict) -> tuple[str, str, str]:
    """Return (system, user, prompt_version) for one tour's attempt-1 batch record."""
    system = build_s1_system_prompt(state)
    user = build_s1_user_prompt(state)
    return system, user, prompt_version_of(system)
