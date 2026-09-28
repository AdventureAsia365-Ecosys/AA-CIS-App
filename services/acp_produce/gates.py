"""
services.acp_produce.gates — MODULE F: N7 QA gate stack.

F1 grounding (AA-298 Phần A) · F2 banned patterns, F3 structural variance,
F4 brief compliance, F5 atom density (AA-404, see below), F6
route-to-sellable, F7 FAQ dedup (AA-372) · F8 framework judge, F9 brand/SEO
audit (blog) + F9 social (facebook/tiktok, AA-372) — cross-weight LLM judges.

Numbering note: F2/F3/F4/F6/F7 follow AA-372's OWN renumbering (confirmed
against its Linear text, not the aamc/ prototype's F2=atom density/
F3=banned/F4=structural/F5=brief-compliance/F6=route/F7=faq scheme). AA-372's
renumbering shifted atom density's original slot away and never re-assigned
it a number, leaving "F5" genuinely unused (docs/implementation-notes/
AA-364.md's own docstring in pipeline.py said so explicitly: "F5 does not
exist by design"). AA-404's N0-N8 defense-layer audit (docs/claude_audit/
AA-404-N0-N8-defense-layer-audit.md) confirmed atom density itself — not just
its number — was never built at all, and is CONTEXT.md §1.6's own
highest-leverage anti-AI-voice layer ("AI-sounding text is an information-
density problem, not a style problem: starve genericity out with
specifics"), directly explaining F9's #1 blog failure code
(BODY_EXPERIENCE_DETAILS_TOO_GENERIC, 22/109 real occurrences). Built here as
`gate_atom_density()`, claiming the open "F5" slot — a real gate this time,
not just a number.

F1 ported from the aa-marketing-v2 research build's aamc/gates.py::
gate_grounding() with the P0-1 bug fixed during the port (ADR-2026-029)
rather than carrying it forward and patching it later — see
docs/implementation-notes/AA-298.md.

F2 (AA-372) folds in the fix for B12 (aamc/'s banned-pattern scan flagged
verbatim-cited atom/fact text as a false positive). AA-327 traced B12/B10 to
what THAT issue calls "F3" — AA-327 was written before AA-372's renumbering
and used the aamc scheme, where banned-patterns WAS F3; under AA-372's real
numbering both bugs belong to F2, not F3 (docs/implementation-notes/AA-372.md
§4 works through the citation trail). B10 (BrandRubric.compile_brand()
reading the wrong reject-list field) does not apply to F2 here — no
BrandRubric/compile_brand() subsystem exists in this repo (AA-327, Backlog)
for F2 to inherit the bug from; it just scans a hardcoded global lexicon.

Shares its entailment mechanism with S1-from-atom
(services/content_generation/s1_from_atom.py::check_grounding(), AA-325) via
services/acp_shared/grounding.py — one implementation, not two that can drift
(ADR-2026-033). ADR-2026-033 also documents why this is a narrow
numeric/measurement check rather than the whole-sentence token-overlap ratio
ADR-2026-029 originally specified: tested against 107 real (sentence, atom)
pairs from a live production audit, the token-overlap approach could not
separate real violations from real good content at any threshold.
"""
from __future__ import annotations

import json
import re
from typing import Callable

import structlog

from services.acp_produce.judge_client import invoke_judge, parse_judge_json
from services.acp_produce.models import (REPAIR_BUDGET_CAP, REPAIR_TOTAL_MAX, Brief, GateResult,
                                            Piece, RepairRoundLog)
from services.acp_shared.grounding import find_novel_numeric_claims

logger = structlog.get_logger()

TAG_RE = re.compile(r"\[(?:R|F):([^\]]+)\]")
# Same sentence-boundary heuristic used to build the real-data test fixture
# (tests/unit/fixtures/aa325_grounding_units.json) and s1_from_atom.py's gate.
#
# AA-405: tolerate up to 2 markdown bold asterisks (`**`) immediately around the
# whitespace boundary. Without this, `**Q: ...**` / `**A:` FAQ markers (faq.py::
# render_faq_section()'s real format) never split from the sentence before them
# -- "...into Seoul [R:atom_x].\n\n**Q: What is the 52 hour rule...**" stayed ONE
# "sentence" carrying a citation from the itinerary paragraph and numbers from an
# unrelated FAQ answer, so gate_grounding() blamed the wrong citation for numbers
# it never claimed. Confirmed via re.split() test and the real week=2 fabrication
# alarm (docs/implementation-notes/AA-405.md). `{0,2}` matches real markdown's `**`
# exactly; it does not touch the common non-bold case (`\*{0,2}` matches empty).
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\*{0,2}\s+\*{0,2}(?=[A-Z\"'‘’“”])")

# AA-404 N0-N8 audit follow-up, Q1's #3 finding: E1's outline assigns atoms per SECTION
# (`Brief.atoms_by_section`, `OutlineSection.atom_ids`) but F1 has only ever checked a tag
# against the WHOLE piece's atom pool (`valid_ids` below) — a section could cite an atom
# genuinely valid for the piece but never assigned to THAT section, and F1 would never notice.
# `_H2_RE` segments `body_tagged` the same way F4_brief_compliance already does
# (`re.findall(r"^## (.+)$", body, re.MULTILINE)`) — reused, not a new convention.
_H2_RE = re.compile(r"^## (.+)$", re.MULTILINE)


def _check_section_atom_scoping(body: str, atoms_by_section: dict[str, list[str]]) -> list[str]:
    """AA-404 N0-N8 audit follow-up (opt-in — see gate_grounding()'s own docstring for why
    this is not on by default). Within EACH H2 section, only that section's own
    outline-assigned atom_ids are valid citations — a tag cited in a DIFFERENT section (even
    though globally valid for the piece) is now flagged. A section title that isn't a key in
    `atoms_by_section` (e.g. "FAQ" — `generation.py::build_outline()` never assigns it one,
    E4/faq.py owns that section) contributes no section-scoped check for that stretch of
    text — the existing whole-piece closed-world/entailment checks above still cover it."""
    violations: list[str] = []
    matches = list(_H2_RE.finditer(body))
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        if title not in atoms_by_section:
            continue
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        section_text = body[start:end]
        allowed = set(atoms_by_section[title])
        cited = set(TAG_RE.findall(section_text))
        for aid in sorted(cited - allowed):
            violations.append(
                f"section '{title}' cites [R:{aid}], which the outline assigned to a DIFFERENT "
                f"section — each section may only cite its own assigned atoms"
            )
    return violations


def gate_grounding(
    body_tagged: str, valid_ids: set[str], text_by_id: dict[str, str],
    atoms_by_section: Optional[dict[str, list[str]]] = None,
) -> GateResult:
    """F1 grounding (DET). Two checks, both required to pass:

    1. Closed-world — every [R:id]/[F:id] tag references an id that actually
       exists in `valid_ids` (the atom/fact set assigned to this brief).
    2. Entailment — no cited sentence asserts a number/measurement absent
       from the text of the id(s) it cites (see services/acp_shared/
       grounding.py for why this is narrower than plain tag-presence: a
       valid tag on a fabricated sentence used to pass this gate — that was
       P0-1).

    `atoms_by_section` (AA-404 N0-N8 audit follow-up, opt-in, default `None`): when given,
    ALSO enforces that each H2 section only cites the atoms the outline actually assigned to
    IT (see `_check_section_atom_scoping()`). Left OFF by default and not wired into
    `pipeline.py`'s live blog call site in this PR — real-data impact analysis (23 real blog
    pieces across all 6 N7 runs to date) found 9/23 (39%) already have at least one atom cited
    in more than one section (mostly an overview/intro section legitimately previewing a
    detail a later section develops in depth) — enforcing this unconditionally today would
    likely convert some currently-passing or already-marginal pieces into new F1 failures.
    Built and tested here so the mechanism exists and is reviewable; enabling it for real
    traffic is a decision left to Nghiep (see the PR description for this change)."""
    violations: list[str] = []
    body = body_tagged or ""

    tags = TAG_RE.findall(body)
    unknown = sorted({t for t in tags if t not in valid_ids})
    for uid in unknown:
        violations.append(f"unknown provenance id [{uid}] — not in the atom/fact set for this brief")

    if atoms_by_section:
        violations.extend(_check_section_atom_scoping(body, atoms_by_section))

    for sent in _SENT_SPLIT_RE.split(body):
        cited = TAG_RE.findall(sent)
        if not cited:
            continue
        cited_texts = [text_by_id[c] for c in cited if c in text_by_id]
        novel = find_novel_numeric_claims(sent, cited_texts)
        if novel:
            violations.append(
                f"sentence states {novel} not present in its cited id(s): '{sent.strip()[:100]}'"
            )

    return GateResult(gate="F1_grounding", passed=not violations, violations=violations)


# ---------------------------------------------------------------- F5 atom density (DET, AA-404)

# aa-marketing-v2/CONTEXT.md §1.6.1, verbatim: "Atom density validator: every 200–300 words
# of body must cite ≥1 atom/facts entry. Zero-atom paragraphs are flagged — that's where
# AI-voice lives." The real aamc/ prototype (aamc/config.py::ATOM_DENSITY_WORDS) resolved
# "200–300" to a single window of 300 (the upper bound) rather than leaving it a range —
# ported verbatim here, not re-decided, same value.
ATOM_DENSITY_WORDS = 300


def gate_atom_density(body_tagged: str) -> GateResult:
    """F5 atom density (DET, AA-404 — see module docstring's Numbering note for why "F5").
    Faithful port of aamc/gates.py::gate_atom_density() — same algorithm, same window
    constant, same violation shape — not a re-design. Chunks `body_tagged` into
    non-overlapping windows of `ATOM_DENSITY_WORDS` words (mechanical word-count chunking,
    not markdown-paragraph-aware — matches the aamc original, which also just did
    `body.split()` over the raw text); a chunk fails if it contains zero `[R:atom_id]`/
    `[F:fact_id]` tags (reuses this module's own `TAG_RE`, not a new parser).

    Threshold: ANY zero-atom chunk fails the whole piece (`passed = not violations`) — no
    percentage/leniency knob. This mirrors the aamc original's own already-decided design
    (`if not TAG_RE.search(chunk): violations.append(...)`, unconditional) — not a threshold
    invented for this port. Flagged for Nghiep's review anyway (PR description) since it's a
    strict, all-or-nothing rule same as F9's, on a NEW gate — worth an explicit sign-off even
    though it's not new decision-making, just adopting the existing spec's own strictness.

    A trailing chunk shorter than `ATOM_DENSITY_WORDS // 2` is skipped, not flagged — same
    aamc guard, prevents a short leftover fragment at the very end of the body (which could
    never reasonably reach a full window) from spuriously failing the gate. This is also what
    naturally exempts short-form channels: no explicit `channel` check was added (the aamc
    original's `gate_atom_density(piece)` never took one either) — real facebook (80-150
    words)/tiktok (100-150 words) piece bodies fall entirely under the 150-word floor, so the
    single implicit "chunk" they'd form is skipped outright, before the tag-presence check
    ever runs. Verified by test (`test_facebook_length_body_is_exempted_by_window_floor` /
    `test_tiktok_length_body_is_exempted_by_window_floor`), not just asserted."""
    body = body_tagged or ""
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
                f"words {start}-{start + len(chunk_words)}: zero atom/fact citations in this "
                f"stretch — that's where AI-voice lives (CONTEXT.md §1.6.1); add a specific, "
                f"verifiable detail or cut it. First 80 chars: '{chunk[:80]}'"
            )
    return GateResult(gate="F5_atom_density", passed=not violations, violations=violations)


# ---------------------------------------------------------------- F2 banned patterns (DET)

# Global AI-voice lexicon seed — ported verbatim from the aa-marketing-v2
# research build's aamc/config.py::BANNED_PATTERNS_SEED (that build's
# CONTEXT.md §1.6.2: "banned-pattern lexicon... seeded globally, extended by
# audit failures"). No tenant-extension mechanism here — that needs the
# brand-rubric-compiler subsystem (BrandRubric.compile_brand()), AA-327
# (Backlog), out of scope for this chunk.
BANNED_PATTERNS_SEED: list[str] = [
    r"\bnestled\b",
    r"\btapestry\b",
    r"\bhidden gem(s)?\b",
    r"\bmust[- ]visit\b",
    r"\bmust[- ]see\b",
    r"\bunforgettable\b",
    r"\bbreathtaking\b",
    r"\bbucket[- ]list\b",
    r"\bwhether you'?re .{3,40} or .{3,40}\b",
    r"\bin conclusion\b",
    r"\bembark on\b",
    r"\bawait(s)? you\b",
    r"\bimmerse yourself\b",
    r"\blook no further\b",
    r"\bdelve\b",
]

_BANNED_PATTERNS_COMPILED = [re.compile(p, re.IGNORECASE) for p in BANNED_PATTERNS_SEED]


def gate_banned_patterns(body_tagged: str, text_by_id: dict[str, str]) -> GateResult:
    """F2 banned patterns (DET). Scans `body_tagged` for the global AI-voice
    lexicon above. B12 fix folded into the port (see module docstring, not
    patched later): a match is EXEMPT only when the sentence it falls in
    carries a `[R:id]`/`[F:id]` citation AND the matched substring is itself
    present (case-insensitive) in that id's own source text — i.e. the
    phrase is genuinely quoted from the registry/atom, not AI-generated
    prose that happens to sit near a citation. `text_by_id` reuses
    gate_grounding()'s (F1) exact parameter shape — no new data plumbing.
    Same sentence-boundary heuristic as F1 (`_SENT_SPLIT_RE`)."""
    body = body_tagged or ""
    violations: list[str] = []

    for sent in _SENT_SPLIT_RE.split(body):
        cited_ids = TAG_RE.findall(sent)
        cited_texts = [text_by_id[c].lower() for c in cited_ids if c in text_by_id]
        for pattern in _BANNED_PATTERNS_COMPILED:
            for m in pattern.finditer(sent):
                phrase = m.group(0).lower()
                if cited_texts and any(phrase in t for t in cited_texts):
                    continue  # B12: genuinely verbatim-cited registry text, exempt
                violations.append(f"banned pattern /{pattern.pattern}/ -> '{m.group(0)}'")

    return GateResult(gate="F2_banned_patterns", passed=not violations, violations=violations)


# ---------------------------------------------------------------- F3 structural variance (DET)

def gate_structural_variance(body_tagged: str, channel: str) -> GateResult:
    """F3 structural variance (DET) — blog only (facebook/tiktok have their
    own short, template-driven shape from adapt.py's format instructions;
    the "no rhythm variance" AI-tell this gate targets is a long-form-article
    problem). Direct port of aamc/gates.py::gate_structural_variance() — no
    B10/B12 baggage here (see module docstring: both bugs trace to F2/
    banned-patterns despite AA-372's own text filing them under a stale "F3"
    label).

    Checks (each independent, all must pass): (1) at least one genuinely
    one-sentence paragraph exists (uniform paragraph length is an AI tell);
    (2) with >=3 H2 sections, the longest is notably longer (>=1.4x) than
    the second-longest (uniform section length is another AI-uniformity
    tell); (3) at most 1 bulleted list in the whole piece."""
    if channel != "blog":
        return GateResult(gate="F3_structural_variance", passed=True)

    body = body_tagged or ""
    violations: list[str] = []
    paras = [p for p in body.split("\n\n") if p.strip() and not p.startswith("#")]

    if not any(
        len(re.split(r"(?<=[.!?])\s+", p.strip())) == 1 and len(p.split()) >= 2
        for p in paras
    ):
        violations.append("no one-sentence paragraph found (variance rule)")

    sections = re.split(r"^## ", body, flags=re.MULTILINE)[1:]
    if len(sections) >= 3:
        lens = sorted(len(s.split()) for s in sections)
        if lens and lens[-1] < lens[-2] * 1.4:
            violations.append("no section is notably longer than the others")

    blocks = len([b for b in body.split("\n\n") if re.match(r"^\s*[-*] ", b)])
    if blocks > 1:
        violations.append(f"{blocks} bulleted lists — max 1 per article")

    return GateResult(gate="F3_structural_variance", passed=not violations, violations=violations)


# ---------------------------------------------------------------- F4 brief compliance (DET)

def gate_brief_compliance(body_tagged: str, channel: str, brief: Optional[Brief]) -> GateResult:
    """F4 brief compliance (DET) — blog only, ported from aamc/gates.py::
    gate_brief_compliance() against the real AA-369 `Brief` fields (keyword,
    required_h2s, word_range, internal_links — all exist as-is, no invented
    fields). `brief=None` FAILS CLOSED (L6: can't verify compliance against
    nothing, so don't silently pass) — unlike the channel branch, a real
    blog piece missing its Brief is a caller-contract bug worth surfacing,
    not a legitimate "nothing to check" state."""
    if channel != "blog":
        return GateResult(gate="F4_brief_compliance", passed=True)
    if brief is None:
        return GateResult(gate="F4_brief_compliance", passed=False,
                           violations=["no Brief available to check compliance against"])

    body = body_tagged or ""
    violations: list[str] = []

    if brief.keyword.lower() not in body.lower():
        violations.append(f"keyword '{brief.keyword}' absent from body")

    got_h2s = {h.strip().lower() for h in re.findall(r"^## (.+)$", body, re.MULTILINE)}
    for h in brief.required_h2s:
        if h.strip().lower() not in got_h2s:
            violations.append(f"required H2 missing: '{h}'")

    wc = len(TAG_RE.sub("", body).split())
    lo, hi = brief.word_range
    if not (lo * 0.7 <= wc <= hi * 1.3):
        violations.append(f"word count {wc} outside range {lo}-{hi} (+/-30%)")

    for link in brief.internal_links:
        if link and link not in body and link != brief.cta_target:
            violations.append(f"internal link not placed: {link}")

    return GateResult(gate="F4_brief_compliance", passed=not violations, violations=violations)


# ---------------------------------------------------------------- F6 route-to-sellable (DET)

def gate_route_to_sellable(
    body_tagged: str, channel: str, cta_target: Optional[str], url_alive: Optional[bool],
) -> GateResult:
    """F6 route-to-sellable (DET). Fail-closed by design (Nghiep, 06/08/2026):
    no `acp_deliver.tenant_tour_pages` row for this tour is treated the same
    as a confirmed-dead route — `url_alive` must be exactly `True`, not just
    not-`False`. No `manual_check`/soft status here — a route-to-sellable
    failure blocks `publish_mode` past `propose_only` (`packets.py::
    set_publish_mode()`'s `ALLOWED_PUBLISH_MODES_UNTIL_F6` guard), it does
    not create a review queue outside the defined HITL gates.

    `cta_target`/`url_alive` are pre-fetched by the caller (same convention
    as F9's `brand_rubric_text` and F1's `text_by_id` — this gate does no DB
    I/O itself). The literal-CTA-in-body sub-check is blog-only: FB/TikTok
    captions reference the trip conversationally, never embedding a literal
    URL (confirmed against adapt.py's real output format)."""
    violations: list[str] = []

    if not cta_target:
        violations.append("no CTA target -- a beautiful dead-end is a failure")
    if url_alive is not True:
        violations.append(
            "route-to-sellable not confirmed alive (no acp_deliver.tenant_tour_pages row, "
            "or url_alive is not True) -- fail-closed, never a silent/manual_check pass"
        )
    if channel == "blog" and cta_target and cta_target not in (body_tagged or ""):
        violations.append(f"CTA {cta_target} not present in body")

    return GateResult(gate="F6_route_to_sellable", passed=not violations, violations=violations)


# ---------------------------------------------------------------- F7 FAQ dedup (DET, intra-piece)

_FAQ_ANSWER_RE = re.compile(r"\*\*Q: .*?\*\*\s*\nA: (.*?)(?=\n\*\*Q: |\Z)", re.DOTALL)
_FAQ_TOKEN_RE = re.compile(r"[a-z]{5,}")
_FAQ_DEDUP_THRESHOLD = 0.85  # ported from aamc/gates.py::gate_faq_dedup()


def gate_faq_dedup(body_tagged: str) -> GateResult:
    """F7 FAQ dedup (DET) — INTRA-piece only for this chunk
    (docs/implementation-notes/AA-372.md §8): does a FAQ answer just restate
    a body paragraph within the SAME piece? Cross-time dedup against
    previously-published FAQs for the same tour/slot needs a new table that
    does not exist yet (`faq.py` confirms `Piece.faq_items`/`faq_jsonld` are
    Pydantic-only) — flagged as a follow-up, not built here.

    Matches the REAL AA-371 FAQ section format (`faq.py::render_faq_section()`
    — `"## FAQ"` then `"**Q: ...**"` / `"A: ..."` pairs), not the aamc
    prototype's different markdown shape. No-op (passed=True) when the piece
    has no FAQ section at all."""
    body = body_tagged or ""
    if "## FAQ" not in body:
        return GateResult(gate="F7_faq_dedup", passed=True)

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

    return GateResult(gate="F7_faq_dedup", passed=not violations, violations=violations)


# ---------------------------------------------------------------- F8 framework judge (LLM, cross-weight)

# Deterministic rubric table — not LLM-invented, matches the framework the N6
# allocator (services/acp_planning/constants.FRAMEWORK_TABLE) already assigned
# per (funnel_stage, channel). The judge scores against THIS fixed list, never
# against its own idea of what a "good hub article" looks like.
FRAMEWORK_RUBRICS: dict[str, list[str]] = {
    "hub": ["covers the topic comprehensively via subsections",
            "each section answers a distinct sub-question"],
    "PAS": ["opens with the reader's problem", "agitates concretely",
            "resolves with the trip as solve"],
    "AIDA": ["attention hook first", "interest via specifics",
             "desire built on concrete moments", "single clear action (CTA)"],
    "hook_story_cta": ["first line is the hook", "one atom, one emotion"],
    "hook_beats_payoff": ["hook stated", "timed beats present", "payoff lands"],
    "reader_as_hero": ["reader is the subject, not the brand", "single CTA"],
}
# AA-382: exported (no leading underscore) — repair.py imports this alongside
# FRAMEWORK_RUBRICS so a repair round targeting an F8 violation can see the
# framework's FULL rubric, not just the one failing criterion's bare name
# (see repair.py::PieceInvariants.framework_rubric_items).
DEFAULT_FRAMEWORK_RUBRIC = ["structure matches the stated framework"]

# AA-396 follow-up: "ends with CTA" pulled out of hook_story_cta's Nova Pro
# rubric and made deterministic. Real N7 data (docs/implementation-notes/
# AA-391-report-data.json) showed the judge was actively unreliable on this
# specific criterion: a facebook/blog piece whose body ended in a literal
# `[Design This Journey](url)` link scored 0, while a sibling piece whose CTA
# link was followed by more prose scored 1 -- the judge's verdict was
# uncorrelated with, sometimes inverted from, the actual text. "Ends with a
# CTA" is a structural/positional question, not a semantic one -- a
# deterministic check is strictly more reliable here. AIDA's "single clear
# action (CTA)" and reader_as_hero's "single CTA" are left on the LLM judge:
# those ask about clarity/singularity of the ask, a genuinely semantic
# question this fix does not touch.
#
# Every real CTA instance in the corpus -- markdown-linked (blog) or plain
# prose (facebook/tiktok) -- uses the one brand-standard phrase
# (services/content_generation/brand_standards.py: CTA is always "Design
# This Journey", never "Book Now"), so the check anchors on that phrase
# rather than a generic link/URL pattern (a URL-only pattern would wrongly
# fail every facebook piece -- gate_route_to_sellable()'s own docstring
# confirms FB/TikTok captions "reference the trip conversationally, never
# embedding a literal URL").
_CTA_PHRASE_RE = re.compile(r"design this journey", re.IGNORECASE)


def _ends_with_cta(piece_body: str) -> bool:
    """Checks the FINAL `\\n\\n`-delimited paragraph of the body for the CTA
    phrase, not just the literal trailing characters -- a real passing
    example (slot_4139's blog piece) closes its CTA link with a short coda
    sentence in the same paragraph ("[Design This Journey](url) with the
    understanding that..."); that's still "ending with" the CTA in the sense
    the rubric means, just not the literal last token."""
    body = (piece_body or "").rstrip()
    if not body:
        return False
    last_para = body.split("\n\n")[-1]
    return bool(_CTA_PHRASE_RE.search(last_para))

def _format_audit_reason(
    failure_codes: list[str], notes: Optional[str], flagged_phrases: Optional[list[str]] = None,
) -> str:
    """AA-396: build the repair-visible reason string for a failed F9 audit
    (blog or social). Previously `", ".join(failure_codes) or audit.get("notes")`
    dropped `notes` entirely whenever any failure_codes existed — repair_fn
    (repair.py) only ever saw the terse code list, never the judge's actual
    explanation. `notes` is now appended alongside the codes, not used only
    as a fallback when codes is empty.

    `flagged_phrases` (AA-382, keyword-only, defaults to `None` so every
    pre-AA-382 caller keeps producing the exact same string): the judge's own
    verbatim quote(s) of the offending text for a SUMMARY_OFF_BRAND/
    GENERIC_AI_WORDING code (`audit["flagged_phrases"]`, AA-404 PR #153 fix
    #3 — captured into the audit dict since then but never threaded past it
    into `result.violations`, the only channel `repair_fn` actually reads).
    Real evidence this gap mattered (docs/claude_audit/AA-404-n7-run6-
    results.md, F9_brand_seo_audit_social): 3 repair rounds on the same
    piece each flagged a DIFFERENT phrase and never converged — repair was
    never told which exact phrase the judge meant, only a failure code name
    plus a free-text `notes` summary, so it had to guess where to intervene.
    Appended here (not swapped in for codes/notes) so the fix is additive:
    every existing caller/test asserting a substring of the old format still
    matches."""
    codes = ", ".join(failure_codes)
    notes = (notes or "").strip()
    if codes and notes:
        reason = f"{codes} -- {notes}"
    else:
        reason = codes or notes or "(no reason given)"
    phrases = [p for p in (flagged_phrases or []) if p]
    if phrases:
        quoted = "; ".join(f'"{p}"' for p in phrases)
        reason += f" -- exact flagged phrase(s): {quoted}"
    return reason


_JUDGE_SYSTEM_PROMPT = (
    "You are a structural editor. You score writing against a fixed rubric — you do not "
    "rewrite, you do not soften scores, and every score of 1 must be backed by an exact "
    "quote from the piece as evidence. You have not seen and do not know how this piece "
    "was generated or instructed to be written; judge only what is on the page in front "
    "of you."
)


def _log_n7_judge_call(raw: dict, *, gate: str, passed: bool, extra: dict) -> None:
    """AA-505 — shared by this file's 3 invoke_judge() call sites (F8, F9 blog, F9 social).
    tenant_id=None — same reasoning as quality_gates.py's own _log_t10_judge_call(): these gate
    functions take only piece_body/rubric text, no tenant/piece identity, by design."""
    from shared.llm_client.pricing import calc_cost
    from shared.llm_client.call_log import record_call_sync
    model = raw.get("model_used", "unknown")
    in_tok, out_tok = raw.get("input_tokens", 0), raw.get("output_tokens", 0)
    cost = raw.get("cost_usd")
    record_call_sync(
        stage="n7_judge", role="judge", model=model,
        tokens_in=in_tok, tokens_out=out_tok,
        cost_usd=cost if cost is not None else calc_cost(model, in_tok, out_tok),
        tenant_id=None,
        quality_signal={"gate": gate, "passed": passed, **extra},
        stop_reason=raw.get("stop_reason"),
        account=raw.get("account"), fallback_used=raw.get("fallback_used"),
    )


def gate_framework(piece_body: str, framework: str) -> GateResult:
    """F8 framework judge — LLM, Nova Pro, cross-weight from the writer per
    ADR-2026-014/ADR-2026-027/L3, PLUS one deterministic sub-check (AA-396
    follow-up: hook_story_cta's "ends with CTA", see _ends_with_cta() above
    for why this one criterion was pulled off the LLM). The judge receives
    ONLY the piece body and a hard-anchored rubric (see FRAMEWORK_RUBRICS
    above) — never the writer's generation system/user prompt (services/
    acp_produce/judge_client.py documents why that isolation is structural,
    not just promised). Binary 1/0 per criterion with a MANDATORY evidence
    quote for every 1 — never a 1-10 scale, which invites drift with no
    accountable evidence trail."""
    violations: list[str] = []
    if framework == "hook_story_cta" and not _ends_with_cta(piece_body):
        violations.append("framework criterion failed: ends with CTA")

    rubric_items = FRAMEWORK_RUBRICS.get(framework, DEFAULT_FRAMEWORK_RUBRIC)
    contract = json.dumps({
        "items": [{"criterion": "str", "score": "1|0",
                   "evidence": "exact quote from the piece, or empty string if score is 0"}],
    }, indent=1)
    user_prompt = (
        f"PIECE:\n{piece_body}\n\n"
        f"RUBRIC (framework: {framework}) — score each item 1 (met) or 0 (not met), quoting "
        f"exact evidence from the piece for every 1:\n- " + "\n- ".join(rubric_items) +
        f"\n\nOutput ONLY JSON matching this contract:\n{contract}"
    )
    try:
        raw = invoke_judge(_JUDGE_SYSTEM_PROMPT, user_prompt, stage="n7_judge")
        data = parse_judge_json(raw["text"])
    except Exception as e:
        violations.append(f"judge unavailable: {e} — manual check")
        return GateResult(gate="F8_framework", passed=False, violations=violations)

    items = data.get("items") or []
    for item in items:
        criterion = item.get("criterion", "(unnamed criterion)")
        score = str(item.get("score"))
        evidence = item.get("evidence") or ""
        if score != "1":
            violations.append(f"framework criterion failed: {criterion}")
        elif not evidence.strip():
            violations.append(f"framework criterion '{criterion}' scored 1 with no evidence quote — treated as fail")
    if not items:
        violations.append("judge returned no rubric items — treated as fail, not a silent pass")
    _log_n7_judge_call(raw, gate="F8_framework", passed=not violations,
                        extra={"items_total": len(items),
                               "items_passed": sum(1 for i in items if str(i.get("score")) == "1")})
    return GateResult(gate="F8_framework", passed=not violations, violations=violations)


# ---------------------------------------------------------------- F9 brand_seo_audit (LLM, cross-weight)

# Fixed failure-code vocabulary — the judge must classify into ONE of these,
# never invent its own label (a free-text failure reason can't be tracked,
# trended, or turned into an acp_output_rules entry later, N8 flywheel).
#
# AA-396: 4 of these codes were originally verbatim copies of the S1 pipeline's
# own failure vocabulary (services/content_generation/graph.py::_CODE_FIELD_MAP /
# flag_fix_node.py — where SEO_TITLE_*, aa_highlights, aa_itineraries, seo_meta
# are REAL discrete generated_content DB columns). N7 has no such columns —
# every one of these qualities lives inside the single `piece_body` prose
# handed to the judge. A real repair (Sonnet) run confused the two: reading
# "SEO_TITLE_WEAK"/"HIGHLIGHTS_TOO_GENERIC"/etc., it concluded these named
# fields that "don't exist in this document" and refused to act, leaking that
# reasoning into the piece body instead of fixing it (see repair.py's
# leak-guard, same AA-396 fix). Renamed the 4 colliding codes below to remove
# the exact-string collision at its source — no code elsewhere imports these
# as literals (confirmed: this list has zero cross-module consumers outside
# this file), so the rename has no other blast radius.
BRAND_SEO_FAILURE_CODES = [
    "PRODUCT_TRUTH_RISK", "SUMMARY_OFF_BRAND", "BODY_EXPERIENCE_DETAILS_TOO_GENERIC",
    "BODY_DAY_FLOW_STRUCTURE_WEAK", "BODY_OPENING_TITLE_WEAK", "BODY_SUMMARY_LINE_INCOMPLETE",
    "DFS_INTENT_UNDERUSED", "KEYWORD_STUFFING_RISK", "GENERIC_AI_WORDING",
    "FACT_CHECK_MANUAL_CHECK",
]


# AA-404 F9 STEP 0 follow-up fix #2 (docs/implementation-notes/AA-404.md's F9
# root-cause section): neither BRAND_SEO_FAILURE_CODES nor
# SOCIAL_SEO_FAILURE_CODES ever defined what "GENERIC_AI_WORDING"/
# "SUMMARY_OFF_BRAND" concretely mean -- the judge was free to invent a fresh
# definition every round, which real data confirmed (3 different phrases
# flagged across 3 repair rounds on the same piece, never converging). Same
# "concrete contrast anchor" pattern already used for S1 rewrites
# (graph.py::_build_brand_diff_block()'s "GENERIC PHRASING TO AVOID" block) --
# a negative example plus a positive one, not just an abstract instruction.
# The GOOD example below is not invented: it is the exact sentence a real F9
# audit (week=1 retrigger, 15/08/2026, piece
# de8337ba...:slot_845eb6ec83cdf1f082ec:blog#facebook) flagged as
# GENERIC_AI_WORDING/SUMMARY_OFF_BRAND -- confirmed on inspection to be
# specific, verifiable, on-brand prose, not generic filler. Using the judge's
# own real false positive as the calibration anchor is deliberate: it targets
# the exact miscalibration observed, not a guessed-at one.
GENERIC_AI_WORDING_ANCHOR = (
    "\n\nWHAT COUNTS AS GENERIC_AI_WORDING (concrete anchor, not a vague vibe check):\n"
    "- BAD (generic -- flag this): \"Experience the best of South Korea's rich culture and stunning "
    "landscapes on this unforgettable journey.\" -- templated superlatives, no concrete detail, "
    "swappable onto any destination or any brand with a find-and-replace.\n"
    "- GOOD (specific, on-brand -- do NOT flag this): \"A bullet train departing Seoul southward "
    "delivers you to Gyeongju -- ancient capital of the Silla Kingdom -- in under two hours, arriving "
    "at a city where royal burial mounds still rise from the centre of residential streets.\" -- a "
    "concrete, verifiable detail (place name, real fact, specific geography); calm and precise, no "
    "adjective padding, no superlatives.\n"
    "A sentence built from a SPECIFIC, concrete, verifiable detail is NOT generic, even when its "
    "register is quiet or understated -- do not flag calm, precise, unhurried writing as \"too "
    "abstract\", \"too poetic\", or \"not curated enough\" just because it lacks superlatives or "
    "sounds evocative. Reserve GENERIC_AI_WORDING for templated filler that could be copy-pasted "
    "onto any other brand's any other trip unchanged.\n\n"
    "WHAT COUNTS AS SUMMARY_OFF_BRAND: the text violates a REQUIRED or FORBIDDEN word from the brand "
    "rubric above, or contradicts a stated voice attribute outright (e.g. reads salesy/urgent when "
    "the rubric calls for calm and unhurried) -- not merely \"could have been phrased differently.\" "
    "The brand's own REQUIRED language (including its mandated CTA phrase, if the rubric states one) "
    "is never grounds for SUMMARY_OFF_BRAND or GENERIC_AI_WORDING on its own -- required language "
    "cannot simultaneously be a violation.\n"
)


def gate_brand_seo_audit(piece_body: str, brand_rubric_text: str) -> tuple[GateResult, dict | None]:
    """F9 brand/SEO audit — LLM, Nova Pro, cross-weight (same isolation
    guarantee as F8, see gate_framework() and judge_client.py). Caller
    supplies `brand_rubric_text` already fetched (this function does no DB
    I/O itself, same convention as gate_grounding() taking pre-fetched
    valid_ids/text_by_id).

    `brand_rubric_text` source (AA-404 F9 fix #1, docs/implementation-notes/AA-404.md): the real
    caller (`brand.py::fetch_brand_rubric_text()`, originally written in `slot_runner.py` for F9
    fix #1, moved to a shared leaf module in the writer-side wire follow-up so E2/E3/E4/E5 could
    import it too without a layering problem) now reads `shared.tenant_brand_rules`'s `'default'`
    row for the tenant — the hardcoded, generic `AA_BRAND_IDENTITY_PROMPT` constant is now only a
    fallback for a tenant with no populated brand content, not the live path for `aa_internal`
    anymore. This corrects two earlier states of this docstring: it originally (incorrectly)
    claimed `shared.tenant_brand_rules` was already the source before any wiring existed at all;
    a later pass corrected that to say the opposite (hardcoded-only) after an investigation found
    `aa_internal`'s `'default'` row empty and its other 6 rows mis-attached test/demo data for
    other (fictional B2B demo) tenants — that content has since been (a) populated with
    `aa_internal`'s real brand voice on the `'default'` row and (b) had the 6 mis-attached rows
    removed (same session as this wiring). See `GENERIC_AI_WORDING_ANCHOR`
    above for the concrete good/bad examples still injected directly into the prompt regardless
    of rubric source — that mitigation stays, this is additive on top of it, not a replacement.

    Binary 1/0 fields, fixed failure-code vocabulary — never a free-text
    verdict that can't be tracked or trended. Returns (GateResult, audit_dict
    | None) — audit_dict is None only when the judge call itself failed."""
    contract = json.dumps({
        "status": "pass|flagged|manual_check",
        "brand_fit": "1|0", "human_read": "1|0", "seo_fit": "1|0",
        "trip_type_accuracy": "1|0", "publish_readiness": "1|0",
        "failure_codes": [f"subset of {BRAND_SEO_FAILURE_CODES}"],
        "flagged_phrases": ["exact quoted phrase from the piece for each SUMMARY_OFF_BRAND/"
                             "GENERIC_AI_WORDING code above -- [] if neither code is used"],
        "notes": "str",
    }, indent=1)
    user_prompt = (
        f"PIECE:\n{piece_body}\n\n"
        f"BRAND RUBRIC:\n{brand_rubric_text}\n"
        f"{GENERIC_AI_WORDING_ANCHOR}\n"
        "Audit in this order: product truth -> brand fit -> trip type -> highlights -> "
        "readability -> SEO -> publish readiness. Score every field 1 or 0. Use ONLY the "
        f"listed failure codes: {BRAND_SEO_FAILURE_CODES}. If you use SUMMARY_OFF_BRAND or "
        "GENERIC_AI_WORDING, you MUST quote the exact offending phrase in `flagged_phrases` -- one "
        "entry per phrase; never use either code without a quote. When uncertain about a factual "
        "claim: status=manual_check + FACT_CHECK_MANUAL_CHECK.\n\n"
        f"Output ONLY JSON matching this contract:\n{contract}"
    )
    try:
        raw = invoke_judge(_JUDGE_SYSTEM_PROMPT, user_prompt, stage="n7_judge")
        data = parse_judge_json(raw["text"])
    except Exception as e:
        return GateResult(gate="F9_brand_seo_audit", passed=False,
                           violations=[f"judge unavailable: {e} — manual check"]), None

    status = data.get("status", "manual_check")
    failure_codes = [c for c in (data.get("failure_codes") or []) if c in BRAND_SEO_FAILURE_CODES]
    flagged_phrases = [p for p in (data.get("flagged_phrases") or []) if isinstance(p, str) and p.strip()]
    audit = {
        "status": status,
        "brand_fit": data.get("brand_fit"), "human_read": data.get("human_read"),
        "seo_fit": data.get("seo_fit"), "trip_type_accuracy": data.get("trip_type_accuracy"),
        "publish_readiness": data.get("publish_readiness"),
        "failure_codes": failure_codes, "flagged_phrases": flagged_phrases, "notes": data.get("notes"),
    }
    passed = status == "pass"
    violations = []
    if not passed:
        reason = _format_audit_reason(failure_codes, audit.get("notes"), flagged_phrases)
        violations = [f"audit {status}: {reason}"]
    _log_n7_judge_call(raw, gate="F9_brand_seo_audit", passed=passed,
                        extra={"status": status, "failure_codes": failure_codes})
    return GateResult(gate="F9_brand_seo_audit", passed=passed, violations=violations), audit


# ---------------------------------------------------------------- F9 social (LLM rubric, AA-372)

# Separate, minimal fixed vocabulary from BRAND_SEO_FAILURE_CODES (blog) —
# kept append-only-per-domain rather than merged, so a future N8 flywheel
# dashboard querying blog failure codes doesn't have to filter out
# social-only codes that never apply to blog pieces, and vice versa.
SOCIAL_SEO_FAILURE_CODES = [
    "SUMMARY_OFF_BRAND", "CTA_MISSING_OR_WEAK", "HOOK_WEAK",
    "GENERIC_AI_WORDING", "FACT_CHECK_MANUAL_CHECK",
]

_SOCIAL_RUBRIC_FIELDS: dict[str, list[str]] = {
    "facebook": ["brand_fit", "cta_clear", "human_read"],
    "tiktok": ["hook_strength", "cta_clear"],
}


# AA-404 Part 4b: real week=1 data showed the judge flagging the literal,
# brand-MANDATED CTA phrase itself ("Design This Journey" -- required exactly
# as-is, brand_standards.py:12/AA_BRAND_IDENTITY_PROMPT's CTA line) as
# GENERIC_AI_WORDING (docs/implementation-notes/AA-404.md §3 quotes the real
# round-2 audit: "the summary uses phrases like 'Design This Journey' which
# ... feels somewhat generic"). Required brand language cannot be simultaneously
# off-brand -- this is a rubric bug, not a writer-prompt gap the writer could
# ever fix by rewriting. NOTE (risk flag for review, AA-404 PR description):
# this changes the EVALUATION CRITERIA of a rubric Ms. Thu originally wrote,
# not just a prompt -- narrowly scoped to the one confirmed false-positive
# shape (CTA phrase mistaken for generic wording), not a rewrite of
# GENERIC_AI_WORDING/SUMMARY_OFF_BRAND's broader (still uncalibrated, see
# gate_brand_seo_audit_social()'s own docstring) definition.
_CTA_PHRASE_ONLY_CODES = {"SUMMARY_OFF_BRAND", "GENERIC_AI_WORDING"}


def _is_cta_phrase_only_evidence(flagged_phrases: list[str]) -> bool:
    """True only when EVERY quoted phrase offered as evidence is itself the
    brand CTA phrase — a judge response quoting the CTA phrase ALONGSIDE a
    genuinely different off-brand phrase does not qualify (that's still a
    real complaint, not a CTA-phrase false positive) — `all()` over an empty
    list would vacuously be True, which is why this also requires
    `flagged_phrases` to be non-empty."""
    return bool(flagged_phrases) and all(_CTA_PHRASE_RE.search(p) for p in flagged_phrases)


def gate_brand_seo_audit_social(
    piece_body: str, channel: str, brand_rubric_text: str,
) -> tuple[GateResult, Optional[dict]]:
    """F9 social (LLM, cross-weight, AA-372) — minimal per-channel rubric,
    NOT a parameterization of gate_brand_seo_audit() (blog). Decided
    06/08/2026 (option b of AA-372's open question, resolved outside Linear):
    2-3 criteria per channel, because no real FB/TikTok piece has run
    through F8/F9 yet to know what it actually needs (Mistake-to-Rule,
    ADR-2026-009 — extend from real failures, don't guess the full
    blog-shaped rubric ahead of data). Banned-pattern policing is
    deliberately excluded here — that's F2's job (deterministic, cheaper,
    more consistent than asking an LLM to also police brand-voice red
    lines).

    Facebook (3 fields): brand_fit, cta_clear (single unambiguous action,
    not a vague sign-off), human_read (reads like a person, not templated AI
    copy). TikTok (2 fields): hook_strength (the HOOK line would stop a
    scroll in the first second), cta_clear (SCRIPT ends with an actual call
    to action, not just a fact). Same isolation guarantee, `status`/
    manual_check convention, and judge call as gate_brand_seo_audit()."""
    fields = _SOCIAL_RUBRIC_FIELDS.get(channel)
    if fields is None:
        raise ValueError(f"gate_brand_seo_audit_social() has no rubric for channel={channel!r}")

    contract = json.dumps({
        "status": "pass|flagged|manual_check",
        **{f: "1|0" for f in fields},
        "failure_codes": [f"subset of {SOCIAL_SEO_FAILURE_CODES}"],
        "flagged_phrases": ["exact quoted phrase from the piece for each SUMMARY_OFF_BRAND/"
                             "GENERIC_AI_WORDING code above -- [] if neither code is used"],
        "notes": "str",
    }, indent=1)
    user_prompt = (
        f"PIECE ({channel}):\n{piece_body}\n\n"
        f"BRAND RUBRIC:\n{brand_rubric_text}\n"
        f"{GENERIC_AI_WORDING_ANCHOR}\n"
        "Score every field 1 or 0. Use ONLY the listed failure codes: "
        f"{SOCIAL_SEO_FAILURE_CODES}. If you use SUMMARY_OFF_BRAND or GENERIC_AI_WORDING, you MUST "
        "quote the exact offending phrase in `flagged_phrases` -- one entry per phrase. The brand's "
        "own mandated CTA phrase (\"Design This Journey\") is REQUIRED language, never grounds for "
        "SUMMARY_OFF_BRAND or GENERIC_AI_WORDING on its own -- do not flag it. When uncertain about "
        "a factual claim: status=manual_check + FACT_CHECK_MANUAL_CHECK.\n\n"
        f"Output ONLY JSON matching this contract:\n{contract}"
    )
    try:
        raw = invoke_judge(_JUDGE_SYSTEM_PROMPT, user_prompt, stage="n7_judge")
        data = parse_judge_json(raw["text"])
    except Exception as e:
        return GateResult(gate="F9_brand_seo_audit_social", passed=False,
                           violations=[f"judge unavailable: {e} -- manual check"]), None

    status = data.get("status", "manual_check")
    failure_codes = [c for c in (data.get("failure_codes") or []) if c in SOCIAL_SEO_FAILURE_CODES]
    flagged_phrases = [p for p in (data.get("flagged_phrases") or []) if isinstance(p, str) and p.strip()]

    # AA-404 Part 4b allowlist (see _is_cta_phrase_only_evidence()'s own
    # comment above): drop SUMMARY_OFF_BRAND/GENERIC_AI_WORDING ONLY when the
    # CTA phrase is the sole quoted evidence for them -- any other flagged
    # code (CTA_MISSING_OR_WEAK, HOOK_WEAK, FACT_CHECK_MANUAL_CHECK) is left
    # exactly as the judge returned it.
    if _is_cta_phrase_only_evidence(flagged_phrases) and _CTA_PHRASE_ONLY_CODES & set(failure_codes):
        failure_codes = [c for c in failure_codes if c not in _CTA_PHRASE_ONLY_CODES]
        if not failure_codes and status == "flagged":
            status = "pass"

    audit = {
        "status": status, "channel": channel,
        **{f: data.get(f) for f in fields},
        "failure_codes": failure_codes, "flagged_phrases": flagged_phrases, "notes": data.get("notes"),
    }
    passed = status == "pass"
    violations = []
    if not passed:
        reason = _format_audit_reason(failure_codes, audit.get("notes"), flagged_phrases)
        violations = [f"audit {status}: {reason}"]
    _log_n7_judge_call(raw, gate="F9_brand_seo_audit_social", passed=passed,
                        extra={"status": status, "failure_codes": failure_codes, "channel": channel})
    return GateResult(gate="F9_brand_seo_audit_social", passed=passed, violations=violations), audit


# ---------------------------------------------------------------- orchestration + repair budget (P0-3)

# AA-396 follow-up (option C, Nghiep — decided over option B "repair every
# failing gate in one round"): ADR-2026-029's flat REPAIR_TOTAL_MAX=3 starves
# a piece that starts with multiple gates failing at once, because run_gates()
# below only ever targets ONE gate per round (the first-failing gate, in
# gate_fns order) — 3 rounds can fix at most 3 simultaneous failures. Real
# data proved this: piece slot_b09166b7f2fdc28c87fd:blog (docs/
# implementation-notes/AA-391-report-data.json, aa394_followup_test) started
# with F3+F4+F8+F9 all failing and was mathematically unable to converge
# within budget. Confirmed via the same corpus that 4 is the worst case
# actually observed across all 9 real held pieces — see REPAIR_BUDGET_CAP's
# own comment in models.py for how that calibrates the cap below.
def compute_repair_budget(initial_failing_gate_count: int, base_repairs: int = REPAIR_TOTAL_MAX) -> int:
    """Scales the repair-round budget to how many gates were ALREADY failing
    when a piece's repair loop started — computed once from that first
    gate-stack run (see run_gates()), never recomputed on a later round, so
    the budget is fixed per piece at the outset rather than a moving target.
    `initial_failing_gate_count <= 1` always returns exactly `base_repairs`
    (3, ADR-2026-029) — the common single-gate-failure case is completely
    unaffected by this change. Each additional simultaneous failure beyond
    the first earns exactly one more round, capped at REPAIR_BUDGET_CAP so a
    pathological piece can't spiral into unbounded Sonnet spend."""
    return min(base_repairs + max(0, initial_failing_gate_count - 1), REPAIR_BUDGET_CAP)


def run_gates(
    piece: Piece,
    gate_fns: list[Callable[[str], GateResult]],
    repair_fn: Callable[[str, list[str]], str],
    max_repairs: int = REPAIR_TOTAL_MAX,
    is_repairable: Callable[[GateResult], bool] = lambda result: True,
) -> Piece:
    """P0-3 fix: after EVERY repair, re-run the ENTIRE gate stack, not just the
    gate that just failed. The aamc/gates.py bug this replaces re-checked only
    the single gate that had failed — a repair aimed at fixing gate 3 could
    silently re-break gate 1, and the old code would ship it because it never
    looked at gate 1 again. Each call to `repair_fn` still targets one gate's
    violations (the first failing gate found, in `gate_fns` order) — the fix
    is in what gets VALIDATED afterward, not in trying to fix everything at
    once. `piece.gate_ledger` after return is always the ledger from the
    round that decided the outcome (all-pass or held), not a stale one from a
    superseded gate.

    `is_repairable` (AA-376): an optional predicate over the first failing
    `GateResult`, defaulting to always-True so every existing caller/test is
    unaffected. This orchestrator stays gate-agnostic on purpose (the same
    reason F8's rubric routing and F9's blog/social routing live in
    `pipeline.py`'s closures, not here) — the one real caller
    (`pipeline.py::run_piece_through_produce_gates()`) supplies a real
    predicate that holds immediately (without spending a repair round or a
    Sonnet call) on F6 violations that are external caller/DB state
    ("no cta_target", "url_alive not True") rather than something a
    body_tagged rewrite could ever fix.

    A `repair_fn` that raises is treated the same as "repair could not fix
    this" (L6: hold visible with the ORIGINAL failure's reason, never crash
    the whole slot-production run over one piece's repair infra failure) —
    `piece.repair_count` is only incremented on a repair_fn call that
    actually returns, matching the aamc/ prototype's own repair() (which
    only incremented its own counter after a successful LLM call).

    AA-396 follow-up (option C, dynamic repair budget): `max_repairs` is now
    the BASE budget (ADR-2026-029's 3), not always the piece's final
    ceiling — `compute_repair_budget()` scales it up once, from THIS piece's
    first gate-stack run's failing-gate count, before any repair happens.
    Every existing caller/test passing `max_repairs=3` against a
    single-failing-gate scenario is unaffected: the computed budget equals
    `max_repairs` exactly whenever the piece starts with 0 or 1 failing
    gates. The repair STRATEGY itself (one gate targeted per round, full
    P0-3 re-run after each repair) is unchanged — only the round ceiling is
    dynamic now.

    `piece.repair_budget`/`piece.initial_failing_gate_count`/
    `piece.repair_log` (models.py, AA-396 follow-up) are populated here and
    persisted by the caller (`pipeline.py::_persist_piece()`) so a human
    reviewing a held piece has the full per-round repair history in the same
    place they already look (`acp_deliver.pieces`), not a separate log
    store. Promoting a recurring pattern from that history into an
    `acp_output_rules` row stays a MANUAL step. H-3's automatic
    Haiku-extraction path (`h3_rule_extractor.py`) was keyed on a human
    reviewer's rejection note tied to a real `acp_hitl_requests` row, which
    an auto-repair-exhausted N7 piece never has; it had no caller left and
    was deleted in AA-685.

    AA-415 (verify-real-run follow-up, docs/implementation-notes/AA-415-
    verify-real-run.md): `repair_budget` is still computed ONCE from the
    piece's first gate-stack run (unchanged) but MAY be extended by exactly
    +1, capped at `REPAIR_BUDGET_CAP`, the first time a repair round targets
    a gate that was NOT among that first run's failing gates. Real evidence
    (run `363f22c9`, `slot_efb6c6d175e23bad4767:blog#tiktok`):
    `initial_failing_gate_count=1` (only F9_social) gave `repair_budget=3`;
    rounds 1-2 targeted F9_social, and by the time F1_grounding appeared for
    the first time in round 3 (a real side effect of the F9 repair rewrite,
    the exact class of regression PR #170's `atom_text_by_id` narrows but
    does not eliminate — see that doc's Step 4), only ONE round of budget
    was left for it, which failed, and the piece held with F1 never having
    gotten a fair, `compute_repair_budget()`-sized share of rounds. Treating
    a late-discovered gate as "one more distinct failing gate this piece has
    had to deal with" is the SAME additive spirit `compute_repair_budget()`
    already applies to gates failing simultaneously at the start — this
    reuses that spirit for gates discovered mid-loop instead of inventing a
    second budget mechanism. The extension is keyed per DISTINCT late gate
    (tracked in `late_gates_extended` below), not per round, so a late gate
    that keeps failing round after round doesn't re-trigger the bump every
    time — `REPAIR_BUDGET_CAP` remains the one hard ceiling against
    pathological Sonnet spend, same as before this change."""
    repair_budget: int | None = None
    pending_round: RepairRoundLog | None = None
    initial_failing_gates: set[str] = set()
    late_gates_extended: set[str] = set()

    while True:
        piece.gate_ledger = []
        first_failure: GateResult | None = None
        failing_count = 0
        for gate_fn in gate_fns:
            result = gate_fn(piece.body_tagged)
            piece.gate_ledger.append(result)
            if not result.passed:
                failing_count += 1
                if first_failure is None:
                    first_failure = result

        if repair_budget is None:
            repair_budget = compute_repair_budget(failing_count, base_repairs=max_repairs)
            piece.initial_failing_gate_count = failing_count
            piece.repair_budget = repair_budget
            initial_failing_gates = {g.gate for g in piece.gate_ledger if not g.passed}
            logger.info("n7_repair_budget_computed", piece_id=piece.piece_id,
                        initial_failing_gate_count=failing_count, repair_budget=repair_budget)

        if pending_round is not None:
            targeted = next((g for g in piece.gate_ledger if g.gate == pending_round.gate_targeted), None)
            pending_round.outcome = "passed" if (targeted is None or targeted.passed) else "failed"
            logger.info("n7_repair_round_result", piece_id=piece.piece_id,
                        round=pending_round.round, gate_targeted=pending_round.gate_targeted,
                        outcome=pending_round.outcome)
            pending_round = None

        if first_failure is None:
            piece.status = "passed"
            _log_repair_loop_summary(piece, "passed")
            return piece

        if not is_repairable(first_failure):
            _hold(piece, first_failure)
            _log_repair_loop_summary(piece, "held")
            return piece

        # AA-415: a gate that was passing on the first gate-stack run but is
        # failing NOW is a late-discovered failure (most often a side effect
        # of an earlier round's repair targeting a DIFFERENT gate) — give it
        # the same one-extra-round headroom compute_repair_budget() already
        # gives a gate that was simultaneously failing from the start,
        # rather than letting it get squeezed by whatever budget the
        # ORIGINAL failing gates already used up. Only the first round
        # targeting a given late gate extends the budget — a late gate that
        # keeps failing across several rounds doesn't get a fresh +1 every
        # time, and REPAIR_BUDGET_CAP still bounds the total regardless.
        if (first_failure.gate not in initial_failing_gates
                and first_failure.gate not in late_gates_extended
                and repair_budget < REPAIR_BUDGET_CAP):
            late_gates_extended.add(first_failure.gate)
            repair_budget = min(repair_budget + 1, REPAIR_BUDGET_CAP)
            piece.repair_budget = repair_budget
            logger.info("n7_repair_budget_extended_for_late_gate", piece_id=piece.piece_id,
                        gate=first_failure.gate, new_repair_budget=repair_budget)

        if piece.repair_count >= repair_budget:
            _hold(piece, first_failure)
            _log_repair_loop_summary(piece, "held")
            return piece

        round_entry = RepairRoundLog(
            round=piece.repair_count + 1, gate_targeted=first_failure.gate,
            violations=list(first_failure.violations), outcome="attempted",
        )
        piece.repair_log.append(round_entry)
        pending_round = round_entry
        logger.info("n7_repair_round_attempt", piece_id=piece.piece_id, round=round_entry.round,
                    gate_targeted=round_entry.gate_targeted, violations=round_entry.violations)

        try:
            piece.body_tagged = repair_fn(piece.body_tagged, first_failure.violations)
        except Exception as e:
            logger.warning("gate_repair_fn_failed", gate=first_failure.gate, error=str(e))
            round_entry.outcome = "exception"
            pending_round = None
            _hold(piece, first_failure)
            _log_repair_loop_summary(piece, "held")
            return piece
        piece.repair_count += 1


def _hold(piece: Piece, result: GateResult) -> Piece:
    """L6: hold VISIBLY with a concrete reason — never a silent gap."""
    piece.status = "held"
    piece.held_reason = f"{result.gate}: {'; '.join(result.violations[:3])}"
    return piece


def _log_repair_loop_summary(piece: Piece, outcome: str) -> None:
    """AA-396 follow-up: one final structured log line per piece that went
    through run_gates(), independent of the per-round persisted repair_log
    (this is for live CloudWatch visibility; repair_log on the Piece is the
    durable, human-reviewable record). `never_repaired_gates` is only
    meaningful for outcome="held" — a "passed" piece has nothing left
    failing by definition."""
    never_repaired = [g.gate for g in piece.gate_ledger if not g.passed] if outcome == "held" else []
    logger.info(
        "n7_repair_loop_summary", piece_id=piece.piece_id,
        initial_failing_gate_count=piece.initial_failing_gate_count,
        repair_budget=piece.repair_budget, rounds_used=piece.repair_count,
        outcome=outcome, never_repaired_gates=never_repaired,
    )
