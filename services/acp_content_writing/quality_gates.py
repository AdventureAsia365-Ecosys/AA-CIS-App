"""
services.acp_content_writing.quality_gates — T10, inline in the same request as T9 (Nghiep's
confirmed architecture, docs/claude_audit/AA-450-01-t9-t10-retry-loop-investigation.md). Full
gate-by-gate mapping/reasoning: docs/claude_audit/AA-450-02-t10-gate-map.md,
docs/claude_audit/AA-452-t10-nine-gates.md.

6 gates for every channel (F6/F1/F2/F4/F8/F9-adjusted), mirroring services/acp_produce/gates.py's
own `GateResult`/one-function-per-gate/`gate_fns` list shape (kept for the same reason N7 has it
— "dễ đọc/debug/mở rộng sau này", per the AA-450 build task's own instruction) WITHOUT importing
anything from that module — every gate here is written fresh against T9's short single-channel
content, referenced not reused (ADR §0.5).

AA-452: `channel == 'blog'` gets 3 MORE gates (F5_atom_density/F3_structural_variance/
F7_faq_dedup, 9 total) — `channel_style.py`'s own `blog` entry describes N7-shaped structure
(H2 sections, optional FAQ) that `prompts.py` now actually instructs the writer to produce in
markup terms for that one channel. F5 needs `[R:{atom_id}]` citation tags the writer is told to
emit ONLY for blog — those tags are internal provenance markup, never shown to the tenant:
`strip_citation_tags()`/`deep_strip_citation_tags()` (below) remove every tag from `content_text`
and from every gate_ledger/repair_log violation string before `service.write_and_check()` ever
persists or returns a piece (all 8 channels — the other 7 never produce a tag to strip, so this
is a no-cost safety net for them, not dead code). All 3 new gates are pure DET (rule-based, ported
from `acp_produce/gates.py`'s own F3/F5/F7 — no LLM call, confirmed against that module's own
"(DET)" labels), so they need no new `asyncio.to_thread()` wrapping of their own.

Each gate is a plain SYNCHRONOUS function — same "wrap at the async/sync boundary" decision as
generate.py (see that module's docstring). `run_quality_gates()` (the one function service.py
calls) is also synchronous for the same reason; service.py wraps the WHOLE call in
`asyncio.to_thread()`, not each gate individually, since every gate for one attempt always runs
together on the same request.

AA-514: 2 more gates, ported from `src/aa_social/gates/__init__.py`'s GATES tuple (11 origin
gates total, read verbatim — docs/claude_audit/AA-514-step0-investigation.md):
  - `gate_promises_an_option()` — NOT F-numbered (genuinely new, no N7/T10 analog to extend).
    Runs for EVERY channel (origin's own `channels=None`), never auto-fixed (ADR 0023 flag-not-
    block — the same reasoning F8/F9's judge-scored fields already aren't auto-repaired, just
    disclosed as a violation for a human to read).
  - `gate_seo_surface()` — a sibling of F4_extreme_length (grouped under the "F4 family" in the
    ledger, kept as its own function for clarity rather than folding 2 concerns into 1), blog-only,
    `repairable=True` — participates in the SAME uniform attempt-2 rewrite loop F2_banned_patterns
    already does (STEP0 §3 corrected a stale premise: T10 has no distinct "3-rounds-per-gate-type"
    mechanism the way the origin's own `repair()` does; MAX_ATTEMPTS=2 is a flat, gate-agnostic
    cap already applied uniformly here — "join the existing fixable treatment", not build a new
    one). Needs `content_piece.seo_title`/`meta_description`/`slug` (migration 136, AA-514) — a
    real architecture fork Nghiệp confirmed directly (full port, not a heuristic substitute).
"""
from __future__ import annotations

import json
import re
from typing import Optional, TypedDict

import structlog

from services.acp_content_writing.framework_rubrics import get_framework_rubric
from services.acp_content_writing.hygiene import (HygieneContext, contrast_excess, find_issues, sentence_spans,
                                                  sibling_templates)
from services.acp_shared.channel_spec import (B2B_RE, EDITORIAL, GENERIC_OPENER_RE, HASHTAG_RE, STOCK_PATTERNS,
                                              ChannelSpec, channel_spec, first_sentence, opener_overlap,
                                              post_words)
from services.acp_produce.judge_client import invoke_judge, parse_judge_json
from services.acp_shared.grounding import find_novel_numeric_claims
from shared.llm_client.call_log import record_call_sync

logger = structlog.get_logger()

_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'‘’“”])")

# AA-452 — blog-only citation tag, same shape as acp_produce/gates.py::TAG_RE (referenced, not
# imported — ADR §0.5). T9 has exactly one atom per piece for 7 of 8 channels and every non-Route
# Segment pick (AA-513 STEP0 corrected this comment — a Route/Blog pick can carry several
# Segments' own distinct ids, see services/acp_content_writing/service.py::
# _fetch_route_segments()) — but there is still no closed-world/multi-id CHECK made against this
# pattern (unlike N7): its only job here is "does this stretch of text have >=1 citation at all,
# of ANY id" (gate_atom_density(), F5) — this generic capture group already matches any id,
# whether there's one or several in the piece, with no code change needed for AA-513.
TAG_RE = re.compile(r"\[(?:R|F):([^\]]+)\]")

# Same window constant as acp_produce/gates.py::ATOM_DENSITY_WORDS — CONTEXT.md §1.6.1's "every
# 200-300 words" resolved to the upper bound, ported verbatim, not re-decided.
ATOM_DENSITY_WORDS = 300

# AA-514 — ported verbatim from the origin's `reference/offered-phrases.toml` (not re-authored;
# see docs/claude_audit/AA-514-step0-investigation.md §5). Said of something the traveller has
# not bought (at_a_price) or something included but not compulsory (offered) — both want the same
# hedge in the prose, so gate_promises_an_option() treats them as one list, same as the origin.
_OFFERED_PHRASES = frozenset({
    "additional cost", "at own expense", "at your own expense", "extra charge",
    "not included", "own expense", "pre-book", "pre-bookable", "supplementary charge",
    "supplement",
    "at your leisure", "can be arranged", "choice is yours", "for those who",
    "if desired", "if time allows", "if you prefer", "if you wish", "may wish",
    "on request", "optional", "optionally", "perhaps", "should you wish",
    "the option of", "those who wish", "upon request", "you may choose",
    "you may visit", "your choice",
})
# What a sentence has to say for an offered moment to be described rather than promised — read
# by the gate, ported verbatim from the same reference file.
_HEDGE_PHRASES = frozenset({
    "can ", "choose", "could ", "if ", "may ", "might ", "offer", "on request",
    "opt ", "option", "perhaps", "possible", "prefer", "there is time",
    "those who", "time to", "want to", "wish", "you can", "you may",
})

# AA-514 — exact numeric thresholds ported verbatim from the origin's own
# `reference/gate-thresholds.toml` (not invented), for gate_seo_surface().
_SEO_TITLE_MAX_CHARS = 60
# AA-768 (S233) — a title under 45 chars is a stub, not a headline (S232: 21-char
# "Bhutan's Craft Bazaar" shipped on 6/20 blogs, two of them approved).
_SEO_TITLE_MIN_CHARS = 45
_META_DESCRIPTION_MIN_CHARS = 120
_META_DESCRIPTION_MAX_CHARS = 158
_SLUG_MAX_CHARS = 60
_SLUG_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")

# Strips a citation tag AND one leading space if present, so "sentence. [R:id] Next." doesn't
# leave a double space where the tag used to sit ("sentence.  Next.") — "sentence.[R:id] Next."
# (no leading space) is unaffected either way.
_STRIP_TAG_RE = re.compile(r"\s?\[(?:R|F):[^\]]+\]")


def strip_citation_tags(text: Optional[str]) -> str:
    """AA-452 — removes every [R:id]/[F:id] citation tag from `text`. This is the ONLY place
    T9's tagged intermediate content (blog channel, written so gate_atom_density()/
    gate_grounding() have something to check) becomes what a tenant actually sees — called once,
    at the very end of service.write_and_check(), on content_text AND (via
    deep_strip_citation_tags() below) every violation string in gate_ledger/repair_log, since a
    gate's own violation message can itself quote a tagged excerpt (see gate_grounding()'s
    message shape below). Non-blog channels never produce a tag in the first place
    (prompts.py only emits the tag instruction for channel=='blog') — calling this
    unconditionally on every channel's output is a deliberate no-cost safety net, not dead code
    for 7 of T9's 8 channels."""
    return _STRIP_TAG_RE.sub("", text or "")


def deep_strip_citation_tags(value):
    """AA-452 — recursively applies strip_citation_tags() to every string inside a nested
    list/dict. Used on gate_ledger/repair_log, not just content_text, so a violation message
    that happens to quote tagged content can never leak a raw [R:...]/[F:...] tag to the tenant
    through that channel either — defense in depth on top of strip_citation_tags() alone."""
    if isinstance(value, str):
        return strip_citation_tags(value)
    if isinstance(value, list):
        return [deep_strip_citation_tags(v) for v in value]
    if isinstance(value, dict):
        return {k: deep_strip_citation_tags(v) for k, v in value.items()}
    return value

# AA-450-02 gate map, F2 row: union of N7's own BANNED_PATTERNS_SEED (gates.py) and
# SKILL_v2.md's own "Avoid" list — the two only partially overlap, T9 is answerable to its own
# source doc's list too, not only N7's. Kept as regexes (case-insensitive), same shape as N7's
# _BANNED_PATTERNS_COMPILED.
_BANNED_PATTERNS_SEED = [
    r"\bnestled\b", r"\btapestry\b", r"\bhidden gem(s)?\b", r"\bmust[- ]visit\b",
    r"\bmust[- ]see\b", r"\bunforgettable\b", r"\bbreathtaking\b", r"\bbucket[- ]list\b",
    r"\bwhether you'?re .{3,40} or .{3,40}\b", r"\bin conclusion\b", r"\bembark on\b",
    r"\bawait(s)? you\b", r"\bimmerse yourself\b", r"\blook no further\b", r"\bdelve\b",
    # SKILL_v2.md §Avoid — not in N7's own list
    r"\bin today'?s fast[- ]paced world\b", r"\bgame[- ]changing\b", r"\brevolutionary\b",
    r"\bunlock your potential\b", r"\btake your .{2,30} to the next level\b",
    r"\bwhether you'?re a beginner or (an? )?expert\b",
]
_BANNED_PATTERNS_COMPILED = [re.compile(p, re.IGNORECASE) for p in _BANNED_PATTERNS_SEED]

_JUDGE_SYSTEM_PROMPT = (
    "You are a structural editor. You score writing against a fixed rubric — you do not "
    "rewrite, you do not soften scores, and every score of 1 must be backed by an exact quote "
    "from the piece as evidence. You have not seen and do not know how this piece was generated "
    "or instructed to be written; judge only what is on the page in front of you."
)

# AA-450-02 gate map, F9 row: N7's own real facebook rubric reused verbatim as the baseline
# across ALL channels — not per-channel bespoke fields, per ADR-2026-009 ("extend from real
# failures, don't guess ahead of data") given T9 has zero real production pieces yet for any
# channel. Flagged, not oversold as final — the same caveat gates.py's own F9-social docstring
# carries for its 2-channel version of this same choice.
_BRAND_VOICE_FIELDS = ["brand_fit", "cta_clear", "human_read"]
_BRAND_VOICE_FAILURE_CODES = [
    "SUMMARY_OFF_BRAND", "CTA_NOT_REFLECTED", "GENERIC_AI_WORDING", "FACT_CHECK_MANUAL_CHECK",
]

# Same concrete good/bad calibration anchor as gates.py::GENERIC_AI_WORDING_ANCHOR (referenced,
# not imported — this module has no dependency on services.acp_produce) — the mechanism (a
# judge given only an abstract label drifts; a concrete good/bad pair anchors it) is real and
# worth keeping, the exact anchor text is T9's own (facebook/tiktok-shaped copy, not blog).
_GENERIC_AI_WORDING_ANCHOR = (
    "\n\nWHAT COUNTS AS GENERIC_AI_WORDING (concrete anchor, not a vague vibe check):\n"
    "- BAD (generic — flag this): \"Experience the best of this incredible destination on an "
    "unforgettable journey.\" — templated superlatives, no concrete detail, swappable onto any "
    "destination unchanged.\n"
    "- GOOD (specific, on-brand — do NOT flag this): a sentence built from a real, concrete, "
    "verifiable detail from the content seed (a place name, a real fact, a specific number or "
    "geography) — specific, concrete, in THIS brand's voice, no adjective padding, no superlatives.\n"
    "Reserve GENERIC_AI_WORDING for templated filler that could be copy-pasted onto any other "
    "brand's any other piece unchanged.\n"
)
# AA-771 (S234) — the anchor used to say "calm and precise … Do not flag calm, precise, unhurried
# writing": a calm house voice, which is why all 3 calm Trail Pulse posts (voice: Energetic, Fresh,
# Lightly playful) were approved while the panel rated them 0/3 light edit. The brand's own voice
# adjectives now go into the same judge call (no extra call).


class GateResultLite(TypedDict):
    gate: str
    passed: bool
    violations: list[str]
    repairable: bool
    blocking: bool


def _result(
    gate: str, violations: list[str], repairable: bool = True, blocking: bool = True,
) -> GateResultLite:
    # AA-519 Việc 5 — `blocking` defaults True so every pre-existing call site (F1/F2/F3/F4/F6/
    # F7/F8/F9/F4-seo-surface) is byte-identical in behavior without touching them; only
    # gate_promises_an_option() below passes blocking=False. See run_quality_gates()'s own
    # docstring for how this changes first_failure/passed without touching the other 8 gates.
    return {
        "gate": gate, "passed": not violations, "violations": violations,
        "repairable": repairable, "blocking": blocking,
    }


def _downgrade_to_warn(result: GateResultLite) -> GateResultLite:
    """AA-613 — flip a gate result to non-blocking (warn) at the orchestration layer without
    touching the gate function itself. Used for F8_framework: the gate still computes its
    verdict the same way, but a failure ships as a flag rather than holding/retrying the piece.
    A non-blocking failed gate is never `first_failure` and is collected into `flags` (see
    run_quality_gates)."""
    return {**result, "blocking": False, "repairable": False}


# ---------------------------------------------------------------- CTA gate (F6-DET-half)

def gate_cta_present(cta: Optional[str]) -> GateResultLite:
    """AA-450-02 gate map, F6 row — direct precedent from N7's own `_is_f6_content_fixable()`
    filter: a missing CTA is external/caller-state, not something a rewrite can fix, so this
    fails NON-repairable (immediate hold, no repair round spent) exactly like N7's "no
    cta_target" case. This gate runs FIRST for the same reason N7's `output_rules` runs before
    the rest of the stack — no reason to pay for the 2 LLM-judge gates below on a piece that was
    always going to hold regardless."""
    if not cta or not cta.strip():
        return _result("F6_cta_present", ["no CTA available for this request"], repairable=False)
    return _result("F6_cta_present", [])


# ---------------------------------------------------------------- F1-adjusted (grounding)

def gate_grounding(content_text: str, atom_text: str) -> GateResultLite:
    """AA-450-02 gate map, F1 row — no [R:atom_id] tags (T9's output is tenant-facing copy, a
    visible citation tag would look broken, same reasoning acp_s4_social/writer.py's
    tag-free output already implied). Reuses services.acp_shared.grounding.
    find_novel_numeric_claims() directly — the SAME shared utility N7's own gate_grounding()
    and S1's check_grounding() already share (ADR-2026-033, "one implementation, not two that
    can drift") — sentence-by-sentence against the ONE chosen atom's text (no tag parsing
    needed, there is exactly one source)."""
    violations: list[str] = []
    for sent in _SENT_SPLIT_RE.split(content_text or ""):
        novel = find_novel_numeric_claims(sent, [atom_text])
        if novel:
            # AA-452: strip_citation_tags() here too, not just at the final output boundary —
            # for channel='blog' `sent` may carry a [R:atom_id] tag; deep_strip_citation_tags()
            # (service.py) already catches this defensively, but scrubbing at the source means
            # this specific violation string is never tagged in the first place.
            quoted = strip_citation_tags(sent.strip())[:100]
            violations.append(f"sentence states {novel} not present in the content seed: '{quoted}'")
    return _result("F1_grounding", violations)


# ---------------------------------------------------------------- F2-adjusted (banned patterns)

def gate_banned_patterns(content_text: str, atom_text: str) -> GateResultLite:
    """AA-450-02 gate map, F2 row — deterministic, cheap. A match is exempt only when it's
    genuinely present (case-insensitive) in the atom's own source text — same B12 carve-out N7's
    own gate_banned_patterns() applies, adapted to T9's single-atom shape."""
    violations: list[str] = []
    atom_lower = (atom_text or "").lower()
    for pattern in _BANNED_PATTERNS_COMPILED:
        for m in pattern.finditer(content_text or ""):
            phrase = m.group(0).lower()
            if phrase in atom_lower:
                continue
            violations.append(f"banned pattern /{pattern.pattern}/ -> '{m.group(0)}'")
    return _result("F2_banned_patterns", violations)


# ---------------------------------------------------------------- F2b / F11: tenant brand (AA-771, S233)

def _sentence_with(content_text: str, needle: str) -> str:
    for sent in _SENT_SPLIT_RE.split(content_text or ""):
        if needle.lower() in sent.lower():
            return strip_citation_tags(sent.strip())[:160]
    return needle


def gate_brand_forbidden_words(content_text: str, atom_text: str,
                               forbidden_words: Optional[list[str]]) -> GateResultLite:
    """F2_brand_forbidden — blocking, repairable: a word or phrase the tenant's own T0 brand pack
    forbids (before S233 these reached only the F9 judge as text, a warn-only signal). Exempt
    when the phrase is in the source itself, same carve-out as F2."""
    violations: list[str] = []
    atom_lower = (atom_text or "").lower()
    for phrase in forbidden_words or []:
        phrase = phrase.strip()
        if len(phrase) < 2 or phrase.lower() in atom_lower:
            continue
        pattern = re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)", re.IGNORECASE)
        m = pattern.search(content_text or "")
        if m:
            violations.append(f"uses '{m.group(0)}', which the brand forbids: "
                              f"'{_sentence_with(content_text, m.group(0))}'")
    return _result("F2_brand_forbidden", violations)


def gate_brand_name(content_text: str, atom_text: str, brand_name: Optional[str]) -> GateResultLite:
    """F11_brand_name — blocking, repairable: the piece speaks as a brand that is not the tenant
    ("At Arcadia Adventures, we…" for Trail Pulse, S232) or names the platform (Adventure Asia /
    AA) to a tenant's readers."""
    from services.acp_shared.brand_pack import foreign_brand_mentions
    violations = [
        f"names a brand that is not {brand_name or 'this brand'} ('{name}'): "
        f"'{_sentence_with(content_text, name)}'"
        for name in dict.fromkeys(foreign_brand_mentions(content_text, brand_name, atom_text))
    ]
    return _result("F11_brand_name", violations)


# ---------------------------------------------------------------- F10 (cross-tenant cannibalization, AA-484)

def gate_cannibalization(match: Optional[dict]) -> GateResultLite:
    """AA-484 (traced to AA-332's original design, Nghiệp-confirmed Q6=B, 25/07/2026, cited in
    this issue's own Linear comment): a piece that reads too similar to one already published by
    a DIFFERENT tenant risks 2 tenants' readers noticing duplicate marketing copy for
    (realistically) the same shared-pool tour. BLOCKING + repairable — "chặn + yêu cầu đổi góc"
    (block + require a different angle), matching the confirmed design's own words, not a softer
    flag like AA-499's within-tenant `within_tenant_reuse` signal.

    Deliberately a PURE function taking an already-computed `match` (or `None`) — this module has
    no DB access anywhere else and that stays true here too (`run_quality_gates()`'s own
    docstring: every other gate is synchronous/no-I/O). The caller (`service.py`, which already
    has pool access) does the embedding + `find_similar_pieces(cross_tenant=True)` lookup BEFORE
    calling `run_quality_gates()`, filters to >= the confirmed 0.92 threshold, and passes `None`
    when there's no match (or the embedding call itself soft-failed) — a missing/failed embedding
    must never itself cause a hold, same soft-fail contract every other embedding consumer in
    this codebase follows.

    `match` shape (a plain dict, not `services.acp_shared.piece_similarity.SimilarPiece` — kept
    dependency-light, this module doesn't import anything DB-adjacent): `{"piece_id": str,
    "tenant_id": str, "similarity": float, "writer_missing_brand_rules": bool}`. The held/
    violation text names the colliding piece (not the tenant's real display name — this module
    has no brand/tenant lookup either) so a human reviewer has a real lead to follow, without
    this gate itself doing a second query. `writer_missing_brand_rules` (AA-484's own issue text,
    citing AA-425's real finding) surfaces the highest-risk root cause as a diagnostic hint, not
    a second gate — a tenant with no active brand rules is exactly the group this issue names as
    most likely to drift into generic, convergence-prone copy."""
    if not match:
        return _result("F10_cannibalization_cross_tenant", [])
    hint = (
        " This tenant has no active brand rules configured — generic, unbranded copy is more "
        "likely to converge with other tenants' content; setting up brand rules may reduce "
        "this going forward." if match.get("writer_missing_brand_rules") else ""
    )
    violations = [
        f"content is {match['similarity']:.2f} cosine-similar to a piece already published by "
        f"a different tenant (piece_id={match['piece_id']}) — above the confirmed 0.92 "
        f"cannibalization threshold. Two tenants publishing near-identical copy risks readers "
        f"noticing duplicate marketing content.{hint}"
    ]
    return _result("F10_cannibalization_cross_tenant", violations, repairable=True)


# ---------------------------------------------------------------- promises an option (new, AA-514)

def gate_promises_an_option(
    content_text: str, atom_text: str, route_segments: Optional[list[tuple[str, str]]] = None,
) -> GateResultLite:
    """AA-514/AA-519 — ADR 0023 (flag-not-block, Ms. Thư repo) + ADR 0026 ("an offered moment is
    ranked and never promised"): a sentence citing an OFFERED (optional/at-a-price) moment must
    hedge it ("there is time to visit...", "you may choose to...") rather than state it as
    something the reader will definitely do ("visit the..."). `never repairable` — this is a
    judgement call about what the sentence claims, same class of gap gate_grounding()/F1 already
    leaves to a human rather than asking a model to insert a hedge (which the origin's own
    docstring warns "will hedge the whole paragraph into mush").

    AA-519 Việc 5 — `blocking=False`: AA-514 shipped this as `repairable=False`, which T10's loop
    (service.py) reads as an immediate HOLD — a block. ADR 0023 read verbatim (and ADR 0026's own
    closing line, "a Piece built on an offered moment will usually carry a flag on its first
    draft, and that is the normal case under ADR 0023 rather than a failure") says this must ship
    flagged, not held. `repairable` stays False (still never sent to the writer — same "the
    world, not the shape" reasoning) — only `blocking` changes.

    Applies to EVERY channel (origin's own `channels=None`), not just blog — but the DETECTION
    shape differs by whether real per-sentence citation tags exist (AA-513, blog Route pick
    only):
      - `route_segments` given (>1 real Segment): map each cited sentence to ITS OWN Segment's
        text via the tag it actually carries, and check ONLY that Segment's own offered-ness —
        a sentence tagged with a non-offered Segment is never flagged even if another Segment in
        the same piece is offered.
      - Otherwise (every non-blog channel, and every single-atom blog piece): T9's whole seed is
        the ONE atom `content_text` is written from, so if THAT atom's own text carries an
        offered-phrase, every substantive sentence in the piece is checked for a hedge — there is
        no second, unrelated source in the piece to accidentally flag.

    Known, disclosed approximation (single-atom branch only): a sentence that is not actually
    ABOUT the offered moment at all (a pure CTA line, a brand-framing opener) can still be
    checked, since T9 has no per-sentence topic boundary to exclude it — the origin's own
    per-sentence tag check avoids this by construction (every checked sentence carries a real
    citation), which the multi-Segment branch above already replicates faithfully. Not fixed
    here — flagged as a real, narrow false-positive surface for a future refinement, not a
    silent gap.
    """
    violations = [
        (f"{', '.join(ids)} is offered rather than included, and this states it as done: '{sent[:120]}'"
         if ids else f"content seed is offered rather than included, and this states it as done: '{sent[:120]}'")
        for sent, ids, _ in promised_sentences(content_text, atom_text, route_segments)
    ]
    return _result("promises_an_option", violations, repairable=False, blocking=False)


def promised_sentences(
    content_text: str, atom_text: str, route_segments: Optional[list[tuple[str, str]]] = None,
) -> list[tuple[str, list[str], str]]:
    """The sentences gate_promises_an_option() flags: (sentence without tags, offered Segment ids
    — [] on the single-atom branch —, the offered source text). Shared with the AA-701 Jev observer
    so both read the same detection."""
    out: list[tuple[str, list[str], str]] = []
    if route_segments and len(route_segments) > 1:
        text_by_id = {aid: text for aid, text in route_segments}
        for sent in _SENT_SPLIT_RE.split(content_text or ""):
            tags = TAG_RE.findall(sent)
            offered_ids = [t for t in tags if t in text_by_id and _is_offered(text_by_id[t])]
            if not offered_ids:
                continue
            plain = strip_citation_tags(sent).lower()
            if any(h in plain for h in _HEDGE_PHRASES):
                continue
            ids = sorted(set(offered_ids))
            out.append((strip_citation_tags(sent.strip()), ids, "\n".join(text_by_id[i] for i in ids)))
    elif _is_offered(atom_text):
        for sent in _SENT_SPLIT_RE.split(content_text or ""):
            plain = strip_citation_tags(sent).lower()
            if not plain.strip():
                continue
            if any(h in plain for h in _HEDGE_PHRASES):
                continue
            out.append((strip_citation_tags(sent.strip()), [], atom_text))
    return out


def _is_offered(text: str) -> bool:
    plain = (text or "").lower()
    return any(phrase in plain for phrase in _OFFERED_PHRASES)


# ---------------------------------------------------------------- F4-adjusted (extreme length only)

# STEP0/build task's own explicit instruction: no hardcoded exact per-channel word limit (no
# source document has one). "Extreme" is deliberately generous — this catches an empty/near-empty
# response or a multi-thousand-word runaway generation, nothing narrower.
# AA-531 — that "no source document has one" premise was true for the 7 short channels but wrong
# for blog: the origin repo (`aa-social-media`) specs blog at 800-2,200 words (~4,700-13,000
# chars) verbatim (docs/claude_audit referenced in AA-525 Phần 13, real finding — 3 real blog
# pieces for tenant wanderlux-travel, 6,679-7,266 chars, were held by the shared 6,000-char
# ceiling despite being BELOW the correct 2,200-word/~13,000-char ceiling). `_MIN_CHARS`/
# `_MAX_CHARS` stay the shared default for the other 7 channels (facebook/instagram/tiktok/etc —
# no evidence any of them need a different threshold); blog gets its own pair via
# `_length_bounds_for_channel()`, not a value swap on the shared constants, so the other channels'
# behavior is byte-identical to before this change.
_MIN_CHARS = 20
_MAX_CHARS = 6000
_BLOG_MIN_CHARS = 4700  # ~800 words
_BLOG_MAX_CHARS = 13000  # ~2,200 words


def _length_bounds_for_channel(channel: Optional[str]) -> tuple[int, int]:
    """AA-531 — returns (min_chars, max_chars) for `channel`. `blog` gets its own wider band
    (see constants above); every other channel (including an unrecognized/None value, so an
    unexpected channel fails the SAME way it always has, not a new one) keeps the original
    shared _MIN_CHARS/_MAX_CHARS pair, byte-identical to pre-AA-531 behavior."""
    if channel == "blog":
        return _BLOG_MIN_CHARS, _BLOG_MAX_CHARS
    return _MIN_CHARS, _MAX_CHARS


def gate_extreme_length(content_text: str, channel: Optional[str] = None) -> GateResultLite:
    """AA-450-02 gate map, F4 row (replaced, not ported — N7's version needs a Brief.word_range
    T9 has no equivalent of).

    AA-531 — `channel` (default None, so every pre-existing direct caller/test keeps the old
    7-channel-shaped threshold unless it opts in) selects the length band via
    `_length_bounds_for_channel()`: blog gets its own 4,700-13,000 char band (~800-2,200 words,
    matching the origin repo's real spec), every other channel is unchanged. Scope deliberately
    narrow per the issue's own instruction — no FAQ/H2-structure change here, purely the
    threshold."""
    min_chars, max_chars = _length_bounds_for_channel(channel)
    length = len(content_text or "")
    if length < min_chars:
        return _result("F4_extreme_length", [f"content is only {length} chars — effectively empty"])
    if length > max_chars:
        return _result("F4_extreme_length", [f"content is {length} chars — far beyond any real channel example"])
    return _result("F4_extreme_length", [])


# ---------------------------------------------------------------- F0: complete text (AA-768, S233)

# Prose that stops on a letter, digit, comma, connector or a stray backslash was cut by the
# token ceiling (S232: 5/20 final blogs, one approved, ended "...unobstructed t" / "starting" /
# a literal "\\"). json_repair turns a cut blog envelope into a body that looks finished, so the
# check reads the text itself; the writer also retries a ceiling stop (generate._generate_full).
_END_OK_RE = re.compile(r"""[.!?…)"'\u201d\u2019\]*]\s*$""")


def _last_prose_line(text: str) -> Optional[str]:
    for line in reversed((text or "").strip().splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        # hashtag lines, list items, headings and bare links end without a full stop by design
        if stripped.startswith(("#", "- ", "* ", "http")) or re.match(r"^\d+[.)]\s", stripped):
            return None
        return stripped
    return None


def gate_complete(content_text: str) -> GateResultLite:
    """F0_complete — blocking, repairable: the piece must not end mid-sentence."""
    text = strip_citation_tags(content_text).rstrip()
    if text.endswith("\\"):
        return _result("F0_complete", ["the text ends with a stray backslash — it was cut off"])
    last = _last_prose_line(text)
    if last is not None and not _END_OK_RE.search(last):
        return _result("F0_complete", [f"the text stops mid-sentence: '...{last[-80:]}'"])
    return _result("F0_complete", [])


# ---------------------------------------------------------------- F4 family: SEO surface (new, AA-514, blog only)

_KW_STOP = {"and", "the", "for", "with", "from", "into", "tour", "tours", "trip", "trips", "travel"}


def _fold(text: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c)).lower()


def keyword_covered(text: Optional[str], keyword: Optional[str]) -> bool:
    """S233 — DFS keywords are raw search queries ("khovsgol and gobi tour mongolia reviews");
    a headline cannot carry them verbatim. Covered = every content word of the keyword appears in
    the text, accents and case ignored, with at most one generic word (tour/trip/travel…) or one
    word in a long keyword missing. Empty keyword = covered."""
    if not keyword:
        return True
    words = [w for w in re.findall(r"[a-z0-9]{3,}", _fold(keyword)) if w not in _KW_STOP]
    if not words:
        return True
    hay = set(re.findall(r"[a-z0-9]{3,}", _fold(text or "")))
    hay |= {w[:-1] for w in hay if w.endswith("s")} | {w + "s" for w in hay}  # camp ~ camps
    missing = [w for w in words if w not in hay]
    return not missing or (len(words) >= 4 and len(missing) == 1)


def gate_seo_surface(
    seo_title: Optional[str], meta_description: Optional[str], slug: Optional[str],
    keyword: Optional[str], *, seed: Optional[str] = None, body: Optional[str] = None,
) -> GateResultLite:
    """AA-514 — ported from `gates/shape.py::seo_surface()` (thresholds verbatim from
    `gate-thresholds.toml`). Blog-only (dispatch in run_quality_gates(), not here — same
    "gate functions stay channel-agnostic" shape F3/F5/F7 already use). `repairable=True` —
    joins F2_banned_patterns in the existing uniform attempt-2 rewrite loop (STEP0 §3 corrected
    premise: no separate "3 rounds" mechanism exists in T10 to build).

    All 3 fields come from the SAME JSON envelope the blog-channel writer now returns alongside
    `content_text` (prompts.py/generate.py, AA-514) — `None` means the writer's JSON response
    was missing that key entirely (a real parse gap, not "field legitimately empty"), reported
    as its own violation rather than silently skipped."""
    violations: list[str] = []
    kw = (keyword or "").lower()

    if not seo_title:
        violations.append("no SEO title — the search headline is not optional")
    else:
        if len(seo_title) > _SEO_TITLE_MAX_CHARS:
            violations.append(
                f"SEO title is {len(seo_title)} characters — it truncates above {_SEO_TITLE_MAX_CHARS}"
            )
        elif len(seo_title) < _SEO_TITLE_MIN_CHARS:
            violations.append(
                f"SEO title is {len(seo_title)} characters — a stub; write {_SEO_TITLE_MIN_CHARS}-"
                f"{_SEO_TITLE_MAX_CHARS} characters"
            )
        if kw and not keyword_covered(seo_title, keyword):
            violations.append(f"SEO title does not contain the keyword '{keyword}'")

    if not meta_description:
        violations.append("no meta description — the search snippet is not optional")
    else:
        length = len(meta_description)
        if not (_META_DESCRIPTION_MIN_CHARS <= length <= _META_DESCRIPTION_MAX_CHARS):
            violations.append(
                f"meta description is {length} characters — outside "
                f"{_META_DESCRIPTION_MIN_CHARS}-{_META_DESCRIPTION_MAX_CHARS}"
            )
        if not meta_description.rstrip().endswith((".", "!", "?")):
            violations.append("meta description is not a complete sentence")
        if kw and not keyword_covered(meta_description, keyword):
            violations.append(f"meta description does not contain the keyword '{keyword}'")
        # AA-772 (S234 round 2) — the link preview may not sell a product the seed and the post
        # never mention ('a Bhutan meditation retreat' for a dzong visit). Checked only when the
        # caller passes the seed.
        if seed is not None:
            from services.acp_content_writing.seo_grounding import meta_unsupported_products
            for word in meta_unsupported_products(meta_description, seed, body):
                violations.append(f"meta description promises '{word}', which neither the seed nor the post mentions")

    if not slug:
        violations.append("no slug")
    elif not _SLUG_RE.fullmatch(slug):
        violations.append(f"slug '{slug}' is not lowercase-kebab")
    elif len(slug) > _SLUG_MAX_CHARS:
        violations.append(f"slug is {len(slug)} characters — over {_SLUG_MAX_CHARS}")

    return _result("F4_seo_surface", violations)


# ---------------------------------------------------------------- AA-505 judge call logging

def _log_t10_judge_call(raw: dict, *, gate: str, passed: bool, extra: dict) -> None:
    """Shared by gate_framework()/gate_brand_voice() below. Both gate functions are deliberately
    channel/context-agnostic (this file's own module docstring), so no tenant identity is threaded
    down into them — the row's tenant comes from the running T9 job (call_log.bind_tenant,
    AA-741 G1). cost_usd uses pricing.calc_cost() — accurate for the real production judge (gpt-4.1, in
    COST_TABLE); an approximation (Sonnet-tier fallback rate) for a manual nova_pro/gpt56
    override, which is not the shipped default — acceptable for an observability log, not used
    for billing."""
    from shared.llm_client.pricing import calc_cost
    model = raw.get("model_used", "unknown")
    in_tok, out_tok = raw.get("input_tokens", 0), raw.get("output_tokens", 0)
    cost = raw.get("cost_usd")
    record_call_sync(
        stage="t10_judge", role="judge", model=model,
        tokens_in=in_tok, tokens_out=out_tok,
        cost_usd=cost if cost is not None else calc_cost(model, in_tok, out_tok),
        quality_signal={"gate": gate, "passed": passed, **extra},
        stop_reason=raw.get("stop_reason"),
        account=raw.get("account"), fallback_used=raw.get("fallback_used"),
    )


# ---------------------------------------------------------------- F8-adjusted (framework judge)

def gate_framework(content_text: str, goal_key: str) -> GateResultLite:
    """AA-450-02 gate map, F8 row — same Nova Pro judge, same binary 1/0 + mandatory-evidence-
    quote contract, same writer-prompt isolation as N7's gate_framework() (reuses
    services.acp_produce.judge_client.invoke_judge/parse_judge_json directly — those two
    functions are pure LLM-call plumbing, not N7 business logic, importing them is not a
    departure from ADR §0.5's "don't reuse acp_s4_social/acp_produce business logic"). Rubric
    items come from framework_rubrics.py (derived from goals.py's own `logic` field), not N7's
    FRAMEWORK_RUBRICS (T8's 8 goals include SLAP/FAB/BAB/5W1H, which N7's table doesn't cover)."""
    rubric_items = get_framework_rubric(goal_key)
    contract = json.dumps({
        "items": [{"criterion": "str", "score": "1|0",
                    "evidence": "exact quote from the piece, or empty string if score is 0"}],
    }, indent=1)
    user_prompt = (
        f"PIECE:\n{content_text}\n\n"
        f"RUBRIC — score each item 1 (met) or 0 (not met), quoting exact evidence from the "
        f"piece for every 1:\n- " + "\n- ".join(rubric_items) +
        f"\n\nOutput ONLY JSON matching this contract:\n{contract}"
    )
    try:
        raw = invoke_judge(_JUDGE_SYSTEM_PROMPT, user_prompt, stage="t10_judge")
        data = parse_judge_json(raw["text"])
    except Exception as e:
        logger.warning("t10_f8_judge_unavailable", error=str(e))
        return _result("F8_framework", [f"judge unavailable: {e} — treated as fail"])

    violations: list[str] = []
    items = data.get("items") or []
    for item in items:
        criterion = item.get("criterion", "(unnamed criterion)")
        score = str(item.get("score"))
        evidence = (item.get("evidence") or "").strip()
        if score != "1":
            violations.append(f"framework criterion failed: {criterion}")
        elif not evidence:
            violations.append(f"framework criterion '{criterion}' scored 1 with no evidence quote — treated as fail")
    if not items:
        violations.append("judge returned no rubric items — treated as fail, not a silent pass")
    # AA-505 — real judge outcome (item-level pass ratio), not a placeholder. The tenant comes
    # from the running job (call_log.bind_tenant), not from this context-agnostic function.
    _log_t10_judge_call(raw, gate="F8_framework", passed=not violations,
                         extra={"items_total": len(items),
                                "items_passed": sum(1 for i in items if str(i.get("score")) == "1")})
    return _result("F8_framework", violations)


# ---------------------------------------------------------------- F9-adjusted (brand/CTA/voice judge)

# AA-768 — the F9 judge now reads the source and returns unsupported_claims; on the Luna route
# (reasoning tokens count toward the cap) 2048 ran out on a route blog: all 3 models returned
# empty content (max_tokens / finish_reason=length) and the piece held as "judge unavailable".
_F9_MAX_TOKENS = 8192


def _voice_block(voice: Optional[list[str]]) -> str:
    """AA-771 (S234) — the brand's voice adjectives for the F9 judge; '' when the pack has none."""
    words = [v.strip() for v in voice or [] if isinstance(v, str) and v.strip()]
    if not words:
        return ""
    return ("\nBRAND VOICE (judge the tone against these words, never against a calm house style): "
            + ", ".join(words) + ". Use SUMMARY_OFF_BRAND, quoting the phrase, when the tone is clearly the "
            "opposite of them (e.g. calm and essayistic for an energetic, playful brand).\n")


def gate_brand_voice(
    content_text: str, cta: str, brand_rubric_text: str, source_text: Optional[str] = None,
    voice: Optional[list[str]] = None,
) -> list[GateResultLite]:
    """AA-450-02 gate map, F9 row — see module docstring for the rubric-field/failure-code
    choices. `cta_clear` also absorbs N7's dropped F6 literal-CTA-substring check (semantic
    judgment, since T9's CTA is a free-text tenant value, not N7's one fixed brand phrase — no
    hardcoded-phrase false-positive carve-out ports as-is for this reason).

    AA-613 — returns a LIST of two results from ONE judge call (Nghiệp's 3-tier severity split):
    `F9_cta_fact` (blocking, product-truth: CTA/FACT_CHECK) and `F9_brand_style` (non-blocking
    warn: off-brand voice / generic AI wording). See the split logic below.

    AA-768 — `source_text` (the writer's own source: seed + Facts + audience/trip lines, the same
    text gate_grounding() checks against) is shown to the judge. Before this the judge saw only
    the piece and was told "when uncertain about a factual claim: manual_check" — with nothing to
    check against, every fact-bearing piece came back FACT_CHECK_MANUAL_CHECK and no rewrite
    could fix it (S231 pilot: 5/5 held). The judge must now quote the claim it flags, so the
    repair round gets a sentence to fix instead of a bare code. `None` keeps the old prompt for
    any caller without a source."""
    contract = json.dumps({
        "status": "pass|flagged|manual_check",
        **{f: "1|0" for f in _BRAND_VOICE_FIELDS},
        "failure_codes": [f"subset of {_BRAND_VOICE_FAILURE_CODES}"],
        "flagged_phrases": ["exact quoted phrase for each SUMMARY_OFF_BRAND/GENERIC_AI_WORDING/"
                             "FACT_CHECK_MANUAL_CHECK code above — [] if none is used"],
        "notes": "str",
        # AA-768 round 4 — only used when a SOURCE is given (see below).
        **({"unsupported_claims": [{"sentence": "exact sentence from the piece",
                                    "claim": "the specific checkable fact it asserts",
                                    "why_not_in_source": "what the SOURCE says instead, or that it is silent"}]}
           if source_text else {}),
    }, indent=1)
    if source_text:
        source_block = f"SOURCE (everything the writer was given as fact):\n{source_text}\n\n"
        # AA-768 round 2 — the first live re-run showed the judge flagging opinions and rhetoric
        # ("This is not a performance.", "That distinction matters…") and the brand describing
        # its own approach. Only a checkable statement of fact is in scope for this code.
        fact_rule = (
            "FACT_CHECK_MANUAL_CHECK: use it ONLY for a checkable statement of fact — something "
            "that is true or false about the place, the trip, or what travellers will do, see, "
            "get or pay (an activity, service, inclusion, time, duration, price, number, "
            "transport, or a promise) — that the SOURCE above does not support or contradicts. "
            "Quote that exact sentence in flagged_phrases. These are NOT factual claims and must "
            "never get this code: opinions, impressions, mood or rhetorical sentences, "
            "generalisations about how travellers feel, the brand describing its own approach "
            "or values (as in the BRAND RUBRIC), and the required call to action. Widely known "
            "background about a place the SOURCE names — what it is, its history and era, who "
            "founded it, its religious or cultural role, whether it is still in use — is allowed "
            "when it does not contradict the SOURCE (AA-768). Always unsupported: placing the "
            "place in a town, region or country other than the SOURCE's Location; invented scene "
            "detail (what the place looks, sounds or smells like, its paths, steps or roads, how "
            "it is reached, crowds) the SOURCE does not give. If no sentence is an unsupported "
            "checkable fact, do "
            "not use the code. For every FACT_CHECK_MANUAL_CHECK flag, list the claim in "
            "unsupported_claims with why the SOURCE does not support it. A claim the SOURCE "
            "states — even in other words — is supported: never list it. No entry in "
            "unsupported_claims means no FACT_CHECK_MANUAL_CHECK."
        )
    else:
        source_block = ""
        fact_rule = "When uncertain about a factual claim: status=manual_check + FACT_CHECK_MANUAL_CHECK."
    user_prompt = (
        f"PIECE:\n{content_text}\n\n"
        f"{source_block}"
        f"REQUIRED CALL TO ACTION FOR THIS PIECE: {cta}\n\n"
        f"BRAND RUBRIC:\n{brand_rubric_text}\n"
        f"{_voice_block(voice)}"
        f"{_GENERIC_AI_WORDING_ANCHOR}\n"
        "Score every field 1 or 0. cta_clear=1 only if the required CTA above is present and "
        "reads as a single, unambiguous action — not a vague sign-off. Use ONLY the listed "
        f"failure codes: {_BRAND_VOICE_FAILURE_CODES}. If you use SUMMARY_OFF_BRAND or "
        "GENERIC_AI_WORDING, you MUST quote the exact offending phrase in flagged_phrases. The "
        "required CTA text itself is never grounds for SUMMARY_OFF_BRAND or GENERIC_AI_WORDING "
        f"on its own — do not flag it. {fact_rule}\n\n"
        f"Output ONLY JSON matching this contract:\n{contract}"
    )
    try:
        raw = invoke_judge(_JUDGE_SYSTEM_PROMPT, user_prompt, max_tokens=_F9_MAX_TOKENS, stage="t10_judge")
        data = parse_judge_json(raw["text"])
    except Exception as e:
        logger.warning("t10_f9_judge_unavailable", error=str(e))
        # Fail-closed on the BLOCKING (product-truth) side; brand-style passes (no signal to
        # flag on). AA-613 — same two-result shape as the success path below.
        return [
            # AA-770 (S233) — an outage is not a verdict: the piece ships, the admin sees the flag.
            _result("F9_cta_fact", [f"judge unavailable: {e} — not checked"], repairable=False,
                    blocking=False),
            _result("F9_brand_style", [], repairable=False, blocking=False),
        ]

    status = data.get("status", "manual_check")
    failure_codes = [c for c in (data.get("failure_codes") or []) if c in _BRAND_VOICE_FAILURE_CODES]
    notes = data.get("notes") or ""
    phrases = [p for p in (data.get("flagged_phrases") or []) if isinstance(p, str) and p.strip()]
    # AA-768 round 4 — with a SOURCE, a FACT_CHECK flag counts only when the judge names the
    # unsupported claim and why. Round-3 re-run: the judge flagged sentences the SOURCE states
    # verbatim ("Pawangka was built by … Songtsen Gampo"). A flag it cannot justify is dropped;
    # a justified one is quoted to the rewrite as claim + reason.
    claims = []
    if source_text:
        claims = [c for c in (data.get("unsupported_claims") or [])
                  if isinstance(c, dict) and str(c.get("claim") or "").strip()
                  and str(c.get("why_not_in_source") or "").strip()]
        if "FACT_CHECK_MANUAL_CHECK" in failure_codes and not claims:
            failure_codes = [c for c in failure_codes if c != "FACT_CHECK_MANUAL_CHECK"]
            if not failure_codes and status != "pass":
                status = "pass"  # the only reason to fail was an unjustified fact flag

    # AA-613 — split the single F9 judge call into TWO gate results by failure class, so the
    # T10 loop can treat them differently (Nghiệp's 3-tier severity model):
    #   - F9_cta_fact  (BLOCKING / product-truth): CTA_NOT_REFLECTED, FACT_CHECK_MANUAL_CHECK.
    #     A missing/unclear CTA or an unverified factual claim is a contract/truth failure — it
    #     still triggers a retry and still gates publish.
    #   - F9_brand_style (NON-BLOCKING / warn): SUMMARY_OFF_BRAND, GENERIC_AI_WORDING. Off-brand
    #     voice or generic AI wording is a quality note, not a reason to withhold the piece from
    #     the tenant (ships flagged, per ADR 0023 flag-not-block).
    # A judge failure/unavailability is treated as a BLOCKING fail (fail-closed), same as before.
    # One judge call, two results — the judge already returns per-code failure_codes.
    _PRODUCT_TRUTH_CODES = {"CTA_NOT_REFLECTED", "FACT_CHECK_MANUAL_CHECK"}
    _BRAND_STYLE_CODES = {"SUMMARY_OFF_BRAND", "GENERIC_AI_WORDING"}
    judge_ok = status == "pass"

    def _viol(codes: set[str]) -> list[str]:
        hit = [c for c in failure_codes if c in codes]
        if not hit:
            return []
        reason = ", ".join(hit)
        if claims and "FACT_CHECK_MANUAL_CHECK" in hit:
            return [f"audit {status}: {reason} — unsupported claim(s): " + "; ".join(
                f'"{c.get("sentence") or c["claim"]}" (claims: {c["claim"]}; source: {c["why_not_in_source"]})'
                for c in claims)]
        # AA-768 — FACT_CHECK quotes its claim too, so the rewrite knows which sentence to fix.
        if phrases and set(hit) & (_BRAND_STYLE_CODES | {"FACT_CHECK_MANUAL_CHECK"}):
            reason += " — exact flagged phrase(s): " + "; ".join(f'"{p}"' for p in phrases)
        return [f"audit {status}: {reason}"]

    cta_fact_viol = _viol(_PRODUCT_TRUTH_CODES)
    brand_style_viol = _viol(_BRAND_STYLE_CODES)
    # A non-pass status with NO recognized code, or a bare manual_check with notes only, is
    # ambiguous about which class it belongs to — treat it as product-truth (fail-closed on the
    # blocking side) rather than silently downgrading it to a warn.
    if not judge_ok and not cta_fact_viol and not brand_style_viol:
        cta_fact_viol = [f"audit {status}: {', '.join(failure_codes) or notes or '(no reason given)'}"]

    _log_t10_judge_call(raw, gate="F9_brand_voice", passed=judge_ok,
                         extra={"status": status, "failure_codes": failure_codes})
    return [
        _result("F9_cta_fact", cta_fact_viol, blocking=True),
        _result("F9_brand_style", brand_style_viol, repairable=False, blocking=False),
    ]


# ---------------------------------------------------------------- F5-adjusted (atom density, blog only)

def gate_atom_density(content_text: str) -> GateResultLite:
    """AA-452 gate map (docs/claude_audit/AA-452-t10-nine-gates.md) — F5 port, channel=='blog'
    only (dispatch lives in run_quality_gates() below, not here — same "gate functions stay
    channel-agnostic, run_quality_gates() decides what to call" shape F3/F7 also use). Ported
    from acp_produce/gates.py::gate_atom_density() (referenced, not imported, ADR §0.5): chunks
    `content_text` into non-overlapping ATOM_DENSITY_WORDS(300)-word windows; a window with zero
    [R:id]/[F:id] tags fails. Runs on content WITH tags still present — this is the one gate
    whose entire job is to see the tags gate_grounding()/the tenant-facing output never keep. A
    trailing chunk shorter than window//2 is skipped, same guard as the N7 original (avoids
    flagging a short leftover fragment that could never reach a full window)."""
    body = content_text or ""
    words = body.split()
    violations: list[str] = []
    window = ATOM_DENSITY_WORDS
    for start in range(0, max(1, len(words)), window):
        chunk_words = words[start:start + window]
        if len(chunk_words) < window // 2:
            continue
        chunk = " ".join(chunk_words)
        if not TAG_RE.search(chunk):
            violations.append(
                f"words {start}-{start + len(chunk_words)}: zero atom citations in this "
                f"stretch — that's where AI-voice lives; add a specific, verifiable detail or "
                f"cut it. First 80 chars: '{chunk[:80]}'"
            )
    return _result("F5_atom_density", violations)


# ---------------------------------------------------------------- F3-adjusted (structural variance, blog only)

def gate_structural_variance(
    content_text: str, route_segments: Optional[list[tuple[str, str]]] = None,
) -> GateResultLite:
    """AA-452 gate map — F3 port, channel=='blog' only (dispatch in run_quality_gates()). Ported
    from acp_produce/gates.py::gate_structural_variance() (referenced, not imported): (1) at
    least one genuinely one-sentence paragraph exists, (2) variance check (below), (3) at most 1
    bulleted list. Runs on content WITH citation tags still present — paragraph/section
    boundaries are blank-line/`## `-delimited, tag tokens inside a paragraph don't affect either
    boundary, so no stripping is needed here (unlike gate_extreme_length(), which
    run_quality_gates() deliberately calls on the STRIPPED text since a citation tag shouldn't
    count toward a length ceiling meant to bound what the tenant reads).

    AA-514 — (2), the variance check itself, is now ROUTE-AWARE when `route_segments` carries
    >1 real Segment: instead of comparing lengths between ANY H2 sections (which might not
    correlate to a Segment at all), each section is mapped to the Segment whose own id is cited
    most inside it (first citation found, same "representative" convention
    `_fetch_route_segments()`/AA-513 already use elsewhere), and variance is measured strictly
    BETWEEN those Segment-mapped sections — the real ask ("biến thiên GIỮA CÁC ĐOẠN TƯƠNG ỨNG
    TỪNG SEGMENT", not between arbitrary H2s). Falls back to the original generic H2 check,
    byte-identical, when `route_segments` is None or has <=1 Segment (every non-Route blog
    piece, unchanged from before this build)."""
    body = content_text or ""
    violations: list[str] = []
    paras = [p for p in body.split("\n\n") if p.strip() and not p.startswith("#")]

    if not any(
        len(re.split(r"(?<=[.!?])\s+", p.strip())) == 1 and len(p.split()) >= 2
        for p in paras
    ):
        violations.append("no one-sentence paragraph found (variance rule)")

    sections = re.split(r"^## ", body, flags=re.MULTILINE)[1:]
    if route_segments and len(route_segments) > 1:
        segment_ids = {aid for aid, _ in route_segments}
        by_segment: dict[str, int] = {}
        for sec in sections:
            tags = [t for t in TAG_RE.findall(sec) if t in segment_ids]
            if tags:
                seg_id = tags[0]
                by_segment[seg_id] = by_segment.get(seg_id, 0) + len(sec.split())
        if len(by_segment) < 2:
            violations.append(
                "fewer than 2 Segment-mapped sections found — cannot measure route-aware variance"
            )
        else:
            lens = sorted(by_segment.values())
            if lens[-1] < lens[-2] * 1.4:
                violations.append("no Segment-section is notably longer than the others (route-aware variance)")
    elif len(sections) >= 3:
        lens = sorted(len(s.split()) for s in sections)
        if lens and lens[-1] < lens[-2] * 1.4:
            violations.append("no section is notably longer than the others")

    blocks = len([b for b in body.split("\n\n") if re.match(r"^\s*[-*] ", b)])
    if blocks > 1:
        violations.append(f"{blocks} bulleted lists — max 1 per article")

    return _result("F3_structural_variance", violations)


# ---------------------------------------------------------------- F7-adjusted (FAQ dedup, blog only)

_FAQ_ANSWER_RE = re.compile(r"\*\*Q: .*?\*\*\s*\nA: (.*?)(?=\n\*\*Q: |\Z)", re.DOTALL)
_FAQ_TOKEN_RE = re.compile(r"[a-z]{5,}")
_FAQ_DEDUP_THRESHOLD = 0.85  # ported from acp_produce/gates.py::gate_faq_dedup()


def gate_faq_dedup(content_text: str) -> GateResultLite:
    """AA-452 gate map — F7 port, channel=='blog' only (dispatch in run_quality_gates()). Ported
    from acp_produce/gates.py::gate_faq_dedup() (referenced, not imported): does a FAQ answer
    just restate a body paragraph within the SAME piece? INTRA-piece only, same real gap the N7
    original documents (cross-time dedup against previously-published FAQs needs a table that
    doesn't exist for T9 either). No-op (passed=True) when the piece has no '## FAQ' section —
    prompts.py's blog instructions only ask for one "if the piece includes a FAQ section", most
    blog pieces won't have one. Matches the real '**Q: ...**'/'A: ...' format prompts.py now
    instructs the writer to use, same shape acp_produce/faq.py::render_faq_section() renders for
    N7."""
    body = content_text or ""
    if "## FAQ" not in body:
        return _result("F7_faq_dedup", [])

    pre, _, faq = body.partition("## FAQ")
    pre_tokens = set(_FAQ_TOKEN_RE.findall(pre.lower()))

    violations: list[str] = []
    for i, m in enumerate(_FAQ_ANSWER_RE.finditer(faq)):
        answer = m.group(1).strip()
        answer_tokens = set(_FAQ_TOKEN_RE.findall(answer.lower()))
        if answer_tokens and len(answer_tokens & pre_tokens) / len(answer_tokens) > _FAQ_DEDUP_THRESHOLD:
            violations.append(
                f"FAQ answer {i + 1} restates a body paragraph -- cut or add a detail the body lacks"
            )
    return _result("F7_faq_dedup", violations)


_FAQ_PAIR_RE = re.compile(r"\*\*Q: .*?\*\*\s*\nA: .*?(?=\n\*\*Q: |\Z)", re.DOTALL)


def drop_restated_faq(content_text: str) -> tuple[str, int]:
    """AA-770 (S233) — F7 auto-fix: remove FAQ pairs whose answer restates the body (same test as
    gate_faq_dedup) instead of holding or rewriting the whole piece. Drops the "## FAQ" heading
    too when no pair is left. Returns (text, pairs removed)."""
    body = content_text or ""
    if "## FAQ" not in body:
        return body, 0
    pre, _, faq = body.partition("## FAQ")
    pre_tokens = set(_FAQ_TOKEN_RE.findall(pre.lower()))
    kept, dropped = [], 0
    for m in _FAQ_PAIR_RE.finditer(faq):
        answer = _FAQ_ANSWER_RE.match(m.group(0))
        tokens = set(_FAQ_TOKEN_RE.findall((answer.group(1) if answer else "").lower()))
        if tokens and len(tokens & pre_tokens) / len(tokens) > _FAQ_DEDUP_THRESHOLD:
            dropped += 1
        else:
            kept.append(m.group(0).strip())
    if not dropped:
        return body, 0
    if not kept:
        return pre.rstrip() + "\n", dropped
    return pre + "## FAQ\n\n" + "\n\n".join(kept) + "\n", dropped


# AA-768 (S234) — FAQ questions the seed cannot answer. S233 r1: 5/8 blogs asked about age
# suitability, fitness or price; the answers came from the brand's AUDIENCE line or were invented.
# Each topic: (question regex, support regex). A pair is dropped when its question hits a topic
# and neither the moment's OWN seed lines nor the FACTS block has a support term for it — the
# "other trips" lines, AUDIENCE and BRAND never count as support. The question side asks about the
# topic itself (review S234): a family brand names children in most questions ('What will kids
# see at the reindeer camp?'), so bare kids/children, 'fit', 'include' or 'when should' never
# count — only age suitability, effort level, price/inclusions and timing/season wording.
_KIDS = r"(?:young(?:er)? |small |little )?(?:children|kids|toddlers|teens|teenagers|families)"
_FAQ_TOPICS = {
    "age": (r"\b(?:what|which|any) (?:ages?|age range|age group)\b|\bages?\s+(?:range|limit|groups?"
            r"|requirements?|restrictions?)\b|\bage of (?:the )?(?:children|kids)\b|\bminimum age\b|\bhow old\b"
            r"|\btoo young\b|\bold enough\b|\bage[- ]appropriate\b|\b(?:child|kid|family)[- ]friendly\b"
            rf"|\bsuit(?:able|ed) (?:for|to) {_KIDS}|\b(?:good|right|ok|okay|safe|appropriate) for {_KIDS}"
            r"|\b(?:children|kids|toddlers) (?:under|over|aged|of all ages)\b",
            r"\b(?:aged \d|ages? \d|\d+\s*(?:-|–|to)\s*\d+\s*years|minimum age|children (?:under|over|from|aged)"
            r"|suitable for (?:young )?(?:children|kids|families)|(?:child|kid|family)[- ]friendly)"),
    "fitness": (r"\b(?:fitness|how fit|fit enough|physically|physical (?:effort|demands?|condition)|demanding"
                r"|difficult|difficulty|strenuous|challenging|how hard|level of (?:effort|exertion)"
                r"|altitude sickness)\b",
                r"\b(?:strenuous|demanding|difficult|difficulty|fitness|steep|challenging|easy|gentle|moderate"
                r"|level of exertion)\b"),
    "price": (r"\b(?:price|prices|pricing|cost|costs|how much|budget|fees?|inclusions?)\b"
              r"|\bwhat(?:'s| is| are)? included in the (?:price|tour|trip|package|cost)\b"
              r"|\bwhat does the (?:price|tour|trip|package|cost) include\b",
              r"(?:\b(?:price|prices|cost|costs|usd|fees?|included|includes|inclusive)\b|[$€£])"),
    "booking": (r"\b(?:permits?|visas?|book|booking|reserve|reservations?)\b",
                r"\b(?:permits?|visas?|book|booking|reserv\w*)\b"),
    "accommodation": (r"\b(?:hotels?|accommodation|where (?:do|will|does) (?:we|you|families|the group) (?:stay|sleep)"
                      r"|wi-?fi|bathrooms?|showers?|toilets?)\b",
                      r"\b(?:hotels?|lodges?|ger|gers|camps?|guesthouses?|homestays?|accommodation|overnight|wi-?fi"
                      r"|bathrooms?|showers?)\b"),
    "best_time": (r"\b(?:best time|best season|which season|what season|time of year|weather|climate|temperatures?"
                  r"|when (?:is the best|to go|to visit|to travel))\b",
                  r"(?:^Season:|\b(?:seasons?|summer|winter|spring|autumn|monsoon|weather|climate|temperatures?"
                  r"|cold|warm|rain)\b)"),
}
_FAQ_Q_RE = re.compile(r"\*\*Q: (.*?)\*\*", re.DOTALL)


def unanswerable_faq_topics(question: str, own_seed_text: str, facts_text: Optional[str] = None) -> list[str]:
    """The topics `question` asks about that neither the seed's own lines nor FACTS support."""
    support = f"{own_seed_text or ''}\n{facts_text or ''}"
    q = question.lower()
    return [topic for topic, (ask, ok) in _FAQ_TOPICS.items()
            if re.search(ask, q) and not re.search(ok, support, re.I | re.M)]


def drop_unanswerable_faq(content_text: str, own_seed_text: str,
                          facts_text: Optional[str] = None) -> tuple[str, list[str]]:
    """AA-768 (S234) — remove FAQ pairs whose question the source cannot answer (see
    `_FAQ_TOPICS`). `own_seed_text` must be the moment's own lines (hygiene.split_seed), never the
    grounding text with AUDIENCE/BRAND. When fewer than 2 pairs are left, the whole FAQ goes (it is
    optional per the prompt). Returns (text, one log line per dropped pair)."""
    body = content_text or ""
    if "## FAQ" not in body:
        return body, []
    pre, _, faq = body.partition("## FAQ")
    kept, log = [], []
    for m in _FAQ_PAIR_RE.finditer(faq):
        q = _FAQ_Q_RE.match(m.group(0))
        topics = unanswerable_faq_topics(q.group(1) if q else "", own_seed_text, facts_text)
        if topics:
            log.append(f"dropped FAQ the source cannot answer ({', '.join(topics)}): "
                       f"'{strip_citation_tags(q.group(1) if q else m.group(0))[:160]}'")
        else:
            kept.append(m.group(0).strip())
    if not log:
        return body, []
    if len(kept) < 2:
        if kept:
            log.append(f"dropped the FAQ section: only {len(kept)} answerable pair left")
        return pre.rstrip() + "\n", log
    return pre + "## FAQ\n\n" + "\n\n".join(kept) + "\n", log


# ---------------------------------------------------------------- F16 FAQ contract (AA-768, S234 round 2)

# S234 panel §2 class 4: 6/8 blogs ended with no FAQ (#2, #17) or one pair (#1, #16, #18, #19) —
# the single deciding structural defect of those blogs. A blog must end on "## FAQ" with at least
# FAQ_MIN_PAIRS pairs, counted AFTER drop_restated_faq / drop_unanswerable_faq ran. service.py
# regenerates a short FAQ once (generate.write_faq, t9_repair) before the gates read the text.
FAQ_MIN_PAIRS = 3
F16_GATE = "F16_faq"


def faq_pair_count(content_text: str) -> int:
    """Q/A pairs under the "## FAQ" heading (the same pair shape the droppers read)."""
    body = content_text or ""
    if "## FAQ" not in body:
        return 0
    return len(_FAQ_PAIR_RE.findall(body.partition("## FAQ")[2]))


def replace_faq_section(content_text: str, pairs: list[tuple[str, str]]) -> str:
    """The text with its FAQ section replaced by `pairs` ((question, answer), no markup), as the
    last section. Prose blocks that sat after the FAQ pairs (a CTA written after the FAQ) move
    above the FAQ, so the CTA stays before it (blog format rule)."""
    body = (content_text or "").rstrip()
    before, _, faq = body.partition("## FAQ")
    trailing = [b.strip() for b in re.split(r"\n\s*\n", faq)
                if b.strip() and not b.strip().startswith(("**Q:", "A:"))]
    head = before.rstrip()
    if trailing:
        head += "\n\n" + "\n\n".join(trailing)
    qa = "\n\n".join(f"**Q: {q.strip()}**\nA: {a.strip()}" for q, a in pairs if q.strip() and a.strip())
    return f"{head}\n\n## FAQ\n\n{qa}\n" if qa else head + "\n"


def gate_faq_contract(content_text: str) -> GateResultLite:
    """F16_faq (blog) — the post must end on a "## FAQ" section with at least FAQ_MIN_PAIRS pairs.
    Blocking and not repairable: the FAQ step in service.py already had its one regeneration, so a
    short FAQ holds the piece (admin label) instead of forcing a full rewrite that invents facts.
    sentence_repair.plan skips it (owned by that step), so it never blocks a sentence repair."""
    n = faq_pair_count(content_text)
    if n >= FAQ_MIN_PAIRS:
        return _result(F16_GATE, [], repairable=False)
    return _result(F16_GATE, [f"the blog ends with {n} FAQ pair(s) after unanswerable and restated ones were "
                              f"dropped; it needs at least {FAQ_MIN_PAIRS}"], repairable=False)


# ---------------------------------------------------------------- F12 / F13 (AA-768, S234)

# F12_scope warns for one checkpoint round (S234 plan C4); flip to True once the replay and the
# live checkpoint show 0 false positives. It is repairable either way: a body violation quotes
# its sentence, so sentence_repair can fix it once it blocks.
F12_SCOPE_BLOCKING = False


def gate_scope(content_text: str, hygiene_ctx: Optional[HygieneContext], seo_title: Optional[str] = None,
               meta_description: Optional[str] = None) -> GateResultLite:
    """F12_scope — the title, meta, headings or body name a place outside the piece's SCOPE (the
    TRIP name's or another trip's places; hygiene.build_scope_lexicon). No context → passes."""
    if hygiene_ctx is None:
        return _result("F12_scope", [], blocking=F12_SCOPE_BLOCKING)
    found = find_issues(content_text, hygiene_ctx, seo_title=seo_title, meta_description=meta_description)
    return _result("F12_scope", found["scope"], repairable=True, blocking=F12_SCOPE_BLOCKING)


F13_LEAK_GATE = "F13_leak"


def gate_leak(content_text: str, hygiene_ctx: Optional[HygieneContext]) -> GateResultLite:
    """F13_leak (blocking, sentence-repairable) — what hygiene.autofix could not remove safely: the
    writer's instructions ('A Sentence on…', 'This guide covers…', the SCOPE's day span), the
    brand's audience ranges ('25–40'), the source notes' own words, or a piece that opens on a
    pronoun with no referent (a repair artifact, S234 #19). Each quotes its sentence."""
    if hygiene_ctx is None:
        return _result(F13_LEAK_GATE, [])
    return _result(F13_LEAK_GATE, find_issues(content_text, hygiene_ctx)["leak"], repairable=True)


def gate_hygiene(content_text: str, hygiene_ctx: Optional[HygieneContext]) -> GateResultLite:
    """F13_hygiene (warn) — what autofix could not fix safely: a source-notes leak, social proof,
    a repeated sentence, a misspelt hashtag, too many hashtags."""
    if hygiene_ctx is None:
        return _result("F13_hygiene", [], repairable=False, blocking=False)
    return _result("F13_hygiene", find_issues(content_text, hygiene_ctx)["hygiene"], repairable=False,
                   blocking=False)


# ---------------------------------------------------------------- F14 / F15 (AA-771, S234)

# F15_voice (stock-phrase hard list only) blocks — and goes to sentence repair, never a full
# rewrite — on the FIRST attempt only, and never holds a piece (S234 plan PR3 + review): a style
# phrase is not product truth (AA-613 'held'). Trade register, audience leak and opener checks are a
# separate warn-only result, F15_voice_warn, so sentence_repair.plan never rewrites them.
F15_STOCK_BLOCKING = True
F14_GATE = "F14_social_shape"
F15_GATE = "F15_voice"
F15_WARN_GATE = "F15_voice_warn"
# Gates that may block (to trigger one repair) but never hold a piece on their own.
STYLE_ONLY_GATES = frozenset({F15_GATE})
# S234 round 2 — gates a dedicated deterministic/one-call step in service.py fixes every attempt
# (like F4_seo_surface and the SEO fit): sentence_repair.plan never turns them into a rewrite.
OWNED_BY_FIX_STEP = frozenset({"F16_faq", "F14_first_line"})
_MIN_QUOTE_WORDS = 4  # sentence_repair.locate_sentence needs 4 words to find a sentence
OPENER_REPEAT_JACCARD = 0.6


def gate_social_shape(content_text: str, spec: Optional[ChannelSpec]) -> GateResultLite:
    """F14_social_shape — LinkedIn word count, hashtags and first line against the brand's channel
    spec. Warn outside the word band; BLOCKING (a full rewrite with the explicit target) only when
    the post is over 1.3 × the band's top (325 words; 390 editorial). Hashtags and the first line are
    normally fixed in place by hygiene.autofix first, so they show here only as residue. No spec
    (every other channel) → passes."""
    if spec is None:
        return _result(F14_GATE, [], repairable=False, blocking=False)
    text = strip_citation_tags(content_text or "")
    n = post_words(text)
    lo, hi = spec.words
    tlo, thi = spec.target_words
    violations: list[str] = []
    blocking = n > spec.block_words
    if blocking:
        violations.append(f"post is {n} words, far over the {lo}–{hi}-word band for this brand on LinkedIn — "
                          f"rewrite it to {tlo}–{thi} words, keeping the same angle and facts")
    elif n > hi:
        violations.append(f"post is {n} words, over the {lo}–{hi}-word band for this brand on LinkedIn")
    elif n < lo:
        violations.append(f"post is {n} words, under the {lo}–{hi}-word band for this brand on LinkedIn")
    tags = list(dict.fromkeys(HASHTAG_RE.findall(text)))
    if len(tags) > spec.hashtags_max:
        violations.append(f"{len(tags)} hashtags, more than {spec.hashtags_max}: '{' '.join(tags)}'")
    return _result(F14_GATE, violations, repairable=blocking, blocking=blocking)


F14_FIRST_LINE_GATE = "F14_first_line"


def gate_first_line(content_text: str, spec: Optional[ChannelSpec]) -> GateResultLite:
    """F14_first_line (S234 round 2) — the hook line is still longer than the brand's first_line_max
    after hygiene.autofix split it and service.py's one first-sentence rewrite (t9_repair) ran
    (S234 #4 165, #12 209 chars). Blocking, not repairable: the rewrite step owns it
    (sentence_repair.plan skips it), so it holds the piece instead of forcing a full rewrite."""
    if spec is None:
        return _result(F14_FIRST_LINE_GATE, [], repairable=False)
    text = strip_citation_tags(content_text or "")
    head = text.strip().split("\n", 1)[0].strip() if text.strip() else ""
    if len(head) <= spec.first_line_max:
        return _result(F14_FIRST_LINE_GATE, [], repairable=False)
    return _result(F14_FIRST_LINE_GATE,
                   [f"first line is {len(head)} characters, over {spec.first_line_max}: '{head[:300]}'"],
                   repairable=False)


def _quote(spans: list[dict], start: int, end: int) -> str:
    """The sentence(s) of `spans` covering [start, end), extended by the next sentence while it
    has fewer than `_MIN_QUOTE_WORDS` words, so sentence repair can locate it."""
    idx = [i for i, sp in enumerate(spans) if sp["end"] > start and sp["start"] < end]
    if not idx:
        return ""
    i, j = idx[0], idx[-1]
    while len(" ".join(sp["clean"] for sp in spans[i:j + 1]).split()) < _MIN_QUOTE_WORDS and j + 1 < len(spans):
        j += 1
    return " ".join(sp["clean"] for sp in spans[i:j + 1])


_SEG_NUM_RE = re.compile(r"\d+\s*(?:[–—-]|to)\s*\d+|\d+\s*\+")


def _norm_num(text: str) -> str:
    """'25 – 40' / '25 to 40' → '25-40'; '18 +' → '18+'."""
    t = re.sub(r"(\d)\s*(?:[–—-]|to)\s*(\d)", r"\1-\2", text or "")
    return re.sub(r"(\d)\s*\+", r"\1+", t)


def _has_num(text: str, key: str) -> bool:
    return re.search(rf"(?<!\d){re.escape(key)}(?!\d)", _norm_num(text)) is not None


# S234 round 2 — contrast frames are fixed only down to the allowed density, worst first: a stock
# 'This is not a metaphor', then a two-sentence "It's not X. It's Y.", then "is not X, it is Y",
# then the milder "not just X but Y" / "less X than Y" (index into hygiene.CONTRAST_FRAME_RES).
_CONTRAST_RANK = (5, 2, 0, 1, 6, 3, 7, 4)


def _contrast_violations(text: str, spans: list[dict]) -> list[str]:
    n, allowed, frames = contrast_excess(text)
    if n <= allowed:
        return []
    words = len(strip_citation_tags(text).split())
    worst = sorted(frames, key=lambda f: (_CONTRAST_RANK.index(f[3]), f[0]))[:n - allowed]
    out = []
    for start, end, match, _kind in sorted(worst):
        q = _quote(spans, start, end)
        if q:
            out.append(f"contrast frame '{' '.join(match.split())[:80]}' ({n} in {words} words, over 1 per 150) "
                       f"— say it directly, without the 'not X but Y' turn: '{q}'")
    return out


def _template_violations(text: str, sibling_texts: Optional[list[str]], exclude_texts: Optional[list[str]],
                         brand_name: Optional[str], spans: list[dict], keyword: Optional[str] = None) -> list[str]:
    out = []
    kept = {sp["clean"] for sp in spans}
    for sentence, run in sibling_templates(text, sibling_texts or [], exclude=exclude_texts or [],
                                           brand_name=brand_name, keyword=keyword):
        if sentence in kept and len(sentence.split()) >= _MIN_QUOTE_WORDS:
            out.append(f"repeats a line from an earlier piece of this brief ('{run}') — write it fresh: '{sentence}'")
    return out


def gate_voice(content_text: str, *, spec: Optional[ChannelSpec], stock_blocking: bool = True,
               sibling_texts: Optional[list[str]] = None, exclude_texts: Optional[list[str]] = None,
               brand_name: Optional[str] = None, keyword: Optional[str] = None) -> GateResultLite:
    """F15_voice — the stock-phrase hard list for a channel with a spec (LinkedIn; S233: 30/41 posts
    — 'worth sitting with', 'not incidental', 'Not as filler. Not as an afterthought.'). Blocking when
    F15_STOCK_BLOCKING and `stock_blocking` (the caller passes it for attempt 1 only), so it is
    repaired sentence by sentence (sentence_repair._ELIGIBLE_GATES) at most once, never by a full
    rewrite; `hold_failure` never holds a piece on it. Every violation reads
    "<problem>: '<sentence>'" (the shape sentence_repair.plan parses)."""
    text = content_text or ""
    spans = [sp for sp in sentence_spans(text) if sp["kind"] in ("prose", "bullet")]
    stock: list[str] = []
    if spec is not None:
        for pattern in STOCK_PATTERNS:
            for m in pattern.finditer(text):
                q = _quote(spans, m.start(), m.end())
                if q:
                    stock.append(f"stock phrase '{' '.join(m.group(0).split())}' — say it plainly or cut it: '{q}'")
    # S234 round 2 — every channel: contrast-frame density and lines shared with earlier pieces of
    # the same brief (S234 panel §5 fix 6). Same quoted shape, same one-repair-then-flag rule.
    stock += _contrast_violations(text, spans)
    stock += _template_violations(text, sibling_texts, exclude_texts, brand_name, spans, keyword)
    if not stock:
        return _result(F15_GATE, [], repairable=spec is not None, blocking=False)
    blocking = F15_STOCK_BLOCKING and stock_blocking and bool(stock)
    return _result(F15_GATE, list(dict.fromkeys(stock)), repairable=True, blocking=blocking)


def gate_voice_warn(content_text: str, *, spec: Optional[ChannelSpec], brand_pack: Optional[dict] = None,
                    atom_text: str = "", sibling_openers: Optional[list[str]] = None) -> GateResultLite:
    """F15_voice_warn — warn-only voice checks for a channel with a spec (LinkedIn), kept apart from
    F15_voice so a stock-phrase repair never rewrites them:
    - writing to (or as) the travel trade for a non-editorial brand (S233 Trail Pulse 3/3: 'Active
      travel brands have a pacing problem.'; channel_spec.TRADE_PATTERN);
    - the brand's Audience numbers in the text when the source does not have them (S233 #6
      'clients in the 25–40 range');
    - a generic 'There is a …' opener, or the same opener as an earlier piece from the same brief
      (token Jaccard ≥ 0.6; S233 Terra 3/3 identical).
    Same "<problem>: '<sentence>'" shape, for the admin highlights."""
    if spec is None:
        return _result(F15_WARN_GATE, [], repairable=False, blocking=False)
    text = content_text or ""
    spans = [sp for sp in sentence_spans(text) if sp["kind"] in ("prose", "bullet")]
    warn: list[str] = []
    if spec.register != EDITORIAL:
        for sp in spans:
            m = B2B_RE.search(sp["clean"])
            if m:
                warn.append(f"writes to the travel trade ('{m.group(0)}'), not to travellers: "
                            f"'{_quote(spans, sp['start'], sp['end'])}'")
    # The gates' source carries the brand's AUDIENCE line (service._grounding_text, for F1) — the
    # very text a leak repeats — so it is left out here.
    source = "\n\n".join(p for p in (atom_text or "").split("\n\n") if not p.startswith("AUDIENCE"))
    for num in dict.fromkeys(_SEG_NUM_RE.findall((brand_pack or {}).get("customer_segment") or "")):
        key = _norm_num(num).strip()
        if _has_num(source, key):
            continue
        for sp in spans:
            if _has_num(sp["clean"], key):
                warn.append(f"repeats the brand's audience definition ('{num.strip()}') — the Audience line is not a "
                            f"fact about this trip: '{_quote(spans, sp['start'], sp['end'])}'")
                break
    if spans:
        opener = first_sentence(text)
        q = _quote(spans, spans[0]["start"], spans[0]["end"])
        if GENERIC_OPENER_RE.match(opener):
            warn.append(f"generic 'There is a …' opener: '{q}'")
        same = next((o for o in sibling_openers or [] if opener_overlap(opener, o) >= OPENER_REPEAT_JACCARD), None)
        if same:
            warn.append(f"opens like an earlier piece from this brief ('{same[:80]}'): '{q}'")
    return _result(F15_WARN_GATE, list(dict.fromkeys(warn)), repairable=False, blocking=False)


def hold_failure(gate_ledger: list[GateResultLite]) -> Optional[GateResultLite]:
    """The failed blocking gate a piece is HELD on — the first one that is not style-only
    (STYLE_ONLY_GATES: F15_voice may block to get one sentence repair, but a stock phrase is not
    unresolved product truth, AA-613). None → nothing holds the piece; it ships with the flags."""
    return next((g for g in gate_ledger if not g["passed"] and g.get("blocking", True)
                 and g["gate"] not in STYLE_ONLY_GATES), None)


# ---------------------------------------------------------------- orchestration

class QualityCheckOutcome(TypedDict):
    passed: bool
    gate_ledger: list[GateResultLite]
    first_failure: Optional[GateResultLite]
    flags: list[GateResultLite]


def run_quality_gates(
    *, content_text: str, atom_text: str, cta: Optional[str], goal_key: str,
    brand_rubric_text: str, channel: str,
    route_segments: Optional[list[tuple[str, str]]] = None,
    seo_title: Optional[str] = None, meta_description: Optional[str] = None,
    slug: Optional[str] = None, keyword: Optional[str] = None,
    cannibalization_match: Optional[dict] = None,
    brand_pack: Optional[dict] = None,
    hygiene_ctx: Optional[HygieneContext] = None,
    sibling_openers: Optional[list[str]] = None,
    stock_blocking: bool = True,
    sibling_texts: Optional[list[str]] = None,
) -> QualityCheckOutcome:
    """Runs the T10 gate stack for ONE attempt. CTA-presence runs first and short-circuits every
    other gate on failure (same "don't pay for a judge call on content that was always going to
    be rejected" reasoning N7's own output_rules pre-check uses) — every other gate always runs
    regardless of an earlier gate's outcome, same as N7's run_gates() (a repair fixing one gate
    must never ship while silently regressing another the caller never re-checked).
    `first_failure` is the first FAILED, BLOCKING gate in this fixed order (AA-519 Việc 5 —
    was just "first failed gate" before `blocking` existed), used by service.py to pick which
    violations to feed the attempt-2 rewrite / decide a hold. A failed non-blocking gate is never
    `first_failure` and never holds/repairs a piece — it's collected into the returned `flags`
    list instead, still visible in `gate_ledger` either way.

    AA-452: `channel` is now required (every real caller — service.py — always has it; existing
    tests updated to pass one explicitly rather than given a default, same "explicit over
    implicit" discipline this codebase already applies elsewhere, e.g. gate_brief_compliance's
    fail-closed `brief=None`). `channel == 'blog'` runs 3 more DET gates
    (gate_atom_density/gate_structural_variance/gate_faq_dedup, F5/F3/F7) between F4 and F8 —
    the other 7 channels get exactly the 6-gate stack this function has always run, unchanged.
    gate_extreme_length() (F4) is deliberately called on the TAG-STRIPPED text, not raw
    `content_text` — citation tags are internal markup and shouldn't count toward a length
    ceiling meant to bound what the tenant actually reads (AA-531: also passed `channel` now, so
    blog gets its own wider length band instead of the shared 7-channel one — see
    `_length_bounds_for_channel()`); every other gate below still sees the
    tagged text, since F1/F5/F3/F7 all need it (F1/F5 read the tags directly; F3/F7 just don't
    need it stripped, see their own docstrings).

    AA-514: `gate_promises_an_option()` runs for EVERY channel (origin's own `channels=None`,
    right after F2 — deliberately unaffected by the blog-only branch below) — `route_segments`
    (None for every non-Route request, unchanged default) lets it map a cited sentence to its
    OWN Segment when one exists. `gate_seo_surface()` (F4 family) is blog-only, alongside the
    existing 3 blog-only DET gates — `seo_title`/`meta_description`/`slug`/`keyword` are all
    `None` for every non-blog channel (nothing to check, matches every other blog-only gate's
    own "channel decides, not the gate function" convention)."""
    cta_result = gate_cta_present(cta)
    ledger = [cta_result]
    if not cta_result["passed"]:
        return {"passed": False, "gate_ledger": ledger, "first_failure": cta_result, "flags": []}

    length_check_text = strip_citation_tags(content_text)
    spec = channel_spec(channel, brand_pack)
    gate_ledger = [
        cta_result,
        gate_grounding(content_text, atom_text),
        gate_banned_patterns(content_text, atom_text),
        # AA-771 (S233) — the tenant's own forbidden words and brand name, deterministic.
        gate_brand_forbidden_words(content_text, atom_text, (brand_pack or {}).get("forbidden_words")),
        gate_brand_name(content_text, atom_text, (brand_pack or {}).get("brand_name")),
        # AA-484 — every channel, not blog-only (cannibalization risk isn't channel-specific);
        # placed right after F2 (another content-safety-class gate), before the DET/judge gates.
        gate_cannibalization(cannibalization_match),
        gate_promises_an_option(content_text, atom_text, route_segments),
        # AA-531 — channel-aware length band (blog gets its own wider band, see
        # _length_bounds_for_channel()); the other 7 channels are unaffected.
        gate_extreme_length(length_check_text, channel),
        gate_complete(content_text),  # AA-768 (S233) — a cut-off piece never ships as finished
        # AA-768 (S234) — scope (warn for one round) and residual hygiene (warn), every channel;
        # both pass when the caller gives no hygiene context.
        gate_scope(content_text, hygiene_ctx, seo_title, meta_description),
        gate_hygiene(content_text, hygiene_ctx),
        gate_leak(content_text, hygiene_ctx),  # S234 round 2 — blocking leaks, sentence-repairable
        # AA-771 (S234) — LinkedIn shape and voice from the brand's channel spec; both pass for a
        # channel without a spec (every channel but LinkedIn).
        gate_social_shape(content_text, spec),
        # S234 review — F15_voice blocks (stock phrases) only while `stock_blocking` (attempt 1);
        # the warn-only checks are their own result so a stock repair never rewrites them.
        # S234 round 2 — plus contrast density and sibling templates, every channel.
        gate_voice(content_text, spec=spec, stock_blocking=stock_blocking, sibling_texts=sibling_texts,
                   exclude_texts=[t for t in (hygiene_ctx.lexicon.own_text if hygiene_ctx else atom_text, cta,
                                              hygiene_ctx.facts if hygiene_ctx else None) if t],
                   brand_name=(brand_pack or {}).get("brand_name"), keyword=keyword),
        gate_voice_warn(content_text, spec=spec, brand_pack=brand_pack, atom_text=atom_text,
                        sibling_openers=sibling_openers),
    ]
    if channel == "blog":
        gate_ledger += [
            gate_seo_surface(seo_title, meta_description, slug, keyword, seed=atom_text, body=content_text),
            # AA-770 (S233) — blog shape is advice, not product truth: F5 (citation density), F3
            # (rhythm) and F7 (FAQ restates; service.py auto-drops those pairs first) warn only.
            # S232: they held 12/20 final blogs and forced full rewrites that invented new facts.
            _downgrade_to_warn(gate_atom_density(content_text)),
            _downgrade_to_warn(gate_structural_variance(content_text, route_segments)),
            _downgrade_to_warn(gate_faq_dedup(content_text)),
        ]
    # AA-613 — F8 framework/formula-fit is now a WARN (non-blocking): a piece that misses its
    # goal's framework is a quality note for the tenant/admin, not a reason to withhold it. F9
    # returns TWO results (blocking F9_cta_fact + non-blocking F9_brand_style), spread in here.
    gate_ledger.append(_downgrade_to_warn(gate_framework(content_text, goal_key)))
    gate_ledger += gate_brand_voice(content_text, cta or "", brand_rubric_text, source_text=atom_text,
                                    voice=(brand_pack or {}).get("voice"))
    # S234 round 2 — contract gates owned by a dedicated fix step in service.py (FAQ regeneration,
    # first-line rewrite). Last in the ledger and not repairable: when one is the only blocking
    # failure left the piece is held at once; when another gate fails first, that gate's repair
    # runs (sentence_repair.plan skips these two).
    if channel == "blog":
        gate_ledger.append(gate_faq_contract(content_text))
    if spec is not None:
        gate_ledger.append(gate_first_line(content_text, spec))
    # AA-519 Việc 5 — first_failure (drives service.py's hold/repair decision) only ever
    # considers a BLOCKING gate now; a failed non-blocking gate (currently only
    # promises_an_option) never holds/repairs a piece, but its result is still in gate_ledger AND
    # collected into `flags` below so it isn't silently dropped — it ships WITH the piece as a
    # note, per ADR 0023/0026.
    first_failure = next((g for g in gate_ledger if not g["passed"] and g["blocking"]), None)
    flags = [g for g in gate_ledger if not g["passed"] and not g["blocking"]]
    return {
        "passed": first_failure is None,
        "gate_ledger": gate_ledger,
        "first_failure": first_failure,
        "flags": flags,
    }


__all__ = [
    "GateResultLite", "QualityCheckOutcome", "TAG_RE", "ATOM_DENSITY_WORDS",
    "strip_citation_tags", "deep_strip_citation_tags", "gate_cta_present", "gate_grounding",
    "gate_banned_patterns", "gate_cannibalization", "gate_complete", "gate_brand_forbidden_words",
    "gate_brand_name", "gate_promises_an_option", "gate_extreme_length",
    "gate_seo_surface", "gate_atom_density", "gate_structural_variance", "gate_faq_dedup",
    "gate_framework", "gate_brand_voice", "run_quality_gates", "drop_restated_faq", "keyword_covered",
    "drop_unanswerable_faq", "unanswerable_faq_topics", "gate_scope", "gate_hygiene", "F12_SCOPE_BLOCKING",
    "gate_social_shape", "gate_voice", "gate_voice_warn", "hold_failure", "F15_STOCK_BLOCKING", "STYLE_ONLY_GATES",
    "FAQ_MIN_PAIRS", "F16_GATE", "faq_pair_count", "replace_faq_section", "gate_faq_contract", "F13_LEAK_GATE",
    "gate_leak", "F14_FIRST_LINE_GATE", "gate_first_line", "OWNED_BY_FIX_STEP",
]
