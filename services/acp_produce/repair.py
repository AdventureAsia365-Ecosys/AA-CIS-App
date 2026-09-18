"""
services.acp_produce.repair — N7 E5 (Repair), AA-376.

Ported from the aa-marketing-v2 research build's aamc/generation.py::repair()
(docs/implementation-notes/AA-376.md STEP 0 confirmed this is real prior art,
not a fresh design) with the same behavior contract: fix ONLY the listed
violations, preserve structure/voice/length, return the full corrected
`body_tagged` — never a diff/patch, never a partial section. One function
handles every gate's violations (F1-F9 alike) — the prototype's own
`run_gates()` never split repair by gate either, it always called the same
`repair()` regardless of which gate failed.

Wiring (AA-376): this is the `repair_fn: Callable[[str, list[str]], str]`
`run_gates()` (gates.py) has accepted in its signature since AA-298/P0-3 but
never had a real implementation for — `pipeline.py` previously passed
`_repair_not_available()` with `max_repairs=0`, so this path was never
reachable (see pipeline.py's pre-AA-376 docstring in git history). Model is
Bedrock satellite acc3 Sonnet (AA-397, acc1 fallback under it), same
`invoke_claude(..., model="sonnet")` call as E2/E3 (generation.py/adapt.py) —
CHỐT per AA-334 (Palmyra Cancelled, see
generation.py's own docstring). Palmyra must never appear anywhere in this
module.

Not repaired here (caller's job — see gates.py::run_gates()'s `is_repairable`
filter, AA-376): a violation that isn't fixable by editing `body_tagged` at
all (F6's "no cta_target"/"url_alive not True" — external DB/caller-supplied
state, not content) must never reach this function — `run_gates()` filters
those out and holds immediately instead of wasting a repair round + a Sonnet
call on a violation this module cannot possibly fix by rewriting text.

AA-382 (F8/F9 rubric-context fix, docs/implementation-notes/AA-382-repair-rubric-context.md):
before this change, an F8/F9 (LLM-judge) violation reached this module as a SHORT string only —
F9's failure code(s) + free-text `notes` (gates.py::_format_audit_reason()), F8's one failing
criterion's bare 3-6 word name — never the rubric those judges actually scored against. Real
evidence this mattered (docs/claude_audit/AA-404-n7-run6-results.md,
F9_brand_seo_audit_social): 3 repair rounds on the same piece flagged 3 DIFFERENT phrases,
never converging. `brand_rubric_text` (AA-404) already closed part of this gap for F9 — this
fix closes the rest: (1) F9's `flagged_phrases` (the judge's own exact quote of the offending
text, captured into the audit dict since AA-404 PR #153 fix #3 but never threaded past it) now
rides along inside the violation string itself (`_format_audit_reason()`), so repair knows
WHICH phrase, not just which failure code; (2) `GENERIC_AI_WORDING_ANCHOR` (gates.py's own
concrete good/bad calibration example) is now in repair's system prompt too, not just the
judge's; (3) F8's full framework rubric (`FRAMEWORK_RUBRICS`, gates.py) is now carried in
`PieceInvariants.framework_rubric_items` alongside the one-criterion violation string, the same
"gate's own rubric reaches repair, not just a narrow violation string" shape AA-404 already
established for F9. F8 currently has no `GENERIC_AI_WORDING_ANCHOR`-equivalent concrete
good/bad anchor — deliberately not fabricated here (no real F8 false-positive/convergence data
to calibrate one from yet, unlike F9's, which was built from an observed real case; same
Mistake-to-Rule/ADR-2026-009 discipline gates.py's own F9-social docstring already cites: extend
from real failures, don't guess ahead of data).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional

import structlog

from services.acp_produce.atom_usage import ATOM_CITE_RE
from services.acp_produce.gates import DEFAULT_FRAMEWORK_RUBRIC, FRAMEWORK_RUBRICS, GENERIC_AI_WORDING_ANCHOR
from services.content_generation.brand_standards import AA_BRAND_IDENTITY_PROMPT
from shared.llm_client.bedrock_satellite import BedrockUnavailable, invoke_claude
from shared.llm_client.role_config import get_stage_config_sync
from shared.llm_client.call_log import record_call_sync
from shared.llm_client.pricing import calc_cost

logger = structlog.get_logger()

# Brand-mandated CTA phrase (also gates.py::_CTA_PHRASE_RE, adapt.py's
# _FACEBOOK_INSTRUCTIONS, AA_BRAND_IDENTITY_PROMPT's own "CTA:" line — this
# repo has no single shared constant for it yet; each module that needs the
# literal string already carries its own copy, this is one more, not a new
# duplication pattern).
_CTA_PHRASE = "Design This Journey"

_MAX_INVOKE_ATTEMPTS = 3  # same retry policy as E2/E3 (generation.py/adapt.py)
_RETRY_BACKOFF_SECONDS = 2.0
# Repair returns the FULL piece body (not a batch/section) — same ceiling as
# the project-wide "max_tokens = 4096, NOT 2000" rule (JSON-truncation fix).
_MAX_TOKENS = 4096

# AA-404 writer-side wire (F9 deep-dive TL;DR #1): the brand block used to be
# baked into a module-level constant built once at import time from the
# generic `AA_BRAND_IDENTITY_PROMPT` — repair.py was one of the 4 writer
# modules (with generation.py/adapt.py/faq.py) still doing this while F9's
# judge (PR #158) had already moved on to a real per-tenant rubric. Now built
# per-call from `PieceInvariants.brand_rubric_text` (see `_build_repair_
# system_prompt()` below) so a repair round judges/rewrites against the SAME
# rubric text F9 scores it against — `AA_BRAND_IDENTITY_PROMPT` stays as the
# hard default for the no-`invariants` call shape every pre-AA-404 caller/test
# still uses.
_REPAIR_HARD_RULES = (
    "\n\nHARD RULES FOR THIS REPAIR TASK:\n"
    "- Fix ONLY the violations listed below. Do not rewrite anything else.\n"
    "- Preserve the existing structure, voice, and length as closely as possible.\n"
    "- Preserve every [R:atom_id]/[F:fact_id] provenance tag exactly as given, unless a\n"
    "  violation specifically requires removing or changing one.\n"
    "- Any NEW prose you write to fix a violation must still avoid every word in the\n"
    "  FORBIDDEN language list above — fixing one gate's violation must never reintroduce a\n"
    "  banned word into content that was already clean before this repair round (AA-404: this\n"
    "  is the exact mechanism behind the real F4-repair -> F2-regression cases).\n"
    "- Output ONLY the full repaired text, same format as the input — no commentary, no\n"
    "  explanation, no markdown fence.\n"
)


# AA-382: F9's judge (gates.py::gate_brand_seo_audit()/gate_brand_seo_audit_social()) has
# scored GENERIC_AI_WORDING/SUMMARY_OFF_BRAND against this same concrete good/bad anchor since
# AA-404 fix #2 -- but only the JUDGE prompt ever saw it (gates.py's own two call sites).
# repair.py had NO calibration for what counts as "generic" beyond the bare failure code name,
# so a repair round could easily rewrite a flagged phrase into different prose the NEXT judge
# round would ALSO consider generic (root-cause half of the "3 rounds, 3 different phrases
# flagged, never converges" pattern -- see GENERIC_AI_WORDING_ANCHOR's own docstring in gates.py
# for the real piece this anchor was calibrated from). Appended unconditionally alongside
# brand_rubric_text (not gated on which gate's violations this round targets) because the same
# "don't write templated filler" principle applies to every repair round's NEW prose, not just
# an F9-triggered one -- matches the existing HARD RULES block's own unconditional scope.
def _build_repair_system_prompt(brand_rubric_text: str) -> str:
    return (
        "You are repairing an ALREADY-WRITTEN piece that failed a QA check.\n\n"
        + brand_rubric_text.strip() + "\n" + GENERIC_AI_WORDING_ANCHOR + _REPAIR_HARD_RULES
    )


# AA-396 (piece-7 class): a real Sonnet repair call, confused by the
# BRAND_SEO_FAILURE_CODES naming collision with the S1 pipeline's real DB
# fields (see gates.py's own AA-396 comment), returned its own chain-of-
# thought about why it couldn't act ("Looking at the violations, I need to
# identify... The current text is a summary/editorial piece — it does not
# contain discrete AA_HIGHLIGHTS... which means they cannot be repaired
# within this document as written.") instead of a repaired body, and that
# text got PERSISTED as the piece's real content. Narrow, evidence-based
# guard: only checked against the first paragraph (real leaks precede the
# actual repaired content, this one did) and only for phrases that are
# self-referential about the repair task itself — not any use of words like
# "violations" or "structure" a legitimate travel article might contain
# deeper in the body.
_LEAK_SIGNAL_RE = re.compile(
    r"\b(the violations?(\s+to\s+fix)?|cannot be repaired|i need to identify|"
    r"the current text is (a|an)\b|does not contain discrete|re-reading carefully)\b",
    re.IGNORECASE,
)


def _looks_like_leaked_reasoning(text: str) -> bool:
    """AA-396: does `text` open with repair's own meta-commentary about the
    repair task, rather than actual repaired content? Checked only against
    the first paragraph/prefix on purpose — narrows the guard to the exact
    corruption shape observed in real data (leak precedes the real content)
    instead of scanning the whole body, which would risk false-flagging
    legitimate prose that later discusses e.g. itinerary structure."""
    prefix = text.split("\n\n", 1)[0][:500]
    return bool(_LEAK_SIGNAL_RE.search(prefix))


@dataclass
class PieceInvariants:
    """AA-404 (STEP 0 "mở rộng" repair.py blind spot survey,
    docs/implementation-notes/AA-404.md §"repair.py::_build_prompt()"):
    piece-wide constraints that generation (E1-E3: generation.py/adapt.py)
    already decided for THIS piece and that must survive every repair round
    below, regardless of which gate's violations triggered that round.
    `repair_piece()` previously received only `(body_tagged, violations)` —
    the first failing gate's own narrow view — with zero visibility into any
    OTHER gate's structural requirements, which is exactly why a repair
    aimed at gate X could silently regress gate Y (confirmed real: 14
    regression events / 13 pieces across 4 causal pairs, see the doc above).

    All fields are optional/default-off so a piece with no applicable
    invariant (most non-blog, non-hook_story_cta pieces) produces an empty
    structural-context block — `_build_prompt()` never fabricates a
    constraint that generation didn't actually set.

    Every field here is either a direct `Brief` pass-through or re-derived
    from a pure function generation.py/adapt.py already export
    (`build_outline`/`_select_variance_owners`) — no new state storage, no
    new `Piece`/DB field, per the doc's own recommendation. The single
    exception, `single_atom_required`, is a flag only: the actual atom id(s)
    to preserve are re-derived fresh from the CURRENT `body_tagged` at
    prompt-build time (see `_currently_cited_atom_ids()`), so it stays
    correct even after an earlier repair round in the same loop already
    touched the body — a stored id could go stale, a re-derived one can't."""

    channel: str = "blog"
    # F3_structural_variance (blog only, AA-404 Part 1): the two section
    # titles generation.py::_select_variance_owners() assigned as the ONE
    # long section / ONE short-single-sentence-paragraph owner. Real
    # regression: F4-repair (adding/restructuring an H2) broke this 3x by
    # rewriting section content with no awareness of which section was
    # supposed to stay short vs. run long.
    long_section_title: Optional[str] = None
    short_para_section_title: Optional[str] = None
    # F4_brief_compliance (blog only): Brief.required_h2s, direct pass-
    # through, minus the synthetic "FAQ" entry (E4's job, never E2's). Real
    # regression: F9-repair (blog brand/SEO rewrite) broke this 3x by
    # silently dropping/renaming a required heading.
    required_h2s: list[str] = field(default_factory=list)
    # F8_framework hook_story_cta (facebook, AA-404 Part 2): True forces
    # repair to keep the piece citing only the atom id(s) already present in
    # the CURRENT body — the single-atom ceiling adapt.py's
    # _select_atoms_for_channel() enforces at generation time has no
    # equivalent enforcement once repair starts rewriting.
    single_atom_required: bool = False
    # F8_framework hook_story_cta (facebook): True when this piece's
    # framework requires the literal brand CTA phrase as its closing
    # sentence (gates.py::_ends_with_cta(), a deterministic check). Real
    # regression: F9-social-repair broke this 6x — the single dominant
    # pattern in the whole dataset (9/14 events came from an F9 variant).
    cta_required: bool = False
    cta_phrase: str = _CTA_PHRASE
    # F8_framework AIDA (blog, AA-404 Part 3): same class of gap as F3's —
    # which OutlineSection is first/last in the outline, for the opening-
    # hook/closing-single-CTA notes generation.py's E2 prompt gives them.
    # Not yet observed regressing in real data (only 1 real AIDA failure so
    # far, not preceded by an unrelated repair round) but structurally
    # identical exposure to F3's confirmed 3x — included so it doesn't need
    # its own follow-up patch once real data eventually exercises it.
    aida_opening_section_title: Optional[str] = None
    aida_closing_section_title: Optional[str] = None
    # AA-404 writer-side wire: the SAME real per-tenant rubric text F9's judge
    # (gate_brand_seo_audit()/gate_brand_seo_audit_social(), gates.py) scores
    # this piece against — see brand.py::fetch_brand_rubric_text(). Defaults
    # to the generic constant so every pre-AA-404 `PieceInvariants(...)` call
    # site (this repo's own tests included) that doesn't set this field keeps
    # repairing against exactly the prompt it always has.
    brand_rubric_text: str = AA_BRAND_IDENTITY_PROMPT
    # AA-415: the exact gap AA-404's own STEP-0-mở-rộng doc named and
    # explicitly deferred rather than guessed at ahead of real data (see
    # docs/claude_audit/AA-404-n7-run6-results.md) — real N7 run #6 confirmed
    # it: a repair round targeting F5_atom_density or F9_brand_seo_audit
    # writes brand-new prose to close a "too generic" / "under-cited" gap,
    # but repair_piece() had no atom/fact text to check that new prose
    # against, so it either added an untagged claim or tagged a claim the
    # cited id's text doesn't actually support — F1_grounding then fails for
    # the FIRST time on the piece's very last gate-stack check, after the
    # repair budget (sized off the ORIGINAL failing-gate count) is already
    # spent, so the piece never gets a dedicated F1 repair round. Same shape
    # as `required_h2s`/`brand_rubric_text` above: this is the SAME `dict[str,
    # str]` `gate_grounding()` (F1) and `gate_banned_patterns()` (F2) already
    # take as `text_by_id` — no new data plumbing, just carried into
    # `PieceInvariants` too. Defaults to `{}` (via `field`) so every
    # pre-AA-415 call site produces no grounding guidance, same
    # additive-only contract every other field here already follows.
    atom_text_by_id: dict[str, str] = field(default_factory=dict)
    # AA-382: F8_framework's full rubric for THIS piece's resolved framework (gates.py::
    # FRAMEWORK_RUBRICS[effective_framework]) — an F8 violation previously reached repair as
    # only the ONE failing criterion's bare name (e.g. "framework criterion failed: one atom,
    # one emotion"), with no visibility into the framework's OTHER criteria a fix must not
    # break, and no elaboration of what the failing criterion itself means beyond its own
    # 3-6 word phrase. Same class of gap AA-404 already fixed for F9's brand_rubric_text (a
    # gate's judge-side rubric now also reaches repair, not just the terse per-round
    # violation string) — this is the same fix applied to F8. `framework` is the resolved
    # key (pipeline.py's `effective_framework` — facebook/tiktok's rubric key, never the
    # caller's raw blog `framework` string, same resolution `_f8`'s own closure already
    # uses) so the rubric shown here always matches what F8's judge actually scored against.
    # Defaults to `None`/`[]` so every pre-AA-382 call site produces no framework block.
    framework: Optional[str] = None
    framework_rubric_items: list[str] = field(default_factory=list)


def _currently_cited_atom_ids(body_tagged: str) -> list[str]:
    """Unique `[R:atom_id]` ids in `body_tagged`, first-seen order — same
    regex (`atom_usage.ATOM_CITE_RE`) and same first-seen-order convention as
    `atom_usage.py::atom_ids_cited()`, reimplemented against a bare string
    here rather than a `Piece` (that function requires one; repair only ever
    has the current body string mid-round, not a `Piece` to construct)."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for atom_id in ATOM_CITE_RE.findall(body_tagged or ""):
        if atom_id not in seen_set:
            seen_set.add(atom_id)
            seen.append(atom_id)
    return seen


def _build_grounding_note(body_tagged: str, invariants: PieceInvariants) -> str:
    """AA-415: the F1_grounding-awareness line, folded into STRUCTURAL
    CONTEXT like every other invariant above rather than a separate prompt
    section — F5's ("add a specific, verifiable detail") and F9's ("too
    generic") violations both push repair toward writing brand-new prose,
    and the real regression (docs/claude_audit/AA-404-n7-run6-results.md)
    was that new prose either had no provenance tag at all or was tagged
    with a claim its cited id's own text doesn't support. Listing the known
    atom/fact text here lets repair pull a REAL detail instead of inventing
    one, and lets it self-check a claim against the id it's about to cite.

    When `invariants.single_atom_required` is set, only the atom(s) already
    cited in the CURRENT body are listed — showing the full pool here would
    directly contradict that invariant's own "cite ONLY these ids" line
    (`_build_structural_context()` above), which fires from the same
    `lines` list."""
    pool = invariants.atom_text_by_id
    if invariants.single_atom_required:
        cited = _currently_cited_atom_ids(body_tagged)
        pool = {aid: pool[aid] for aid in cited if aid in pool}
        if not pool:
            return (
                "- Grounding (F1_grounding gate): do not add any NEW [R:atom_id]/[F:fact_id] "
                "citation — this piece's single-atom ceiling (above) already covers which id(s) "
                "you may cite."
            )

    atom_lines = "\n".join(f"  [R:{aid}]: {text}" for aid, text in pool.items())
    return (
        "- Grounding (F1_grounding gate): if a fix requires writing a NEW sentence with a number, "
        "date, price, or other verifiable claim, that sentence MUST carry a [R:atom_id]/[F:fact_id] "
        "tag from the list below, AND the claim must actually be supported by that id's own text — "
        "never invent a claim with no tag, and never attach a tag to a claim its text doesn't "
        "support. Prefer pulling a real specific detail from one of these over writing generic "
        "prose. A sentence with no verifiable claim needs no tag. Known atoms/facts for this "
        f"piece:\n{atom_lines}"
    )


def _build_structural_context(body_tagged: str, invariants: Optional[PieceInvariants]) -> str:
    """AA-404: the "STRUCTURAL CONTEXT TO PRESERVE" block — every invariant
    line here is conditional on `invariants` actually carrying that field,
    so a piece with no applicable constraint (e.g. a tiktok piece, which has
    no single-atom ceiling and no CTA-phrase requirement) gets no
    fabricated guidance. Returns "" when nothing applies, so
    `_build_prompt()` can splice this in unconditionally."""
    if invariants is None:
        return ""
    lines: list[str] = []

    if invariants.long_section_title:
        lines.append(
            f'- The "## {invariants.long_section_title}" section was deliberately written to run '
            "notably longer than the others (structural-variance rule). If you are not fixing that "
            "section, leave its length alone — do not shrink it, and do not grow any OTHER section "
            "to rival it."
        )
    if invariants.short_para_section_title:
        lines.append(
            f'- The "## {invariants.short_para_section_title}" section carries the piece\'s one '
            "required short, single-sentence standalone paragraph (structural-variance rule). If "
            "you are not fixing that section, leave that short paragraph exactly as it is."
        )
    if invariants.required_h2s:
        headings = ", ".join(f'"{h}"' for h in invariants.required_h2s)
        lines.append(
            f"- This piece's required section headings are: {headings}. Never remove, rename, or "
            "merge any of these while fixing a violation from a different gate."
        )
    if invariants.single_atom_required:
        cited = _currently_cited_atom_ids(body_tagged)
        if cited:
            ids = ", ".join(f"[R:{aid}]" for aid in cited)
            lines.append(
                f"- This is a single-atom piece: it must cite ONLY {ids} (already in the text "
                "above) — never add a citation to any other atom id, even while fixing a violation "
                "that has nothing to do with citations."
            )
    if invariants.cta_required:
        lines.append(
            f'- This piece\'s format REQUIRES ending on the exact brand CTA phrase "'
            f'{invariants.cta_phrase}" as its final sentence. Even when fixing a violation that '
            "isn't about the CTA, do not let that fix remove, move, or bump the CTA out of the "
            "final position."
        )
    if invariants.aida_opening_section_title:
        lines.append(
            f'- The "## {invariants.aida_opening_section_title}" section is this piece\'s OPENING '
            "(AIDA framework) — it must open on a genuine attention hook. Don't let a fix for an "
            "unrelated violation replace that opening with generic scene-setting."
        )
    if invariants.aida_closing_section_title:
        lines.append(
            f'- The "## {invariants.aida_closing_section_title}" section is this piece\'s CLOSING '
            "(AIDA framework) — it must end on exactly ONE clear call to action. Don't introduce a "
            "second CTA or remove the existing one while fixing an unrelated violation."
        )
    if invariants.atom_text_by_id:
        lines.append(_build_grounding_note(body_tagged, invariants))

    if invariants.framework_rubric_items:
        items = "\n".join(f"  - {c}" for c in invariants.framework_rubric_items)
        fw_label = f' "{invariants.framework}"' if invariants.framework else ""
        lines.append(
            f"- This piece's framework{fw_label} (F8_framework gate) is judged against ALL of these "
            f"rubric criteria, not just whichever one is named in the violation above — a fix for "
            f"one criterion must not break any of the others:\n{items}"
        )

    if not lines:
        return ""
    return (
        "STRUCTURAL CONTEXT TO PRESERVE (regardless of which violation above you're fixing — these "
        "were decided when the piece was first written and must survive every repair round):\n"
        + "\n".join(lines)
    )


# AA-404 STEP 0 §"repair.py::_build_prompt()": the brand block already
# mentions the CTA phrase once (AA_BRAND_IDENTITY_PROMPT's "CTA:" line,
# already in every repair call's system prompt) yet real facebook pieces
# still failed to add it across 4 repair rounds — the terse violation string
# ("framework criterion failed: ends with CTA") never connects to that
# one-line brand fact elsewhere in context. This lookup is unconditional
# (not gated on `invariants`) because the violation text itself only ever
# appears for hook_story_cta pieces (gates.py::_ends_with_cta(), the only
# gate check that emits it) — a defense-in-depth match on top of the
# `cta_required` structural-context line above, not a replacement for it.
_VIOLATION_HINTS: tuple[tuple[str, str], ...] = (
    (
        "ends with cta",
        'HINT for the "ends with CTA" violation: the exact required phrase is "{cta_phrase}" — it '
        "must be the piece's final sentence, written as natural prose (never \"Book Now\", never a "
        "bare link or markdown), not merely present somewhere earlier in the text.",
    ),
)


def _violation_hints(violations: list[str], cta_phrase: str) -> list[str]:
    hints = []
    for v in violations:
        v_lower = v.lower()
        for needle, hint_template in _VIOLATION_HINTS:
            if needle in v_lower:
                hints.append(hint_template.format(cta_phrase=cta_phrase))
    return hints


class RepairFailed(Exception):
    """E5 could not produce a repaired body — Sonnet invoke kept failing
    after retries. Raised so the caller (`gates.py::run_gates()`) can hold
    the piece immediately rather than silently returning the unrepaired body
    or crashing the whole slot-production run (L6: hold visible, never
    silent — same spirit as DraftGenerationFailed/AdaptChannelFailed/
    FAQAnswerFailed)."""


def repair_piece(body_tagged: str, violations: list[str], *, invariants: Optional[PieceInvariants] = None) -> str:
    """E5. Matches the `Callable[[str, list[str]], str]` contract
    `run_gates()` (gates.py) requires for its `repair_fn` parameter: given
    the current `body_tagged` and the violations list from the first failing
    gate, returns the full repaired text. Raises `RepairFailed` after
    `_MAX_INVOKE_ATTEMPTS` failed Sonnet invokes — never returns a
    fabricated or partial repair.

    `invariants` (AA-404, keyword-only, defaults to `None`): optional
    piece-wide constraints from generation time (see `PieceInvariants`) that
    get folded into the prompt regardless of which gate's violations this
    round is fixing. `None` (every caller/test before this change, and
    `run_gates()`'s own bare `Callable[[str, list[str]], str]` contract)
    reproduces the exact pre-AA-404 prompt shape — this parameter is
    strictly additive."""
    prompt = _build_prompt(body_tagged, violations, invariants)
    brand_rubric_text = invariants.brand_rubric_text if invariants is not None else AA_BRAND_IDENTITY_PROMPT
    system_prompt = _build_repair_system_prompt(brand_rubric_text)
    # AA-518 — "n7_repair" stage config (seeded sonnet/acc3, matching the prior hardcoded literals).
    cfg = get_stage_config_sync("n7_repair")
    last_err: BedrockUnavailable | None = None
    for attempt in range(1, _MAX_INVOKE_ATTEMPTS + 1):
        try:
            result = invoke_claude(
                prompt, model=cfg.model_id, max_tokens=_MAX_TOKENS, system=system_prompt,
                account=cfg.account_route or "acc3",
            )
        except BedrockUnavailable as e:
            last_err = e
            logger.warning("e5_repair_sonnet_retry", attempt=attempt,
                            max_attempts=_MAX_INVOKE_ATTEMPTS, error=str(e))
            if attempt < _MAX_INVOKE_ATTEMPTS:
                time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
            continue

        repaired = result.text.strip()
        leaked = _looks_like_leaked_reasoning(repaired)
        record_call_sync(
            stage="n7_repair", role="writer", model=result.model_used,
            tokens_in=result.usage.get("input_tokens"), tokens_out=result.usage.get("output_tokens"),
            cost_usd=calc_cost(cfg.model_id, result.usage.get("input_tokens", 0),
                                result.usage.get("output_tokens", 0)),
            tenant_id=None,
            quality_signal={"leaked_reasoning_rejected": leaked, "violations_targeted": len(violations)},
            stop_reason=result.stop_reason,
            account=cfg.account_route or "acc3", fallback_used=False, provider="bedrock-satellite",
        )
        if leaked:
            logger.warning("e5_repair_leaked_reasoning_rejected", violations=violations,
                            prefix=repaired[:200])
            raise RepairFailed(
                "repair output failed sanity check -- looks like leaked reasoning about the "
                "repair task itself (AA-396 piece-7 class), not repaired content"
            )

        logger.info("e5_repair_success", model_used=result.model_used,
                     latency_ms=result.latency_ms, usage=result.usage, violations=violations)
        return repaired

    raise RepairFailed(
        f"Sonnet invoke failed after {_MAX_INVOKE_ATTEMPTS} attempts: {last_err}"
    ) from last_err


def _build_prompt(body_tagged: str, violations: list[str], invariants: Optional[PieceInvariants] = None) -> str:
    cta_phrase = invariants.cta_phrase if invariants is not None else _CTA_PHRASE
    parts = [
        "CURRENT TEXT:\n" + body_tagged + "\n\n"
        "VIOLATIONS TO FIX (fix ONLY these, nothing else):\n- " + "\n- ".join(violations)
    ]

    hints = _violation_hints(violations, cta_phrase)
    if hints:
        parts.append("\n\n" + "\n".join(hints))

    structural_context = _build_structural_context(body_tagged, invariants)
    if structural_context:
        parts.append("\n\n" + structural_context)

    return "".join(parts)


__all__ = ["repair_piece", "RepairFailed", "PieceInvariants"]
