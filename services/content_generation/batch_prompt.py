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

from .prompts import SYSTEM_PROMPT, build_rewrite_prompt


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
    diff_block += (
        "\n\nCONTRAST REQUIREMENT: The summary, highlights, itineraries (including each day-title), "
        "and the overall framing MUST be written from THIS brand's specific angle and mindset above. "
        "Do NOT produce generic copy that would fit any travel brand. If the same tour were rewritten "
        "for a different brand, the wording, emphasis, and framing must be clearly distinct — not a "
        "synonym swap. Lead with what makes THIS brand's take different."
    )
    diff_block += (
        "\n\nGENERIC PHRASING TO AVOID (these read identically for any brand — do NOT write like this): "
        "'connects the country's primary cultural regions', 'moves through layered geography', "
        "'a journey through diverse landscapes', 'experience the best of' — they describe a route, not "
        "THIS brand's mission. Instead, lead every field with THIS brand's specific mission angle and "
        "let it shape what you foreground; do not merely synonym-swap a generic description."
    )
    return diff_block


def build_s1_system_prompt(state: dict) -> str:
    """Assemble the S1 writer SYSTEM prompt exactly as graph.generate_node's attempt-1 does.

    base SYSTEM_PROMPT + language convention + tenant brand append + AA-202 brand-diff block +
    forbidden words. Pure string assembly, no I/O — reused by both generate_node (sync) and the
    batch manifest builder so they never diverge.
    """
    brand_sp    = state.get("brand_system_prompt", "") or ""
    language    = state.get("rewrite_language", "en-US") or "en-US"

    system = SYSTEM_PROMPT
    if language == "en-GB":
        system += (
            "\n\nLANGUAGE: Use British English spelling and conventions "
            "(e.g. 'colour', 'travelling', 'organised')."
        )
    else:
        system += "\n\nLANGUAGE: Use American English spelling and conventions."
    if brand_sp:
        system += f"\n\nCLIENT BRAND CONTEXT (append only — do not override the base rules above):\n{brand_sp}"
    system += build_brand_diff_block(state)
    fw = [w for w in (state.get("brand_forbidden_words") or []) if w]
    if fw:
        system += "\n\nFORBIDDEN WORDS (never use): " + ", ".join(fw)
    return system


def build_s1_user_prompt(state: dict) -> str:
    """The S1 writer USER prompt for attempt-1 (no retry feedback). style_guide is appended the
    same way generate_node does (onto the user prompt, not the system prompt)."""
    prompt = build_rewrite_prompt(
        state["tour"],
        state.get("seo", {}),
        state.get("few_shots", []),
        subtitle_focus=state.get("subtitle_focus", "standard"),
    )
    style_guide = state.get("brand_style_guide", "") or ""
    if style_guide:
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
