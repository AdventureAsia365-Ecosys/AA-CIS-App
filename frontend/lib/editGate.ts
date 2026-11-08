// lib/editGate.ts — AA-1052 W6 (root ADR 0020 d12): which admin edits of the writing path need evidence and are
// frozen during a rerun wave. Mirrors services/quality/edit_gate.py and change_control.GATED_STAGE_PREFIXES (the
// unit test lib/editGate.test.mjs reads the Python file and fails when the two lists differ). The API decides; the
// page only asks for the evidence before it sends.

/** The writing path's gated stage prefixes (services/quality/change_control.py GATED_STAGE_PREFIXES). */
export const GATED_STAGE_PREFIXES = ["s1_", "t2_", "t3_", "t8_", "t9_", "t10_", "writing_memory_eval"] as const;

/** The route-policy row that holds the defaults of every stage. */
export const DEFAULT_POLICY_STAGE = "*";

/** True for a stage of the gated prefixes, or "*" (the route-policy defaults of every stage). */
export function isGatedStage(stage: string): boolean {
  return stage === DEFAULT_POLICY_STAGE || GATED_STAGE_PREFIXES.some((p) => stage.startsWith(p));
}

/** A route-policy save (or a reset: `after` null) needs evidence when it moves the hold switch of a gated stage. */
export function holdChangeNeedsEvidence(stage: string, before: boolean | null | undefined,
  after: boolean | null | undefined): boolean {
  return isGatedStage(stage) && (before ?? null) !== (after ?? null);
}
