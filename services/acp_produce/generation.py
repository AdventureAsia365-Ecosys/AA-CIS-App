"""
services.acp_produce.generation — N7 E1 (Outline) + E2 (Draft), AA-370.

STEP 0 (Linear AA-370 comment, 06/08/2026) confirmed the design executed here:
- E1 (`build_outline`) is fully deterministic — no LLM. `Brief.required_h2s`
  + `Brief.atoms_by_section` (both already built by AA-369's
  `research.py::compile_brief()`) are sufficient; the per-section "goal" is a
  template string, never generated text.
- E2 (`generate_draft`) is the one real LLM writer call in N7 Produce. Model
  is Bedrock satellite acc1 Sonnet (`shared/llm_client/bedrock_satellite.py`,
  `model="sonnet"`) — CHỐT, not a proposal: AA-334 (bake-off issue) was
  Cancelled 06/08/2026 with an explicit sign-off ("Nghiep xác nhận trực
  tiếp: Palmyra X5 không dùng được ... chuyển thẳng sang Bedrock satellite
  acc1 Sonnet, không qua bake-off"). Palmyra must never appear anywhere in
  this module.
- Sections are drafted in batches of 2-3 (never one call for the whole
  piece) — the same lesson AA-353/AA-357 already applied to S1's itinerary
  generation ("tránh lặp lại lỗi kiến trúc compression"). H2 headings are
  inserted by CODE from the outline, never written by the model — mirrors
  AA-353's "day-title formatting no longer trusted to the model".
- Every factual claim gets a `[R:atom_id]` tag. `[F:fact_id]` is
  deliberately NOT offered to the model — `Brief.facts_ids` is always empty
  (no Facts pack exists anywhere in this repo, AA-369 scope note) so the tag
  would have nothing real to reference.

This module used to hardcode `services.content_generation.brand_standards
.AA_BRAND_IDENTITY_PROMPT` (S1's generic brand rubric) into every draft call.
AA-404's F9 deep-dive (`docs/implementation-notes/AA-404-F9-deep-dive.md`,
TL;DR #1) found that gap: F9's judge (PR #158) had already moved on to a real
per-tenant rubric (`services.acp_produce.brand.fetch_brand_rubric_text()`)
while every writer module, this one included, kept aiming at the old, vaguer
generic target. `generate_draft()` now takes `brand_rubric_text` as a
parameter — `AA_BRAND_IDENTITY_PROMPT` is still imported here, but only as
that parameter's default (the fallback value `fetch_brand_rubric_text()`
itself already returns for a tenant with no populated brand row), not as a
hardcoded constant baked into the module.

`generation.py` is the name every existing forward-reference in this package
already anticipates (`judge_client.py`, `pipeline.py`, `reliability.py`,
`sweeper_lambda.py`, `packets.py`, `rule_adapter.py` docstrings all say
"E1-E5 generation" / name this file directly) — not a new naming choice.

Wiring into the rest of N7 Produce (AA-364): `generate_draft()` returns a
plain `str` — the caller assigns it straight to `Piece(piece_id=...,
body_tagged=that_str)` and passes the `Piece` to
`pipeline.py::run_piece_through_produce_gates()` exactly like every existing
test already hand-constructs. No change needed anywhere else in the package.
`atom_text_by_id` (the same `dict[str, str]` this module takes as input) is
also exactly the shape `run_piece_through_produce_gates(text_by_id=...)`
wants, and `set(atom_text_by_id)` is its `valid_ids` — confirmed compatible
with `gates.py::gate_grounding()`'s `TAG_RE` in AA-370 STEP 0, no format gap.
"""
from __future__ import annotations

import re
import time
from typing import Optional

import structlog

from services.acp_produce.models import Brief, OutlineSection
from services.content_generation.brand_standards import AA_BRAND_IDENTITY_PROMPT
from shared.llm_client.bedrock_satellite import BedrockInvokeResult, BedrockUnavailable, invoke_claude
from shared.llm_client.role_config import get_stage_config_sync
from shared.llm_client.call_log import record_call_sync
from shared.llm_client.pricing import calc_cost

logger = structlog.get_logger()

_TARGET_BATCH_SIZE = 3  # sections/call — "batch 2-3 section/call", never 1-call-whole-piece
_MAX_INVOKE_ATTEMPTS = 3  # 1 original + 2 retries, per AA-370 IMPLEMENT scope
_RETRY_BACKOFF_SECONDS = 2.0
_MIN_MAX_TOKENS = 512
_MAX_MAX_TOKENS = 4096  # same ceiling as the JSON-truncation fix elsewhere in this repo

_SECTION_MARKER_RE = re.compile(r"===SECTION:.*?===\s*\n", re.MULTILINE)

# AA-404 Part 1: F3_structural_variance (gates.py::gate_structural_variance())
# needs (a) >=1 genuinely one-sentence paragraph SOMEWHERE in the piece and
# (b) one section running >=1.4x longer than the second-longest, whenever the
# piece has >=3 H2 sections. STEP 0 (docs/implementation-notes/AA-404.md §2)
# confirmed research.py's _VARIANCE_DIRECTIVES already says both of these in
# plain English, on every batch call -- and still 7/33 real pieces fail here,
# because sections are drafted in independent 2-3-section batches that never
# see each other's output. A piece-wide directive repeated identically to
# every batch has no owner: each batch can assume another batch will handle
# it, and (per the real data) none reliably does. Fix: give each directive to
# exactly ONE section, deterministically, so exactly one batch call ever
# receives it.
_LONG_SECTION_MULTIPLIER = 1.7  # gate needs >=1.4x; margin for real writer variance
_MIN_SECTION_WORDS = 60

_ONE_SENTENCE_PARAGRAPH_NOTE = (
    "\nRHYTHM DIRECTIVE (this section specifically -- AA-404): include, somewhere in this "
    "section's prose, exactly one standalone paragraph that is a single short sentence (a "
    "deliberate rhythm break). This is the ONLY section in the piece assigned this requirement -- "
    "every other paragraph, here and elsewhere in the piece, should be normal multi-sentence prose."
)
_LONG_SECTION_NOTE = (
    "\nLENGTH DIRECTIVE (this section specifically -- AA-404): this is the ONE section in the "
    "piece assigned to run notably longer than the others -- develop it in real depth, do not "
    "compress it to match a typical section's length."
)

# AA-404 Part 3: F8_framework's AIDA rubric ("attention hook first" / "single
# clear action (CTA)") failed in real week=3 data (docs/implementation-notes/
# AA-404.md §1) because this writer prompt only ever passed the bare
# framework NAME ("AIDA") with zero explanation of what it requires -- a
# total gap, not a weak one. hook_story_cta (facebook, adapt.py) already
# proves the fix shape: its "first line is the hook" / single-CTA criteria
# pass cleanly in all 33 real pieces because adapt.py spells out exactly
# where the hook/CTA must sit, in positional terms. Mirrored here for AIDA's
# two failing criteria specifically -- not copied verbatim (adapt.py's
# facebook piece is one single short paragraph with one obvious first/last
# sentence; a blog piece is drafted across several independently-batched H2
# sections, so the hook/CTA requirement has to be pinned to whichever section
# is actually first/last in the OUTLINE, not to "the first/last sentence of
# this batch's response").
#
# hub/PAS originally had the same bare-label gap, deliberately left untouched
# at the time ("no real failure yet... flagged as a follow-up," this exact
# comment, pre-AA-404-N0-N8-audit) -- that call is revisited here per the
# N0-N8 defense-layer audit (docs/claude_audit/AA-404-N0-N8-defense-layer-
# audit.md, Q1's #2 finding), on real data the original deferral didn't have:
# F8's overall repair success rate is 14.6% (41 rounds, 6 passed) -- third
# worst of every gate in the stack, and the two gates below it (F3 2.8%, F9
# 2.5%) are both whole-piece/multi-criterion gates the same way F8 is, not a
# coincidence. hub/PAS pieces are a real fraction of that 41-round sample
# (framework mix isn't logged per-round, so an exact hub/PAS-only rate isn't
# available from existing data -- flagged as a real gap, not glossed over),
# but the *mechanism* (bare label, LLM guessing what an unexplained framework
# name requires) is identical to AIDA's already-confirmed-and-fixed case.
# Closing it now rather than waiting for a hub/PAS-specific failure to
# accumulate first.
_AIDA_FRAMEWORK_GUIDANCE = (
    "AIDA FRAMEWORK (Attention-Interest-Desire-Action): the piece as a whole must move through all "
    "four beats in order -- open on Attention, build Interest with specifics, build Desire on "
    "concrete sensory moments (not abstract claims), and close on Action. Whichever section(s) "
    "you're drafting, write the right beat for their position in that arc."
)
_AIDA_OPENING_HOOK_NOTE = (
    "\nATTENTION-HOOK REQUIREMENT (this is the piece's OPENING section): the very first sentence "
    "you write must be a genuine attention hook -- a specific, concrete, surprising detail or a real "
    "question that earns the next sentence. Never open with a generic scene-setting line."
)
_AIDA_SINGLE_CTA_NOTE = (
    "\nSINGLE-CTA REQUIREMENT (this is the piece's CLOSING section): end on exactly ONE clear call "
    "to action. Do not offer more than one distinct ask (e.g. do not both invite booking AND suggest "
    "reading another article, or CTA both mid-section and again at the very end) -- one unambiguous "
    "action, once."
)

# AA-404 N0-N8 audit follow-up: hub's F8 rubric (gates.py::FRAMEWORK_RUBRICS["hub"]) is
# "covers the topic comprehensively via subsections" + "each section answers a distinct
# sub-question" -- both whole-piece/every-section properties (unlike AIDA's positional
# opening/closing beats), so one general per-batch guidance line is the right shape here, not
# a per-section positional note the way AIDA/PAS below get one.
_HUB_FRAMEWORK_GUIDANCE = (
    "HUB FRAMEWORK (comprehensive topic coverage): the piece as a whole must cover the topic "
    "comprehensively across its sections, and EACH section must answer a genuinely distinct "
    "sub-question -- no two sections may cover overlapping ground. Whichever section(s) you're "
    "drafting, make sure the sub-question it answers is one the OTHER sections don't already cover."
)

# hub/PAS's own equivalent of the AIDA fix above, same reasoning (F8's rubric is real,
# positional for PAS: "opens with the reader's problem" / "agitates concretely" / "resolves
# with the trip as solve" -- a 3-beat arc, same class of gap as AIDA's 4-beat one, same fix
# shape: one opening-section note + one closing-section note, pinned to whichever OUTLINE
# section is actually first/last, not "the first/last sentence of this batch's response").
_PAS_FRAMEWORK_GUIDANCE = (
    "PAS FRAMEWORK (Problem-Agitate-Solve): the piece as a whole must open with the reader's real "
    "problem, agitate it with concrete specifics (not vague hand-wringing), and resolve it with the "
    "trip itself as the solve. Whichever section(s) you're drafting, write the right beat for their "
    "position in that arc."
)
_PAS_OPENING_PROBLEM_NOTE = (
    "\nPROBLEM-OPENING REQUIREMENT (this is the piece's OPENING section): open with the reader's "
    "real problem or unmet want, stated concretely -- not a generic scene-setting line, and not the "
    "trip's solution yet. Earn the agitation that follows."
)
_PAS_CLOSING_RESOLVE_NOTE = (
    "\nRESOLVE REQUIREMENT (this is the piece's CLOSING section): resolve the problem raised at the "
    "opening with the trip itself as the solve -- make the connection between problem and trip "
    "explicit, don't just end on a generic summary or CTA with no callback to what opened the piece."
)

_FRAMEWORK_GUIDANCE: dict[str, str] = {
    "hub": _HUB_FRAMEWORK_GUIDANCE,
    "PAS": _PAS_FRAMEWORK_GUIDANCE,
    "AIDA": _AIDA_FRAMEWORK_GUIDANCE,
}

# AA-404 writer-side wire (F9 deep-dive TL;DR #1): this used to be a
# module-level constant built once at import time from the generic
# `AA_BRAND_IDENTITY_PROMPT` — E2 was one of the 4 writer modules still doing
# this while F9's judge (PR #158) had already moved on to a real per-tenant
# rubric (`brand.py::fetch_brand_rubric_text()`). Now built per-call from
# `generate_draft()`'s own `brand_rubric_text` parameter, which defaults to
# `AA_BRAND_IDENTITY_PROMPT` — every pre-AA-404 caller/test that doesn't pass
# it keeps drafting against exactly the prompt it always has.
_DRAFT_SYSTEM_PROMPT_RULES = (
    "\n\nADDITIONAL RULES FOR THIS DRAFT TASK:\n"
    "- You will be given one or more sections to write. For EACH section, output a line\n"
    "  \"===SECTION:<title>===\" followed by that section's body prose — nothing else on\n"
    "  that line, and output the sections in the exact order given.\n"
    "- Do NOT write the section heading/H2 as prose — the marker line above is enough,\n"
    "  the reader-facing heading is inserted separately by code from the outline.\n"
    "- Every factual claim MUST carry a [R:atom_id] tag citing the exact atom id supplied\n"
    "  for that section. Never invent an atom id. Never state a fact with no cited atom.\n"
    "- Do NOT use [F:...] tags anywhere — no Facts pack exists for this brief.\n"
    "- If a section lists no atoms, write no factual claims in it — keep it brief and\n"
    "  transitional (e.g. a bridge into the next section), not padded with invented detail."
)


def _build_draft_system_prompt(brand_rubric_text: str) -> str:
    return (
        "You are the Adventure Asia content writer for N7 (blog/social) pieces.\n\n"
        + brand_rubric_text.strip() + _DRAFT_SYSTEM_PROMPT_RULES
    )


class DraftGenerationFailed(Exception):
    """E2 could not produce a real draft for one batch — Sonnet invoke kept
    failing after retries, or its response didn't parse into one body per
    requested section. Raised with a concrete reason (never a silently empty
    or fabricated section) — caller (the not-yet-built slot runner) is
    expected to log this to unknown_ledger and skip the slot, same "hold
    visible, never silent" spirit as gates.py's F1-F9 (L6)."""


# ---------------------------------------------------------------- E1 outline (deterministic, no LLM)

def build_outline(brief: Brief) -> list[OutlineSection]:
    """E1. One OutlineSection per `brief.required_h2s` entry, atom_ids read
    straight off `brief.atoms_by_section`. Calls no LLM — verified by this
    function not importing/calling `invoke_claude` at all.

    Skips the literal "FAQ" title (research.py::compile_brief() appends it
    when `fw_cfg.get("faq")`) — AA-371 (E4, faq.py) owns that section
    entirely now, answering `Brief.faq_candidates` with real grounded
    answers instead of E2 drafting a generic throwaway paragraph off
    whatever atoms `atoms_by_section["FAQ"]` happened to collect (AA-370's
    original behavior, before E4 existed to do this properly). `required_h2s`
    always appends "FAQ" last (research.py line ~282), so E4 appending its
    rendered FAQ block to the end of `body_tagged` reproduces the same
    position this section would have occupied."""
    sections: list[OutlineSection] = []
    for title in brief.required_h2s:
        if title == "FAQ":
            continue
        atom_ids = brief.atoms_by_section.get(title, [])
        sections.append(OutlineSection(title=title, atom_ids=atom_ids, goal=_section_goal(title, atom_ids)))
    return sections


def _section_goal(title: str, atom_ids: list[str]) -> str:
    n = len(atom_ids)
    if n == 0:
        return f"No atoms assigned to '{title}' — keep brief/transitional, no factual claims."
    return f"Cover {n} supporting fact{'s' if n != 1 else ''} for '{title}'."


# ---------------------------------------------------------------- E2 draft (Sonnet, batched)

def generate_draft(
    brief: Brief, outline: list[OutlineSection], atom_text_by_id: dict[str, str],
    brand_rubric_text: str = AA_BRAND_IDENTITY_PROMPT,
) -> str:
    """E2. Drafts `outline` in batches of 2-3 sections/Sonnet call, inserts
    H2 headings from the outline (code, never the model), and joins the
    result into one `body_tagged` string in outline order. Raises
    `DraftGenerationFailed` rather than ever emitting an empty/fabricated
    section.

    AA-404 Part 1/3: `long_title`/`short_para_title` give the F3 variance
    directives a single deterministic owner section each (see module-level
    comment above `_LONG_SECTION_MULTIPLIER`); `extra_directives` folds that
    together with AIDA's opening-hook/closing-CTA notes (Part 3) into one
    per-section-title -> [extra prompt lines] map, computed once per piece
    (not per batch) so every batch call sees a consistent, non-duplicated
    plan.

    `brand_rubric_text` (AA-404 writer-side wire): the real per-tenant rubric
    F9's judge is scored against (`brand.py::fetch_brand_rubric_text()`),
    threaded down by `slot_runner.py`'s one per-slot fetch — defaults to the
    generic `AA_BRAND_IDENTITY_PROMPT` for any caller/test that doesn't have
    one (unchanged pre-AA-404 behavior)."""
    if not outline:
        raise ValueError("generate_draft() requires a non-empty outline — run build_outline() first")

    long_title, short_para_title = _select_variance_owners(outline)
    words_per_section = _compute_words_per_section(brief, outline, long_title)
    extra_directives = _build_extra_section_directives(brief, outline, long_title, short_para_title)
    system_prompt = _build_draft_system_prompt(brand_rubric_text)

    section_bodies: dict[str, str] = {}
    for batch in _batch_sections(outline):
        prompt = _build_batch_prompt(brief, batch, atom_text_by_id, words_per_section, extra_directives)
        batch_words = sum(words_per_section.get(s.title, 0) for s in batch)
        max_tokens = min(max(int(batch_words * 1.6) + 150, _MIN_MAX_TOKENS), _MAX_MAX_TOKENS)

        result = _invoke_sonnet_with_retry(prompt, max_tokens, system_prompt)
        logger.info(
            "e2_draft_batch_success", model_used=result.model_used,
            sections=[s.title for s in batch], latency_ms=result.latency_ms, usage=result.usage,
        )

        parsed = _parse_batch_response(result.text, batch)
        # AA-505 — cost priced off the REQUESTED tier ("sonnet"/"haiku", matches pricing.py's
        # short-key table), not result.model_used ("sonnet-4-6" — a real audit label but not a
        # pricing key calc_cost() recognizes, which would silently mis-price a Haiku call at
        # Sonnet's fallback rate).
        _cfg = get_stage_config_sync("n7_draft")
        record_call_sync(
            stage="n7_draft", role="writer", model=result.model_used,
            tokens_in=result.usage.get("input_tokens"), tokens_out=result.usage.get("output_tokens"),
            cost_usd=calc_cost(_cfg.model_id, result.usage.get("input_tokens", 0),
                                result.usage.get("output_tokens", 0)),
            tenant_id=None,  # N7's own writer functions don't have tenant_id in scope (module-level)
            quality_signal={"sections_parsed": len(parsed), "sections_requested": len(batch)},
            stop_reason=result.stop_reason,
            account=_cfg.account_route or "acc3", fallback_used=False, provider="bedrock-satellite",
        )
        if len(parsed) != len(batch):
            raise DraftGenerationFailed(
                f"Sonnet response for batch {[s.title for s in batch]} did not contain "
                f"{len(batch)} parseable ===SECTION:...=== block(s) (got {len(parsed)})"
            )
        section_bodies.update(parsed)

    return "\n\n".join(f"## {s.title}\n\n{section_bodies[s.title]}" for s in outline)


def _batch_sections(sections: list[OutlineSection], target: int = _TARGET_BATCH_SIZE) -> list[list[OutlineSection]]:
    """Balanced chunking so every batch lands in [2,3] whenever the total
    allows it (e.g. 5 sections -> [3,2], not [3,3,-1] or a trailing [1])."""
    n = len(sections)
    if n == 0:
        return []
    num_batches = -(-n // target)  # ceil
    base, extra = divmod(n, num_batches)
    batches, i = [], 0
    for b in range(num_batches):
        size = base + (1 if b < extra else 0)
        batches.append(sections[i:i + size])
        i += size
    return batches


def _select_variance_owners(outline: list[OutlineSection]) -> tuple[str, str]:
    """AA-404 Part 1: picks exactly one section to own the "notably longer"
    directive and exactly one (different, when possible) section to own the
    "include a one-sentence paragraph" directive — deterministic, not random,
    so the same Brief always produces the same assignment.

    `long`: the section with the MOST assigned atoms — the section with the
    most material to develop, so asking it to run longer is a natural fit
    rather than an arbitrary constraint the model has to invent content to
    satisfy. `short_para`: the section with the FEWEST atoms, excluded from
    being the same section as `long` whenever more than one section exists —
    a light/transitional section is a natural place for one short, punchy
    standalone sentence. Ties broken by outline order (Python's `max`/`min`
    keep the first-encountered maximal/minimal item), so the choice is fully
    reproducible for a given outline."""
    if not outline:
        raise ValueError("_select_variance_owners() requires a non-empty outline")
    n = len(outline)
    long_idx = max(range(n), key=lambda i: len(outline[i].atom_ids))
    remaining = [i for i in range(n) if i != long_idx] or [long_idx]
    short_idx = min(remaining, key=lambda i: len(outline[i].atom_ids))
    return outline[long_idx].title, outline[short_idx].title


def _compute_words_per_section(brief: Brief, outline: list[OutlineSection], long_title: str) -> dict[str, int]:
    """AA-404 Part 1: F3's section-length check only fires with >=3 H2
    sections (`gates.py::gate_structural_variance()`) — below that, don't
    distort section sizing for a rule that can't even apply. At >=3
    sections, `long_title` gets `_LONG_SECTION_MULTIPLIER`x the uniform
    baseline and every other section is shrunk to compensate, so the TOTAL
    stays close to the original `words_mid` budget — `F4_brief_compliance`'s
    word-count check (±30%) still has to pass; only the distribution across
    sections changes, not the sum."""
    n = len(outline)
    words_mid = sum(brief.word_range) // 2
    base = max(words_mid // n, 100)
    if n < 3:
        return {s.title: base for s in outline}
    long_target = int(base * _LONG_SECTION_MULTIPLIER)
    extra = long_target - base
    others = [s for s in outline if s.title != long_title]
    reduction_each = extra // len(others) if others else 0
    result: dict[str, int] = {}
    for s in outline:
        if s.title == long_title:
            result[s.title] = long_target
        else:
            result[s.title] = max(base - reduction_each, _MIN_SECTION_WORDS)
    return result


def _build_extra_section_directives(
    brief: Brief, outline: list[OutlineSection], long_title: str, short_para_title: str,
) -> dict[str, list[str]]:
    """AA-404 Part 1 + Part 3: one map, title -> extra prompt lines for that
    section, computed once per piece from the full outline (not per batch —
    every batch call sees the same, already-decided plan)."""
    directives: dict[str, list[str]] = {s.title: [] for s in outline}
    if len(outline) >= 3:
        directives[long_title].append(_LONG_SECTION_NOTE)
    directives[short_para_title].append(_ONE_SENTENCE_PARAGRAPH_NOTE)
    if brief.framework == "AIDA" and outline:
        directives[outline[0].title].append(_AIDA_OPENING_HOOK_NOTE)
        directives[outline[-1].title].append(_AIDA_SINGLE_CTA_NOTE)
    if brief.framework == "PAS" and outline:
        directives[outline[0].title].append(_PAS_OPENING_PROBLEM_NOTE)
        directives[outline[-1].title].append(_PAS_CLOSING_RESOLVE_NOTE)
    return directives


def _build_batch_prompt(
    brief: Brief, batch: list[OutlineSection], atom_text_by_id: dict[str, str],
    words_per_section: dict[str, int], extra_directives: dict[str, list[str]],
) -> str:
    lines = [
        f"KEYWORD: {brief.keyword}",
        f"FRAMEWORK: {brief.framework}",
    ]
    guidance = _FRAMEWORK_GUIDANCE.get(brief.framework)
    if guidance:
        lines.append(guidance)
    lines += [
        f"CTA TARGET: {brief.cta_target}",
        "VARIANCE DIRECTIVES (apply across the whole piece): " + "; ".join(brief.variance_directives),
        "",
        "Write the body prose for these sections, in order:",
    ]
    for s in batch:
        lines.append(f"\nSECTION: {s.title}")
        lines.append(f"GOAL: {s.goal}")
        lines.append(f"TARGET LENGTH: approximately {words_per_section.get(s.title, 0)} words.")
        lines.extend(extra_directives.get(s.title, []))
        if s.atom_ids:
            lines.append("ATOMS (cite each factual claim with [R:atom_id]):")
            lines += [f"- {aid}: {atom_text_by_id.get(aid, '')}" for aid in s.atom_ids]
        else:
            lines.append("ATOMS: (none assigned — no factual claims in this section)")
    return "\n".join(lines)


def _invoke_sonnet_with_retry(prompt: str, max_tokens: int, system: str) -> BedrockInvokeResult:
    # AA-518 — "n7_draft" stage config (seeded sonnet/acc3, matching the prior hardcoded literals).
    cfg = get_stage_config_sync("n7_draft")
    last_err: Optional[BedrockUnavailable] = None
    for attempt in range(1, _MAX_INVOKE_ATTEMPTS + 1):
        try:
            return invoke_claude(
                prompt, model=cfg.model_id, max_tokens=max_tokens, system=system,
                account=cfg.account_route or "acc3",
            )
        except BedrockUnavailable as e:
            last_err = e
            logger.warning("e2_draft_sonnet_retry", attempt=attempt, max_attempts=_MAX_INVOKE_ATTEMPTS, error=str(e))
            if attempt < _MAX_INVOKE_ATTEMPTS:
                time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
    raise DraftGenerationFailed(
        f"Sonnet invoke failed after {_MAX_INVOKE_ATTEMPTS} attempts: {last_err}"
    ) from last_err


def _parse_batch_response(raw_text: str, batch: list[OutlineSection]) -> dict[str, str]:
    """Splits on `===SECTION:...===` markers and maps chunk i -> batch[i].title
    POSITIONALLY (not by matching the marker's own title text) — same
    defensive-fallback shape as AA-353's day-number mismatch handling
    ("positional fallback ... simple and good enough"). Returns {} (a
    parse failure, handled by the caller) if the marker count doesn't match
    the requested section count — never guesses a partial mapping."""
    markers = list(_SECTION_MARKER_RE.finditer(raw_text))
    if len(markers) != len(batch):
        return {}
    bodies = {}
    for i, m in enumerate(markers):
        end = markers[i + 1].start() if i + 1 < len(markers) else len(raw_text)
        bodies[batch[i].title] = raw_text[m.end():end].strip()
    return bodies


__all__ = ["build_outline", "generate_draft", "DraftGenerationFailed"]
