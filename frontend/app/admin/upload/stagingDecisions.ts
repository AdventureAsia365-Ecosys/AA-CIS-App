// app/admin/upload/stagingDecisions.ts — the Duplicate Review decisions of a staged re-import (moved out of page.tsx,
// AA-1146): their labels, the request body of each, and whether every chosen merge is ready to send.

import type { MergeChoice } from "./MergePanel";

export type Decision = "bypass" | "replace" | "keep_both" | "restore" | "create_new" | "merge";

type DecisionLabel = "decBypass" | "decMerge" | "decReplace" | "decKeepBoth" | "decRestore" | "decCreateNew";

// Message keys (messages.ts). AA-1146 T1: "merge" keeps the tour and its hand edits, taking the untouched fields from
// the sheet (MergePanel).
const DECISION_LABELS: Record<"bypass" | "merge" | "replace" | "keep_both", DecisionLabel> = {
  bypass:    "decBypass",
  merge:     "decMerge",
  replace:   "decReplace",
  keep_both: "decKeepBoth",
};

// AA-986 L2 (Q6, ADR 0015) — a re-upload of a tour deleted at S0/A0 never revives it silently: staff choose to
// restore the deleted tour (tenants that were warned are told it is back) or to start a new tour from the row.
const DELETED_DECISION_LABELS: Record<"bypass" | "restore" | "create_new", DecisionLabel> = {
  bypass:     "decBypass",
  restore:    "decRestore",
  create_new: "decCreateNew",
};

// AA-1181 — a tour made by hand in the content workspace never takes a supplier sheet (no merge, replace or keep
// both; the API refuses them): skip the row, or keep the sheet's tour as a separate new tour.
const HAND_MADE_DECISION_LABELS: Record<"bypass" | "create_new", DecisionLabel> = {
  bypass:     "decBypass",
  create_new: "decCreateNew",
};

// AA-1187 — a row that matched a live supplier tour on Name + country but names another Provider may be another
// operator's tour of the same name: Create a new tour keeps both, the matched tour untouched (a Merge asks before it
// changes the Provider).
const OTHER_PROVIDER_DECISION_LABELS: Record<"bypass" | "merge" | "create_new" | "replace" | "keep_both", DecisionLabel> = {
  bypass:     "decBypass",
  merge:      "decMerge",
  create_new: "decCreateNew",
  replace:    "decReplace",
  keep_both:  "decKeepBoth",
};

export function decisionLabels(
  item: { matched_deleted?: boolean; matched_hand_made?: boolean; matched_other_provider?: boolean },
): Record<string, DecisionLabel> {
  if (item.matched_deleted) return DELETED_DECISION_LABELS;
  if (item.matched_hand_made) return HAND_MADE_DECISION_LABELS;
  return item.matched_other_provider ? OTHER_PROVIDER_DECISION_LABELS : DECISION_LABELS;
}

/** The POST body of one decision: a merge carries the person's choices and reason. */
export function decisionBody(decision: Decision, merge: MergeChoice | undefined): Record<string, unknown> {
  if (decision !== "merge") return { decision };
  return { decision, resolutions: merge?.resolutions ?? {}, reason: merge?.reason ?? "" };
}

/** Confirm is enabled when nothing is being sent and every row set to merge has its reason. */
export function canConfirm(
  submitting: boolean, items: { staging_id: string }[], decisions: Record<string, Decision>,
  merges: Record<string, MergeChoice>,
): boolean {
  if (submitting) return false;
  return items.every(it => decisions[it.staging_id] !== "merge" || merges[it.staging_id]?.ready === true);
}
