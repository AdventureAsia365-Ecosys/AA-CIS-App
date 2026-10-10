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
from services.content_generation.seo_meta_utils import SEO_TITLE_MAX, fit_seo_meta, fit_seo_title  # noqa: F401
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


def _t3_grounding_check(rewritten: dict, source_texts: list[str]) -> list[dict]:
    """T3 grounding gate — does the tenant-rewritten content assert a number/
    measurement absent from source_texts (T2's INPUT: the pre-rewrite
    published_tours fields)? Returns a list of {field, sentence, novel_numbers}."""
    violations: list[dict] = []
    for field in _T3_GATED_FIELDS:
        val = rewritten.get(field)
        texts = val if isinstance(val, list) else [val] if val else []
        for t in texts:
            for sent in _SENT_SPLIT_RE.split(str(t)):
                novel = find_novel_numeric_claims(sent, source_texts)
                if novel:
                    violations.append({
                        "field": field, "sentence": sent.strip(), "novel_numbers": novel,
                    })
    return violations


# AA-699 T3-1 — before a numeric hit costs a full rewrite, ask Jev whether the sentence is supported
# by the master content (unit conversion, sum, rephrasing are not fabrications). Same Noul and state
# shape as A1 (`a1_claim_supported`, docs/calibration/a1_claim_supported.md): enforced accept ≥ 0.90
# clears the hit; there is no reject floor, so Jev never adds a violation. Tenant content → only for
# allow-listed tenants (decide() returns `skipped` for the rest, and T3 behaves as before).
T3_JEV_STAGE = "t3_grounding"
T3_JEV_Q = "a1_claim_supported"
_T3_JEV_CONCURRENCY = 4


async def t3_jev_clear(grounding: list[dict], tour_dict: dict,
                       tenant_id: str | None) -> tuple[list[dict], list[dict]]:
    """Split T3 numeric hits into (kept, cleared). Cleared = Jev confidently (enforce, accept zone)
    says the sentence is supported by the master content. Never raises; fails open to `kept`."""
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
    except Exception as exc:
        logger.warning("t3_jev_clear_failed", error=str(exc)[:200])
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
    state = {
        "generated": generated,
        "tour": tour_dict,
        "brand_forbidden_words": brand_rules.get("forbidden_words") or [],
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
            "FACT CHECK FAILED — these sentences state numbers that do NOT appear anywhere in the "
            "source tour. Use only figures (distances, altitudes, durations, counts, prices, years) "
            "that the source itself gives; otherwise remove the number or rephrase without one. "
            "Do not add conversions (e.g. feet) or outside facts."
        )
        for g in grounding[:_T3_FEEDBACK_MAX_SENTENCES]:
            sent = " ".join(str(g.get("sentence", "")).split())[:240]
            parts.append(f"- [{g.get('field')}] numbers {', '.join(g.get('novel_numbers', []))}: \"{sent}\"")
        if len(grounding) > _T3_FEEDBACK_MAX_SENTENCES:
            extra = len(grounding) - _T3_FEEDBACK_MAX_SENTENCES
            parts.append(f"- …and {extra} more sentence(s) with the same problem.")
    if structural:
        parts.append("STRUCTURE CHECK FAILED — fix these issues: " + ", ".join(structural[:15]))
    return "\n".join(parts)


async def run_t3_qa_gate(
    tour_dict: dict,
    source_texts: list[str],
    initial_result: dict,
    brand_rules: dict,
    max_repairs: int = TENANT_QA_MAX_REPAIRS,
    seo_data: dict = None,
    tenant_id: str = None,  # AA-620: thread through so a T3 repair-round logs t2_generate + tenant
) -> dict:
    """Self-repair loop, max `max_repairs` regenerate attempts (default 2). Attempt 0
    checks `initial_result` (T2's already-computed output — no extra LLM call for the
    first check). Each failing attempt regenerates via _rewrite_tour() (imported
    locally to avoid a v1_pipeline <-> tenant_pipeline import cycle at module load)
    and re-checks both gates on the fresh output.

    AA-445-02 — seo_data: the same dict the T2 entry point (v1_tours.py::trigger_rewrite())
    now builds and passes to its own _rewrite_tour() call (see that file for the
    fetch-or-reuse-seo_context logic). Threaded through here so a T3 repair-round
    regenerate doesn't silently drop back to seo={} after the entry-point fix — without
    this, only ATTEMPT 0's content would ever carry real SEO context.

    Returns {"passed": bool, "result": <latest rewrite result dict>, "attempts": int,
    "structural_issues": [...], "grounding_issues": [...]}."""
    from api.routers.v1_pipeline import _rewrite_tour

    result = initial_result
    attempt = 0
    jev_cleared = 0
    while True:
        generated = result.get("generated") or {}
        # AA-639: an over-long SEO title is fixed deterministically here — live, a single
        # SEO_TITLE_TOO_LONG (flag_fix's LLM pass missed the 60-char count) cost a full rewrite of an
        # 18-day tour. Mutates the result that gets persisted.
        if isinstance(generated.get("seo_title"), str):
            generated["seo_title"] = fit_seo_title(generated["seo_title"])
        # AA-641: same for an over-long seo_meta (an LLM counting characters is unreliable).
        if isinstance(generated.get("seo_meta"), str):
            generated["seo_meta"] = fit_seo_meta(generated["seo_meta"], brand_rules.get("forbidden_words"))
        structural = _t3_structural_issues(generated, tour_dict, brand_rules)
        grounding = _t3_grounding_check(generated, source_texts)
        grounding, cleared = await t3_jev_clear(grounding, tour_dict, tenant_id)   # AA-699 T3-1
        if cleared:
            jev_cleared += len(cleared)
            logger.info("t3_jev_cleared", attempt=attempt, cleared=len(cleared), kept=len(grounding),
                        tenant_id=tenant_id)

        if not structural and not grounding:
            return {
                "passed": True, "result": result, "attempts": attempt,
                "structural_issues": [], "grounding_issues": [], "jev_cleared": jev_cleared,
            }

        if attempt >= max_repairs:
            return {
                "passed": False, "result": result, "attempts": attempt,
                "structural_issues": structural, "grounding_issues": grounding, "jev_cleared": jev_cleared,
            }

        attempt += 1
        logger.info("t3_qa_repair_attempt", attempt=attempt,
                    structural_count=len(structural), grounding_count=len(grounding),
                    structural_codes=structural[:10],
                    novel_numbers=sorted({n for g in grounding for n in g["novel_numbers"]})[:20])
        result = await _rewrite_tour(
            tour_dict, idx=0, total=1, brand_rules=brand_rules, is_tenant_rewrite=True,
            seo=seo_data,
            tenant_id=tenant_id,             # AA-620: same tenant as attempt 0
            generate_stage="t2_generate",    # AA-620: T2 repair-round stays on the tenant stage
            # AA-639: tell the writer WHAT failed. Before this, a repair round was a blind
            # re-roll (feedback="") hoping the next draft happened to pass — live, a tour took
            # 1 write + 3 full rewrites (~4 min) to clear T3.
            feedback=t3_repair_feedback(structural, grounding),
        )


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
            "check_id": "grounding:novel_numeric_claim", "field": v["field"],
            "description": (
                f"Unsupported number(s) {v['novel_numbers']} in a {v['field']} sentence"
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
