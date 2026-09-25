"""
services/acp_produce/tenant_pipeline.py — AA-425 [A/T-T2-T5] T3 (QA gate) + T5 (atomize).

Extends the tenant-rewrite flow that already exists at api/routers/v1_tours.py's
trigger_rewrite() / _do_rewrite_and_save() closure — T2 (the rewrite itself, via
_rewrite_tour() / content_generation/graph.py) already runs correctly there, with
real tenant brand_config (AA-424). This module is what T2's caller runs AFTER a
successful rewrite: T3 QA gate, then (on pass) T5 atomize. T4 ("Tenant Tour Pool")
is not a separate step here — it's the existing gold_aa_internal.tenant_tour_versions
UPDATE the caller already does; this module only adds the qa_status verdict that
write now carries (see migration 107).

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
import uuid

import structlog

from services.acp_shared.atom_extraction import (
    SYSTEM_PROMPT as _SYSTEM_PROMPT,
    build_day_user_prompt as _build_day_user_prompt,
    checkable_evidence as _checkable_evidence,
    build_user_prompt as _build_user_prompt,
    content_hash_atom_id as _content_hash_atom_id,
    day_fingerprint as _day_fingerprint,
    derive_atom_text as _derive_atom_text,
    source_hash as _source_hash,
    strip_json_fence as _strip_json_fence,
)
from services.acp_shared.grounding import find_novel_numeric_claims
from services.content_generation.itinerary_utils import parse_canonical_itinerary_days
from shared.llm_client.bedrock_satellite import invoke_claude
from shared.llm_client.role_config import get_stage_config
from shared.llm_client.call_log import record_call_with_pool
from shared.llm_client.pricing import calc_cost

logger = structlog.get_logger()

TENANT_QA_MAX_REPAIRS = 2  # AA-425 — separate from acp_produce.models.REPAIR_TOTAL_MAX (N7, =3)

# AA-508/AA-619 — the day-fingerprint's "model" input is now read from the live t5_atomize stage
# config (_t5_cfg.model_id) at the top of _atomize_per_day(), NOT a hardcoded constant here. That
# keeps the fingerprint's model and the actual invoke_claude() model always identical within a run
# (the old `_T5_MODEL_TIER = "sonnet"` constant could drift from the DB config — e.g. after AA-619
# switched the config to haiku — leaving stale-model atoms un-invalidated). Constant removed.

# AA-526 — run_t5_atomize()'s own `tenant_id` param doubles as BOTH the free-text owner_scope
# value (any string is valid, tour_atoms.owner_scope has no type/FK constraint) AND the value
# passed straight through to record_call_with_pool(tenant_id=...), whose INSERT casts it
# `$1::uuid` (shared/llm_client/call_log.py). Every caller before AA-526 always passed a real
# tenant UUID, so this never mattered — now that A3's platform-scope atomize (owner_scope=
# "platform", a non-UUID string) reuses this same function, an uncaught cast error would fire on
# every single LLM call it logs (silently swallowed by that function's own try/except, but still
# a real, avoidable loss of cost/usage visibility for a stage that runs on every published tour).
def _llm_log_tenant_id(owner_scope: str) -> str | None:
    try:
        uuid.UUID(owner_scope)
        return owner_scope
    except (ValueError, AttributeError, TypeError):
        return None  # not a real tenant (e.g. "platform") — log with no tenant attribution

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


SEO_TITLE_MAX = 60  # same limit graph.py validate_node applies (SEO_TITLE_TOO_LONG)
_TITLE_SEPARATORS = (" — ", " – ", " | ", ": ", " - ")
_TITLE_DANGLING = {"and", "&", "with", "of", "in", "to", "for", "the", "a", "an", "—", "–", "-", "|", ":", ","}


def fit_seo_title(title: str, max_len: int = SEO_TITLE_MAX) -> str:
    """AA-639 — shorten an SEO title to max_len without an LLM call. First drop trailing
    separator-delimited segments ("Manaslu Circuit Trek — 18 Days | Nepal" → "Manaslu Circuit
    Trek — 18 Days") while that keeps a meaningful title; otherwise cut at the last word boundary
    and trim a dangling connector/punctuation. Titles already within the limit are unchanged."""
    t = " ".join(title.split())
    if len(t) <= max_len:
        return t
    head = t
    while len(head) > max_len:
        cut = max((head.rfind(sep) for sep in _TITLE_SEPARATORS), default=-1)
        if cut <= 0:
            break
        head = head[:cut].rstrip()
    # a clean leading segment (the tour's own name) beats a phrase cut mid-way
    if len(head) <= max_len and len(head) >= min(15, max_len // 2):
        return head
    words = t[: max_len + 1].split(" ")
    if len(t) > max_len:
        words = words[:-1] if len(words) > 1 else words
    while words and words[-1].lower().strip(",:;") in _TITLE_DANGLING:
        words.pop()
    out = " ".join(words).rstrip(" ,;:—–-|")
    return out[:max_len] if out else t[:max_len]


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
    while True:
        generated = result.get("generated") or {}
        # AA-639: an over-long SEO title is fixed deterministically here — live, a single
        # SEO_TITLE_TOO_LONG (flag_fix's LLM pass missed the 60-char count) cost a full rewrite of an
        # 18-day tour. Mutates the result that gets persisted.
        if isinstance(generated.get("seo_title"), str):
            generated["seo_title"] = fit_seo_title(generated["seo_title"])
        structural = _t3_structural_issues(generated, tour_dict, brand_rules)
        grounding = _t3_grounding_check(generated, source_texts)

        if not structural and not grounding:
            return {
                "passed": True, "result": result, "attempts": attempt,
                "structural_issues": [], "grounding_issues": [],
            }

        if attempt >= max_repairs:
            return {
                "passed": False, "result": result, "attempts": attempt,
                "structural_issues": structural, "grounding_issues": grounding,
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


async def escalate_t5_atomize_failure(
    pool, tenant_id: str, tour_id: str, version_id: str, error: str,
) -> None:
    """AA-469 Việc 5 — T5's real gap, closed: `run_t5_atomize()`'s own failures (already
    structured in code, `result["error"]` = `f"{type(e).__name__}: {e}"` or `f"invalid atom JSON
    from model: {e}"`) used to only ever reach CloudWatch (`logger.error()`), never a table A4
    reads. Mirrors `escalate_t3_failure()`'s exact row shape ABOVE — same table
    (`silver_aa_internal.review_queue`), same 5 columns, same `escalate_detail` per-item shape
    (`{check_id, field, description, source_span, suggested_fix}`) — deliberately NOT a new table/
    schema, per Việc 5's own brief ("mirror T2's pattern if it fits, rather than inventing a new
    one") and because `tenant_tour_version_id` (this function's own `version_id` param) is exactly
    the join key `GET /admin/a4/review-log` already filters on (`IS NOT NULL`) — a T5 failure row
    written here needs ZERO new A4 endpoint, it surfaces through the EXISTING review-log
    automatically. `check_id` is prefixed `t5_atomize:` (vs. T3's `structural:`/`grounding:`) so
    the frontend's existing per-check_id grouping/badge UI separates T5 from T3 rows without any
    UI logic change — only that page's static header text needed updating (see AA-469.md).

    Only 1 real call site exists for `run_t5_atomize()` as of AA-469 Việc 1 (its T2→T5-chain call
    was removed) — `api/routers/v1_tours.py::atomize_version()`, the standalone trigger endpoint,
    which already has `version_id`/`tour_id` in scope from its own DB read. Called from there,
    not from inside `run_t5_atomize()` itself, which never learns `version_id` (it only knows
    `tour_id`)."""
    category = error.split(":", 1)[0].strip() if ":" in error else "failed"
    detail = [{
        "check_id": f"t5_atomize:{category}", "field": None,
        "description": error, "source_span": None, "suggested_fix": None,
    }]
    summary = f"T5 atomize failed: {error[:200]}"
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO silver_aa_internal.review_queue
                (tour_id, tenant_id, tenant_tour_version_id, failure_summary, escalate_detail)
            VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5::jsonb)
        """, tour_id, tenant_id, version_id, summary, json.dumps(detail))
    logger.info("t5_escalated", tenant_id=tenant_id, tour_id=tour_id, version_id=version_id,
                category=category)


async def run_t5_atomize(
    tenant_id: str, tour_id: str, rewritten: dict, pool, country: str = "",
    version_id: str | None = None,
) -> dict:
    """T5 — decompose atoms from T4 output (tenant-rewritten), owner_scope=tenant_id.
    Reuses AA-299's proven prompt/parse pipeline, now living in
    services/acp_shared/atom_extraction.py (AA-475 — moved out of the deleted
    api/routers/v1_atoms.py, the old platform-scope N2 atomize endpoint this module
    never called directly, only imported these pure helpers from)
    (_build_user_prompt, _SYSTEM_PROMPT, _strip_json_fence, invoke_claude) — the
    two changes AA-425 asks for: (1) `row` built from T4 output, not a
    v_trip_registry SELECT (raw source); (2) owner_scope=tenant_id, not 'platform'.

    tour_id here MUST be silver_aa_internal.raw_tours.tour_id (acp_contract.
    tour_atoms.tour_id's FK target) — the caller passes published_tours.tour_id
    (same value, already selected alongside published_tours.id in
    trigger_rewrite()), not tenant_tour_versions.id or published_tours.id.

    AA-445-02 — country: the tour's raw_tours.country (caller already has tour_dict["country"]
    in scope), used to look up this tenant's declared competitor domains for score_distinctiveness()
    (acp_silver_s2.competitor_inputs is (tenant_id, country)-scoped). Only wired here (T5,
    owner_scope=tenant_id) — NOT at N2's platform-scope decompose — see AA-445-02 implementation
    notes Decision 1 for why (CompetitorIndex is tenant-relative; platform atoms have no single
    owning tenant). An empty/unresolvable country just yields an empty CompetitorIndex
    (score_distinctiveness() already handles that — returns MED), not an error.

    AA-508 — dispatches to one of two paths, per STEP0/STEP0b (docs/claude_audit/AA-508-step0*.md):

    - `_atomize_per_day()`: the new default. Splits `rewritten["itineraries"]` into individual
      days (parse_canonical_itinerary_days(), the SAME "Day N — Title\\nBody" parser validate_node/
      flag_fix_node already trust for this exact string — T4 reuses T2's engine, graph.py's
      generate_node/_process_itineraries, so this format is what real T4 output actually is), then
      atomizes/fingerprints/UPSERTs one day at a time instead of the whole tour in one shot.
      Requires `version_id` (the fingerprint table's key, acp_contract.atomize_day_fingerprint) —
      the one real call site (v1_tours.py::atomize_version()) always has it.
    - `_atomize_whole_tour_legacy()`: the pre-AA-508 behavior, byte-for-byte. Used when the
      itinerary isn't in canonical day format (parse_canonical_itinerary_days() returns {} —
      "cannot determine", not "zero days", per its own docstring — e.g. older/legacy rewritten
      content) or `version_id` is omitted (defensive: a future caller that forgets it gets the old
      whole-tour behavior, not a mis-atomize where everything is silently attributed to day
      None). Kept verbatim, not merged into the new path, specifically so this codebase's own
      pre-existing tests (test_aa445_t5_distinctiveness.py, both cases pass `itineraries=""`)
      keep exercising the real function unchanged.
    """
    row = {
        "id": tour_id,
        "name": rewritten.get("name") or "",
        "aa_summary": rewritten.get("summary") or "",
        "aa_highlights": rewritten.get("highlights") or [],
        "itinerary_source": rewritten.get("itineraries") or "",
    }
    # Whole-tour hash — AA-508 keeps this (STEP0 build-task instruction: "giữ lại source_hash
    # cấp-tour hiện có làm fallback/audit"). Still written onto every atom row either path
    # produces; no longer what decides skip-or-not in the per-day path (the fingerprint table
    # does), but still readable for audit/debugging and still what the legacy path skips on.
    source_hash = _source_hash(row)

    days = parse_canonical_itinerary_days(row["itinerary_source"])
    if not days or not version_id:
        return await _atomize_whole_tour_legacy(tenant_id, tour_id, row, source_hash, pool, country)
    return await _atomize_per_day(
        tenant_id, tour_id, version_id, row, days, source_hash, pool, country,
    )


async def _atomize_whole_tour_legacy(
    tenant_id: str, tour_id: str, row: dict, source_hash: str, pool, country: str,
) -> dict:
    """Pre-AA-508 behavior, unchanged (see run_t5_atomize()'s own docstring for when this runs).
    Random atom_id, one LLM call for the whole itinerary, source_hash-over-the-whole-tour skip."""
    async with pool.acquire() as conn:
        latest_hash = await conn.fetchval(
            """SELECT source_hash FROM acp_contract.tour_atoms
               WHERE tour_id = $1::uuid AND owner_scope = $2
               ORDER BY created_at DESC LIMIT 1""",
            tour_id, tenant_id,
        )
    if latest_hash is not None and latest_hash == source_hash:
        logger.info("t5_atomize_skipped", tour_id=tour_id, tenant_id=tenant_id,
                    reason="source unchanged (hash match)")
        return {"status": "skipped", "atom_count": 0}

    prompt = _build_user_prompt(row)
    # AA-518 — "t5_atomize" stage config (seeded sonnet/acc3). account="acc3" is now EXPLICIT
    # (was previously omitted here, silently defaulting to invoke_claude()'s own 'acc1' default —
    # unintentional drift every sibling Mechanism-B call site didn't have, flagged in AA-518.md
    # round 2 STEP0; fixed here rather than in a separate follow-up since this task already
    # touches this exact call site to wire config + persist-log).
    _t5_cfg = await get_stage_config("t5_atomize")
    try:
        llm_result = await asyncio.to_thread(
            invoke_claude, prompt, model=_t5_cfg.model_id, max_tokens=4096, system=_SYSTEM_PROMPT,
            account=_t5_cfg.account_route or "acc3",
        )
    except Exception as e:
        logger.error("t5_atomize_llm_failed", tour_id=tour_id, tenant_id=tenant_id,
                     error_type=type(e).__name__, error=str(e))
        return {"status": "failed", "error": f"{type(e).__name__}: {e}"}

    try:
        atoms = json.loads(_strip_json_fence(llm_result.text))["atoms"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.error("t5_atomize_parse_failed", tour_id=tour_id, tenant_id=tenant_id, error=str(e))
        return {"status": "failed", "error": f"invalid atom JSON from model: {e}"}

    # AA-445-02 (B4/score_distinctiveness()) — build the CompetitorIndex once per call
    # (not once per atom) and reuse it across every atom this tour produces; the corpus
    # itself is cache-backed (acp_shared.competitor_index_cache, 24h TTL) so this is a DB
    # read on a cache hit, not a re-fetch of every competitor homepage per tour.
    #
    # AA-526 — the real bug this build found live: competitor_index.py's own queries cast
    # `tenant_id = $1::uuid` (competitor_inputs/competitor_index_cache are BOTH genuinely
    # tenant-scoped, never platform-scoped — see that module's own docstring, which already
    # correctly excluded the old platform-scope N2 decompose endpoint from ever calling this).
    # owner_scope="platform" is not a valid UUID literal — crashed this call outright on every
    # single A3 atomize (confirmed via a real live-verify run, 05/09/2026), never reaching the
    # atom-insert loop below at all. Skip the fetch entirely for a non-tenant owner_scope —
    # score_distinctiveness()'s own empty-index fallback (returns "MED") is exactly the
    # documented, correct behavior here, not a workaround.
    from services.acp_shared.competitor_index import CompetitorIndex, build_competitor_index, score_distinctiveness
    if atoms and _llm_log_tenant_id(tenant_id) is not None:
        competitor_idx = await build_competitor_index(tenant_id, country, pool)
    else:
        competitor_idx = CompetitorIndex() if atoms else None

    inserted = 0
    async with pool.acquire() as conn:
        if atoms:
            for atom in atoms:
                atom_id = f"atom_{uuid.uuid4().hex[:10]}"
                place = atom.get("place") or ""
                action = atom.get("action") or ""
                text = _derive_atom_text(place, action)
                distinctiveness = score_distinctiveness(text, competitor_idx)
                await conn.execute("""
                    INSERT INTO acp_contract.tour_atoms
                        (atom_id, tour_id, owner_scope, text, place, action, activity_type,
                         emotional_hook, visual_potential, persona_fit, season_note, starred,
                         deleted, weight, source_hash, itinerary_day, distinctiveness,
                         created_at, updated_at)
                    VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11, $12, $13,
                            $14, $15, $16, $17, now(), now())
                """, atom_id, tour_id, tenant_id, text, place or None, action or None,
                    atom.get("activity_type"), atom.get("emotional_hook"),
                    atom.get("visual_potential", 1), json.dumps(atom.get("persona_fit") or []),
                    atom.get("season_note"), False, False, 1.0, source_hash,
                    atom.get("itinerary_day"), distinctiveness)
                inserted += 1
        else:
            marker_id = f"atom_marker_{uuid.uuid4().hex[:10]}"
            await conn.execute("""
                INSERT INTO acp_contract.tour_atoms
                    (atom_id, tour_id, owner_scope, text, starred, deleted,
                     is_empty_marker, weight, source_hash, created_at, updated_at)
                VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9, now(), now())
            """, marker_id, tour_id, tenant_id,
                "(zero-atom marker — no content, see is_empty_marker)",
                False, False, True, 1.0, source_hash)

    logger.info("t5_atomize_done", tour_id=tour_id, tenant_id=tenant_id, atom_count=inserted)
    # AA-505 — real, computed quality_signal: how many atoms this exact call actually produced
    # vs. a zero-atom marker (a real, meaningful proxy for a stage with no judge — see decision 1,
    # docs/implementation-notes/AA-518.md).
    await record_call_with_pool(
        pool, stage="t5_atomize", role="writer", model=llm_result.model_used,
        tokens_in=llm_result.usage.get("input_tokens"), tokens_out=llm_result.usage.get("output_tokens"),
        cost_usd=calc_cost(_t5_cfg.model_id, llm_result.usage.get("input_tokens", 0),
                            llm_result.usage.get("output_tokens", 0),
                            cache_read=llm_result.usage.get("cache_read_input_tokens", 0) or 0,
                            cache_write=llm_result.usage.get("cache_creation_input_tokens", 0) or 0),
        tenant_id=_llm_log_tenant_id(tenant_id),
        quality_signal={"atoms_extracted": inserted, "is_empty_marker": inserted == 0},
        stop_reason=llm_result.stop_reason,
        account=_t5_cfg.account_route or "acc3", fallback_used=False, provider="bedrock-satellite",
    )
    return {"status": "success", "atom_count": inserted}


async def _atomize_per_day(
    tenant_id: str, tour_id: str, version_id: str, row: dict, days: dict,
    source_hash: str, pool, country: str,
) -> dict:
    """AA-508 — one day at a time: fingerprint-gated skip (blocks the LLM call, not just logged
    after one), content-hash atom_id, real UPSERT. See atom_extraction.py::content_hash_atom_id()/
    day_fingerprint() for the two hash formulas and their reasoning vs. the reference repo's.

    Days are read SEQUENTIALLY, not concurrently (unlike aa-social-media's own 16-wide
    ThreadPoolExecutor) — AA-418's own prior investigation (services/acp_produce/pipeline.py's
    AA-416 comment) found concurrent invoke_claude() calls unverified-safe on this codebase's
    Bedrock satellite setup (unverified acc3 quota under concurrency); one call at a time keeps
    this inside the pattern already load-tested here, trading the reference repo's wall-clock
    speed for that. A day that fails does not lose days already read: each day's atoms +
    fingerprint row are written (and committed — no transaction spans more than one day, same
    autocommit-per-statement shape the pre-AA-508 code already had) before moving to the next day,
    mirroring the reference repo's own "committed before the failure is raised... keeps the days
    that did answer" guarantee (atoms.py). A day whose LLM call or JSON parse fails simply keeps
    no fingerprint row for itself, so the next call re-asks exactly that day and only that day.
    """
    # AA-619 — resolve the stage config ONCE, up front, so the day-fingerprint is keyed to the
    # SAME model the call below actually uses (read from DB config, not a hardcoded constant).
    # This is what makes a model change (e.g. Sonnet->Haiku, AA-619) correctly invalidate every
    # day's fingerprint so they re-atomize with the new model, instead of silently keeping the
    # old model's atoms. (Was `_T5_MODEL_TIER` — a module constant that drifted from the config.)
    _t5_cfg = await get_stage_config("t5_atomize")

    async with pool.acquire() as conn:
        existing = await conn.fetch(
            """SELECT day_number, fingerprint_hash FROM acp_contract.atomize_day_fingerprint
               WHERE tenant_tour_version_id = $1::uuid""",
            version_id,
        )
    existing_fp = {r["day_number"]: r["fingerprint_hash"] for r in existing}

    to_ask = []
    for day_num in sorted(days):
        day = days[day_num]
        fp = _day_fingerprint(day["title"], day["body"], _t5_cfg.model_id)
        if existing_fp.get(day_num) != fp:
            to_ask.append((day_num, day, fp))

    if not to_ask:
        logger.info("t5_atomize_all_days_skipped", tour_id=tour_id, version_id=version_id,
                    day_count=len(days))
        return {
            "status": "skipped", "atom_count": 0,
            "days_total": len(days), "days_read": 0, "days_skipped": len(days),
        }

    # AA-445-02 (B4/score_distinctiveness()) — built once for the whole call, reused across
    # every day that gets read, same reasoning as the legacy path's own comment.
    #
    # AA-526 — same real, live-confirmed bug as the legacy path above: skip the (genuinely
    # tenant-only) competitor fetch for a non-tenant owner_scope, fall back to the documented
    # empty-index "MED" default instead of crashing on the ::uuid cast.
    from services.acp_shared.competitor_index import CompetitorIndex, build_competitor_index, score_distinctiveness
    if _llm_log_tenant_id(tenant_id) is not None:
        competitor_idx = await build_competitor_index(tenant_id, country, pool)
    else:
        competitor_idx = CompetitorIndex()

    # AA-619 — _t5_cfg already resolved at the top of this function (used to key the fingerprint
    # above); reused here for the actual call + cost so the fingerprint model and the call model
    # can never drift within a single run.
    inserted = 0
    days_read = 0
    days_failed = []
    for day_num, day, fp in to_ask:
        prompt = _build_day_user_prompt(row, day_num, day["title"], day["body"])
        try:
            llm_result = await asyncio.to_thread(
                invoke_claude, prompt, model=_t5_cfg.model_id, max_tokens=4096, system=_SYSTEM_PROMPT,
                account=_t5_cfg.account_route or "acc3",
            )
        except Exception as e:
            logger.error("t5_atomize_day_llm_failed", tour_id=tour_id, version_id=version_id,
                         day_number=day_num, error_type=type(e).__name__, error=str(e))
            days_failed.append(day_num)
            continue
        try:
            atoms = json.loads(_strip_json_fence(llm_result.text))["atoms"]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.error("t5_atomize_day_parse_failed", tour_id=tour_id, version_id=version_id,
                         day_number=day_num, error=str(e))
            days_failed.append(day_num)
            continue

        new_atom_ids = []
        async with pool.acquire() as conn:
            if atoms:
                for atom in atoms:
                    place = atom.get("place") or ""
                    action = atom.get("action") or ""
                    text = _derive_atom_text(place, action)
                    # AA-610 — evidence: the verbatim source-text span `said` (atom_ranking.py)
                    # should measure, not `text`'s place—action join. checkable_evidence()
                    # falls back to the whole day's body if the model's quote doesn't actually
                    # check out against it, per its own docstring — never None here.
                    evidence = _checkable_evidence(atom.get("evidence") or "", day["body"])
                    atom_id = _content_hash_atom_id(tenant_id, tour_id, day_num, place, action)
                    distinctiveness = score_distinctiveness(text, competitor_idx)
                    # ON CONFLICT never touches starred/weight — starred is a human curation
                    # flag, weight is content_metrics.py's own learned value (usage-log-derived).
                    # An UPSERT that reset either on every re-atomize would silently erase both
                    # every time a day's fingerprint happened to change.
                    await conn.execute("""
                        INSERT INTO acp_contract.tour_atoms
                            (atom_id, tour_id, owner_scope, text, place, action, evidence,
                             activity_type, emotional_hook, visual_potential, persona_fit,
                             season_note, starred, deleted, weight, source_hash, itinerary_day,
                             distinctiveness, created_at, updated_at)
                        VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12,
                                $13, $14, $15, $16, $17, $18, now(), now())
                        ON CONFLICT (atom_id) DO UPDATE SET
                            text = excluded.text, place = excluded.place,
                            action = excluded.action, evidence = excluded.evidence,
                            activity_type = excluded.activity_type,
                            emotional_hook = excluded.emotional_hook,
                            visual_potential = excluded.visual_potential,
                            persona_fit = excluded.persona_fit,
                            season_note = excluded.season_note,
                            source_hash = excluded.source_hash,
                            distinctiveness = excluded.distinctiveness,
                            deleted = false, updated_at = now()
                    """, atom_id, tour_id, tenant_id, text, place or None, action or None,
                        evidence, atom.get("activity_type"), atom.get("emotional_hook"),
                        atom.get("visual_potential", 1),
                        json.dumps(atom.get("persona_fit") or []), atom.get("season_note"),
                        False, False, 1.0, source_hash, day_num, distinctiveness)
                    new_atom_ids.append(atom_id)
                    inserted += 1
            else:
                marker_id = _content_hash_atom_id(
                    tenant_id, tour_id, day_num, "__empty__", "__empty__",
                )
                await conn.execute("""
                    INSERT INTO acp_contract.tour_atoms
                        (atom_id, tour_id, owner_scope, text, starred, deleted,
                         is_empty_marker, weight, source_hash, itinerary_day, created_at, updated_at)
                    VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10, now(), now())
                    ON CONFLICT (atom_id) DO UPDATE SET
                        deleted = false, source_hash = excluded.source_hash, updated_at = now()
                """, marker_id, tour_id, tenant_id,
                    "(zero-atom marker — no content, see is_empty_marker)",
                    False, False, True, 1.0, source_hash, day_num)
                new_atom_ids.append(marker_id)

            # AA-508 — this day's content changed (that's why it was in `to_ask`) and may now
            # produce FEWER atoms than a prior read of the same day did; soft-delete whichever of
            # THIS day's previously-live atoms this read did not reproduce. Mirrors aa-social-
            # media's own delete_missing() (atoms.py) — soft, not hard, per this table's existing
            # admin-PATCH soft-delete convention (no hard DELETE precedent on tour_atoms).
            await conn.execute("""
                UPDATE acp_contract.tour_atoms SET deleted = true, updated_at = now()
                WHERE tour_id = $1::uuid AND owner_scope = $2 AND itinerary_day = $3
                  AND NOT deleted AND atom_id != ALL($4::text[])
            """, tour_id, tenant_id, day_num, new_atom_ids)

            await conn.execute("""
                INSERT INTO acp_contract.atomize_day_fingerprint
                    (tenant_tour_version_id, day_number, fingerprint_hash, atomized_at)
                VALUES ($1::uuid, $2, $3, now())
                ON CONFLICT (tenant_tour_version_id, day_number) DO UPDATE SET
                    fingerprint_hash = excluded.fingerprint_hash, atomized_at = now()
            """, version_id, day_num, fp)
        days_read += 1
        # AA-505 — per-day atom count, real and immediate (same reasoning as the legacy path).
        await record_call_with_pool(
            pool, stage="t5_atomize", role="writer", model=llm_result.model_used,
            tokens_in=llm_result.usage.get("input_tokens"),
            tokens_out=llm_result.usage.get("output_tokens"),
            cost_usd=calc_cost(_t5_cfg.model_id, llm_result.usage.get("input_tokens", 0),
                                llm_result.usage.get("output_tokens", 0),
                                cache_read=llm_result.usage.get("cache_read_input_tokens", 0) or 0,
                                cache_write=llm_result.usage.get("cache_creation_input_tokens", 0) or 0),
            tenant_id=_llm_log_tenant_id(tenant_id),
            quality_signal={"atoms_extracted": len(new_atom_ids), "day_number": day_num,
                             "is_empty_marker": not atoms},
            stop_reason=llm_result.stop_reason,
            account=_t5_cfg.account_route or "acc3", fallback_used=False, provider="bedrock-satellite",
        )

    result = {
        "status": "failed" if days_failed else "success",
        "atom_count": inserted, "days_total": len(days),
        "days_read": days_read, "days_skipped": len(days) - len(to_ask),
    }
    if days_failed:
        result["error"] = (
            f"day(s) {days_failed} failed to atomize "
            f"({days_read} day(s) succeeded and were kept)"
        )
        result["days_failed"] = days_failed
    logger.info("t5_atomize_per_day_done", tour_id=tour_id, version_id=version_id, **{
        k: v for k, v in result.items() if k not in ("error",)
    })
    return result
