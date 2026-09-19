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
    "geography) — calm and precise, no adjective padding, no superlatives.\n"
    "Do not flag calm, precise, unhurried writing as \"too plain\" just because it lacks "
    "superlatives. Reserve GENERIC_AI_WORDING for templated filler that could be copy-pasted "
    "onto any other brand's any other piece unchanged.\n"
)


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
    violations: list[str] = []
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
            violations.append(
                f"{', '.join(sorted(set(offered_ids)))} is offered rather than included, and "
                f"this states it as done: '{strip_citation_tags(sent.strip())[:120]}'"
            )
    elif _is_offered(atom_text):
        for sent in _SENT_SPLIT_RE.split(content_text or ""):
            plain = strip_citation_tags(sent).lower()
            if not plain.strip():
                continue
            if any(h in plain for h in _HEDGE_PHRASES):
                continue
            violations.append(
                f"content seed is offered rather than included, and this states it as done: "
                f"'{strip_citation_tags(sent.strip())[:120]}'"
            )
    return _result("promises_an_option", violations, repairable=False, blocking=False)


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


# ---------------------------------------------------------------- F4 family: SEO surface (new, AA-514, blog only)

def gate_seo_surface(
    seo_title: Optional[str], meta_description: Optional[str], slug: Optional[str],
    keyword: Optional[str],
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
        if kw and kw not in seo_title.lower():
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
        if kw and kw not in meta_description.lower():
            violations.append(f"meta description does not contain the keyword '{keyword}'")

    if not slug:
        violations.append("no slug")
    elif not _SLUG_RE.fullmatch(slug):
        violations.append(f"slug '{slug}' is not lowercase-kebab")
    elif len(slug) > _SLUG_MAX_CHARS:
        violations.append(f"slug is {len(slug)} characters — over {_SLUG_MAX_CHARS}")

    return _result("F4_seo_surface", violations)


# ---------------------------------------------------------------- AA-505 judge call logging

def _log_t10_judge_call(raw: dict, *, gate: str, passed: bool, extra: dict) -> None:
    """Shared by gate_framework()/gate_brand_voice() below. tenant_id=None — both gate functions
    are deliberately channel/context-agnostic (this file's own module docstring), so no piece/
    tenant identity is threaded down into them; the same piece's t9_write row (service.py) DOES
    carry tenant_id/angle_gate_request_id and is joinable by created_at proximity if ever needed.
    cost_usd uses pricing.calc_cost() — accurate for the real production judge (gpt-4.1, in
    COST_TABLE); an approximation (Sonnet-tier fallback rate) for a manual nova_pro/gpt56
    override, which is not the shipped default — acceptable for an observability log, not used
    for billing."""
    from shared.llm_client.pricing import calc_cost
    model = raw.get("model_used", "unknown")
    in_tok, out_tok = raw.get("input_tokens", 0), raw.get("output_tokens", 0)
    record_call_sync(
        stage="t10_judge", role="judge", model=model,
        tokens_in=in_tok, tokens_out=out_tok, cost_usd=calc_cost(model, in_tok, out_tok),
        tenant_id=None,
        quality_signal={"gate": gate, "passed": passed, **extra},
        stop_reason=raw.get("stop_reason"),
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
        raw = invoke_judge(_JUDGE_SYSTEM_PROMPT, user_prompt)
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
    # AA-505 — real judge outcome (item-level pass ratio), not a placeholder. tenant_id=None: this
    # function is deliberately channel/context-agnostic (module docstring) so no piece/tenant
    # identity is threaded in here — the SAME piece's t9_write row (which does carry tenant_id/
    # angle_gate_request_id) is joinable by created_at proximity if that's ever needed.
    _log_t10_judge_call(raw, gate="F8_framework", passed=not violations,
                         extra={"items_total": len(items),
                                "items_passed": sum(1 for i in items if str(i.get("score")) == "1")})
    return _result("F8_framework", violations)


# ---------------------------------------------------------------- F9-adjusted (brand/CTA/voice judge)

def gate_brand_voice(content_text: str, cta: str, brand_rubric_text: str) -> list[GateResultLite]:
    """AA-450-02 gate map, F9 row — see module docstring for the rubric-field/failure-code
    choices. `cta_clear` also absorbs N7's dropped F6 literal-CTA-substring check (semantic
    judgment, since T9's CTA is a free-text tenant value, not N7's one fixed brand phrase — no
    hardcoded-phrase false-positive carve-out ports as-is for this reason).

    AA-613 — returns a LIST of two results from ONE judge call (Nghiệp's 3-tier severity split):
    `F9_cta_fact` (blocking, product-truth: CTA/FACT_CHECK) and `F9_brand_style` (non-blocking
    warn: off-brand voice / generic AI wording). See the split logic below."""
    contract = json.dumps({
        "status": "pass|flagged|manual_check",
        **{f: "1|0" for f in _BRAND_VOICE_FIELDS},
        "failure_codes": [f"subset of {_BRAND_VOICE_FAILURE_CODES}"],
        "flagged_phrases": ["exact quoted phrase for each SUMMARY_OFF_BRAND/GENERIC_AI_WORDING "
                             "code above — [] if neither code is used"],
        "notes": "str",
    }, indent=1)
    user_prompt = (
        f"PIECE:\n{content_text}\n\n"
        f"REQUIRED CALL TO ACTION FOR THIS PIECE: {cta}\n\n"
        f"BRAND RUBRIC:\n{brand_rubric_text}\n"
        f"{_GENERIC_AI_WORDING_ANCHOR}\n"
        "Score every field 1 or 0. cta_clear=1 only if the required CTA above is present and "
        "reads as a single, unambiguous action — not a vague sign-off. Use ONLY the listed "
        f"failure codes: {_BRAND_VOICE_FAILURE_CODES}. If you use SUMMARY_OFF_BRAND or "
        "GENERIC_AI_WORDING, you MUST quote the exact offending phrase in flagged_phrases. The "
        "required CTA text itself is never grounds for SUMMARY_OFF_BRAND or GENERIC_AI_WORDING "
        "on its own — do not flag it. When uncertain about a factual claim: "
        "status=manual_check + FACT_CHECK_MANUAL_CHECK.\n\n"
        f"Output ONLY JSON matching this contract:\n{contract}"
    )
    try:
        raw = invoke_judge(_JUDGE_SYSTEM_PROMPT, user_prompt)
        data = parse_judge_json(raw["text"])
    except Exception as e:
        logger.warning("t10_f9_judge_unavailable", error=str(e))
        # Fail-closed on the BLOCKING (product-truth) side; brand-style passes (no signal to
        # flag on). AA-613 — same two-result shape as the success path below.
        return [
            _result("F9_cta_fact", [f"judge unavailable: {e} — treated as fail"], blocking=True),
            _result("F9_brand_style", [], repairable=False, blocking=False),
        ]

    status = data.get("status", "manual_check")
    failure_codes = [c for c in (data.get("failure_codes") or []) if c in _BRAND_VOICE_FAILURE_CODES]
    notes = data.get("notes") or ""
    phrases = [p for p in (data.get("flagged_phrases") or []) if isinstance(p, str) and p.strip()]

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
        if phrases and (codes & _BRAND_STYLE_CODES):
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
    gate_ledger = [
        cta_result,
        gate_grounding(content_text, atom_text),
        gate_banned_patterns(content_text, atom_text),
        # AA-484 — every channel, not blog-only (cannibalization risk isn't channel-specific);
        # placed right after F2 (another content-safety-class gate), before the DET/judge gates.
        gate_cannibalization(cannibalization_match),
        gate_promises_an_option(content_text, atom_text, route_segments),
        # AA-531 — channel-aware length band (blog gets its own wider band, see
        # _length_bounds_for_channel()); the other 7 channels are unaffected.
        gate_extreme_length(length_check_text, channel),
    ]
    if channel == "blog":
        gate_ledger += [
            gate_seo_surface(seo_title, meta_description, slug, keyword),
            gate_atom_density(content_text),
            gate_structural_variance(content_text, route_segments),
            gate_faq_dedup(content_text),
        ]
    # AA-613 — F8 framework/formula-fit is now a WARN (non-blocking): a piece that misses its
    # goal's framework is a quality note for the tenant/admin, not a reason to withhold it. F9
    # returns TWO results (blocking F9_cta_fact + non-blocking F9_brand_style), spread in here.
    gate_ledger.append(_downgrade_to_warn(gate_framework(content_text, goal_key)))
    gate_ledger += gate_brand_voice(content_text, cta or "", brand_rubric_text)
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
    "gate_banned_patterns", "gate_cannibalization", "gate_promises_an_option", "gate_extreme_length",
    "gate_seo_surface", "gate_atom_density", "gate_structural_variance", "gate_faq_dedup",
    "gate_framework", "gate_brand_voice", "run_quality_gates",
]
