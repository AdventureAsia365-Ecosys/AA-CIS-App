"""
services/acp_produce/tenant_pipeline.py — AA-425 [A/T-T2] T3 (tenant rewrite QA gate).

Extends the tenant-rewrite flow that already exists at api/routers/v1_tours.py's
trigger_rewrite() / _do_rewrite_and_save() closure — T2 (the rewrite itself, via
_rewrite_tour() / content_generation/graph.py) already runs correctly there, with
real tenant brand_config (AA-424). This module is what T2's caller runs AFTER a
successful rewrite: the T3 QA gate. T4 ("Tenant Tour Pool") is not a separate step
here — it's the existing gold_aa_internal.tenant_tour_versions UPDATE the caller
already does; this module only adds the qa_status verdict that write now carries
(see migration 107).

AA-757 (S224) — the atomize code (run_t5_atomize / the per-day + whole-tour paths,
ground_day_atoms, atom_subject_key, ATOM_STAGE/ATOM_Q, _atom_forbidden_words,
_strip_atom_action and their helpers) moved to services/acp_contract/a3_atomize.py
and was renamed `run_a3_atomize` (atomize is A3 platform only — tenants never
atomize). This module keeps only the T3 (tenant rewrite QA) functions.

Per AA-425's updated plan (Linear comment, 21/08, after AA-426): no temp trigger
endpoint. PoolTab.tsx's existing "Rewrite" button (-> POST /pool/{id}/rewrite ->
_rewrite_tour()) IS the entry point T1 will eventually relabel, not replace.

Decisions (see docs/implementation-notes/AA-425.md for the full list):
- T3's "structural" check calls validate_node (graph.py:448) fresh on the exact
  content that gets persisted, rather than trusting T2's own result["failure_codes"]
  — verified live (AA-425) that the two can diverge: flag_fix_node can rewrite
  `generated` in place without revalidate_node updating the ORIGINAL failure_codes
  list, so trusting it stale escalated a real rewrite for a forbidden word its
  persisted content didn't actually contain.
- T3's "grounding" check reuses find_novel_numeric_claims() (services/acp_shared/
  grounding.py) but, unlike s1_from_atom.py's citation-keyed check_grounding(),
  compares each rewritten sentence against the WHOLE T2-input corpus. graph.py's
  free-writing engine produces no [R:atom_id] citations to key a per-citation check
  off of the way s1_from_atom.py's atom-assembly engine does.
- T3's repair loop is NOT services/acp_produce/gates.py::run_gates() — that function
  is typed around N7's Piece object (piece.body_tagged/gate_ledger/repair_count),
  a much richer domain object than a tour dict; adapting it would mean faking a
  Piece rather than really reusing it. This module reimplements the same
  gate-then-repair SPIRIT (bounded rounds, re-check everything after each repair)
  at the smaller scale T3 actually needs.
- TENANT_QA_MAX_REPAIRS=2 is a NEW constant, deliberately not services/acp_produce/
  models.py::REPAIR_TOTAL_MAX (=3, N7's unrelated budget) — Nghiep's explicit
  naming guidance in the AA-425 decision comment, to keep the two systems' budgets
  from being confused with each other.
"""
from __future__ import annotations

import asyncio
import json
import re

import structlog

from services.acp_shared.grounding import find_novel_numeric_claims
from services.content_generation.seo_meta_utils import (  # noqa: F401
    SEO_TITLE_MAX, fit_seo_meta, fit_seo_meta_final, fit_seo_title,
)
from shared.llm_client.decide import decide

logger = structlog.get_logger()

TENANT_QA_MAX_REPAIRS = 2  # AA-425 — separate from acp_produce.models.REPAIR_TOTAL_MAX (N7, =3)

# Fields checked for both gates — same set graph.py's validate_node treats as the
# rewrite's real prose output (name/seo_title/seo_meta excluded: short/derived,
# not narrative claims — same exclusion s1_from_atom.py's _GATED_FIELDS makes).
_T3_GATED_FIELDS = ("subtitle", "summary", "highlights", "itineraries")

# Sentence-boundary heuristic — same value services/content_generation/s1_from_atom.py
# uses for its own entailment check (that module's _SENT_SPLIT_RE is private, so this
# is a copy, not an import; the two checks compare against different ground truth so
# they aren't the same function anyway — see module docstring).
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'‘’“”])")


def _t3_novel_numbers(sentence: str, source_texts: list[str], *,
                      background: bool) -> tuple[list[str], list[str]]:
    """(unsupported, background) novel numbers of one sentence. Without the AA-793 allowance this is
    exactly `find_novel_numeric_claims` (plain digit-run reading). With it, year ranges and decades
    are read as years (AA-785 `year_forms`) and the numbers `t3_background` accepts as place
    background (past years, centuries, elevations) move to the second list."""
    if not background:
        return find_novel_numeric_claims(sentence, source_texts), []
    from services.acp_produce.t3_background import is_background_number

    novel = find_novel_numeric_claims(sentence, source_texts, year_forms=True)
    kept = [n for n in novel if not is_background_number(n, sentence)]
    return kept, [n for n in novel if n not in kept]


def _t3_grounding_check(rewritten: dict, source_texts: list[str], *, background: bool | None = None,
                        cleared: list | None = None) -> list[dict]:
    """T3 grounding gate — does the tenant-rewritten content assert a number/
    measurement absent from source_texts (T2's INPUT: the pre-rewrite
    published_tours fields)? Returns a list of {field, sentence, novel_numbers}.

    AA-793: `background` (default: env T3_BACKGROUND_FACTS, off) applies the background-fact
    allowance; each sentence whose numbers were accepted that way is appended to `cleared`
    ({field, sentence, background_numbers}) so the run can log what was let through."""
    if background is None:
        from services.content_generation.t2_repair import background_facts_enabled
        background = background_facts_enabled()
    from services.acp_shared.signature_vocabulary import service_level_claims
    violations: list[dict] = []
    for field in _T3_GATED_FIELDS:
        val = rewritten.get(field)
        texts = val if isinstance(val, list) else [val] if val else []
        for t in texts:
            for sent in _SENT_SPLIT_RE.split(str(t)):
                novel, allowed = _t3_novel_numbers(sent, source_texts, background=background)
                if allowed and cleared is not None:
                    cleared.append({"field": field, "sentence": sent.strip(), "background_numbers": allowed})
                # AA-800 evidence rule: a service-level positioning claim ("a luxury lodge", "a tailored
                # itinerary") the source does not support is a fidelity finding, for every tenant.
                claims = [c["phrase"] for c in service_level_claims(sent, *source_texts)]
                if novel or claims:
                    v = {"field": field, "sentence": sent.strip(), "novel_numbers": novel}
                    if claims:
                        v["unsupported_claims"] = claims
                    violations.append(v)
    return violations


def _t3_unsupported(sentence: str, source_texts: list[str], *, background: bool) -> bool:
    """A sentence still fails T3 grounding: a novel number, or (AA-800) an unsupported service claim."""
    from services.acp_shared.signature_vocabulary import service_level_claims
    return bool(_t3_novel_numbers(sentence, source_texts, background=background)[0]
                or service_level_claims(sentence, *source_texts))


def t3_brand_pack(brand_rules: dict) -> dict:
    """AA-800: the brand-pack view of T2's `brand_rules` (t2_rewrite_job.brand_rules_from_pack keys) that
    the word layers read: forbidden words, T0 text that declares signature words, the explicit list."""
    br = brand_rules or {}
    return {"forbidden_words": br.get("forbidden_words") or [], "brand_type": br.get("brand_type"),
            "core_idea": br.get("core_idea"), "voice": br.get("voice_examples") or [],
            "style_guide": br.get("style_guide"), "good_examples": br.get("good_examples"),
            "signature_words": br.get("signature_words") or []}


# AA-699 T3-1 — before a numeric hit costs a full rewrite, ask Jev whether the sentence is supported
# by the master content (unit conversion, sum, rephrasing are not fabrications). Same Noul and state
# shape as A1 (`a1_claim_supported`, docs/calibration/a1_claim_supported.md): enforced accept ≥ 0.90
# clears the hit; there is no reject floor, so Jev never adds a violation. Tenant content → only for
# allow-listed tenants (decide() returns `skipped` for the rest, and T3 behaves as before).
T3_JEV_STAGE = "t3_grounding"
T3_JEV_Q = "a1_claim_supported"
_T3_JEV_CONCURRENCY = 4


async def t3_jev_clear(grounding: list[dict], tour_dict: dict,
                       tenant_id: str | None, errors: list | None = None) -> tuple[list[dict], list[dict]]:
    """Split T3 numeric hits into (kept, cleared). Cleared = Jev confidently (enforce, accept zone)
    says the sentence is supported by the master content. Never raises; fails open to `kept`.

    AA-925: ``errors`` (when given) collects one ``{"mode"}`` per Jev outage (zone error, or the whole call
    failing). Behaviour is unchanged: Jev only clears hits here, so an outage keeps every hit (fail-closed)."""
    if not grounding or not tenant_id:
        return grounding, []
    from services.content_generation.grounding import source_text, subject_key

    source = source_text(tour_dict or {})
    if not source:
        return grounding, []

    async def one(g: dict):
        d = await decide(T3_JEV_STAGE, subject_key(source, g["sentence"]),
                         {"source": source, "sentence": g["sentence"]}, [T3_JEV_Q], tenant_id=tenant_id)
        return d.verdicts.get(T3_JEV_Q), d.accepted(T3_JEV_Q)

    try:
        first_v, first_ok = await one(grounding[0])
        if first_v is not None and first_v.zone == "skipped":   # not allow-listed / mode off
            return grounding, []
        sem = asyncio.Semaphore(_T3_JEV_CONCURRENCY)

        async def bounded(g: dict):
            async with sem:
                return await one(g)

        rest = await asyncio.gather(*[bounded(g) for g in grounding[1:]])
        oks = [first_ok] + [ok for _, ok in rest]
        if errors is not None:
            errors.extend({"mode": getattr(v, "mode", None)} for v in [first_v] + [v for v, _ in rest]
                          if v is None or getattr(v, "zone", None) == "error")
    except Exception as exc:
        logger.warning("t3_jev_clear_failed", error=str(exc)[:200])
        if errors is not None:
            errors.extend({"mode": None} for _ in grounding)
        return grounding, []
    kept = [g for g, ok in zip(grounding, oks) if not ok]
    cleared = [g for g, ok in zip(grounding, oks) if ok]
    return kept, cleared


def _t3_structural_issues(generated: dict, tour_dict: dict, brand_rules: dict) -> list[str]:
    """T3 structural gate — reuse validate_node (graph.py:448) directly on the ACTUAL
    final content, rather than trusting `result["failure_codes"]` from T2's own graph
    run. Verified during live testing (AA-425) that the two can diverge: graph.py's
    internal chain runs validate -> judge -> brand_audit -> flag_fix -> revalidate, and
    flag_fix_node can rewrite `generated` in place to clear a violation without
    revalidate_node updating the ORIGINAL `failure_codes` list attached to the returned
    state — a real tour rewrite escalated to review_queue for FORBIDDEN_WORD despite the
    persisted rewritten_content containing none of graph.py's own _VALIDATE_FORBIDDEN
    words. Calling validate_node fresh, on exactly what gets persisted, is the only way
    this check can't go stale relative to what a human reviewer (or the tenant) actually
    sees."""
    from services.content_generation.graph import validate_node, _HARD_BLOCK_CODES
    # AA-800: checked as the tenant rewrite it is, so validate applies the tenant's layered word list
    # (its signature words pass; platform truth, undeclared positioning words, its own list and a second
    # stock superlative fire FORBIDDEN_WORD).
    pack = t3_brand_pack(brand_rules)
    state = {
        "generated": generated,
        "tour": tour_dict,
        "brand_forbidden_words": pack["forbidden_words"],
        "is_tenant_rewrite": True,
        "brand_type": pack["brand_type"] or "",
        "brand_core_idea": pack["core_idea"] or "",
        "brand_voice_examples": pack["voice"],
        "brand_style_guide": pack["style_guide"] or "",
        "brand_good_examples": pack["good_examples"] or "",
        "brand_signature_words": pack["signature_words"],
        "retry_count": 0,
    }
    codes = list(validate_node(state).get("failure_codes") or [])
    # AA-639: only HARD codes fail T3 — the same line graph.py draws (AA-234 _HARD_BLOCK_CODES:
    # SEO length/floor, forbidden words, missing fields). Soft codes (HIGHLIGHTS_*, *_GENERIC,
    # SUMMARY_OFF_BRAND) are advisory there by design; treating them as failures here made T3
    # re-write the whole tour for e.g. HIGHLIGHTS_TOO_GENERIC, which a full rewrite rarely clears
    # (live: 2 wasted rewrites, then escalated anyway).
    return [c for c in codes if c in _HARD_BLOCK_CODES]


# AA-740: fit_seo_title + its constants moved to seo_meta_utils so the S1 path can use them too.


_T3_FEEDBACK_MAX_SENTENCES = 8


def t3_repair_feedback(structural: list[str], grounding: list[dict]) -> str:
    """AA-639 — turn T3's findings into the writer's PREVIOUS ATTEMPT FEEDBACK block
    (graph.py generate_node appends it to the prompt). Grounding: list each number the draft
    asserted that the source never states, with the sentence it came from, and say what to do
    (keep only figures from the source; drop or rephrase the rest without a number). Structural:
    the validate_node codes. Capped so a noisy draft doesn't blow up the prompt."""
    parts: list[str] = []
    if grounding:
        parts.append(
            "FACT CHECK FAILED: these sentences state numbers that do NOT appear anywhere in the "
            "source tour. Use only figures (distances, altitudes, durations, counts, prices, years) "
            "that the source itself gives; otherwise remove the number or rephrase without one. "
            "Do not add conversions (e.g. feet) or outside facts."
            + (" A service-level claim (luxury, bespoke, tailored, curated or refined lodging, dining,"
               " transport or itinerary) must also be in the source: drop the claim word, keep the fact."
               if any(g.get("unsupported_claims") for g in grounding) else "")
        )
        for g in grounding[:_T3_FEEDBACK_MAX_SENTENCES]:
            sent = " ".join(str(g.get("sentence", "")).split())[:240]
            what = []
            if g.get("novel_numbers"):
                what.append(f"numbers {', '.join(g['novel_numbers'])}")
            if g.get("unsupported_claims"):  # AA-800
                what.append(f"service claim {', '.join(g['unsupported_claims'])} (the source does not state it)")
            parts.append(f"- [{g.get('field')}] {'; '.join(what)}: \"{sent}\"")
        if len(grounding) > _T3_FEEDBACK_MAX_SENTENCES:
            extra = len(grounding) - _T3_FEEDBACK_MAX_SENTENCES
            parts.append(f"- …and {extra} more sentence(s) with the same problem.")
    if structural:
        parts.append("STRUCTURE CHECK FAILED. Fix these issues: " + ", ".join(structural[:15]))
    return "\n".join(parts)


# AA-793 — the T2 fact editor for a grounding-only T3 failure (A1's sentence repair, tenant voice kept,
# no Adventure Asia identity in a tenant's pipeline).
T3_REPAIR_SYSTEM = """You are the fact editor for a travel brand's tour page. The page was written from a
SOURCE tour, and some sentences state figures the source does not give. Rewrite each numbered sentence so
it keeps its place in the text, its tone and the brand's voice, but states only what the SOURCE states.
Drop the unsupported figure (a distance, duration, time, count, price, height, date or size), or the
unsupported service-level claim (a "luxury" lodge, a "tailored" itinerary), keeping the fact itself; never
replace it with another invented one. If nothing in the sentence is supported, return an empty string.
Return strict JSON only."""

_T3_PASS_LINE = 7.0  # graph.MIN_QUALITY — a draft below it gets the judge's feedback on a revision


def _result_score(result: dict) -> float:
    try:
        return float(result.get("quality_score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _draft_key(c: dict) -> tuple:
    """AA-793 best draft: a clean draft (passes T3) first, then the higher score, then fewer issues,
    then the later attempt (a repaired copy of an equally scored draft)."""
    clean = not c["structural"] and not c["grounding"]
    return (clean, c["score"], -(len(c["structural"]) + len(c["grounding"])), c["attempt"])


def _draft_record(c: dict) -> dict:
    return {"attempt": c["attempt"], "via": c["via"], "score": c["score"],
            "structural": len(c["structural"]), "grounding": len(c["grounding"]),
            "background": c["background"], "clean": not c["structural"] and not c["grounding"]}


def _revision_feedback(base: dict) -> str:
    """T3 findings plus, for a draft below the line, the judge's own feedback — so the revision fixes
    what both checks asked for (before AA-793 a repair round only heard T3)."""
    fb = t3_repair_feedback(base["structural"], base["grounding"])
    judge_fb = str(base["result"].get("judge_feedback") or "").strip()
    if judge_fb and base["score"] < _T3_PASS_LINE:
        fb = f"{fb}\nBRAND VOICE FEEDBACK: {judge_fb[:1500]}" if fb else f"BRAND VOICE FEEDBACK: {judge_fb[:1500]}"
    return fb


def _t3_sentence_repair(base: dict, tour_dict: dict, source_texts: list[str], brand_rules: dict,
                        tenant_id: str | None, background: bool) -> dict | None:
    """AA-793 targeted repair: rewrite only the sentences T3 flagged (one t2_flag_fix call) instead of
    the whole tour. Returns the repaired result (the draft's judge score carried: removing a figure
    does not change its brand fit) or None when nothing could be replaced. Sync — run in a thread."""
    from services.acp_shared.signature_vocabulary import tenant_blocked_words
    from services.content_generation.grounding import repair

    def still_unsupported(sentence: str) -> bool:
        return _t3_unsupported(sentence, source_texts, background=background)

    result = base["result"]
    # AA-800: the tenant's layered list (its signature words are not refused in a repaired sentence).
    fixed = repair(result.get("generated") or {}, base["grounding"], tour_dict,
                   forbidden=tenant_blocked_words(t3_brand_pack(brand_rules)),
                   stage="t2_flag_fix", tenant_id=tenant_id, system_prompt=T3_REPAIR_SYSTEM,
                   still_unsupported=still_unsupported)
    if not fixed["fields"]:
        logger.info("t3_sentence_repair_no_change", attempt=base["attempt"], error=fixed.get("error"),
                    tenant_id=tenant_id)
        return None
    # AA-925 repair contract: only the flagged fields change, no figure or name the field and the master text
    # lack, no sentence cut short, no broken bracket. A violating field keeps its pre-repair text.
    from services.quality import repair_contract
    before = result.get("generated") or {}
    repaired, contract = repair_contract.enforce_fields(
        before, fixed["generated"], allowed={g["field"] for g in base["grounding"]}, source="\n".join(source_texts))
    kept = [f for f in fixed["fields"] if f not in contract.fields()]
    if not contract.ok:
        logger.warning("t3_sentence_repair_contract_revert", fields=contract.fields(),
                       invariants=contract.invariants(), tenant_id=tenant_id)
    if not kept:
        return None
    return {**result, "generated": repaired,
            "cost_usd": float(result.get("cost_usd") or 0.0) + float(fixed["cost_usd"] or 0.0),
            "t3_sentence_repaired_fields": kept,
            "t3_contract_reverts": len(contract.fields())}


_T3_META_CODES = frozenset({"SEO_META_TOO_LONG", "META_TOO_SHORT", "META_INCOMPLETE_SENTENCE",
                             "BRAND_SEO_META_VIOLATION"})
_T3_META_SYSTEM = """You are the editorial fixer for {brand}, repairing the SEO meta description of a tour page
written in that brand's own voice. Keep every fact the current meta states; add none. Return strict JSON only."""


def _t3_meta_regenerate(generated: dict, tour_dict: dict, brand_rules: dict, tenant_id: str | None,
                        source_texts: list[str]) -> tuple[str | None, dict | None]:
    """AA-925 — the meta-only LLM regenerate T3 runs BEFORE a whole-tour rewrite when the deterministic fit
    could not land seo_meta in band (AA-740's clause/word cut is gone: it cut "...a 20-kilometre walk" to
    "...a 20-kilometre."). Reuses flag_fix's bounded ``_rerepair_meta`` loop on the t2_flag_fix route (AA-763),
    then the repair contract (no added figure, no cut sentence). Returns (new meta or None, contract violation
    or None). Sync — run in a thread. Without it a length code costs a whole T2 rewrite (AA-794, ~$0.38)."""
    from services.acp_shared.signature_vocabulary import tenant_blocked_words
    from services.content_generation.flag_fix_node import _rerepair_meta
    from services.content_generation.seo_meta_utils import SEO_META_FORBIDDEN, meta_in_band
    from services.quality import repair_contract

    before = generated.get("seo_meta") or ""
    pack = t3_brand_pack(brand_rules)
    forbidden = set(SEO_META_FORBIDDEN) | {w.lower().strip() for w in tenant_blocked_words(pack) if w and w.strip()}
    brand = (pack.get("brand_name") or "").strip() or "this client brand"
    new = _rerepair_meta(before, tour_dict, generated, None, forbidden=forbidden, stage="t2_flag_fix",
                         system_prompt=_T3_META_SYSTEM.replace("{brand}", brand))
    if not new or new == before or not meta_in_band(new, forbidden):
        logger.info("t3_meta_regenerate_no_change", landed=bool(new) and meta_in_band(new, forbidden),
                    tenant_id=tenant_id)
        return None, None
    _, res = repair_contract.enforce_fields({"seo_meta": before}, {"seo_meta": new},
                                            source="\n".join(source_texts or []), names=False)
    if not res.ok:
        logger.warning("t3_meta_regenerate_contract_revert", invariants=res.invariants(), tenant_id=tenant_id)
        return None, {"invariants": res.invariants()}
    return new, None


def _t3_outcome(passed: bool, chosen: dict, attempts: int, candidates: list[dict], jev_cleared: int,
                sentence_repairs: int, degraded_list: list | None = None) -> dict:
    return {
        "passed": passed, "result": chosen["result"], "attempts": attempts,
        "structural_issues": [] if passed else chosen["structural"],
        "grounding_issues": [] if passed else chosen["grounding"],
        "jev_cleared": jev_cleared,
        # AA-793: which draft was kept and the score of every draft T3 saw (job.result).
        "chosen_attempt": chosen["attempt"], "chosen_via": chosen["via"],
        "drafts": [_draft_record(c) for c in candidates],
        "background_cleared": chosen["background"], "sentence_repairs": sentence_repairs,
        # AA-925: the fail-open paths T3 met (Jev outages while clearing hits, repair-contract reverts)
        "degraded": list(degraded_list or []),
    }


async def run_t3_qa_gate(
    tour_dict: dict,
    source_texts: list[str],
    initial_result: dict,
    brand_rules: dict,
    max_repairs: int = TENANT_QA_MAX_REPAIRS,
    seo_data: dict = None,
    tenant_id: str = None,  # AA-620: thread through so a T3 repair-round logs t2_generate + tenant
) -> dict:
    """Self-repair loop, max `max_repairs` repair rounds (default 2). Attempt 0
    checks `initial_result` (T2's already-computed output — no extra LLM call for the
    first check). Each failing attempt repairs and re-checks both gates on the new output.

    AA-445-02 — seo_data: the same dict the T2 entry point (v1_tours.py::trigger_rewrite())
    now builds and passes to its own _rewrite_tour() call (see that file for the
    fetch-or-reuse-seo_context logic). Threaded through here so a T3 repair-round
    regenerate doesn't silently drop back to seo={} after the entry-point fix — without
    this, only ATTEMPT 0's content would ever carry real SEO context.

    AA-793 (switches in services/content_generation/t2_repair.py; all off = the pre-AA-793 loop):
    - T2_BEST_DRAFT: every checked draft is a candidate. A clean draft returns at once; when the
      rounds run out the best candidate is returned (`_draft_key`), not the last one, and each round
      repairs the best candidate so far.
    - T2_TARGETED_REPAIR: a candidate whose only failure is grounding gets one sentence repair
      (`_t3_sentence_repair`) instead of a whole-tour rewrite; otherwise the rewrite revises the
      candidate (`previous_draft`) with the T3 findings plus the judge's feedback.
    - T3_BACKGROUND_FACTS: past years, centuries and elevations of named places are not violations.

    Returns {"passed": bool, "result": <chosen rewrite result dict>, "attempts": int,
    "structural_issues": [...], "grounding_issues": [...], "jev_cleared": int, "chosen_attempt",
    "chosen_via", "drafts": [...], "background_cleared": int, "sentence_repairs": int}."""
    from api.routers.v1_pipeline import _rewrite_tour
    from services.content_generation.t2_repair import (
        background_facts_enabled, best_draft_enabled, targeted_repair_enabled,
    )

    best_on, targeted_on = best_draft_enabled(), targeted_repair_enabled()
    background_on = background_facts_enabled()
    result = initial_result
    attempt = 0
    jev_cleared = 0
    sentence_repairs = 0
    via = "initial"
    candidates: list[dict] = []
    from services.quality import degraded
    t3_degraded: list[dict] = []
    meta_tried: set[str] = set()   # AA-925: one meta-only regenerate per distinct meta
    while True:
        generated = result.get("generated") or {}
        # AA-639: an over-long SEO title is fixed deterministically here — live, a single
        # SEO_TITLE_TOO_LONG (flag_fix's LLM pass missed the 60-char count) cost a full rewrite of an
        # 18-day tour. Mutates the result that gets persisted.
        if isinstance(generated.get("seo_title"), str):
            generated["seo_title"] = fit_seo_title(generated["seo_title"])
        # AA-641: same for an over-long seo_meta (an LLM counting characters is unreliable).
        # AA-794: the same full deterministic fit the graph runs (sentence salvage, fact clause). AA-925: no
        # mid-clause cut any more — a meta it cannot land gets the meta-only regenerate below.
        if isinstance(generated.get("seo_meta"), str):
            generated["seo_meta"] = fit_seo_meta_final(
                generated["seo_meta"],
                {"duration": tour_dict.get("duration"), "country": tour_dict.get("country"),
                 "name": tour_dict.get("name")},
                brand_rules.get("forbidden_words"))
        structural = _t3_structural_issues(generated, tour_dict, brand_rules)
        # AA-925: the fit no longer cuts mid-clause, so a meta it cannot land gets a meta-only LLM regenerate
        # (t2_flag_fix) before anything here asks for a whole-tour rewrite.
        _meta = generated.get("seo_meta")
        if set(structural) & _T3_META_CODES and isinstance(_meta, str) and _meta not in meta_tried:
            meta_tried.add(_meta)
            new_meta, violated = await asyncio.to_thread(_t3_meta_regenerate, generated, tour_dict, brand_rules,
                                                         tenant_id, source_texts)
            if violated:
                t3_degraded = degraded.add(t3_degraded, degraded.REPAIR_CONTRACT, detector="t3_meta_regenerate",
                                           detail=", ".join(violated["invariants"]))
            if new_meta:
                generated["seo_meta"] = new_meta
                meta_tried.add(new_meta)
                structural = _t3_structural_issues(generated, tour_dict, brand_rules)
        background: list[dict] = []
        grounding = _t3_grounding_check(generated, source_texts, background=background_on,
                                        cleared=background)
        if background:
            logger.info("t3_background_cleared", attempt=attempt, sentences=len(background),
                        numbers=sorted({n for b in background for n in b["background_numbers"]})[:20],
                        tenant_id=tenant_id)
        jev_errors: list[dict] = []
        grounding, cleared = await t3_jev_clear(grounding, tour_dict, tenant_id, jev_errors)   # AA-699 T3-1
        # Counted, never held: T3's Jev only CLEARS numeric hits, so an outage keeps every hit (fail-closed) and
        # the draft is repaired / escalated on them as before. A "hard" entry here would claim a hold nothing
        # applies (review of PR 3). The severity Jev's mode implied is kept in the detail.
        for e in degraded.jev_entries(jev_errors, detail=T3_JEV_STAGE):
            t3_degraded = degraded.add(t3_degraded, e["name"], detector="t3_jev_clear", count=e["count"],
                                       severity="count",
                                       detail=f"{e['detail']} (mode severity {e['severity']}; hits kept)")
        if cleared:
            jev_cleared += len(cleared)
            logger.info("t3_jev_cleared", attempt=attempt, cleared=len(cleared), kept=len(grounding),
                        tenant_id=tenant_id)
        current = {"result": result, "structural": structural, "grounding": grounding,
                   "attempt": attempt, "via": via, "score": _result_score(result),
                   "background": len(background), "repaired": via == "sentence_repair"}
        candidates.append(current)

        if not structural and not grounding:
            degraded.emit(t3_degraded, stage="t3", tenant_id=tenant_id)
            return _t3_outcome(True, current, attempt, candidates, jev_cleared, sentence_repairs, t3_degraded)

        if attempt >= max_repairs:
            chosen = max(candidates, key=_draft_key) if best_on else current
            if chosen is not current:
                logger.info("t3_best_draft_chosen", chosen_attempt=chosen["attempt"],
                            chosen_score=chosen["score"], last_score=current["score"], tenant_id=tenant_id)
            degraded.emit(t3_degraded, stage="t3", tenant_id=tenant_id)
            return _t3_outcome(False, chosen, attempt, candidates, jev_cleared, sentence_repairs, t3_degraded)

        attempt += 1
        base = max(candidates, key=_draft_key) if best_on else current
        logger.info("t3_qa_repair_attempt", attempt=attempt,
                    structural_count=len(base["structural"]), grounding_count=len(base["grounding"]),
                    structural_codes=base["structural"][:10],
                    novel_numbers=sorted({n for g in base["grounding"] for n in g["novel_numbers"]})[:20],
                    base_attempt=base["attempt"], base_score=base["score"])
        repaired = None
        if targeted_on and not base["structural"] and base["grounding"] and not base["repaired"]:
            base["repaired"] = True   # one sentence repair per draft; the next round rewrites
            repaired = await asyncio.to_thread(_t3_sentence_repair, base, tour_dict, source_texts,
                                               brand_rules, tenant_id, background_on)
        if repaired is not None and repaired.get("t3_contract_reverts"):
            t3_degraded = degraded.add(t3_degraded, degraded.REPAIR_CONTRACT, detector="t3_sentence_repair",
                                       count=repaired["t3_contract_reverts"])
        if repaired is not None:
            result, via = repaired, "sentence_repair"
            sentence_repairs += 1
            continue
        result = await _rewrite_tour(
            tour_dict, idx=0, total=1, brand_rules=brand_rules, is_tenant_rewrite=True,
            seo=seo_data,
            tenant_id=tenant_id,             # AA-620: same tenant as attempt 0
            generate_stage="t2_generate",    # AA-620: T2 repair-round stays on the tenant stage
            # AA-639: tell the writer WHAT failed. Before this, a repair round was a blind
            # re-roll (feedback="") hoping the next draft happened to pass — live, a tour took
            # 1 write + 3 full rewrites (~4 min) to clear T3.
            # AA-793: with T2_TARGETED_REPAIR the writer revises the chosen draft and also hears
            # the judge's feedback.
            feedback=_revision_feedback(base) if targeted_on else t3_repair_feedback(
                base["structural"], base["grounding"]),
            **({"previous_draft": base["result"].get("generated") or None} if targeted_on else {}),
        )
        via = "rewrite"


async def escalate_t3_failure(
    pool, tenant_id: str, tour_id: str, version_id: str,
    structural_issues: list[str], grounding_issues: list[dict],
) -> None:
    """T3 exhausted its repair budget — write a review_queue row a tenant-facing
    view can later filter by tenant_id (migration 107: generated_content_id is now
    nullable — it has no meaning for a tenant-rewrite escalate, and forcing an
    unrelated id in would violate its FK to silver_aa_internal.generated_content).
    escalate_detail carries the issue's mandated {check_id, field, description,
    source_span, suggested_fix} shape per entry — never an LLM self-score."""
    detail = []
    for code in structural_issues:
        detail.append({
            "check_id": f"structural:{code}", "field": None,
            "description": code, "source_span": None, "suggested_fix": None,
        })
    for v in grounding_issues:
        detail.append({
            "check_id": ("grounding:novel_numeric_claim" if v.get("novel_numbers")
                         else "grounding:service_level_claim"),  # AA-800
            "field": v["field"],
            "description": (
                f"Unsupported number(s) {v['novel_numbers']} in a {v['field']} sentence"
                if v.get("novel_numbers") else
                f"Unsupported service claim(s) {v.get('unsupported_claims')} in a {v['field']} sentence"
            ),
            "source_span": v["sentence"][:500], "suggested_fix": None,
        })
    summary = (
        f"T3 QA failed after {TENANT_QA_MAX_REPAIRS} repair attempt(s) — "
        f"{len(structural_issues)} structural, {len(grounding_issues)} grounding issue(s)"
    )
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO silver_aa_internal.review_queue
                (tour_id, tenant_id, tenant_tour_version_id, failure_summary, escalate_detail)
            VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5::jsonb)
        """, tour_id, tenant_id, version_id, summary, json.dumps(detail))
    logger.info("t3_escalated", tenant_id=tenant_id, tour_id=tour_id, version_id=version_id,
                structural_count=len(structural_issues), grounding_count=len(grounding_issues))
