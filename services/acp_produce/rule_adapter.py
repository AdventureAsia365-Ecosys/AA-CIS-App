"""
services.acp_produce.rule_adapter — AA-363 wiring apply_output_rules() (S4's
deterministic post-processor, api/services/acp_post_processor.py, AA-49 H-2)
into N7 Produce, WITHOUT modifying that function (architecture decision,
Linear AA-363 comment 05/08/2026: shared acp_output_rules table, no new
table, no migration — 18/22 seed rules are pipeline-agnostic brand-voice/
anti-leak checks, 4/22 are harmless-but-inert S4-vocabulary strings for N7,
0/22 are S4-structural).

STEP 0 (re-verified, not assumed from the earlier survey):

1. Field-name gap, confirmed real: apply_output_rules()'s _TEXT_FIELDS =
   ("content", "content_md", "seo_title", "seo_meta", "title"). Piece
   (services.acp_produce.models) only has `body_tagged` — none of those
   names. Calling apply_output_rules() with a raw dict built from a Piece
   (e.g. {"body_tagged": ...}) makes _collect_text_fields() return [],
   which makes EVERY rule silently no-op — a fail-open, not a crash. Fixed
   here with piece_to_rule_input() mapping body_tagged -> "content" (the
   first name in _TEXT_FIELDS, and the same fallback key
   acp_post_processor.py's own "replace"/"truncate" logic uses when
   "content_md" is absent — not an S4-specific name, the generic default).
   test_aa363_rule_adapter.py::test_no_adapter_fails_open proves the failure
   mode this fixes actually happens without the adapter.

2. Call point, confirmed from the REAL function, not from AA-298.md's
   pipeline-order note: acp_post_processor.py's own module docstring says
   "Call order in S4 flow: LLM generation -> apply_output_rules() -> ...",
   and the function signature (`output: dict` in, mutated output dict out,
   raises OutputRuleViolation on block) only makes sense against completed
   generated text — there is nothing to check before a draft exists.
   AA-298.md's "C1/C2 DFS -> C3 brief compile -> apply_output_rules() ->
   E1-E5 generation -> F1-F9 gates" one-line pipeline summary is WRONG about
   this ordering (apply_output_rules() cannot run before E1-E5, since E1-E5
   is what produces the text it checks) — not corrected in AA-298.md itself
   (historical record of that session), documented here and in
   docs/implementation-notes/AA-363.md instead. Correct point: AFTER E1-E5
   generation produces piece.body_tagged, before/alongside F1-F9
   (deterministic checks are cheap; running them before the F8/F9 LLM
   judge calls avoids paying for a judge call on content that was always
   going to be rejected for a banned word).

3. NOT plugged into gates.py::run_gates()'s own `gate_fns` list — confirmed
   structurally incompatible: run_gates() calls `gate_fn(piece.body_tagged)`
   synchronously (no `await`), while apply_output_rules() is async and
   requires a live `db` connection + `tenant_id`/`stage`. Also
   OutputRuleViolation is control flow raised as an exception, not a
   returned GateResult, and "flag"-type triggers just mutate a dict key —
   neither shape matches `Callable[[str], GateResult]`.
   apply_output_rules_to_piece() below normalizes both outcomes into a
   GateResult so a future orchestrator can append it to piece.gate_ledger
   the same way run_gates() does for F1/F8/F9, as its OWN separate async
   step — not a change to run_gates() or gates.py.

4. No live N7 orchestrator exists yet to call this from (same situation as
   AA-361's write_usage_log() and AA-362's emit_piece_metrics() — this is a
   ready-to-call building block, not a wired pipeline step).

5. GateResult convention in this codebase: `passed = not violations`
   everywhere (F1/F8/F9, gates.py) — there is no separate "warning, but
   still passed" concept. A "flag"-type rule trigger is therefore mapped to
   passed=False here too, even though S4's own flow treats "flag" as
   informational-only (content still ships, flagged for review) rather than
   a hard reject. This is a real behavior difference for N7 (stricter than
   S4), called out explicitly rather than silently inherited — see
   docs/implementation-notes/AA-363.md for the reasoning.

6. `stage` passed as None for N7 today: every one of the 22 seed rules uses
   stage=NULL (global) already (migration 020) — there is no N7-specific
   stage code to opt into yet, and inventing one here (schema allows any
   smallint, no CHECK constraint) without a real second N7-only rule to
   justify it would be premature. If a genuinely N7-only rule is ever
   needed, choosing that stage number is a decision for whoever adds it,
   not invented in this issue.
"""
from __future__ import annotations

from typing import Optional

import asyncpg

from api.services.acp_post_processor import OutputRuleViolation, apply_output_rules
from services.acp_produce.models import GateResult, Piece

RULE_ADAPTER_GATE_NAME = "output_rules"


def piece_to_rule_input(piece: Piece) -> dict:
    """Adapts a Piece into the dict shape apply_output_rules()'s _TEXT_FIELDS
    contract expects. "content" is the first name in _TEXT_FIELDS and the
    generic single-text-field default already used elsewhere in
    acp_post_processor.py — not an S4-specific choice."""
    return {"content": piece.body_tagged or ""}


async def apply_output_rules_to_piece(
    piece: Piece,
    stage: Optional[int],
    tenant_id: str,
    db: asyncpg.Connection,
) -> GateResult:
    """Runs the REAL, unmodified apply_output_rules() against a Piece's
    generated body_tagged, via piece_to_rule_input(). Returns a GateResult
    (gate="output_rules") rather than mutating the piece directly — the
    caller decides whether/how to append it to piece.gate_ledger, same as
    every other gate in this codebase."""
    output = piece_to_rule_input(piece)
    try:
        output = await apply_output_rules(output, stage, tenant_id, db)
    except OutputRuleViolation as exc:
        return GateResult(
            gate=RULE_ADAPTER_GATE_NAME, passed=False,
            violations=[f"{exc.rule_type} rule {exc.rule_id}: {exc.args[0]}"],
        )

    review_flags = output.get("review_flags") or []
    if review_flags:
        return GateResult(
            gate=RULE_ADAPTER_GATE_NAME, passed=False,
            violations=[f"flag rule {f['rule_id']}: {f['message']}" for f in review_flags],
        )

    return GateResult(gate=RULE_ADAPTER_GATE_NAME, passed=True, violations=[])


__all__ = ["RULE_ADAPTER_GATE_NAME", "piece_to_rule_input", "apply_output_rules_to_piece"]
