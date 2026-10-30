"""
services.acp_content_writing.quality_gates — T10, inline in the same request as T9 (Nghiep's
confirmed architecture, docs/claude_audit/AA-450-01-t9-t10-retry-loop-investigation.md). Full
gate-by-gate mapping/reasoning: docs/claude_audit/AA-450-02-t10-gate-map.md,
docs/claude_audit/AA-452-t10-nine-gates.md.

6 gates for every channel at first (F6/F1/F2/F4/F8/F9-adjusted), mirroring services/acp_produce/gates.py's
own `GateResult`/one-function-per-gate/`gate_fns` list shape (kept for the same reason N7 has it
— "dễ đọc/debug/mở rộng sau này", per the AA-450 build task's own instruction) WITHOUT importing
anything from that module — every gate here is written fresh against T9's short single-channel
content, referenced not reused (ADR §0.5).

AA-452: `channel == 'blog'` got 3 MORE gates (F5_atom_density/F3_structural_variance/
F7_faq_dedup) — AA-787 removed all three (F7 folded into F16_faq; see run_quality_gates()).
`prompts.py` still asks the blog writer for H2 sections, a FAQ and `[R:{atom_id}]` citation tags
— those tags are internal provenance markup (F1 reads them), never shown to the tenant:
`strip_citation_tags()`/`deep_strip_citation_tags()` (below) remove every tag from `content_text`
and from every gate_ledger/repair_log violation string before `service.write_and_check()` ever
persists or returns a piece (all 8 channels — the other 7 never produce a tag to strip, so this
is a no-cost safety net for them, not dead code).

Each gate is a plain SYNCHRONOUS function — same "wrap at the async/sync boundary" decision as
generate.py (see that module's docstring). `run_quality_gates()` (the one function service.py
calls) is also synchronous for the same reason; service.py wraps the WHOLE call in
`asyncio.to_thread()`, not each gate individually, since every gate for one attempt always runs
together on the same request.

AA-514: 2 more gates, ported from `src/aa_social/gates/__init__.py`'s GATES tuple (11 origin
gates total, read verbatim — docs/claude_audit/AA-514-step0-investigation.md):
  - `gate_promises_an_option()` — removed by AA-787 (CIS ADR 0009): F17_trip_fact blocks and
    repairs the same "optional stated as planned" failure.
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

from services.acp_content_writing import authenticity
from services.acp_content_writing.hygiene import (EM_DASH_PATTERN, HygieneContext, brief_phrases, content_words,
                                                  contrast_excess, copies, dangling_opener, find_issues, fold,
                                                  names_place, near_verbatim, phrase_tokens, proper_nouns,
                                                  sentence_spans, sibling_templates, template_lines,
                                                  unsupported_ages, unsupported_reasons)
from services.acp_content_writing.trip_facts import TripFactContext, find_trip_fact_issues
from services.acp_shared.channel_spec import (B2B_RE, EDITORIAL, GENERIC_OPENER_RE, HASHTAG_RE, STOCK_PATTERNS,
                                              ChannelSpec, channel_spec, first_sentence, opener_overlap,
                                              post_words)
from shared.llm_client.judge import invoke_judge, parse_judge_json_checked
from services.acp_shared.citation_tags import TAG_RE, strip_citation_tags as _strip_tags
from services.acp_shared.grounding import find_novel_numeric_claims
from services.acp_shared.offered import STATUS_PREFIX
from services.acp_shared.real_questions import is_off_meaning, single_word_places
from shared.llm_client.call_log import record_billed_failure_sync, record_call_sync
from services.quality import registry as qr
from services.acp_content_writing.seed_align import modes as seed_align
from services.acp_content_writing.seed_align import adjudicate as seed_adjudicate

logger = structlog.get_logger()

_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'‘’“”])")

# AA-452 — blog-only citation tag, same shape as acp_produce/gates.py::TAG_RE (referenced, not
# imported — ADR §0.5). T9 has exactly one atom per piece for 7 of 8 channels and every non-Route
# Segment pick (AA-513 STEP0 corrected this comment — a Route/Blog pick can carry several
# Segments' own distinct ids, see services/acp_content_writing/service.py::
# _fetch_route_segments()) — but there is still no closed-world/multi-id CHECK made against this
# pattern (unlike N7). AA-787 dropped F5 (citation density), its first reader; today it only tells the F9
# log whether a citation tag reached the judge (`piece_tag_free`). It matches any id, one or several.
# AA-912: TAG_RE is the shared definition (services/acp_shared/citation_tags.py), imported above.

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



def strip_citation_tags(text: Optional[str]) -> str:
    """AA-452 — removes every [R:id]/[F:id] citation tag from `text`. This is the ONLY place
    T9's tagged intermediate content (blog channel, written so gate_grounding() has
    something to check) becomes what a tenant actually sees — called once,
    at the very end of service.write_and_check(), on content_text AND (via
    deep_strip_citation_tags() below) every violation string in gate_ledger/repair_log, since a
    gate's own violation message can itself quote a tagged excerpt (see gate_grounding()'s
    message shape below). Non-blog channels never produce a tag in the first place
    (prompts.py only emits the tag instruction for channel=='blog') — calling this
    unconditionally on every channel's output is a deliberate no-cost safety net, not dead code
    for 7 of T9's 8 channels.

    Strips a tag AND one leading space if present, so "sentence. [R:id] Next." doesn't leave a double
    space where the tag used to sit. AA-912: one shared, linear-time definition
    (services/acp_shared/citation_tags.py); a dangling opener the writer left unclosed ('[R:atom_1'
    with no ']') is removed too — only the '[R:' and its id, never the prose after it."""
    return _strip_tags(text)


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
    # AA-800: "breathtaking" left this list — it is a stock superlative (signature_vocabulary layer 2):
    # one per piece is a warning, a second one blocks (gate_signature_words, F2_superlatives).
    r"\bmust[- ]see\b", r"\bunforgettable\b", r"\bbucket[- ]list\b",
    r"\bwhether you'?re .{3,40} or .{3,40}\b", r"\bin conclusion\b", r"\bembark on\b",
    r"\bawait(s)? you\b", r"\bimmerse yourself\b", r"\blook no further\b", r"\bdelve\b",
    # SKILL_v2.md §Avoid — not in N7's own list
    r"\bin today'?s fast[- ]paced world\b", r"\bgame[- ]changing\b", r"\brevolutionary\b",
    r"\bunlock your potential\b", r"\btake your .{2,30} to the next level\b",
    r"\bwhether you'?re a beginner or (an? )?expert\b",
    # AA-778 — the em-dash residue guard (chị Thư bans it). hygiene.fix_dashes replaces every em-dash just
    # before the gates, so this fires only on one it kept (the trip name, a quote copied from the source:
    # exempt here too, the phrase is in the source) or one a later step added. The pattern is the dash
    # with a word on each side because the seed's own labels hold em-dashes: a bare '\u2014' would always
    # be exempt.
    EM_DASH_PATTERN,
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
    # AA-786 (F9 v1.1) — product truth about the text itself: a garbled or broken sentence, a doubled
    # CTA, visible markup, a sentence cut mid-thought. Always quoted (broken_sentences), so the
    # sentence repair can fix or cut it instead of a full rewrite.
    "TEXT_INTEGRITY",
]
F9_PROMPT_VERSION = "f9_v1.1"

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


class Finding(TypedDict):
    """AA-924 — one violation of a merged ledger entry (registry.MERGED_GATES) with its own severity."""
    text: str
    severity: str  # BLOCK | WARN
    source: str  # the legacy gate that found it (a retired name for F2_banned_patterns / F2_brand_forbidden)


class _GateResultBase(TypedDict):
    gate: str
    passed: bool
    violations: list[str]
    repairable: bool
    blocking: bool


class GateResultLite(_GateResultBase, total=False):
    # AA-924 — only on a merged entry (`merge_ledger`): every finding with its severity, the worst severity, and
    # one legacy-shaped record per folded source (no `violations`) so `expand_ledger` rebuilds the legacy entries.
    # `violations` (every finding's text) and `blocking` (worst == block) stay for every older reader.
    findings: list[Finding]
    severity: Optional[str]
    parts: list[dict]
    # AA-789 tier 1 — only on a result seed_align.apply appended 'sa:' findings to: whether the gate's own findings
    # blocked before them (never_holds reads it).
    sa_base_blocking: bool


def _result(
    gate: str, violations: list[str], repairable: bool = True, blocking: bool = True,
) -> GateResultLite:
    # AA-519 Việc 5 — `blocking` defaults True; a warn gate passes blocking=False. See
    # run_quality_gates()'s own docstring for how `blocking` drives first_failure / passed / flags.
    return {
        "gate": gate, "passed": not violations, "violations": violations,
        "repairable": repairable, "blocking": blocking,
    }


# ---------------------------------------------------------------- CTA gate (F6-DET-half)

def gate_cta_present(cta: Optional[str]) -> GateResultLite:
    """AA-450-02 gate map, F6 row — direct precedent from N7's own `_is_f6_content_fixable()`
    filter: a missing CTA is external/caller-state, not something a rewrite can fix, so this
    fails NON-repairable (immediate hold, no repair round spent) exactly like N7's "no
    cta_target" case. This gate runs FIRST for the same reason N7's `output_rules` runs before
    the rest of the stack — no reason to pay for the 2 LLM-judge gates below on a piece that was
    always going to hold regardless."""
    if not cta or not cta.strip():
        return _result(qr.F6_CTA_PRESENT, ["no CTA available for this request"], repairable=False)
    return _result(qr.F6_CTA_PRESENT, [])


# ---------------------------------------------------------------- F1-adjusted (grounding)

def gate_grounding(content_text: str, atom_text: str) -> GateResultLite:
    """AA-450-02 gate map, F1 row — no [R:atom_id] tags (T9's output is tenant-facing copy, a
    visible citation tag would look broken, same reasoning acp_s4_social/writer.py's
    tag-free output already implied). Reuses services.acp_shared.grounding.
    find_novel_numeric_claims() directly — the SAME shared utility N7's own gate_grounding()
    and S1's check_grounding() already share (ADR-2026-033, "one implementation, not two that
    can drift") — sentence-by-sentence against the ONE chosen atom's text (no tag parsing
    needed, there is exactly one source).

    AA-785 (S235) — `year_forms=True`: "1637–38" and "the late 1630s" are read as years and pass
    when the seed has the year (S234: all 22 F1 violations were these forms, none a real invented
    number); T3/A1/source_facts keep the plain reading. The violation format is unchanged —
    sentence_repair._F1_QUOTE_RE parses it."""
    violations: list[str] = []
    for sent in _SENT_SPLIT_RE.split(content_text or ""):
        novel = find_novel_numeric_claims(sent, [atom_text], year_forms=True)
        if novel:
            # AA-452: strip_citation_tags() here too, not just at the final output boundary —
            # for channel='blog' `sent` may carry a [R:atom_id] tag; deep_strip_citation_tags()
            # (service.py) already catches this defensively, but scrubbing at the source means
            # this specific violation string is never tagged in the first place.
            quoted = strip_citation_tags(sent.strip())[:100]
            violations.append(f"sentence states {novel} not present in the content seed: '{quoted}'")
    return _result(qr.F1_GROUNDING, violations)


# ---------------------------------------------------------------- F2-adjusted (banned patterns)

def gate_banned_patterns(content_text: str, atom_text: str) -> GateResultLite:
    """AA-450-02 gate map, F2 row — deterministic, cheap. A match is exempt only when it's
    genuinely present (case-insensitive) in the atom's own source text — same B12 carve-out N7's
    own gate_banned_patterns() applies, adapted to T9's single-atom shape.
    AA-924 — a source check of the lexicon gate: the ledger holds it inside F2_lexicon (gate_lexicon)."""
    return _result(qr.F2_BANNED_PATTERNS, [v for _, _, v in _banned_hits(content_text, atom_text)])


def _banned_hits(content_text: str, atom_text: str) -> list[tuple[str, str, str]]:
    """(phrase, its sentence, violation text) for each banned-pattern match, in gate_banned_patterns' order."""
    hits: list[tuple[str, str, str]] = []
    atom_lower = (atom_text or "").lower()
    # AA-778 — the dash of a testimonial attribution ('"…" <em-dash> Sarah, UK') is kept by fix_dashes on
    # purpose so F19 can flag and cut the invented review; F2 must not also hold it, or the piece loses
    # the F19 sentence repair and stays held on a stale F2 entry after the cut.
    f19_spans = authenticity.attribution_spans(content_text or "")
    for pattern in _BANNED_PATTERNS_COMPILED:
        for m in pattern.finditer(content_text or ""):
            phrase = m.group(0).lower()
            if phrase in atom_lower:
                continue
            if pattern.pattern == EM_DASH_PATTERN and any(s <= m.start() < e or s < m.end() <= e
                                                          for s, e in f19_spans):
                continue
            sentence = _sentence_with(content_text, m.group(0))
            if pattern.pattern == EM_DASH_PATTERN:  # AA-778 — readable, with its sentence, not the regex
                hits.append((m.group(0), sentence, f"uses an em-dash ('{m.group(0)}'); use a comma, a colon or a "
                                                   f"full stop: '{sentence}'"))
                continue
            hits.append((m.group(0), sentence, f"banned pattern /{pattern.pattern}/ -> '{m.group(0)}'"))
    return hits


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
    when the phrase is in the source itself, same carve-out as F2.
    AA-924 — a source check of the lexicon gate: the ledger holds it inside F2_lexicon (gate_lexicon)."""
    return _result(qr.F2_BRAND_FORBIDDEN, [v for _, _, v in _forbidden_hits(content_text, atom_text, forbidden_words)])


def forbidden_exempt(phrase: str, source_text: Optional[str]) -> bool:
    """The F2_brand_forbidden source exemption: a forbidden phrase the source holds (a plain, case-insensitive
    substring) is never flagged. One predicate for the gate and for the AA-918 R0 replay, so its counts match."""
    return phrase.strip().lower() in (source_text or "").lower()


def _forbidden_hits(content_text: str, atom_text: str,
                    forbidden_words: Optional[list[str]]) -> list[tuple[str, str, str]]:
    """(phrase, its sentence, violation text) for each tenant-forbidden word found, in the brand's list order."""
    hits: list[tuple[str, str, str]] = []
    for phrase in forbidden_words or []:
        phrase = phrase.strip()
        if len(phrase) < 2 or forbidden_exempt(phrase, atom_text):
            continue
        pattern = re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)", re.IGNORECASE)
        m = pattern.search(content_text or "")
        if m:
            sentence = _sentence_with(content_text, m.group(0))
            hits.append((m.group(0), sentence, f"uses '{m.group(0)}', which the brand forbids: '{sentence}'"))
    return hits


# ---------------------------------------------------------------- F2 word layers (AA-800)

F2_PLATFORM_GATE = qr.F2_PLATFORM_TRUTH
F2_SUPERLATIVE_GATE = qr.F2_SUPERLATIVES
F2_STYLE_WARN_GATE = qr.F2_STYLE_WARN


def _word_hits(text: str, words, exempt_text: str) -> list[tuple[str, str]]:
    """(word, sentence) for each sentence of `text` holding one of `words` that `exempt_text` (the seed,
    the CTA, the trip name) does not itself contain, in text order."""
    from services.acp_shared.signature_vocabulary import has_word
    exempt = (exempt_text or "").lower()
    out = []
    for sent in _SENT_SPLIT_RE.split(strip_citation_tags(text or "")):
        for w in words:
            if has_word(sent, w) and not has_word(exempt, w):
                out.append((w, sent.strip()[:300]))
    return out


def gate_platform_truth(content_text: str, atom_text: str,
                        own_forbidden: Optional[list[str]] = None) -> GateResultLite:
    """F2_platform_truth — blocking, sentence-repairable: a price or booking promise (cheap, deal,
    discount, book now, instant booking) the platform cannot back, for every tenant (AA-800 layer 1).
    T9 never checked these before. A word in the source (seed, CTA, trip name) is exempt, as for F2.
    AA-924 — a word on the tenant's own forbidden list (`own_forbidden`) is the lexicon gate's, never judged twice
    (gate_signature_words already skipped it)."""
    from services.acp_shared.signature_vocabulary import PLATFORM_TRUTH
    own = {str(w).lower().strip() for w in own_forbidden or []}
    violations = [f"uses '{w}', a price or booking promise the platform cannot back: '{sent}'"
                  for w, sent in _word_hits(content_text, [w for w in PLATFORM_TRUTH if w not in own], atom_text)]
    return _result(F2_PLATFORM_GATE, list(dict.fromkeys(violations)), repairable=True)


def gate_signature_words(content_text: str, atom_text: str,
                         brand_pack: Optional[dict]) -> list[GateResultLite]:
    """AA-800 layer 2 (AA style) for a tenant piece:
    - F2_superlatives — blocking (one sentence repair, never holds: STYLE_ONLY_GATES) when the piece has
      more than one stock superlative (pristine, stunning, breathtaking, magical, paradise); every one
      after the first is quoted;
    - F2_style_warn — warn only: a single stock superlative, or a positioning word (curated, refined,
      tailored, bespoke, luxury) the brand has not declared as its signature vocabulary.
    A word the source has is exempt. A word on the tenant's own list is F2_brand_forbidden's."""
    from services.acp_shared.signature_vocabulary import (STOCK_SUPERLATIVES, SUPERLATIVE_CAP,
                                                          undeclared_positioning)
    pack = brand_pack or {}
    own = {str(w).lower().strip() for w in pack.get("forbidden_words") or []}
    sup = [(w, sent) for w, sent in _word_hits(content_text, STOCK_SUPERLATIVES, atom_text) if w not in own]
    excess = sup[SUPERLATIVE_CAP:] if len(sup) > SUPERLATIVE_CAP else []
    over = [f"stock superlative '{w}' ({len(sup)} in this piece, at most {SUPERLATIVE_CAP}): say it with a "
            f"concrete detail from the seed or cut it: '{sent}'" for w, sent in excess]
    warn = [f"uses one stock superlative ('{w}'), allowed once per piece: '{sent}'"
            for w, sent in (sup if len(sup) == SUPERLATIVE_CAP else [])]
    undeclared = [w for w in undeclared_positioning(pack, include_prompt_only=True) if w not in own]
    warn += [f"uses '{w}', a positioning word this brand has not declared as its own: '{sent}'"
             for w, sent in _word_hits(content_text, undeclared, atom_text)]
    return [_result(F2_SUPERLATIVE_GATE, list(dict.fromkeys(over)), repairable=True, blocking=bool(over)),
            _result(F2_STYLE_WARN_GATE, list(dict.fromkeys(warn)), repairable=False, blocking=False)]


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
    return _result(qr.F11_BRAND_NAME, violations)


# ---------------------------------------------------------------- F19 authenticity (AA-819, AUP 2026-11)

F19_GATE = authenticity.GATE


def gate_authenticity(content_text: str, cta: Optional[str] = None, seo_title: Optional[str] = None,
                      meta_description: Optional[str] = None) -> GateResultLite:
    """F19_authenticity — blocking, sentence-repairable: a review, rating or customer quote, the
    customer / traveller speaking in the first person, or a claim of independence
    (authenticity.find_issues). Anthropic's usage policy from 12/11/2026 forbids fake reviews and
    personas and concealed sponsorship; the tenant publishes this piece under its own brand. Each
    violation quotes its sentence; a title / meta hit ("title …" / "meta …") is the SEO fit's. What a
    repair leaves is cut in code after the loop (service._enforce_authenticity) — a hold alone would
    still reach the tenant (AA-770). Admin-only evidence: the ledger never reaches a tenant."""
    return _result(F19_GATE, authenticity.find_issues(content_text, cta, seo_title=seo_title,
                                                      meta_description=meta_description), repairable=True)


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
        return _result(qr.F10_CANNIBALIZATION_CROSS_TENANT, [])
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
    return _result(qr.F10_CANNIBALIZATION_CROSS_TENANT, violations, repairable=True)


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
        return _result(qr.F4_EXTREME_LENGTH, [f"content is only {length} chars — effectively empty"])
    if length > max_chars:
        return _result(qr.F4_EXTREME_LENGTH, [f"content is {length} chars — far beyond any real channel example"])
    return _result(qr.F4_EXTREME_LENGTH, [])


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
        return _result(qr.F0_COMPLETE, ["the text ends with a stray backslash — it was cut off"])
    last = _last_prose_line(text)
    if last is not None and not _END_OK_RE.search(last):
        return _result(qr.F0_COMPLETE, [f"the text stops mid-sentence: '...{last[-80:]}'"])
    return _result(qr.F0_COMPLETE, [])


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


def title_key(title: Optional[str]) -> str:
    """S234 round 4 — a title compared across pieces: accent-folded words only ('Lake Khövsgöl:
    Mongolia's Family Adventure Hub' == 'lake khovsgol mongolias family adventure hub')."""
    from services.acp_content_writing.hygiene import fold
    return " ".join(re.findall(r"[a-z0-9]+", fold(title or "").replace("'", "").replace("’", "")))


def title_taken(title: Optional[str], taken_titles) -> bool:
    key = title_key(title)
    return bool(key) and any(key == title_key(t) for t in (taken_titles or ()))


def gate_seo_surface(
    seo_title: Optional[str], meta_description: Optional[str], slug: Optional[str],
    keyword: Optional[str], *, seed: Optional[str] = None, body: Optional[str] = None,
    taken_titles: Optional[list[str]] = None,
) -> GateResultLite:
    """AA-514 — ported from `gates/shape.py::seo_surface()` (thresholds verbatim from
    `gate-thresholds.toml`). Blog-only (dispatch in run_quality_gates(), not here — same
    "gate functions stay channel-agnostic" shape). `repairable=True` —
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
        # S234 round 4 (r3 panel §4 fix 5: #0 = #19) — the brand's earlier pieces on this channel
        if title_taken(seo_title, taken_titles):
            violations.append("SEO title is the same as an earlier piece of this brand — search results "
                              "would show two identical headlines")

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

    return _result(qr.F4_SEO_SURFACE, violations)


# ---------------------------------------------------------------- AA-505 judge call logging

def _log_t10_judge_call(raw: dict, *, gate: str, passed: bool, extra: dict) -> None:
    """Used by gate_brand_voice() below (gate_framework() shared it until AA-787). The gate function is
    deliberately channel/context-agnostic (this file's own module docstring), so no tenant identity is threaded
    down into them — the row's tenant comes from the running T9 job (call_log.bind_tenant,
    AA-741 G1). cost_usd is the gateway's own figure for the model that answered; pricing.calc_cost() is
    only the fallback when a reply carries none (an approximation — acceptable for an observability log,
    not used for billing)."""
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


def _log_t10_billed_unusable(raw: dict, *, error: str) -> None:
    """AA-927: the F9 judge reply was billed but no verdict came out of it (unparseable, or the
    post-parse code raised). One billed_unusable row: no `passed` / `status` (the ok-rate keys) and the
    gate under `f9_gate`, so F9 pass-rate readers do not count it."""
    from shared.llm_client.pricing import calc_cost
    model = raw.get("model_used", "unknown")
    in_tok, out_tok = raw.get("input_tokens", 0), raw.get("output_tokens", 0)
    cost = raw.get("cost_usd")
    record_billed_failure_sync(
        stage="t10_judge", role="judge", error=error,
        billed=[{"model": model, "tokens_in": in_tok, "tokens_out": out_tok,
                 "cost_usd": cost if cost is not None else calc_cost(model, in_tok, out_tok),
                 "stop_reason": raw.get("stop_reason"), "account": raw.get("account"),
                 "fallback_used": raw.get("fallback_used")}],
        quality_signal_extra={"f9_gate": qr.F9_BRAND_VOICE, "prompt_version": F9_PROMPT_VERSION})


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


# AA-786 — a quoted string of at least 12 characters inside the judge's free-text notes.
_NOTE_QUOTE_RE = re.compile(r"'([^']{12,})'|\"([^\"]{12,})\"|“([^”]{12,})”")
_SEO_LABEL_RE = re.compile(r"^\s*(?:search\s+)?(?:seo\s+)?(?:title|meta(?:\s+description)?)\s*:\s*", re.IGNORECASE)
F9_NO_REASON = "judge gave no checkable reason"


def _locate_in(text: str, quote: Optional[str]) -> Optional[str]:
    """AA-786 — the whole sentence of `text` the judge's quote points at (tag-stripped), or None when
    the quote matches no sentence (or is under 4 words). Same matcher the sentence repair uses, so a
    quote accepted here is always one plan() can repair."""
    from services.acp_content_writing.sentence_repair import locate_sentence  # circular at import time
    if not quote or not str(quote).strip():
        return None
    span = locate_sentence(text, str(quote))
    if span is None:
        return None
    return " ".join(strip_citation_tags(text[span[0]:span[1]]).split())


def _cmp_fold(text: Optional[str]) -> str:
    text = _SEO_LABEL_RE.sub("", strip_citation_tags(text or ""))
    return " ".join(re.sub(r"[^0-9a-z]+", " ", _fold(text)).split())


def _seo_field_of(sentence: str, seo_title: Optional[str], meta_description: Optional[str]) -> Optional[str]:
    """'title' / 'meta' when a claim's sentence is the search title or the meta description (or a part
    of it); None otherwise. Only asked after the sentence was NOT found in the body."""
    s = _cmp_fold(sentence)
    if len(s.split()) < 4:  # the same floor as sentence_repair.locate_sentence
        return None
    for prefix, field in (("title", seo_title), ("meta", meta_description)):
        f = _cmp_fold(field)
        if f and (s == f or s in f or f in s):
            return prefix
    return None


# AA-777 — the body sentence kinds an F9 verdict can pass (headings, FAQ questions and hashtag lines
# carry no claim of their own); the same 4-word floor as sentence_repair.locate_sentence.
_F9_PASS_KINDS = ("prose", "bullet", "faq_a")
_F9_PASS_MIN_WORDS = 4


# AA-777 (r6 fix 3) — a sentence that opens on a word that needs the sentence before it ('It was founded in
# 1692.', 'These are not performances…', 'Here, monks…'): only its key carries the previous sentence.
# r7 review — kept broad (a miss lets a claim the repaired neighbour no longer backs ship unjudged; a
# false match only costs one more judging): an optional leading conjunction ('But it…', 'And they…'),
# place / time words that point back ('There, …', 'Then …', 'Afterwards', 'Nearby', 'From there', 'After
# that', 'The next day') and any definite noun phrase ('The monastery was founded in 1586.').
_REFERENT_OPEN_RE = re.compile(
    r"^\W*(?:(?:and|but|yet|so|still|or)\s+)?"
    r"(?:it|its|it's|they|their|theirs|them|this|that|these|those|he|she|his|her|here|such|both|which|"
    r"there|then|afterwards?|later|nearby|inside|beyond|from\s+there|from\s+here|after\s+that|"
    r"after\s+this|the)\b", re.I)


def _f9_key(prev_clean: Optional[str], clean: str) -> str:
    """AA-777 — the sticky-pass key of one body sentence, case- and accent-folded but keeping symbols
    and punctuation: a new symbol ('12+' for '12') makes a new key, so the sentence is judged again (PR
    review on cc265e22). A sentence that opens on a referent word (`_REFERENT_OPEN_RE`) also carries the
    sentence before it (any kind, '' at the top): a repaired referent ('We visit X. It was founded in
    1692.') makes it new. r6 fix 3 — any other sentence is keyed on itself alone: r6 17125e0c 'After a
    short trek through forested hills, Day 2 brings you…' was passed at attempt 1, left unchanged, and
    flagged again at attempt 2 only because the hook before it had been repaired."""
    def f(t: Optional[str]) -> str:
        return " ".join(_fold(strip_citation_tags(t or "")).split())
    body = f(clean)
    return f"{f(prev_clean) if _REFERENT_OPEN_RE.match(body) else ''}\n{body}"


def _f9_quote_keys(content_text: str, quote: Optional[str]) -> Optional[list[str]]:
    """AA-777 — the sticky-pass keys of the body sentence(s) a judge quote points at, one per sentence
    (r6 fix 3: a quote spanning two sentences — r6 AA-768 89d60686 / 35ee6a3d — gives two keys instead of
    one two-sentence key that can never be in the passed set); None when it locates nowhere."""
    from services.acp_content_writing.sentence_repair import locate_sentence  # circular at import time
    if not quote or not str(quote).strip():
        return None
    span = locate_sentence(content_text, str(quote))
    if span is None:
        return None
    spans = sentence_spans(content_text)
    ks = [i for i, sp in enumerate(spans) if sp["start"] < span[1] and span[0] < sp["end"]]
    if not ks:
        return None
    return [_f9_key(spans[k - 1]["clean"] if k else None, spans[k]["clean"]) for k in ks]


def _f9_located_key(content_text: str, quote: Optional[str]) -> Optional[str]:
    """AA-777 — the sticky-pass key of the one body sentence a judge quote points at; None when it
    locates nowhere or spans more than one sentence (see `_f9_quote_keys`)."""
    keys = _f9_quote_keys(content_text, quote)
    return keys[0] if keys and len(keys) == 1 else None


def _f9_verdict_quotes(content_text: str, data: dict, phrases: list[str], seo_title: Optional[str],
                       meta_description: Optional[str]) -> tuple[set[str], list[str], bool]:
    """AA-777 — what one F9 verdict flagged: (flagged folds, loose note quotes, complete).
    - flagged folds: each unsupported claim's sentence (justified or not), each broken sentence, each
      flagged phrase, each note quote that is in the body — `_f9_passed_keys` skips a sentence
      containing one;
    - loose note quotes: a note quote found nowhere in the body (a paraphrase, or the SOURCE quoted
      back) — `_f9_passed_keys` also skips every sentence it copies (hygiene.copies, either way);
    - complete: False when a claim or broken-sentence quote locates in no body sentence (a paraphrase,
      or ambiguous between two) and is not the search title / meta. The verdict then flagged a
      sentence nobody can name, so it passes NOTHING (PR review on cc265e22: the paraphrased
      sentence went into the passed set and its exact re-flag was dropped as sticky)."""
    folds: set[str] = set()
    loose: list[str] = []
    complete = True
    quotes: list[str] = []
    for c in data.get("unsupported_claims") or []:
        if isinstance(c, dict):
            quotes.append(str(c.get("sentence") or c.get("claim") or ""))
    for b in data.get("broken_sentences") or []:
        quote = b.get("sentence") if isinstance(b, dict) else b if isinstance(b, str) else None
        if quote:
            quotes.append(str(quote))
    for quote in quotes:
        located = _locate_in(content_text, quote)
        if located:
            folds.add(_cmp_fold(located))
            continue
        folds.add(_cmp_fold(quote))
        if _seo_field_of(quote, seo_title, meta_description) is None:
            complete = False
    for m in _NOTE_QUOTE_RE.finditer(str(data.get("notes") or "")):
        quote = next(g for g in m.groups() if g)
        located = _locate_in(content_text, quote)
        folds.add(_cmp_fold(located or quote))
        if not located:
            loose.append(quote)
    folds |= {_cmp_fold(p) for p in phrases}
    return {f for f in folds if f}, loose, complete


def _f9_passed_keys(content_text: str, flagged: set[str], loose: list[str] = ()) -> set[str]:
    """AA-777 — the sticky-pass keys (`_f9_key`) of the body sentences an F9 verdict passed: every
    prose / bullet / FAQ-answer sentence of at least 4 words that is not flagged, contains no flagged
    phrase or quote, and copies no loose note quote."""
    out: set[str] = set()
    prev = None
    for sp in sentence_spans(content_text):
        clean, prev_clean = sp["clean"], prev
        prev = clean
        if sp["kind"] not in _F9_PASS_KINDS:
            continue
        f = _cmp_fold(clean)
        if len(f.split()) < _F9_PASS_MIN_WORDS or any(q and (q in f or f in q) for q in flagged):
            continue
        if any(copies(q, clean) or copies(clean, q) for q in loose):
            continue
        out.add(_f9_key(prev_clean, clean))
    return out


def _drop_sticky_claims(content_text: str, claims: list[dict],
                        passed: Optional[set[str]]) -> tuple[list[dict], list[str]]:
    """AA-777 — (claims kept, sentences dropped): a claim whose body sentence an earlier verdict of
    this job passed, unchanged and with the same sentence before it, is dropped. A claim in the search
    title / meta, or one whose quote matches no body sentence, is always kept."""
    if not passed or not claims:
        return claims, []
    kept, sticky = [], []
    for c in claims:
        quote = str(c.get("sentence") or c["claim"])
        keys = _f9_quote_keys(content_text, quote)
        if keys and all(k in passed for k in keys):  # r6 fix 3 — a multi-sentence quote: every sentence
            located = _locate_in(content_text, quote)
            if located not in sticky:
                sticky.append(located)
        else:
            kept.append(c)
    return kept, sticky


class F9PassMemory(set):
    """AA-865 — the job's sticky-pass set (`_f9_key` keys, as before) plus `last_text`: the tag-free body the last
    F9 verdict judged. `gate_brand_voice` carries a pass from that text to the current one through a repair edit
    that only deleted words (`_f9_carry_keys`), then sets `last_text`. A plain set keeps the AA-777 behaviour."""

    def __init__(self, *args) -> None:
        super().__init__(*args)
        self.last_text: Optional[str] = None


# AA-865 — words a deletion must never remove: without them the sentence makes a stronger claim ('optional boat
# rides' → 'boat rides', 'about 30 km' → '30 km', 'not open in winter' → 'open in winter', 'most visitors' →
# 'visitors', 'at least two' → 'two'). n't is read as 'not'.
_F9_KEEP_WORDS = frozenset("""not no never nor neither none nothing without cannot unless except only if whether may
might can could would should optional optionally option options choose chosen possible possibly perhaps maybe likely
usually often sometimes occasionally typically generally mostly around about approximately roughly nearly almost
some several up until estimated estimate said reportedly believed thought according traditionally permitting
depending depends subject request most many few just mainly partly least each per every all""".split())
# AA-865 — adverbs a style repair may drop from mid-sentence without re-attaching anything.
_F9_FILLER_WORDS = frozenset("""also very truly really quite simply particularly especially genuinely deeply rather
still even indeed certainly entirely fully well so too""".split())
_F9_WORD_RE = re.compile(r"[0-9a-z]+")
_F9_BREAK_CHARS = frozenset(",;:()[]—–-")
_F9_PLAIN_CHARS = frozenset(".,;:!?'\"()[]-–—’‘“”…")


def _f9_words(text: str) -> list[tuple[str, bool]]:
    """(folded word, is a punctuation break before it) for each word of `text`; n't → not."""
    t = re.sub(r"n['’]t\b", " not", _fold(" ".join(strip_citation_tags(text or "").split())))
    out: list[tuple[str, bool]] = []
    pos = 0
    for m in _F9_WORD_RE.finditer(t):
        out.append((m.group(), any(c in _F9_BREAK_CHARS for c in t[pos:m.start()])))
        pos = m.end()
    return out


def _f9_deletion_only(old: str, new: str) -> bool:
    """AA-865 — True when `new` is `old` with words removed and nothing else: its words are old's words in the
    same order and every removed run is only filler adverbs or ends at a punctuation break: a start run needs a
    break before the first kept word ('Unlike the southern route, the drive…'), an end run a break before the
    first removed word ('…of 1937: a single, spare reminder…'), a middle run a break on both sides (an appositive
    or a parenthetical). No negation / hedge / option / quantifier word is removed and no symbol is new.
    A run cut at a break drops a whole phrase or clause; a run cut without one can drop a qualifier and broaden
    the claim ('Lunch is included on most days of the trek.' → 'Lunch is included.', 'The museum is open daily in
    summer.' → '…open daily.'), so that is judged again. With these limits an F9 pass of `old` still holds for
    `new` (r6: '…of 1937: a single, spare reminder that…' → '…of 1937.' was flagged again only because it
    changed)."""
    if _cmp_fold(old) == _cmp_fold(new):
        return True
    ow, nw = _f9_words(old), _f9_words(new)
    if not nw or len(nw) >= len(ow):
        return False
    if any(c not in _F9_PLAIN_CHARS and not c.isalnum() and not c.isspace() and c not in old for c in new):
        return False  # a new symbol ('12+', '%', '$') is a new claim
    o_words, n_words = [w for w, _ in ow], [w for w, _ in nw]
    if not set(n_words) <= set(o_words):
        return False  # a new word (cheap check before the alignment below)
    for w in _F9_KEEP_WORDS & set(o_words):
        if n_words.count(w) < o_words.count(w):
            return False
    n, m = len(ow), len(nw)

    def run_ok(i: int, k: int) -> bool:  # old words [i, k) removed, 0 <= i < k <= n
        if all(w in _F9_FILLER_WORDS for w in o_words[i:k]):
            return True
        if i == 0:
            return ow[k][1]  # start run: a break before the first kept word
        if k == n:
            return ow[i][1]  # end run: a break before the first removed word
        return ow[i][1] and ow[k][1]  # a break before the run and one right after it

    from functools import lru_cache

    @lru_cache(maxsize=None)
    def fits(i: int, j: int) -> bool:  # new[j:] can be matched in old[i:]
        if j == m:
            return i == n or run_ok(i, n)  # whatever is left of old is an end run
        for k in range(i, n):
            if o_words[k] == n_words[j] and (k == i or run_ok(i, k)) and fits(k + 1, j + 1):
                return True
        return False

    return fits(0, 0)


def _f9_antecedent_kept(sentence: str, old_prev: Optional[str], new_prev: Optional[str]) -> bool:
    """AA-865 — may a pass of `sentence` survive its previous sentence changing from `old_prev` to `new_prev`?
    Yes when it does not open on a referent word (`_f9_key` keys it alone anyway), when the previous sentence is
    the same or a deletion-only edit of the old one, or when it opens on a definite noun phrase ('The sacred cave
    at the heart of the site…', not a pronoun) and every content word it shared with the old previous sentence is
    still in the new one. A pronoun opener after a rewritten neighbour ('It was founded in 1692.') is judged again."""
    body = " ".join(_fold(strip_citation_tags(sentence or "")).split())
    if not _REFERENT_OPEN_RE.match(body):
        return True
    if _cmp_fold(old_prev) == _cmp_fold(new_prev):
        return True
    if old_prev and new_prev and _f9_deletion_only(old_prev, new_prev):
        return True
    opener = re.match(r"^\W*(?:(?:and|but|yet|so|still|or)\s+)?(\w+)", body)
    if not opener or opener.group(1) != "the":
        return False
    shared = set(content_words(sentence)) & set(content_words(old_prev or ""))
    return shared <= set(content_words(new_prev or ""))


def _f9_carry_keys(old_text: str, new_text: str, passed: set[str]) -> set[str]:
    """AA-865 — the sticky-pass keys a repair edit carries from `old_text` (the body the last F9 verdict judged) to
    `new_text`: for each prose / bullet / FAQ-answer sentence of `new_text` (>= 4 words) whose key is not passed
    yet, the old sentence it comes from (difflib alignment of the two sentence lists) must have a passed key, the
    new sentence must be that sentence or a deletion-only edit of it (`_f9_deletion_only`), and a referent opener
    must keep its antecedent (`_f9_antecedent_kept`). r7: an unchanged 'The sacred cave at the heart of the site…'
    got a new key when the hook before it was rewritten, and F9 flagged it again."""
    import difflib

    old = sentence_spans(old_text or "")
    new = sentence_spans(new_text or "")
    if not old or not new or not passed:
        return set()
    sm = difflib.SequenceMatcher(None, [_cmp_fold(s["clean"]) for s in old], [_cmp_fold(s["clean"]) for s in new],
                                 autojunk=False)
    sources: dict[int, list[int]] = {}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for d in range(j2 - j1):
                sources[j1 + d] = [i1 + d]
        elif tag == "replace":
            for j in range(j1, j2):
                sources[j] = list(range(i1, i2))
    out: set[str] = set()
    for j, sp in enumerate(new):
        if sp["kind"] not in _F9_PASS_KINDS or len(_cmp_fold(sp["clean"]).split()) < _F9_PASS_MIN_WORDS:
            continue
        new_prev = new[j - 1]["clean"] if j else None
        key = _f9_key(new_prev, sp["clean"])
        if key in passed:
            continue
        for i in sources.get(j, []):
            old_prev = old[i - 1]["clean"] if i else None
            if (_f9_key(old_prev, old[i]["clean"]) in passed and _f9_deletion_only(old[i]["clean"], sp["clean"])
                    and _f9_antecedent_kept(sp["clean"], old_prev, new_prev)):
                out.add(key)
                break
    return out


def gate_brand_voice(
    content_text: str, cta: str, brand_rubric_text: str, source_text: Optional[str] = None,
    voice: Optional[list[str]] = None, seo_title: Optional[str] = None,
    meta_description: Optional[str] = None, passed_sentences: Optional[set[str]] = None,
) -> list[GateResultLite]:
    """F9 (see `_gate_brand_voice`). AA-927: when the judge reply came back (billed) and the verdict code
    then raised, its llm_call_log row is written here (billed_unusable) before the error propagates."""
    ledger: dict = {"raw": None, "logged": False}
    try:
        return _gate_brand_voice(content_text, cta, brand_rubric_text, source_text=source_text, voice=voice,
                                 seo_title=seo_title, meta_description=meta_description,
                                 passed_sentences=passed_sentences, _ledger=ledger)
    except Exception as e:
        if ledger["raw"] is not None and not ledger["logged"]:
            _log_t10_billed_unusable(ledger["raw"], error=f"{type(e).__name__}: {e}")
        raise


def _gate_brand_voice(
    content_text: str, cta: str, brand_rubric_text: str, source_text: Optional[str] = None,
    voice: Optional[list[str]] = None, seo_title: Optional[str] = None,
    meta_description: Optional[str] = None, passed_sentences: Optional[set[str]] = None,
    _ledger: Optional[dict] = None,
) -> list[GateResultLite]:
    """AA-450-02 gate map, F9 row — see module docstring for the rubric-field/failure-code
    choices. `cta_clear` also absorbs N7's dropped F6 literal-CTA-substring check (semantic
    judgment, since T9's CTA is a free-text tenant value, not N7's one fixed brand phrase — no
    hardcoded-phrase false-positive carve-out ports as-is for this reason).

    AA-613 — returns a LIST of two results from ONE judge call (Nghiệp's 3-tier severity split):
    `F9_cta_fact` (blocking, product-truth: CTA/FACT_CHECK/TEXT_INTEGRITY) and `F9_brand_style`
    (non-blocking warn: off-brand voice / generic AI wording). See the split logic below.

    AA-768 — `source_text` (the writer's own source: seed + Facts + audience/trip lines, the same
    text gate_grounding() checks against) is shown to the judge. Before this the judge saw only
    the piece and was told "when uncertain about a factual claim: manual_check" — with nothing to
    check against, every fact-bearing piece came back FACT_CHECK_MANUAL_CHECK and no rewrite
    could fix it (S231 pilot: 5/5 held). The judge must now quote the claim it flags, so the
    repair round gets a sentence to fix instead of a bare code. `None` keeps the old prompt for
    any caller without a source.

    AA-786 (F9 v1.1, S234 gate evaluation):
    - the piece is judged WITHOUT citation tags (the judge flagged "visible citation markers" the
      tenant never sees — r1#9, r3#4);
    - TEXT_INTEGRITY quotes each broken sentence (`broken_sentences`) → "TEXT_INTEGRITY: '<sentence>'",
      repairable by sentence_repair.plan instead of the full rewrite that fixed F9 0/6 times;
    - a non-pass verdict with no code: a sentence quoted in the notes that exists in the piece
      becomes TEXT_INTEGRITY; with no locatable quote the F9 result is a NON-blocking warn
      (F9_NO_REASON) — a verdict without a reason is not a hold (Gate-1 policy, AA-770 precedent);
    - `seo_title` / `meta_description` (blog) are shown to the judge; an unsupported claim in them is
      a 'title …' / 'meta …' violation that never blocks on its own (F17's body_blocking rule).

    AA-777 (sticky pass) — `passed_sentences` is the job's set of body sentences (keyed with
    `_f9_key`: the sentence plus the one before it) an earlier F9 verdict of the same piece judged
    without a flag. An unsupported claim whose sentence is in that set is dropped (S234: in 10/15
    reverted pieces the judge flagged a sentence it had passed one attempt earlier, unchanged); dropped
    sentences are logged (`F9_sticky_pass`) and listed as `sticky_pass` on the F9_cta_fact result. After the verdict the
    sentences it passed are ADDED to the set (the caller owns it, one per job). A verdict with no
    checkable reason, no verdict (judge unavailable), a truncated / json_repair-salvaged verdict, or
    one with a claim or broken-sentence quote that locates in no body sentence adds nothing. A sticky
    sentence quoted in the notes never becomes TEXT_INTEGRITY.

    AA-865 — when `passed_sentences` is an `F9PassMemory`, the passes of the text the last verdict judged are first
    carried through this attempt's repair edit (`_f9_carry_keys`: deletion-only edits, and unchanged sentences whose
    referent opener kept its antecedent), so a retry does not judge again what an earlier verdict passed and the
    repair did not change. A plain set, and attempt 1, behave as before."""
    content_text = strip_citation_tags(content_text)  # AA-786 — defence in depth; the caller strips too
    has_seo = bool((seo_title or "").strip() or (meta_description or "").strip())
    contract = json.dumps({
        "status": "pass|flagged|manual_check",
        **{f: "1|0" for f in _BRAND_VOICE_FIELDS},
        "failure_codes": [f"subset of {_BRAND_VOICE_FAILURE_CODES}"],
        "flagged_phrases": ["exact quoted phrase for each SUMMARY_OFF_BRAND/GENERIC_AI_WORDING/"
                             "FACT_CHECK_MANUAL_CHECK code above — [] if none is used"],
        # AA-786 — one entry per TEXT_INTEGRITY problem.
        "broken_sentences": [{"sentence": "exact sentence from the piece", "problem": "what is broken"}],
        "notes": "str",
        # AA-768 round 4 — only used when a SOURCE is given (see below).
        **({"unsupported_claims": [{"sentence": "exact sentence from the piece"
                                                + (" (or the search title / meta description)" if has_seo else ""),
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
    seo_block = ""
    if has_seo:
        seo_block = (
            "SEARCH TITLE AND META DESCRIPTION (shown in search results; check their facts like the "
            "body, and for a claim in them quote the title or meta itself as the sentence):\n"
            f"Title: {seo_title or ''}\nMeta description: {meta_description or ''}\n\n")
    integrity_rule = (
        "TEXT_INTEGRITY: use it ONLY for a sentence that is broken as text — garbled or "
        "ungrammatical so it cannot be read as meant, the call to action written twice, visible "
        "markup, a sentence cut off mid-thought. List each one in broken_sentences with the exact "
        "sentence from the piece and what is broken. Never use it for style, tone or facts. "
        "Whenever status is not pass, every reason must be one of the failure codes with its "
        "quoted sentence or phrase — notes alone never explain a fail."
    )
    user_prompt = (
        f"PIECE:\n{content_text}\n\n"
        f"{seo_block}"
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
        f"on its own — do not flag it. {fact_rule}\n{integrity_rule}\n\n"
        f"Output ONLY JSON matching this contract:\n{contract}"
    )
    try:
        raw = invoke_judge(_JUDGE_SYSTEM_PROMPT, user_prompt, max_tokens=_F9_MAX_TOKENS, stage="t10_judge")
        _ledger = _ledger if _ledger is not None else {}
        _ledger["raw"] = raw
        data, repaired = parse_judge_json_checked(raw["text"])
        if not isinstance(data, dict):  # a JSON list / scalar is no verdict
            raise ValueError(f"judge reply is a JSON {type(data).__name__}, not an object")
    except Exception as e:
        logger.warning("t10_f9_judge_unavailable", error=str(e))
        if _ledger and _ledger.get("raw") is not None:
            # AA-927: the reply was billed — log it (no verdict keys) before degrading.
            _log_t10_billed_unusable(_ledger["raw"], error=str(e))
            _ledger["logged"] = True
        # Fail-closed on the BLOCKING (product-truth) side; brand-style passes (no signal to
        # flag on). AA-613 — same two-result shape as the success path below.
        # AA-770 (S233) — an outage is not a verdict: never a blocking gate (no rewrite on it).
        # AA-925 — but it is a hard fail-open detector: the result carries the `degraded` marker, and
        # service._degraded_final labels the piece held when the FINAL attempt has it (admin-only label;
        # the tenant still receives the piece).
        unavailable = _result(qr.F9_CTA_FACT, [f"judge unavailable: {e} — not checked"], repairable=False,
                              blocking=False)
        unavailable["degraded"] = "f9_judge_unavailable"
        return [unavailable, _result(qr.F9_BRAND_STYLE, [], repairable=False, blocking=False)]

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
    # AA-777 — what this verdict flagged (folded), read BEFORE the sticky filter: these sentences are
    # never added to the passed set below.
    flagged_folds, loose_quotes, quotes_complete = _f9_verdict_quotes(
        content_text, data, phrases, seo_title, meta_description)
    # AA-865 — carry the passes of the last judged text through this attempt's repair edit (deletion-only
    # sentences, unchanged sentences whose referent opener kept its antecedent) before the sticky filter.
    prev_text = getattr(passed_sentences, "last_text", None)
    if passed_sentences and prev_text and prev_text != content_text:
        carried = _f9_carry_keys(prev_text, content_text, passed_sentences)
        if carried:
            passed_sentences |= carried
            logger.info(qr.F9_STICKY_CARRY, carried=len(carried))
    if isinstance(passed_sentences, F9PassMemory):
        passed_sentences.last_text = content_text
    claims, sticky = _drop_sticky_claims(content_text, claims, passed_sentences)
    if sticky:
        logger.info(qr.F9_STICKY_PASS, dropped=len(sticky), sentences=[s[:160] for s in sticky[:5]])
        if not claims and "FACT_CHECK_MANUAL_CHECK" in failure_codes:
            failure_codes = [c for c in failure_codes if c != "FACT_CHECK_MANUAL_CHECK"]
            if not failure_codes and status != "pass":
                status = "pass"  # every justified claim was a sentence an earlier verdict passed
    # AA-786 — a claim whose sentence is not in the body but is the search title / meta is owned by
    # the SEO fields: it is reported separately and never blocks on its own.
    body_claims, field_viol = [], []
    for c in claims:
        sentence = str(c.get("sentence") or c["claim"])
        # PR #709 review B1 — a claim that is in the body at all (located, or a short / paraphrased quote whose
        # words still appear in it) stays a blocking body claim even when the title or meta repeats it.
        in_body = bool(_locate_in(content_text, sentence)) or (
            bool(_cmp_fold(sentence)) and _cmp_fold(sentence) in _cmp_fold(content_text))
        field = None if in_body else _seo_field_of(sentence, seo_title, meta_description)
        if field is None:
            body_claims.append(c)
        else:
            field_viol.append(f'{field} unsupported claim: "{sentence}" (claims: {c["claim"]}; '
                              f'source: {c["why_not_in_source"]})')
    # AA-786 — TEXT_INTEGRITY counts only for a broken sentence the piece really contains.
    integrity: list[str] = []
    if "TEXT_INTEGRITY" in failure_codes:
        for b in data.get("broken_sentences") or []:
            quote = b.get("sentence") if isinstance(b, dict) else b if isinstance(b, str) else None
            located = _locate_in(content_text, quote)
            if located and located not in integrity:
                integrity.append(located)

    # AA-613 — split the single F9 judge call into TWO gate results by failure class, so the
    # T10 loop can treat them differently (Nghiệp's 3-tier severity model):
    #   - F9_cta_fact  (BLOCKING / product-truth): CTA_NOT_REFLECTED, FACT_CHECK_MANUAL_CHECK,
    #     TEXT_INTEGRITY (AA-786). A missing/unclear CTA, an unverified factual claim or a broken
    #     sentence is a contract/truth failure — it still triggers a retry and still gates publish.
    #   - F9_brand_style (NON-BLOCKING / warn): SUMMARY_OFF_BRAND, GENERIC_AI_WORDING. Off-brand
    #     voice or generic AI wording is a quality note, not a reason to withhold the piece from
    #     the tenant (ships flagged, per ADR 0023 flag-not-block).
    # One judge call, two results — the judge already returns per-code failure_codes.
    _PRODUCT_TRUTH_CODES = {"CTA_NOT_REFLECTED", "FACT_CHECK_MANUAL_CHECK"}
    _BRAND_STYLE_CODES = {"SUMMARY_OFF_BRAND", "GENERIC_AI_WORDING"}
    judge_ok = status == "pass"

    def _viol(codes: set[str]) -> list[str]:
        hit = [c for c in failure_codes if c in codes]
        if claims and not body_claims:  # AA-786 — every justified claim is in the title / meta
            hit = [c for c in hit if c != "FACT_CHECK_MANUAL_CHECK"]
        if not hit:
            return []
        reason = ", ".join(hit)
        if body_claims and "FACT_CHECK_MANUAL_CHECK" in hit:
            return [f"audit {status}: {reason} — unsupported claim(s): " + "; ".join(
                f'"{c.get("sentence") or c["claim"]}" (claims: {c["claim"]}; source: {c["why_not_in_source"]})'
                for c in body_claims)]
        # AA-768 — FACT_CHECK quotes its claim too, so the rewrite knows which sentence to fix.
        if phrases and set(hit) & (_BRAND_STYLE_CODES | {"FACT_CHECK_MANUAL_CHECK"}):
            reason += " — exact flagged phrase(s): " + "; ".join(f'"{p}"' for p in phrases)
        return [f"audit {status}: {reason}"]

    cta_fact_viol = _viol(_PRODUCT_TRUTH_CODES) + [f"TEXT_INTEGRITY: '{s}'" for s in integrity] + field_viol
    brand_style_viol = _viol(_BRAND_STYLE_CODES)
    # A non-pass status whose reasons all fell away (no recognised code, an unquoted TEXT_INTEGRITY,
    # notes only). AA-786: a sentence of the piece quoted in the notes becomes TEXT_INTEGRITY (the
    # repair gets a sentence); without one the verdict has no checkable reason → a NON-blocking warn,
    # never a hold (S234: 4 notes-only holds no rewrite could fix).
    reasonless = False
    if not judge_ok and not cta_fact_viol and not brand_style_viol:
        quoted: list[str] = []
        for m in _NOTE_QUOTE_RE.finditer(notes):
            quote = next(g for g in m.groups() if g)
            located = _locate_in(content_text, quote)
            # AA-777 — a sticky sentence (dropped above, or passed by an earlier verdict) quoted in the
            # notes is not a broken sentence: never turn it back into a blocking TEXT_INTEGRITY item.
            if located and (located in sticky or (passed_sentences and _f9_located_key(content_text, quote)
                                                  in passed_sentences)):
                continue
            if located and located not in quoted:
                quoted.append(located)
        if quoted:
            cta_fact_viol = [f"TEXT_INTEGRITY: '{s}'" for s in quoted]
        else:
            reasonless = True
            cta_fact_viol = [f"{F9_NO_REASON} (audit {status}: "
                             f"{', '.join(failure_codes) or notes or '(no reason given)'}) — not held"]
    cta_fact_blocking = not reasonless and (
        not cta_fact_viol or any(not v.startswith(("title ", "meta ")) for v in cta_fact_viol))

    _log_t10_judge_call(raw, gate=qr.F9_BRAND_VOICE, passed=judge_ok,
                         extra={"status": status, "failure_codes": failure_codes,
                                "prompt_version": F9_PROMPT_VERSION,
                                "piece_tag_free": not TAG_RE.search(content_text),
                                "reasonless": reasonless, "sticky_pass": len(sticky)})
    _ledger["logged"] = True  # AA-927
    # AA-777 — a verdict adds what it passed only when it is whole and every flag it raised points at a
    # body sentence: not reasonless, not salvaged by json_repair or cut at max_tokens (the entries
    # after the cut would read as passed), and no claim / broken-sentence quote that locates nowhere.
    truncated = repaired or str(raw.get("stop_reason") or "").lower() in ("length", "max_tokens")
    if passed_sentences is not None and not reasonless and not truncated and quotes_complete:
        passed_sentences |= _f9_passed_keys(content_text, flagged_folds | {_cmp_fold(q) for q in integrity},
                                            loose_quotes)
    elif passed_sentences is not None and not reasonless:
        logger.info(qr.F9_STICKY_PASS_SKIPPED, truncated=truncated, unlocated_quote=not quotes_complete)
    cta_fact = _result(qr.F9_CTA_FACT, cta_fact_viol, blocking=cta_fact_blocking, repairable=not reasonless)
    if sticky:
        cta_fact = {**cta_fact, "sticky_pass": sticky}
    return [
        cta_fact,
        _result(qr.F9_BRAND_STYLE, brand_style_viol, repairable=False, blocking=False),
    ]


# ---------------------------------------------------------------- FAQ restates the body (F7 fixer, blog only)

_FAQ_ANSWER_RE = re.compile(r"\*\*Q: .*?\*\*\s*\nA: (.*?)(?=\n\*\*Q: |\Z)", re.DOTALL)
_FAQ_PAIR_RE = re.compile(r"\*\*Q: .*?\*\*\s*\nA: .*?(?=\n\*\*Q: |\Z)", re.DOTALL)


def _body_sentences(pre_faq: str) -> list[str]:
    """What a FAQ answer sentence may not copy: every prose / bullet sentence of the body, and every
    two adjacent ones joined (an answer that merges two body sentences into one, S234 r2 review)."""
    sents = [s["clean"] for s in sentence_spans(pre_faq or "") if s["kind"] in ("prose", "bullet")]
    return sents + [f"{a} {b}" for a, b in zip(sents, sents[1:])]


# What is left of an answer after its copied sentences are cut must still answer the question.
_FAQ_REST_MIN_WORDS = 4


def strip_copied_sentences(answer: str, body_sentences: list[str]) -> tuple[Optional[str], int]:
    """AA-768 (S234 round 2 fix) — `answer` with every sentence that copies the body cut
    (hygiene.copies against one body sentence or two adjacent ones, `_body_sentences`). Returns
    (answer, sentences cut); the answer is None when what is left has fewer than 4 content words
    (the pair goes). An answer that retells a body fact in its own words keeps every sentence
    (product rule: a FAQ may restate a body fact to answer a reader's question; S233 F7 bans copies)."""
    text = answer or ""
    spans = [sp for sp in sentence_spans(text) if sp["clean"].strip()]
    is_cut = [any(copies(sp["clean"], b) for b in body_sentences) for sp in spans]
    cut = [sp for sp, c in zip(spans, is_cut) if c]
    if not cut:
        return text, 0
    # A kept sentence right after a cut one that leans on it ('It also holds…', 'This is a formal
    # requirement…') has lost its referent: the pair goes.
    if any(not c and i and is_cut[i - 1] and dangling_opener(spans[i]["clean"]) for i, c in enumerate(is_cut)):
        return None, len(cut)
    for sp in reversed(cut):
        text = text[:sp["start"]] + text[sp["end"]:]
    text = "\n".join(" ".join(line.split()) for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(content_words(text)) < _FAQ_REST_MIN_WORDS:
        return None, len(cut)
    return text, len(cut)


def answer_copies_body(answer: str, body_sentences: list[str]) -> bool:
    """AA-768 (S234 round 2 fix) — does any sentence of a FAQ answer copy the body
    (strip_copied_sentences would cut it). `body_sentences` comes from `_body_sentences`.

    Replaces the S233 test (answer words of 5+ letters found ANYWHERE in the body > 85%), which in a
    seed-bound blog flags every answerable question — the body already uses the seed's vocabulary —
    and so stripped 3-4 pairs per blog (S234 r1 F7 flags; r2 F16_faq held 7/8)."""
    return strip_copied_sentences(answer, body_sentences)[1] > 0


def restated_faq_answers(content_text: str) -> list[int]:
    """1-based numbers of the FAQ answers that copy a body sentence of the SAME piece (answer_copies_body,
    the test drop_restated_faq cuts on). [] when the piece has no '## FAQ' section.

    AA-787 — this was the F7_faq_dedup gate; F16_faq absorbed it. The pipeline runs drop_restated_faq on
    every attempt before the gates read the text, so a non-empty result after that fixer is a fixer bug,
    not a writer defect: tests use this to prove the fixer leaves nothing behind."""
    body = content_text or ""
    if "## FAQ" not in body:
        return []
    pre, _, faq = body.partition("## FAQ")
    sentences = _body_sentences(pre)
    return [i + 1 for i, m in enumerate(_FAQ_ANSWER_RE.finditer(faq))
            if answer_copies_body(m.group(1).strip(), sentences)]


def drop_restated_faq(content_text: str) -> tuple[str, int]:
    """AA-770 (S233) — F7 auto-fix instead of holding or rewriting the whole piece: every FAQ answer
    sentence that copies the body is cut (strip_copied_sentences); a pair whose answer has too little
    left, or none at all (a repair deleted it), goes. Drops the "## FAQ" heading too when no pair is
    left. Returns (text, pairs removed or trimmed)."""
    body = content_text or ""
    if "## FAQ" not in body:
        return body, 0
    pre, _, faq = body.partition("## FAQ")
    sentences = _body_sentences(pre)
    kept, changed = [], 0
    for m in _FAQ_PAIR_RE.finditer(faq):
        pair = m.group(0).strip()
        answer = _FAQ_ANSWER_RE.match(m.group(0))
        if not answer:
            kept.append(pair)
            continue
        if not strip_citation_tags(answer.group(1)).strip():
            # A sentence repair that deleted the only answer sentence leaves 'A: ' under its
            # question (S234 r2 review) — the pair goes, so F16_faq counts what a reader gets.
            changed += 1
            continue
        rest, cut = strip_copied_sentences(answer.group(1), sentences)
        if not cut:
            kept.append(pair)
            continue
        changed += 1
        if rest is not None:
            kept.append((m.group(0)[:answer.start(1)] + rest).strip())
    if not changed:
        return body, 0
    if not kept:
        return pre.rstrip() + "\n", changed
    return pre + "## FAQ\n\n" + "\n\n".join(kept) + "\n", changed


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
    grounding text with AUDIENCE/BRAND. Every answerable pair stays; the "## FAQ" heading goes only
    when none is left (S234 round 2 fix: the FAQ is required now — F16_faq — so a lone answerable
    pair is kept for the contract step to build on, not thrown away). Returns (text, one log line
    per dropped pair)."""
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
    if not kept:
        return pre.rstrip() + "\n", log
    return pre + "## FAQ\n\n" + "\n\n".join(kept) + "\n", log


# ---------------------------------------------------------------- F16 FAQ contract (AA-768, S234 round 2)

# S234 panel §2 class 4: 6/8 blogs ended with no FAQ (#2, #17) or one pair (#1, #16, #18, #19) —
# the single deciding structural defect of those blogs. A blog must end on "## FAQ" with at least
# FAQ_MIN_PAIRS pairs, counted AFTER drop_restated_faq / drop_unanswerable_faq ran. service.py
# regenerates a short FAQ once (generate.write_faq, t9_repair) before the gates read the text.
FAQ_MIN_PAIRS = 3
F16_GATE = qr.F16_FAQ


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


# The most pairs a merged FAQ keeps (the writer's surviving pairs first, then the regenerated ones).
FAQ_MAX_PAIRS = 5
_FAQ_QA_RE = re.compile(r"\*\*Q:\s*(.*?)\s*\*\*\s*\nA:\s*(.*)", re.DOTALL)


def faq_pairs(content_text: str) -> list[tuple[str, str]]:
    """The (question, answer) pairs under "## FAQ", markup removed (the shape replace_faq_section takes).
    An answer is its first paragraph: prose after the last pair (a CTA) is not part of it —
    replace_faq_section moves that above the FAQ."""
    body = content_text or ""
    if "## FAQ" not in body:
        return []
    out = []
    for m in _FAQ_PAIR_RE.finditer(body.partition("## FAQ")[2]):
        qa = _FAQ_QA_RE.match(m.group(0).strip())
        if qa:
            out.append((qa.group(1).strip(), re.split(r"\n\s*\n", qa.group(2).strip(), maxsplit=1)[0].strip()))
    return out


def drop_faq_pairs(content_text: str, drop: set[int]) -> tuple[str, list[int]]:
    """AA-779 (S247) — the text without the FAQ pairs numbered in `drop` (1-based, faq_pairs order); every other
    part of the text stays verbatim and in place. A dropped pair's whole block goes (its question, its answer and
    any further paragraph under it), the way drop_unanswerable_faq cuts blocks — NOT replace_faq_section, which
    rebuilds the FAQ from first paragraphs and moves every other paragraph above "## FAQ" (S247 review: a dropped
    wrong-subject answer's second paragraph landed in the body). One exception: the LAST block runs to the end of
    the text, and prose there may be a CTA written after the FAQ, so a last pair holding more than one paragraph
    is kept rather than guessed at. "## FAQ" goes when no pair is left. Returns (text, the numbers in `drop` that
    were kept for that reason)."""
    body = content_text or ""
    if not drop or "## FAQ" not in body:
        return body, []
    pre, _, faq = body.partition("## FAQ")
    numbered = [m for m in _FAQ_PAIR_RE.finditer(faq) if _FAQ_QA_RE.match(m.group(0).strip())]
    cuts, skipped = [], []
    for n, m in enumerate(numbered, 1):
        if n not in drop:
            continue
        paragraphs = [p for p in re.split(r"\n\s*\n", m.group(0).strip()) if p.strip()]
        if m.end() == len(faq) and len(paragraphs) > 1:
            skipped.append(n)
            continue
        end = m.end()
        while end < len(faq) and faq[end].isspace():  # the blank line after the block goes with it
            end += 1
        cuts.append((m.start(), end))
    if not cuts:
        return body, skipped
    out, pos = [], 0
    for start, end in cuts:
        out.append(faq[pos:start])
        pos = end
    out.append(faq[pos:])
    rest = "".join(out)
    if not _FAQ_PAIR_RE.search(rest):
        return pre.rstrip() + "\n", skipped
    return (pre + "## FAQ" + rest).rstrip() + "\n", skipped


def _same_question(a: tuple[str, str], b: tuple[str, str]) -> bool:
    qa, qb = set(content_words(a[0])), set(content_words(b[0]))
    if qa and qa == qb:
        return True
    if qa and qb and len(qa & qb) / len(qa | qb) >= 0.6:
        return True
    return near_verbatim(a[1], b[1])


def merge_faq_pairs(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """AA-768 (S234 r2 review) — `pairs` in order (the writer's surviving pairs first, then the
    regenerated ones), without a pair that asks an earlier question again (same question words,
    ≥ 0.6 overlap, or a near-verbatim answer). Run it AFTER the droppers, so a dropped earlier pair
    never shadows its kept twin."""
    out: list[tuple[str, str]] = []
    for pair in pairs:
        if pair[0].strip() and pair[1].strip() and not any(_same_question(pair, o) for o in out):
            out.append(pair)
    return out


# ---------------------------------------------------------------- FAQ subject check + PAA (AA-779, S245)

# r4/r5 panels: FAQ answers that never name what the question asks about — 'Who are the Tsaatan, and
# where do they live?' → 'Their seasonal camps…' (r4 #5), 'What can we do at Lake Khövsgöl beyond
# trekking?' → 'These activities are available…' (r5 #5), 'What is the role of Mörön on this route?' →
# 'The route stops here for lunch…' (r5 #15), 'What animals do the Tsaatan use their reindeer for?' →
# '…meat production is not the primary purpose.' (r5 #18) — and a negation-only answer that names the
# subject but still dodges the question ('What do the Tsaatan use their reindeer for?' → '…the Tsaatan
# do not primarily raise them for meat.', r4 #19). Deterministic: no model call.
_SUBJ_TOKEN_RE = re.compile(r"[a-z0-9]+")
_WH_WORDS = frozenset("what which who whom whose where when why how".split())
_WH_QUESTION_RE = re.compile(r"^\W*(?:what|which|who|whom|whose|where|when|why|how)\b", re.I)
# 'What animals…', 'Which months…', and its 'of' phrase: 'What kind of dwellings…' → dwellings,
# 'What share of fresh water…' → share, fresh, water.
_WH_NOUN_RE = re.compile(r"^\W*(?:what|which)\s+([A-Za-zÀ-ÖØ-öø-ÿ’'-]+)"
                         r"(?:\s+of\s+((?:[A-Za-zÀ-ÖØ-öø-ÿ’'-]+\s*){1,3}))?", re.I)
_Q_WORD_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ’'-]*|[^\sA-Za-zÀ-ÖØ-öø-ÿ]")
# Words that end a "the <noun phrase>" run: prepositions, conjunctions, auxiliaries and the verbs FAQ
# questions use ('What makes the lake distinctive', 'How long ago did the volcano last erupt').
_NP_STOP = frozenset("""
the a an and or but nor of in on at to for with from by as into onto over under about after before during
between near beyond through across along around than then so if is are was were be been being do does did
can could will would may might should must has have had it its this that these those their his her our your
you we they he she them us there here what which who whom whose where when why how not no
make makes made happen happens happened use used uses need needs needed get gets reach reached last erupt
erupted live lives lived see seen visit visited expect expected sit sits differ differs mean means play plays
""".split())
# Abstract nouns a question wraps its real subject in ('the role of Mörön', 'the significance of
# Uran Togoo') — never enough on their own for an answer to count as naming the subject.
_ABSTRACT_NOUNS = frozenset("""
role significance importance purpose reason reasons way ways kind kinds sort type types part parts thing
things best main history story difference differences point use uses time times ecological cultural
""".split())
# A definite description of these never stands for a named subject ('The route stops here for lunch').
_TRIP_WORDS = frozenset("""
route trip journey tour itinerary day days stop stops group family families traveller travellers traveler
travelers visitor visitors guest guests answer question piece post
""".split())
# 'Who lives near the lake?' asks for the people: the answer names them, not the lake — no subject test.
_WHO_SUBJECT_RE = re.compile(r"^\W*who\s+(?!(?:is|are|was|were|do|does|did|can|could|will|would|should|has|have|had"
                             r"|may|might|must)\b)[a-z]", re.I)
_NEGATION_RE = re.compile(r"\b(?:not|never|no|none|neither|nor|nothing)\b|n['’]t\b", re.I)
# 'not just / not only' adds, and a negation followed by what IS the case ('…not for meat — instead,
# the animals are used for riding') answers: neither makes a sentence negative-only.
_ADDITIVE_NOT_RE = re.compile(r"\bnot\s+(?:just|only|merely|simply)\b", re.I)
_POSITIVE_TURN_RE = re.compile(r"\b(?:instead|rather|but)\b", re.I)


def _negative_sentence(sentence: str) -> bool:
    s = _ADDITIVE_NOT_RE.sub(" ", sentence)
    return bool(_NEGATION_RE.search(s)) and not _POSITIVE_TURN_RE.search(s)
_YES_NO_OPENING_RE = re.compile(r"^\W*(?:yes|no)\b", re.I)


def _subject_stems(text: str) -> set[str]:
    """Folded content tokens of `text` with a plural / possessive cut ('Khövsgöl's' → 'khovsgol',
    'animals' → 'animal'), ≥ 3 letters, stop words dropped."""
    out = set()
    for t in _SUBJ_TOKEN_RE.findall(fold(strip_citation_tags(text))):
        if len(t) < 3 or t in _NP_STOP or t in _WH_WORDS:
            continue
        out.add(_stem(t))
    return out


def _stem(t: str) -> str:
    """'volcanoes' → 'volcano', 'valleys' → 'valley', 'communities' → 'community', 'animals' → 'animal'."""
    if len(t) > 4 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 4 and t.endswith(("oes", "ches", "shes", "xes", "sses")):
        return t[:-2]
    if len(t) > 4 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


def faq_subject_tokens(question: str) -> tuple[set[str], set[str]]:
    """(subject tokens, name tokens) of a FAQ question. Subject tokens: every word of its capitalised
    names ('Lake Khövsgöl' → lake, khovsgol), the nouns after 'the/a/an' (up to 4 words, stopping at a
    preposition, verb or punctuation; abstract wrappers like 'role' or 'significance' skipped) and the
    noun after 'What/Which' ('What animals…'). Name tokens: only the capitalised names' tokens (what a
    seed sentence must contain for a class noun to stand for the name — `_alias_ok`)."""
    q = strip_citation_tags(question or "")
    names: set[str] = set()
    for phrase in proper_nouns(q):
        names |= _subject_stems(phrase)
    subject = set(names)
    words = _Q_WORD_RE.findall(q)
    for i, w in enumerate(words):
        if w.lower() not in ("the", "a", "an"):
            continue
        for nxt in words[i + 1:i + 5]:
            if not nxt[0].isalpha() or nxt.lower() in _NP_STOP:
                break
            subject |= {s for s in _subject_stems(nxt) if s not in _ABSTRACT_NOUNS}
    wh = _WH_NOUN_RE.match(q)
    if wh and wh.group(1).lower() not in _NP_STOP:
        subject |= {s for s in _subject_stems(wh.group(1)) if s not in _ABSTRACT_NOUNS}
        for nxt in (wh.group(2) or "").split():
            if nxt.lower() in _NP_STOP:
                break
            subject |= {s for s in _subject_stems(nxt) if s not in _ABSTRACT_NOUNS}
    return subject, names


def _answer_lead(answer: str) -> str:
    """The answer's first sentence; with a bare 'Yes.' / 'No.' first, the next sentence too."""
    spans = [s["clean"] for s in sentence_spans(strip_citation_tags(answer or "")) if s["clean"].strip()]
    if not spans:
        return ""
    if len(spans) > 1 and len(spans[0].split()) <= 3 and _YES_NO_OPENING_RE.match(spans[0]):
        return f"{spans[0]} {spans[1]}"
    return spans[0]


def _alias_ok(lead: str, names: set[str], context_text: str) -> bool:
    """'The volcano last erupted…' names 'Uran Togoo' when the post or the seed has a sentence with
    both the name and 'volcano' ('Uran Togoo is a dormant volcano…'). Only a definite description
    ('The <noun>', one optional adjective) counts, never a trip word ('The route stops here…')."""
    if not names or not context_text:
        return False
    m = re.match(r"^\W*the\s+([A-Za-zÀ-ÖØ-öø-ÿ’'-]+)(?:\s+([A-Za-zÀ-ÖØ-öø-ÿ’'-]+))?", lead, re.I)
    if not m:
        return False
    heads = {s for g in m.groups() if g for s in _subject_stems(g)} - _TRIP_WORDS - _ABSTRACT_NOUNS
    if not heads:
        return False
    for sent in sentence_spans(strip_citation_tags(context_text)):
        stems = _subject_stems(sent["clean"])
        if stems & names and stems & heads:
            return True
    return False


def faq_answer_problem(question: str, answer: str, context_text: str = "") -> Optional[str]:
    """AA-779 — why a FAQ answer does not answer its question, or None:
    'no_subject'    — the answer's first sentence (after a bare 'Yes.'/'No.') names none of the
                      question's subject tokens (`faq_subject_tokens`) and is not a definite
                      description the post / seed ties to the question's name (`_alias_ok`;
                      `context_text` = body + the moment's own seed + Facts). A pronoun or
                      demonstrative opening ('Their seasonal camps…', 'These activities…') fails.
                      A 'Who <verb>…' question ('Who lives near the lake?') asks for the people
                      and is not checked.
    'negative_only' — a what/which/who/where/when/why/how question answered only by negated
                      sentences ('…meat production is not the primary purpose.'). A yes/no
                      question may be answered 'No, …' and is never checked for this.
    A question with no subject tokens ('Can you swim?') passes the subject test."""
    if not (answer or "").strip():
        return None
    subject, names = faq_subject_tokens(question)
    if _WHO_SUBJECT_RE.match(strip_citation_tags(question or "")):
        subject = set()
    lead = _answer_lead(answer)
    if subject and not (_subject_stems(lead) & subject) and not _alias_ok(lead, names, context_text):
        return "no_subject"
    if _WH_QUESTION_RE.match(strip_citation_tags(question or "")):
        sents = [s["clean"] for s in sentence_spans(strip_citation_tags(answer)) if s["clean"].strip()]
        if sents and all(_negative_sentence(s) for s in sents):
            return "negative_only"
    return None


_FAQ_PROBLEM_TEXT = {"no_subject": "the answer does not name what the question asks about",
                     "negative_only": "the answer only says what is not the case"}


def drop_subjectless_faq(content_text: str, source_text: str = "",
                         floor: int = FAQ_MIN_PAIRS) -> tuple[str, list[str], int]:
    """AA-779 — drop FAQ pairs whose answer does not answer the question (`faq_answer_problem`), in
    reading order, only while more than `floor` pairs remain: F16_faq stays the only FAQ hold, and a
    short FAQ is the contract step's job (one write_faq regeneration). `source_text` = the moment's
    own seed + Facts; the body before "## FAQ" is added to it for the alias test. Returns (text, one
    log line per dropped or kept failing pair, failing pairs kept because of the floor)."""
    body = content_text or ""
    if "## FAQ" not in body:
        return body, [], 0
    pairs = faq_pairs(body)
    context = f"{body.partition('## FAQ')[0]}\n{source_text or ''}"
    problems = [faq_answer_problem(q, a, context) for q, a in pairs]
    room = max(0, len(pairs) - floor)
    kept, log, short = [], [], 0
    for (q, a), why in zip(pairs, problems):
        if why and room:
            room -= 1
            log.append(f"dropped FAQ pair ({_FAQ_PROBLEM_TEXT[why]}): '{q[:160]}'")
            continue
        if why:
            short += 1
            log.append(f"kept FAQ pair at the {floor}-pair minimum ({_FAQ_PROBLEM_TEXT[why]}): '{q[:160]}'")
        kept.append((q, a))
    if len(kept) == len(pairs):
        return body, log, short
    return replace_faq_section(body, kept), log, short


# AA-779 — real search questions (services/acp_shared/real_questions.py: the piece's Segment / place
# questions first, then the tour's People Also Ask; not scoped to the piece) the writer may answer in the
# FAQ. Kept only when they name no place outside the piece's scope, ask nothing the source cannot
# answer, are about the place rather than its name as a word (S245), and name one of the moment's own
# places or share ≥ 2 non-generic content words with its seed.
PAA_MAX_QUESTIONS = 6
# Words a seed and a search question share without being about the same thing (content_words stems:
# a trailing 's' is cut from words over 4 letters, so 'famous' → 'famou').
_PAA_GENERIC = frozenset("""
visit visiting tour trip travel best time worth famou know thing place people much many long far going see
get day year good great way top most does have about like near from area region
""".split())


def _seed_countries(own_seed: str) -> set[str]:
    """Folded country words of the seed's 'Location:' lines (after the last comma), so a PAA question
    sharing only the country ('Is Mongolia safe?') is not taken as being about this moment."""
    out: set[str] = set()
    for line in (own_seed or "").splitlines():
        if line.startswith("Location:"):
            tail = re.sub(r"\(.*?\)", " ", line.split(":", 1)[1]).rsplit(",", 1)[-1]
            out |= set(_SUBJ_TOKEN_RE.findall(fold(tail)))
    return out


def seed_place_names(own_seed: str) -> tuple[set[str], set[str], set[str]]:
    """The off-meaning filter's inputs for a moment (real_questions.is_off_meaning): its place name
    tokens (proper nouns, line openers included), the single-word place names written mid-sentence
    (the only ones the lowercase-word test reads: 'Mörön' yes, a line opener 'Fly to …' or a token of
    'Lake Khövsgöl' / 'Great Wall' no), and the seed's country words."""
    countries = _seed_countries(own_seed)
    names: set[str] = set()
    for phrase in proper_nouns(own_seed, keep_initial=True):
        names |= set(phrase_tokens(phrase))
    names -= countries
    single = single_word_places(proper_nouns(own_seed), countries)
    return names, single, countries


def normalize_paa_question(raw) -> Optional[str]:
    """AA-779 — one real search question cleaned for the FAQ: citation tags cut, whitespace collapsed,
    10–160 chars, a capital first letter and a final '?'. None when it is not a usable question.
    Shared by paa_for_scope and the S247 LLM filter (faq_filter.py), so both see the same text."""
    if not isinstance(raw, str):
        return None
    q = " ".join(strip_citation_tags(raw).split()).strip()
    if not 10 <= len(q) <= 160:
        return None
    q = q[0].upper() + q[1:]
    if not q.endswith("?"):
        q = q.rstrip(".!") + "?"
    return q


def paa_for_scope(questions, own_seed: str, facts_text: Optional[str] = None,
                  out_of_scope=(), limit: int = PAA_MAX_QUESTIONS) -> list[str]:
    """AA-779 — the PAA questions a piece's FAQ may use (see the comment above). `own_seed` = the
    moment's own lines (hygiene.split_seed), `out_of_scope` = folded gazetteer keys
    (hygiene.build_scope_lexicon(...).gazetteer). Order kept, duplicates dropped, at most `limit`."""
    # S245 r7 — the lowercase-word test (a place name written as a common word: 'Is moron still used
    # today?') reads only single-word names capitalised mid-sentence (seed_place_names).
    seed_names, mid_names, countries = seed_place_names(own_seed)
    seed_words = set(content_words(f"{own_seed}\n{facts_text or ''}")) - countries
    out: list[str] = []
    seen: set[str] = set()
    for raw in questions or []:
        q = normalize_paa_question(raw)
        if q is None:
            continue
        key = fold(q)
        if key in seen or any(names_place(q, g) for g in out_of_scope):
            continue
        if unanswerable_faq_topics(q, own_seed, facts_text):
            continue
        # S245 (AA-779 item 3) — a question about the name as a word, not the place ('Is moron a bad
        # word?' for the town Mörön) names an own place only by spelling.
        if is_off_meaning(q, seed_names, lowercase_names=mid_names, countries=countries):
            continue
        q_tokens = set(_SUBJ_TOKEN_RE.findall(fold(q)))
        shared = (set(content_words(q)) & seed_words) - _PAA_GENERIC
        if not (q_tokens & seed_names) and len(shared) < 2:
            continue
        seen.add(key)
        out.append(q)
        if len(out) >= limit:
            break
    return out



# AA-768 (S234 round 2 fix) — the seed facts the post has not used yet, so the FAQ regeneration
# answers from them first instead of re-telling the body. A line from the moment's own seed / Facts
# is split into sentences (≥ 6 content words); it counts as unused when less than
# _UNUSED_WORD_COVER of its content words and less than _UNUSED_TRIGRAM_COVER of its content
# trigrams appear in the body. Measured on the S234 r2 blogs: 'Mörön has a small domestic
# airport…' 0.27–0.33 / 0.00 and 'The lake freezes solidly in winter…' 0.20–0.27 / 0.00 (unused);
# the facts each post builds on sit at 0.8–1.0 words. A partly told fact may still be listed — it
# is only a preference in the prompt, the droppers decide what stays.
_UNUSED_WORD_COVER = 0.75
_UNUSED_TRIGRAM_COVER = 0.25
# AA-814 follow-up: a Facts entry's 'Source: <provenance>; agency fact' line is a label, never a fact to tell.
_SEED_META_LINE_RE = re.compile(
    r"^(?:Location|Naming|Days? \d|Thin moment|Place background|SCOPE|TRIP|AUDIENCE|BRAND|FACTS:"
    r"|\[Moment\b|\[Fact\b|Source:|Other trips|" + STATUS_PREFIX + ")", re.I)
_SEED_LINE_PREFIX_RE = re.compile(r"^(?:[-*•]\s*)?(?:(?:Source itinerary|Why it matters):\s*)?")


def _trigrams(words: list[str]) -> set:
    return {tuple(words[i:i + 3]) for i in range(len(words) - 2)}


def unused_source_facts(source_text: str, body: str, limit: int = 8) -> list[str]:
    """Fact sentences of `source_text` (the moment's own seed lines + Facts block) that `body` does
    not tell yet, least-covered first, at most `limit`. Deterministic, no model call."""
    body_words = content_words(strip_citation_tags(body or ""))
    word_set, body_tri = set(body_words), _trigrams(body_words)
    scored, seen = [], set()
    for raw in (source_text or "").splitlines():
        line = _SEED_LINE_PREFIX_RE.sub("", raw.strip(), count=1).strip()
        if not line or _SEED_META_LINE_RE.match(line):
            continue
        for span in sentence_spans(line):
            sent = span["clean"].strip()
            words = content_words(sent)
            if len(words) < 6 or sent in seen:
                continue
            seen.add(sent)
            cover = sum(w in word_set for w in words) / len(words)
            tri = _trigrams(words)
            tri_cover = len(tri & body_tri) / len(tri) if tri else 0.0
            if cover < _UNUSED_WORD_COVER and tri_cover < _UNUSED_TRIGRAM_COVER:
                scored.append((cover + tri_cover, len(scored), sent))
    return [s for _, _, s in sorted(scored)[:limit]]


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
        return _result(qr.F12_SCOPE, [], blocking=F12_SCOPE_BLOCKING)
    found = find_issues(content_text, hygiene_ctx, seo_title=seo_title, meta_description=meta_description)
    return _result(qr.F12_SCOPE, found["scope"], repairable=True, blocking=F12_SCOPE_BLOCKING)


F13_LEAK_GATE = qr.F13_LEAK


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
        return _result(qr.F13_HYGIENE, [], repairable=False, blocking=False)
    return _result(qr.F13_HYGIENE, find_issues(content_text, hygiene_ctx)["hygiene"], repairable=False,
                   blocking=False)


# S234 round 3 (r2 §4 fix 2) — near-duplicates hygiene.dedupe / trip_facts.fix_misplaced could not
# remove safely: a figure told twice, an echoed phrase, an orphan line, a moment repeated under
# another moment's heading. Blocking and sentence-repairable (an empty replacement deletes it).
F13_REPEAT_GATE = qr.F13_REPEAT


def gate_repeat(content_text: str, hygiene_ctx: Optional[HygieneContext]) -> GateResultLite:
    """F13_repeat — each violation quotes the later sentence ("<problem>: '<sentence>'")."""
    if hygiene_ctx is None:
        return _result(F13_REPEAT_GATE, [])
    return _result(F13_REPEAT_GATE, find_issues(content_text, hygiene_ctx)["repeat"], repairable=True)


# S234 round 3 (r2 §4 fixes 3, 5, 8) — trip facts the seed does not give (an optional activity as
# fixed, a time / ordinal / position, a family-brand access caveat left out, a role the seed dates as
# ended written in the present or a wrong span) and rarity / quantifier overclaims. Both blocking and
# sentence-repairable on the cheap t9_repair stage; a title / meta finding ("meta …") is the SEO
# fit's (service._fit_seo_meta rejects such a meta). Held only when the repair fails.
F17_GATE = qr.F17_TRIP_FACT
F18_GATE = qr.F18_RARITY
F17_BLOCKING = True
F18_BLOCKING = True


def gate_trip_facts(content_text: str, trip_ctx: Optional[TripFactContext], seo_title: Optional[str] = None,
                    meta_description: Optional[str] = None) -> list[GateResultLite]:
    """[F17_trip_fact, F18_rarity] from trip_facts.find_trip_fact_issues. No context → both pass."""
    if trip_ctx is None:
        return [_result(F17_GATE, [], blocking=F17_BLOCKING), _result(F18_GATE, [], blocking=F18_BLOCKING)]
    found = find_trip_fact_issues(content_text, trip_ctx, seo_title=seo_title, meta_description=meta_description)

    def body_blocking(vs: list[str], flag: bool) -> bool:
        # a title / meta finding alone never holds a piece: service._fit_seo_meta already refused that
        # meta and asked for a new one; what is left is a warning for the admin
        return flag and any(not v.startswith(("title ", "meta ")) for v in vs)
    return [_result(F17_GATE, found["trip_fact"], repairable=True,
                    blocking=body_blocking(found["trip_fact"], F17_BLOCKING) if found["trip_fact"] else F17_BLOCKING),
            _result(F18_GATE, found["rarity"], repairable=True,
                    blocking=body_blocking(found["rarity"], F18_BLOCKING) if found["rarity"] else F18_BLOCKING)]


# ---------------------------------------------------------------- F14 / F15 (AA-771, S234)

# F15_voice (stock-phrase hard list only) blocks — and goes to sentence repair, never a full
# rewrite — on the FIRST attempt only, and never holds a piece (S234 plan PR3 + review): a style
# phrase is not product truth (AA-613 'held'). Trade register, audience leak and opener checks are a
# separate warn-only result, F15_voice_warn, so sentence_repair.plan never rewrites them. AA-924 — both stay
# source checks for routing; the stored ledger keeps the stock findings in F2_lexicon and the rest (with
# F15_voice_warn's as warn findings) in one F15_voice entry.
F15_STOCK_BLOCKING = True
# AA-780 (S234 r4 plan #7) — a LinkedIn post under the band's floor (120 words) blocks on attempt 1 only
# (the same `stock_blocking` flag): one full rewrite to the target, never a rewrite loop, never a hold
# (`never_holds`). Both r4 false passes were under it (105 and 113 words, Trail Pulse).
F14_FLOOR_BLOCKING = True
F14_GATE = qr.F14_SOCIAL_SHAPE
# The over-the-band text of the one F14 violation that may hold a piece (> 1.3 × the band's top).
_F14_FAR_OVER = "far over the"
F15_GATE = qr.F15_VOICE
F15_WARN_GATE = qr.F15_VOICE_WARN
# Gates that may block (to trigger one repair) but never hold a piece on their own.
# AA-800: a second stock superlative (F2_superlatives) is style too.
STYLE_ONLY_GATES = frozenset({F15_GATE, qr.F2_SUPERLATIVES})
# S234 round 3 — gates whose sentence repair runs on the cheap t9_repair stage route when they are
# the only blocking failures: style (F15), near-duplicates (F13_repeat), trip facts and rarity
# (F17/F18 — the repair keeps or cuts a sentence the seed already bounds).
# AA-819 — F19_authenticity too: the repair rewrites the sentence in the brand's voice or cuts it.
# AA-800 — the F2 word layers too: the repair swaps or cuts one word.
CHEAP_REPAIR_GATES = frozenset({F15_GATE, qr.F13_REPEAT, qr.F17_TRIP_FACT, qr.F18_RARITY, qr.F19_AUTHENTICITY,
                                qr.F2_PLATFORM_TRUTH, qr.F2_SUPERLATIVES})
# S234 round 2 — gates a dedicated deterministic/one-call step in service.py fixes every attempt
# (like F4_seo_surface and the SEO fit): sentence_repair.plan never turns them into a rewrite.
OWNED_BY_FIX_STEP = frozenset({qr.F16_FAQ, qr.F14_FIRST_LINE})
_MIN_QUOTE_WORDS = 4  # sentence_repair.locate_sentence needs 4 words to find a sentence
OPENER_REPEAT_JACCARD = 0.6


def gate_social_shape(content_text: str, spec: Optional[ChannelSpec], *,
                      floor_blocking: bool = False) -> GateResultLite:
    """F14_social_shape — LinkedIn word count, hashtags and first line against the brand's channel
    spec. Warn outside the word band; BLOCKING (a full rewrite with the explicit target) when the post
    is over 1.3 × the band's top (325 words; 390 editorial), and — AA-780 — when it is under the band's
    floor and `floor_blocking` (run_quality_gates passes it on attempt 1 only). The under-floor block
    never holds a piece (`never_holds`); the far-over one still does. Hashtags and the first line are
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
    elif n < lo and floor_blocking:
        # AA-780 — " under the " stays in the text: service._cut_truth_items reads it
        blocking = True
        violations.append(f"post is {n} words, under the {lo}–{hi}-word band for this brand on LinkedIn — "
                          f"rewrite it to {tlo}–{thi} words with more of the source's own detail, keeping the "
                          f"same angle and adding nothing the source does not state")
    elif n < lo:
        violations.append(f"post is {n} words, under the {lo}–{hi}-word band for this brand on LinkedIn")
    tags = list(dict.fromkeys(HASHTAG_RE.findall(text)))
    if len(tags) > spec.hashtags_max:
        violations.append(f"{len(tags)} hashtags, more than {spec.hashtags_max}: '{' '.join(tags)}'")
    return _result(F14_GATE, violations, repairable=blocking, blocking=blocking)


F14_FIRST_LINE_GATE = qr.F14_FIRST_LINE


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
# AA-789 — the two shapes added for recall rank with their nearest kin: the 'Not X. Y X.' pair with the two-sentence
# frame, the reversed 'Y, not X' with 'is not X, it is Y'.
_CONTRAST_RANK = (5, 2, 9, 0, 1, 8, 6, 3, 7, 4)


def _in_stock_sentence(spans: list[dict], start: int, end: int, stock_quotes: tuple) -> bool:
    """Every sentence the frame [start, end) touches is part of a quote a stock finding already made."""
    touched = [sp["clean"] for sp in spans if sp["end"] > start and sp["start"] < end]
    return bool(touched) and all(any(t in q for q in stock_quotes) for t in touched)


def _contrast_violations(text: str, spans: list[dict], stock_quotes: tuple = ()) -> list[str]:
    n, allowed, frames = contrast_excess(text)
    # AA-789 — a frame inside a sentence a stock finding already quotes ('Not as filler. Not as an afterthought.')
    # is repaired with it: it neither counts toward the density nor gets a second, overlapping finding
    if stock_quotes:
        frames = [f for f in frames if not _in_stock_sentence(spans, f[0], f[1], stock_quotes)]
        n = len(frames)
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


def _brief_violations(text: str, briefs: Optional[dict], exclude_texts: Optional[list[str]],
                      brand_name: Optional[str], spans: list[dict], keyword: Optional[str]) -> list[str]:
    out = []
    if not briefs:
        return out
    kept = {sp["clean"] for sp in spans}
    for sentence, run in brief_phrases(text, briefs, exclude=exclude_texts or [], brand_name=brand_name,
                                       keyword=keyword):
        if sentence in kept and len(sentence.split()) >= _MIN_QUOTE_WORDS:
            out.append(f"pastes the brand brief ('{run}') into the copy — say it in this piece's own words: "
                       f"'{sentence}'")
    return out


def gate_voice(content_text: str, *, spec: Optional[ChannelSpec], stock_blocking: bool = True,
               sibling_texts: Optional[list[str]] = None, exclude_texts: Optional[list[str]] = None,
               brand_name: Optional[str] = None, keyword: Optional[str] = None,
               brief_texts: Optional[dict] = None) -> GateResultLite:
    """F15_voice — the stock-phrase hard list for a channel with a spec (LinkedIn; S233: 30/41 posts
    — 'worth sitting with', 'not incidental', 'Not as filler. Not as an afterthought.'). Blocking when
    F15_STOCK_BLOCKING and `stock_blocking` (the caller passes it for attempt 1 only), so it is
    repaired sentence by sentence (sentence_repair._ELIGIBLE_GATES) at most once, never by a full
    rewrite; `hold_failure` never holds a piece on it. Every violation reads
    "<problem>: '<sentence>'" (the shape sentence_repair.plan parses).
    AA-924 — the F15_voice source check. The ledger splits it: the stock findings ('stock phrase', 'stock line')
    sit in F2_lexicon, the contrast frames, sibling templates and brief phrases in F15_voice beside
    gate_voice_warn's findings (registry.MERGED_GATES, merge_ledger)."""
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
    stock += _contrast_violations(text, spans, tuple(v.rsplit(": '", 1)[-1].rstrip("'") for v in stock))
    stock += _template_violations(text, sibling_texts, exclude_texts, brand_name, spans, keyword)
    # S234 round 3 (r2 §4 fix 4) — stock lines that recur across rounds and brands, and phrases pasted
    # from the brand pack / angle brief, on every channel. Same one-repair-then-flag rule.
    quoted = {sp["clean"] for sp in spans}
    for line, sentence in template_lines(text):
        if sentence in quoted and len(sentence.split()) >= _MIN_QUOTE_WORDS and not any(sentence in v for v in stock):
            stock.append(f"stock line '{line}' — say it plainly or cut it: '{sentence}'")
    stock += _brief_violations(text, brief_texts, exclude_texts, brand_name, spans, keyword)
    # AA-790 round 3 — an age band or a founding reason the moment's source (seed lines + Facts: run_quality_gates'
    # exclude_texts) does not give. Same one-repair-then-flag rule, never a hold.
    source = "\n".join(t for t in exclude_texts or () if t)
    for sp in spans:
        for age in unsupported_ages(sp["clean"], source):
            stock.append(f"{UNSUPPORTED_DETAIL_PREFIX}an age ('{age}') the source does not give — say it without the "
                         f"age: '{sp['clean']}'")
        for why in unsupported_reasons(sp["clean"], source):
            stock.append(f"{UNSUPPORTED_DETAIL_PREFIX}a reason ('{why}') the source does not give — keep the source's "
                         f"own purpose: '{sp['clean']}'")
    return _voice_result(list(dict.fromkeys(stock)), spec, stock_blocking)


def _voice_result(violations: list[str], spec: Optional[ChannelSpec], stock_blocking: bool) -> GateResultLite:
    if not violations:
        return _result(F15_GATE, [], repairable=spec is not None, blocking=False)
    return _result(F15_GATE, violations, repairable=True, blocking=F15_STOCK_BLOCKING and stock_blocking)


def gate_voice_warn(content_text: str, *, spec: Optional[ChannelSpec], brand_pack: Optional[dict] = None,
                    atom_text: str = "", sibling_openers: Optional[list[str]] = None,
                    cta: Optional[str] = None) -> GateResultLite:
    """F15_voice_warn — warn-only voice checks for a channel with a spec (LinkedIn), kept apart from
    F15_voice so a stock-phrase repair never rewrites them:
    - writing to (or as) the travel trade for a non-editorial brand (S233 Trail Pulse 3/3: 'Active
      travel brands have a pacing problem.'; channel_spec.TRADE_PATTERN);
    - the brand's Audience numbers in the text when the source does not have them (S233 #6
      'clients in the 25–40 range');
    - a generic 'There is a …' opener, or the same opener as an earlier piece from the same brief
      (token Jaccard ≥ 0.6; S233 Terra 3/3 identical);
    - AA-780 — broetry: ≥ BROETRY_RUN consecutive sentences of ≤ BROETRY_MAX_WORDS words (S234 Trail
      Pulse "That's not filler. That's structure.", "Not squeezed in. Planned in."), one warning per run;
      a sentence of the configured `cta` (ensure_cta puts it in word for word) never counts.
    Same "<problem>: '<sentence>'" shape, for the admin highlights.
    AA-924 — the F15_voice_warn source check: the ledger holds its findings as warn findings of F15_voice."""
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
    warn += _broetry_violations(spans, cta)
    return _result(F15_WARN_GATE, list(dict.fromkeys(warn)), repairable=False, blocking=False)


BROETRY_MAX_WORDS = 5
BROETRY_RUN = 2


def _broetry_violations(spans: list[dict], cta: Optional[str] = None) -> list[str]:
    """AA-780 — one warning per run of ≥ BROETRY_RUN consecutive prose sentences of ≤
    BROETRY_MAX_WORDS words each (hashtag lines are not in `spans`). A bullet, a span with no word
    (an emoji-only line) or a sentence the configured CTA contains ends a run — a short itinerary list
    is a list, and a short "Ask us." is the tenant's line, not a maxim. S234 r1–r4 replay: 4 hits in
    48 LinkedIn posts, all Trail Pulse maxim pairs, 0 false positives."""
    cta_key = " ".join(re.findall(r"\w+", fold(cta or "")))
    out: list[str] = []
    run: list[dict] = []
    for sp in spans + [None]:
        key = " ".join(re.findall(r"\w+", fold(sp["clean"]))) if sp is not None else ""
        in_cta = bool(key) and f" {key} " in f" {cta_key} "
        if (sp is not None and sp["kind"] == "prose" and key and not in_cta
                and len(sp["clean"].split()) <= BROETRY_MAX_WORDS):
            run.append(sp)
            continue
        if len(run) >= BROETRY_RUN:
            out.append(f"broetry: {len(run)} very short sentences in a row — say it in one plain sentence: "
                       f"'{_quote(spans, run[0]['start'], run[-1]['end'])}'")
        run = []
    return out


def never_holds(g: dict) -> bool:
    """A failed result that may block (to get one repair or rewrite) but never holds a piece: a
    style-only gate (STYLE_ONLY_GATES), or — AA-780 — an F14_social_shape that blocked for the
    under-floor rewrite on attempt 1 (only the far-over-the-band violation may hold). Reads the stored
    violation text, so a ledger kept by the AA-528 revert or read back from the DB answers the same."""
    if is_merged(g):  # AA-924 — a merged entry never holds when none of its failed blocking sources may hold
        return all(never_holds(p) for p in expand_ledger([g]) if not p["passed"] and p.get("blocking", True))
    if g.get("gate") in STYLE_ONLY_GATES:
        return True
    # AA-789 tier 1 — seed-alignment findings ('sa:<class>: …') never hold unless one of them may
    # (seed_align.finding_may_hold: a promoted hold-capable class in hold mode; none before ~18/11). The gate's own
    # findings decide as before, and only when they blocked before the `sa:` ones were appended
    # (seed_align.base_blocking): an F17 / F18 'title …' / 'meta …' warning stays a warning.
    vs = [str(v) for v in g.get("violations") or []]
    sa_vs = [v for v in vs if v.startswith(seed_align.FINDING_PREFIX)]
    if sa_vs:
        if any(seed_align.finding_may_hold(v) for v in sa_vs):
            return False
        own = [v for v in vs if not v.startswith(seed_align.FINDING_PREFIX)]
        if not own or not seed_align.base_blocking(g):
            return True
        return never_holds({k: v for k, v in g.items() if k != seed_align.BASE_BLOCKING_KEY} | {"violations": own})
    return g.get("gate") == F14_GATE and not any(_F14_FAR_OVER in str(v) for v in g.get("violations") or [])


def hold_failure(gate_ledger: list[GateResultLite]) -> Optional[GateResultLite]:
    """The failed blocking gate a piece is HELD on — the first one that may hold (`never_holds`:
    F15_voice may block to get one sentence repair, but a stock phrase is not unresolved product
    truth, AA-613; an under-floor LinkedIn post gets one rewrite, AA-780). None → nothing holds the
    piece; it ships with the flags."""
    return next((g for g in expand_ledger(gate_ledger) if not g["passed"] and g.get("blocking", True)
                 and not never_holds(g)), None)


# ---------------------------------------------------------------- AA-924: per-finding severity, merged entries

BLOCK = "block"
WARN = "warn"
# F15_voice findings that belong to the lexicon entry (registry.MERGED_GATES): STOCK_PATTERNS and template lines.
F15_STOCK_PREFIXES = ("stock phrase '", "stock line '")
# The order gate_voice builds F15_voice findings in, by kind (the violation's prefix). The two merged entries hold
# them apart; expand_ledger puts them back in this order, which is the legacy order.
# AA-790 round 3: an age band or a founding reason the moment's source does not give (gate_voice, last).
UNSUPPORTED_DETAIL_PREFIX = "unsupported detail: "
_F15_KIND_ORDER = ("stock phrase '", "contrast frame '", "repeats a line from an earlier piece", "stock line '",
                   "pastes the brand brief", UNSUPPORTED_DETAIL_PREFIX)
# Every source of a merged entry, in legacy ledger order.
_SOURCE_ORDER = tuple(dict.fromkeys(s for srcs in qr.MERGED_GATES.values() for s in srcs))


def _f15_kind(text: str) -> int:
    t = str(text)
    return next((i for i, p in enumerate(_F15_KIND_ORDER) if t.startswith(p)), len(_F15_KIND_ORDER))


def _group_of(source: str, text: str) -> str:
    """The merged entry a legacy finding goes into: F15_voice's stock findings go to the lexicon entry."""
    if source == qr.F15_VOICE:
        return qr.F2_LEXICON if str(text).startswith(F15_STOCK_PREFIXES) else qr.F15_VOICE
    return qr.SOURCE_HOME[source]


def is_merged(g) -> bool:
    """A merged ledger entry (AA-924), as opposed to a legacy one or a merged gate's legacy namesake."""
    return isinstance(g, dict) and g.get("gate") in qr.MERGED_GATES and isinstance(g.get("parts"), list)


def ledger_is_merged(ledger) -> bool:
    return any(is_merged(g) for g in ledger or [])


def _part_severity(part: dict) -> str:
    """A legacy source's findings are BLOCK while it fails and blocks, else WARN (a warn gate, a waived one, a
    style gate after attempt 1)."""
    return BLOCK if not part.get("passed") and part.get("blocking", True) else WARN


def expand_ledger(ledger) -> list:
    """AA-924 — the legacy view of a ledger (or a flags list): every merged entry is replaced by one legacy-shaped
    result per source (its `parts` record plus the texts of its findings), at the merged entry the source sat at in
    the legacy ledger (registry.SOURCE_HOME; the first merged entry holding it when that one is absent), in legacy
    order. F15_voice's findings, split between F2_lexicon and F15_voice, come back in gate_voice's order. Every
    other entry is kept as it is, in place; a list with no merged entry (any piece stored before AA-924) is returned
    unchanged. Every routing decision reads this view, so a merged ledger routes exactly as the legacy one did."""
    entries = list(ledger or [])
    if not ledger_is_merged(entries):
        return entries
    meta: dict[str, dict] = {}
    texts: dict[str, list[str]] = {}
    slot: dict[str, int] = {}
    for i, g in enumerate(entries):
        if not is_merged(g):
            continue
        for p in g["parts"]:
            src = p.get("gate") if isinstance(p, dict) else None
            if not src:
                continue
            meta.setdefault(src, p)
            texts.setdefault(src, [])
            home = qr.SOURCE_HOME.get(src)
            if src not in slot or (home == g["gate"] and entries[slot[src]].get("gate") != home):
                slot[src] = i
        for f in g.get("findings") or []:
            src = f.get("source") if isinstance(f, dict) else None
            if not src:
                continue
            if src not in meta:  # a finding with no part record: rebuild a minimal one
                meta[src] = {"gate": src, "passed": False, "repairable": True, "blocking": f.get("severity") == BLOCK}
                slot.setdefault(src, i)
            texts.setdefault(src, []).append(str(f.get("text")))
    out: list = []
    for i, g in enumerate(entries):
        if not is_merged(g):
            out.append(g)
            continue
        here = sorted((s for s, k in slot.items() if k == i),
                      key=lambda s: _SOURCE_ORDER.index(s) if s in _SOURCE_ORDER else len(_SOURCE_ORDER))
        for src in here:
            vs = texts.get(src, [])
            if src == qr.F15_VOICE:
                vs = sorted(vs, key=_f15_kind)
            m = meta[src]
            out.append({"gate": src, "passed": m.get("passed", not vs), "violations": vs,
                        **{k: v for k, v in m.items() if k not in ("gate", "passed", "violations")}})
    return out


def merge_ledger(ledger) -> list:
    """AA-924 — the stored shape of a ledger (or a flags list): the legacy results of each merged gate
    (registry.MERGED_GATES) become one entry `{gate, passed, violations, repairable, blocking, severity, findings,
    parts}` placed where its home source sat (F2_lexicon at F2_banned_patterns, F15_voice at F15_voice), or at its
    first source when that is absent. A finding's severity is its source's (`_part_severity`); the entry's
    `severity` is the worst one and `blocking` is worst == block (a passed entry keeps its sources' blocking).
    Every other entry is kept as it is, in place. Expands first, so it is idempotent; `expand_ledger(merge_ledger(L))
    == L` for a legacy list L."""
    entries = expand_ledger(ledger)
    members = [g for g in entries if isinstance(g, dict) and g.get("gate") in qr.SOURCE_HOME]
    if not members:
        return entries
    built: dict[str, dict] = {}
    anchor: dict[str, int] = {}
    for m, srcs in qr.MERGED_GATES.items():
        present = [g for g in members if g["gate"] in srcs]
        if not present:
            continue
        home = [g for g in present if qr.SOURCE_HOME[g["gate"]] == m]
        anchor[m] = id((home or present)[0])
        ordered = sorted(present, key=lambda g: srcs.index(g["gate"]))
        parts = [{k: v for k, v in g.items() if k != "violations"} for g in ordered]
        findings: list[Finding] = [{"text": v, "severity": _part_severity(g), "source": g["gate"]}
                                   for g in ordered for v in g.get("violations") or [] if _group_of(g["gate"], v) == m]
        own = {f["source"] for f in findings}
        contributing = [p for p in parts if p["gate"] in own]
        worst = BLOCK if any(f["severity"] == BLOCK for f in findings) else WARN if findings else None
        entry = {"gate": m, "passed": not findings, "violations": [f["text"] for f in findings],
                 "repairable": any(p.get("repairable") for p in (contributing or parts)),
                 "blocking": worst == BLOCK if findings else any(p.get("blocking", True) for p in parts),
                 "severity": worst, "findings": findings, "parts": parts}
        if any(p.get("waived") for p in contributing):
            entry["waived"] = True
        built[m] = entry
    out: list = []
    for g in entries:
        if not (isinstance(g, dict) and g.get("gate") in qr.SOURCE_HOME):
            out.append(g)
            continue
        out += [built[m] for m in qr.MERGED_GATES if anchor.get(m) == id(g)]
    return out


def merge_flags(flags) -> list:
    """The stored shape of a flags list: `merge_ledger`, keeping only the merged entries that hold a finding (a
    stock-only F15 flag is an F2_lexicon flag, with no empty F15_voice entry beside it)."""
    return [g for g in merge_ledger(flags) if not (is_merged(g) and g.get("passed"))]


def reshape_like(entries, ledger, *, flags: bool = False) -> list:
    """`entries` (a legacy view) in the shape `ledger` has: merged when `ledger` holds a merged entry, else as is.
    Lets a routing step that edits the legacy view hand back the ledger shape it was given."""
    if not ledger_is_merged(ledger):
        return list(entries)
    return merge_flags(entries) if flags else merge_ledger(entries)


def ledger_gate(g: dict) -> str:
    """The stored ledger entry a legacy result's findings sit in: F2_banned_patterns / F2_brand_forbidden →
    F2_lexicon, F15_voice_warn → F15_voice, an F15_voice whose findings are all stock → F2_lexicon; any other gate
    is its own. Used for the names a merged piece stores outside the ledger (held_reason, gate_targeted)."""
    gate = g.get("gate")
    if gate == qr.F15_VOICE:
        vs = g.get("violations") or []
        return qr.F2_LEXICON if vs and all(_group_of(gate, v) == qr.F2_LEXICON for v in vs) else qr.F15_VOICE
    return qr.SOURCE_HOME.get(gate, gate)


# ---------------------------------------------------------------- F2_lexicon (AA-924)

_STOCK_FINDING_RE = re.compile(r"^stock (?:phrase|line) '(.+?)' — .*: '(.+)'\s*$", re.DOTALL)
_SAME_SENTENCE_CHARS = 120


def _hit_key(text: str) -> str:
    return " ".join(re.findall(r"\w+", fold(strip_citation_tags(text or ""))))


def _same_hit(hit: tuple[str, str], kept: tuple[str, str]) -> bool:
    """A (phrase, sentence) hit is a word already judged by a `kept` hit: the kept phrase holds the hit's phrase
    and the two sentences are the same (their first 120 folded characters; a quote may run on into the next
    sentence). One-way: a hit whose phrase is LONGER than the kept one ('worth sitting with' beside a forbidden
    'sitting') is a second problem, and the repair is told both."""
    pa, pb = _hit_key(hit[0]), _hit_key(kept[0])
    if not pa or not pb or f" {pa} " not in f" {pb} ":
        return False
    sa, sb = _hit_key(hit[1]), _hit_key(kept[1])
    n = min(len(sa), len(sb), _SAME_SENTENCE_CHARS)
    return n > 0 and sa[:n] == sb[:n]


def lexicon_sources(content_text: str, f2_source: str, brand_pack: Optional[dict] = None, *,
                    atom_text: Optional[str] = None, voice: Optional[GateResultLite] = None,
                    spec: Optional[ChannelSpec] = None) -> list[GateResultLite]:
    """AA-924 — the legacy source results the lexicon gate folds, each word judged once: [F2_banned_patterns,
    F2_brand_forbidden] plus, with `voice` (gate_voice's result), F15_voice. A tenant-forbidden word already found
    as a banned pattern in the same sentence is dropped, and so is a stock finding a banned or forbidden hit already
    covers (the highest severity is kept: both of those block). Only a hit the kept phrase contains is dropped: a
    longer phrase around a shorter kept word is its own problem (`_same_hit`). `f2_source` exempts banned patterns
    (seed + CTA + trip name), `atom_text` the tenant's words (as before: the seed only; `f2_source` when None)."""
    banned = _banned_hits(content_text, f2_source)
    kept = [(p, sent) for p, sent, _ in banned]
    forbidden: list[str] = []
    for p, sent, v in _forbidden_hits(content_text, f2_source if atom_text is None else atom_text,
                                      (brand_pack or {}).get("forbidden_words")):
        if any(_same_hit((p, sent), k) for k in kept):
            continue
        kept.append((p, sent))
        forbidden.append(v)
    out = [_result(qr.F2_BANNED_PATTERNS, [v for _, _, v in banned]), _result(qr.F2_BRAND_FORBIDDEN, forbidden)]
    if voice is not None:
        vs = []
        for v in voice.get("violations") or []:
            m = _STOCK_FINDING_RE.match(v) if v.startswith(F15_STOCK_PREFIXES) else None
            if m and any(_same_hit((m.group(1), m.group(2)), k) for k in kept):
                continue
            vs.append(v)
        out.append(voice if len(vs) == len(voice.get("violations") or []) else
                   {**voice, **_voice_result(vs, spec, bool(voice.get("blocking")))})
    return out


def gate_lexicon(content_text: str, f2_source: str, brand_pack: Optional[dict] = None,
                 spec: Optional[ChannelSpec] = None, stock_blocking: bool = True, include_stock: bool = True, *,
                 atom_text: Optional[str] = None, voice: Optional[GateResultLite] = None) -> GateResultLite:
    """F2_lexicon (AA-924; S234 Report 12 §1.2) — one result for every word list a T9 piece is checked against:
    banned patterns (block, source F2_banned_patterns; the em-dash and F19 attribution carve-outs kept), the
    tenant's forbidden words (block, source F2_brand_forbidden) and the F15 stock list — STOCK_PATTERNS on a channel
    with a spec and hygiene.template_lines everywhere — (block while `stock_blocking`, i.e. attempt 1, then warn;
    source F15_voice). One finding per word and sentence (`lexicon_sources`). The AA-800 layers (F2_platform_truth,
    F2_superlatives, F2_style_warn) stay their own gates and skip the tenant's own words.
    `include_stock=False` (the admin sentence Apply, sentence_repair.check_replacement) checks only the banned and
    forbidden words. `voice` is gate_voice's result when the caller has it (run_quality_gates: one F15 check feeds
    both F2_lexicon and F15_voice)."""
    if include_stock and voice is None:
        voice = gate_voice(content_text, spec=spec, stock_blocking=stock_blocking)
    parts = lexicon_sources(content_text, f2_source, brand_pack, atom_text=atom_text,
                            voice=voice if include_stock else None, spec=spec)
    return next(g for g in merge_ledger(parts) if g["gate"] == qr.F2_LEXICON)


# ---------------------------------------------------------------- orchestration

class _QualityCheckOutcomeBase(TypedDict):
    passed: bool
    gate_ledger: list[GateResultLite]
    first_failure: Optional[GateResultLite]
    flags: list[GateResultLite]


class QualityCheckOutcome(_QualityCheckOutcomeBase, total=False):
    # AA-789 tier 1 — only when a seed-alignment class is not off: its modes and every candidate (shadow included),
    # for writer_input. Absent when every class is off, so the outcome is what it was. AA-1000: tier 2's record under
    # "tier2" when one of its classes is not off.
    seed_align: dict


def run_quality_gates(
    *, content_text: str, atom_text: str, cta: Optional[str],
    brand_rubric_text: str, channel: str,
    seo_title: Optional[str] = None, meta_description: Optional[str] = None,
    slug: Optional[str] = None, keyword: Optional[str] = None,
    cannibalization_match: Optional[dict] = None,
    brand_pack: Optional[dict] = None,
    hygiene_ctx: Optional[HygieneContext] = None,
    sibling_openers: Optional[list[str]] = None,
    stock_blocking: bool = True,
    sibling_texts: Optional[list[str]] = None,
    trip_ctx: Optional[TripFactContext] = None,
    brief_texts: Optional[dict] = None,
    taken_titles: Optional[list[str]] = None,
    f9_passed_sentences: Optional[set[str]] = None,
    f9_results: Optional[list[GateResultLite]] = None,
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

    `channel` is required (AA-452). gate_extreme_length() (F4) is called on the TAG-STRIPPED text,
    not raw `content_text` — citation tags are internal markup and shouldn't count toward a length
    ceiling meant to bound what the tenant actually reads (AA-531: blog gets its own wider band, see
    `_length_bounds_for_channel()`). AA-786: the F9 judge also gets the tag-stripped text (it
    flagged the tags as "citation markers"). `gate_seo_surface()` (F4 family) and the FAQ contract
    (F16) are blog-only; `seo_title`/`meta_description`/`slug`/`keyword` are `None` for every
    non-blog channel.

    AA-787 (S234 gate evaluation, Report 12 §1.2; Nghiệp S234 + S245) removed four results from this
    stack: `promises_an_option` (fired on every blog; F17_trip_fact blocks and repairs the same
    "optional stated as planned" failure — CIS ADR 0009), F8_framework (an LLM call that fired on
    20/24 light LinkedIn posts and showed no lift on blogs), F3_structural_variance (no lift over the
    blog base rate) and F5_atom_density (too rare to mean anything). F7_faq_dedup is folded into
    F16_faq: service.py's `drop_restated_faq` fixer cuts every FAQ answer sentence that copies the
    body before these gates read the text (logged in the `F7_faq_autofix` flag), and F16 counts the
    pairs that are left.

    AA-924 (Report 12 §1.2) — the gates still run as their legacy source checks, and the stored ledger folds them:
    F2_banned_patterns + F2_brand_forbidden + F15_voice's stock findings → one F2_lexicon entry (at F2_banned_patterns'
    place), F15_voice + F15_voice_warn → one F15_voice entry, each finding with its own block / warn severity
    (merge_ledger). That is 26 → 24 distinct results: 23 on a blog, 22 on LinkedIn, 21 on the other channels.
    `first_failure` and `flags` are read on the legacy view (expand_ledger), so routing is unchanged.

    AA-918 (R0 replay, $0) — `f9_results`: the F9 entries to use instead of calling the judge (a replay passes the
    verdicts stored on the piece). None (every writer path) calls gate_brand_voice as before."""
    cta_result = gate_cta_present(cta)
    ledger = [cta_result]
    if not cta_result["passed"]:
        return {"passed": False, "gate_ledger": ledger, "first_failure": cta_result, "flags": []}

    length_check_text = strip_citation_tags(content_text)
    spec = channel_spec(channel, brand_pack)
    # AA-778 — the configured CTA (ensure_cta puts it in word for word, after autofix) and the full trip
    # name (fix_dashes keeps it verbatim) are source text for F2 too (AA-800: and for the word layers)
    f2_source = "\n".join(t for t in (atom_text, cta, hygiene_ctx.trip_name if hygiene_ctx else None) if t)
    # S234 review — F15_voice blocks (stock phrases) only while `stock_blocking` (attempt 1).
    # S234 round 2 — plus contrast density and sibling templates, every channel. AA-924 — one check feeds both
    # F2_lexicon (its stock findings) and F15_voice (the rest), see below.
    voice = gate_voice(content_text, spec=spec, stock_blocking=stock_blocking, sibling_texts=sibling_texts,
                       exclude_texts=[t for t in (hygiene_ctx.lexicon.own_text if hygiene_ctx else atom_text, cta,
                                                  hygiene_ctx.facts if hygiene_ctx else None) if t],
                       brand_name=(brand_pack or {}).get("brand_name"), keyword=keyword, brief_texts=brief_texts)
    # AA-924 — the lexicon sources, each word judged once: banned patterns (AA-778 — the configured CTA and the full
    # trip name are source text for them too), the tenant's own forbidden words (AA-771, S233) and the F15 stock list.
    banned, forbidden, voice = lexicon_sources(content_text, f2_source, brand_pack, atom_text=atom_text, voice=voice,
                                               spec=spec)
    # The legacy (source) results in legacy order; merge_ledger below folds them into the stored entries.
    sources = [
        cta_result,
        gate_grounding(content_text, atom_text),
        banned,
        forbidden,
        # AA-800 — the word layers: platform truth blocks; stock superlatives capped at one; positioning
        # words the brand did not declare are a warning. Same source exemption as F2. AA-924 — a word on the
        # tenant's own forbidden list is the lexicon's only.
        gate_platform_truth(content_text, f2_source, (brand_pack or {}).get("forbidden_words")),
        *gate_signature_words(content_text, f2_source, brand_pack),
        gate_brand_name(content_text, atom_text, (brand_pack or {}).get("brand_name")),
        # AA-819 (AUP 2026-11) — no reviews / testimonials / customer first person / independence claim.
        gate_authenticity(content_text, cta, seo_title, meta_description),
        # AA-484 — every channel, not blog-only (cannibalization risk isn't channel-specific);
        # placed right after F2 (another content-safety-class gate), before the DET/judge gates.
        gate_cannibalization(cannibalization_match),
        # AA-531 — channel-aware length band (blog gets its own wider band, see
        # _length_bounds_for_channel()); the other 7 channels are unaffected.
        gate_extreme_length(length_check_text, channel),
        gate_complete(content_text),  # AA-768 (S233) — a cut-off piece never ships as finished
        # AA-768 (S234) — scope (warn for one round) and residual hygiene (warn), every channel;
        # both pass when the caller gives no hygiene context.
        gate_scope(content_text, hygiene_ctx, seo_title, meta_description),
        gate_hygiene(content_text, hygiene_ctx),
        gate_leak(content_text, hygiene_ctx),  # S234 round 2 — blocking leaks, sentence-repairable
        gate_repeat(content_text, hygiene_ctx),  # S234 round 3 — near-duplicates, sentence-repairable
        # S234 round 3 — trip facts (F17) and rarity (F18), every channel, sentence-repairable
        *gate_trip_facts(content_text, trip_ctx, seo_title, meta_description),
        # AA-771 (S234) — LinkedIn shape and voice from the brand's channel spec; both pass for a
        # channel without a spec (every channel but LinkedIn).
        # AA-780 — under the floor blocks on attempt 1 only (`stock_blocking`), for one full rewrite.
        gate_social_shape(content_text, spec, floor_blocking=F14_FLOOR_BLOCKING and stock_blocking),
        # F15_voice (the source check, deduplicated above) and the warn-only checks (F15_voice_warn), kept apart
        # so a stock repair never rewrites them; AA-924 stores both in one F15_voice entry.
        voice,
        gate_voice_warn(content_text, spec=spec, brand_pack=brand_pack, atom_text=atom_text,
                        sibling_openers=sibling_openers, cta=cta),
    ]
    if channel == "blog":
        sources.append(gate_seo_surface(seo_title, meta_description, slug, keyword, seed=atom_text,
                                        body=content_text, taken_titles=taken_titles))
    # AA-786 — F9 judges what the tenant reads: no citation tags (S234: "visible citation markers"
    # notes on r1#9 / r3#4), plus the blog's search title and meta. F9 returns TWO results (blocking
    # F9_cta_fact + non-blocking F9_brand_style), spread in here.
    seo_kw = {"seo_title": seo_title, "meta_description": meta_description} if channel == "blog" else {}
    # AA-777 — the job's sticky-pass set (sentences an earlier F9 verdict passed); None = no memory.
    if f9_results is not None:
        sources += [dict(g) for g in f9_results]  # AA-918 — a $0 replay: the stored verdicts, no judge call
    else:
        sources += gate_brand_voice(length_check_text, cta or "", brand_rubric_text, source_text=atom_text,
                                    voice=(brand_pack or {}).get("voice"), passed_sentences=f9_passed_sentences,
                                    **seo_kw)
    # S234 round 2 — contract gates owned by a dedicated fix step in service.py (FAQ regeneration,
    # first-line rewrite). Last in the ledger and not repairable: when one is the only blocking
    # failure left the piece is held at once; when another gate fails first, that gate's repair
    # runs (sentence_repair.plan skips these two).
    if channel == "blog":
        sources.append(gate_faq_contract(content_text))
    if spec is not None:
        sources.append(gate_first_line(content_text, spec))
    # AA-789 tier 1 — seed alignment (spaCy + the seed's typed facts + a trigger word). Per class: off (does not run;
    # the ledger is unchanged), shadow (recorded in outcome["seed_align"] only), repair / hold ('sa:<class>: …'
    # findings appended to F17_trip_fact / F18_rarity / F13_leak / F13_repeat before the ledger is merged). Fails open.
    sa_run = seed_align.run(content_text, trip_ctx=trip_ctx, hygiene_ctx=hygiene_ctx, brief_texts=brief_texts)
    # AA-1000 tier 2 — an LLM adjudicator over the sentences tier 1 triggered on but left alone (per class: off / shadow
    # / repair, default off: None, no call). Never holds: at most one 'sa:t2.<class>: …' finding per attempt, routed
    # like tier 1's (never_holds). A $0 replay (f9_results given) calls nothing. Fails open.
    t2_run = seed_adjudicate.run(content_text, tier1=sa_run, trip_ctx=trip_ctx, hygiene_ctx=hygiene_ctx,
                                 brief_texts=brief_texts, replay=f9_results is not None)
    sources = seed_align.apply(sources, sa_run)
    sources = seed_align.apply(sources, t2_run)
    # AA-924 — the stored ledger: F2_banned_patterns + F2_brand_forbidden + the F15 stock findings → F2_lexicon,
    # F15_voice + F15_voice_warn → F15_voice, each finding with its own severity (26 → 24 distinct results).
    gate_ledger = merge_ledger(sources)
    # AA-519 Việc 5 — first_failure (drives service.py's hold/repair decision) only ever
    # considers a BLOCKING gate; a failed non-blocking gate never holds/repairs a piece, but its
    # result is still in gate_ledger AND collected into `flags` below so it isn't silently
    # dropped — it ships WITH the piece as a note.
    # AA-924 — routing reads the legacy view (expand_ledger); `first_failure` is a legacy result (its gate a
    # source name), `flags` has the ledger's shape.
    legacy = expand_ledger(gate_ledger)
    first_failure = next((g for g in legacy if not g["passed"] and g["blocking"]), None)
    flags = reshape_like([g for g in legacy if not g["passed"] and not g["blocking"]], gate_ledger, flags=True)
    outcome: QualityCheckOutcome = {
        "passed": first_failure is None,
        "gate_ledger": gate_ledger,
        "first_failure": first_failure,
        "flags": flags,
    }
    if sa_run is not None or t2_run is not None:
        # AA-1000 — tier 2's record of this attempt sits next to tier 1's (writer_input.seed_align[*].tier2)
        outcome["seed_align"] = {**(sa_run.record if sa_run is not None else {}),
                                 **({"tier2": t2_run.record} if t2_run is not None else {})}
    return outcome


__all__ = [
    "GateResultLite", "QualityCheckOutcome", "TAG_RE",
    "strip_citation_tags", "deep_strip_citation_tags", "gate_cta_present", "gate_grounding",
    "gate_banned_patterns", "gate_cannibalization", "gate_complete", "gate_brand_forbidden_words",
    "gate_brand_name", "gate_extreme_length",
    "gate_seo_surface", "restated_faq_answers",
    "gate_brand_voice", "run_quality_gates", "drop_restated_faq", "keyword_covered",
    "drop_unanswerable_faq", "unanswerable_faq_topics", "answer_copies_body", "strip_copied_sentences",
    "unused_source_facts",
    "faq_pairs", "merge_faq_pairs", "FAQ_MAX_PAIRS",
    "faq_subject_tokens", "faq_answer_problem", "drop_subjectless_faq", "paa_for_scope", "PAA_MAX_QUESTIONS",
    "gate_scope", "gate_hygiene", "F12_SCOPE_BLOCKING",
    "gate_social_shape", "gate_voice", "gate_voice_warn", "hold_failure", "F15_STOCK_BLOCKING", "STYLE_ONLY_GATES",
    "F14_FLOOR_BLOCKING", "never_holds", "BROETRY_MAX_WORDS", "BROETRY_RUN",
    "FAQ_MIN_PAIRS", "F16_GATE", "faq_pair_count", "replace_faq_section", "gate_faq_contract", "F13_LEAK_GATE",
    "gate_leak", "F14_FIRST_LINE_GATE", "gate_first_line", "OWNED_BY_FIX_STEP",
    "F13_REPEAT_GATE", "gate_repeat", "title_key", "title_taken",
    "F17_GATE", "F18_GATE", "gate_trip_facts", "CHEAP_REPAIR_GATES", "F9_NO_REASON", "F9_PROMPT_VERSION",
    "F19_GATE", "gate_authenticity",
    "F2_PLATFORM_GATE", "F2_SUPERLATIVE_GATE", "F2_STYLE_WARN_GATE", "gate_platform_truth", "gate_signature_words",
    "Finding", "BLOCK", "WARN", "F15_STOCK_PREFIXES", "is_merged", "ledger_is_merged", "expand_ledger",
    "merge_ledger", "merge_flags", "reshape_like", "ledger_gate", "gate_lexicon", "lexicon_sources",
]
