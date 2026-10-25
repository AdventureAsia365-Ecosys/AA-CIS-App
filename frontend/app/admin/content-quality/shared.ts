// app/admin/content-quality/shared.ts — AA-769. Types + helpers for the Content Quality page
// (GET /admin/content-quality/*). Cross-tenant, admin only.

import type { SeedInfo } from "../_components/SeedLine";
import { GATE_LABELS, RETIRED_GATES } from "@/lib/quality-registry.generated";

export interface PieceRow {
  piece_id: string;
  tenant_id: string;
  tenant_name: string;
  channel: string | null;
  status: string;
  held_reason: string | null;
  attempt_number: number;
  created_at: string;
  job_id: string | null;
  title: string | null;
  angle_name: string | null;
  goal: string | null;
  content_chars: number;
  has_writer_input: boolean;
  failed_gates: string[];
  flag_gates: string[];
  cost_usd: number;
  // AA-791 — only once migration 231 is applied (`labels_enabled` on the list response).
  label?: PieceLabelValue | null;
  label_count?: number;
  // AA-863 — the Slate seed (angle_gate_request.atom_id): place · country, Segment label, mismatch.
  // null when the piece has no request row or the seed read failed (fail-soft).
  seed?: SeedInfo | null;
}

export interface Gate {
  gate: string;
  passed: boolean;
  blocking?: boolean;
  repairable?: boolean;
  violations: string[];
  // AA-924 — per-violation severity (pieces written since PR1). `severity` = the worst finding; a merged entry
  // (F2_lexicon, F15_voice) names the legacy gate of each finding in `source`. Absent on older ledgers: every
  // violation then reads the entry's `blocking` (lib/gateSeverity.ts).
  severity?: "block" | "warn";
  findings?: { text: string; severity: "block" | "warn"; source?: string }[];
}

/** One flagged span (display). Offsets are Python code points — see toUtf16 in PieceDrawer. */
export interface Highlight {
  start: number; end: number; gate: string; blocking: boolean; problem: string;
  severity?: "block" | "warn" | null; source?: string; // AA-924 — the finding's own severity and source gate
  text: string; sentence_start: number; sentence_end: number; sentence: string; key: string;
}

/** One actionable sentence (S234): every gate that flagged it, and the span Cut / Rewrite send back. */
export interface FlaggedSentence {
  key: string; start: number; end: number; text: string; sentence: string;
  gates: string[]; problems: SentenceProblem[]; blocking: boolean;
}

/** AA-924 — one problem of a flagged sentence: its finding's severity, and the source gate inside a merged entry. */
export interface SentenceProblem {
  gate: string; problem: string; blocking: boolean; severity?: "block" | "warn" | null; source?: string;
}

export interface AdminEdit {
  action: string; actor: string; created_at: string;
  details: {
    before?: string; after?: string; mode?: string; gate?: string | null; published?: boolean; exported?: boolean;
    field?: string; index?: number | null; // AA-802 — T2 version edits name the section
  };
}

/** AA-802 — whether the tenant holds a copy (published post / downloaded file) older than our text.
 *  Nothing is re-synced: an admin edit after `delivered_at` leaves that copy with the old sentence. */
export interface TenantCopy {
  published_at: string | null;
  exported_at: string | null;
  delivered_at: string | null;
  older_copy: boolean;
  edits_after_delivery: number;
}

// blocking: Apply refuses it (F1/F2/brand-forbidden/F11/near-verbatim repeat); otherwise a warning (F12/F13).
export interface CheckFailure { gate: string; violations: string[]; blocking: boolean }

export interface SuggestResponse {
  start: number; end: number; sentence: string; replacement: string;
  checks: CheckFailure[]; model: string | null; cost_usd: number;
}

export interface ApplyBody {
  start: number; end: number; text: string; replacement: string; mode: "cut" | "rewrite" | "manual";
  gate?: string; problem?: string; suggestion?: string; suggestion_model?: string | null; suggestion_cost_usd?: number;
}

export interface CostRow { stage: string; role: string; calls: number; cost_usd: number }

export interface WriterInput {
  seed?: string;
  route_moments?: { atom_id: string; seed: string }[];
  facts?: string;
  scope?: string | null;
  brand_pack?: Record<string, unknown>;
  brand_rubric?: string | null;
  angle?: Record<string, string | null>;
  goal?: string; channel?: string; keyword?: string | null; cta?: string;
  trip?: string | null; destination?: string | null;
  // AA-779 (S245) — blog only: real search questions the writer got, per-source counts, final FAQ sources.
  search_questions?: QuestionWithSource[];
  question_sources?: { candidates?: Record<string, number>; kept?: Record<string, number> };
  faq_questions?: QuestionWithSource[];
}

export interface QuestionWithSource { question: string; source: string }

export interface PieceDetail extends Omit<PieceRow, "failed_gates" | "flag_gates" | "title" | "content_chars" | "has_writer_input"> {
  content_text: string;
  seo_title: string | null;
  meta_description: string | null;
  slug: string | null;
  gate_ledger: Gate[];
  flags: Gate[] | null;
  repair_log: Record<string, unknown>[];
  discarded_attempts: { attempt_number: number; content_text: string; gate_ledger: Gate[] }[];
  writer_input: WriterInput | null;
  angle_why: string | null;
  angle_style: string | null;
  highlights: Highlight[];
  flagged_sentences: FlaggedSentence[];
  published: boolean;
  exported: boolean;
  tenant_copy: TenantCopy;
  admin_edits: AdminEdit[];
  cost_by_stage: CostRow[];
  other_pieces_for_request: { piece_id: string; status: string; attempt_number: number; created_at: string }[];
}

export interface VersionRow {
  id: string;
  tenant_name: string;
  tour_name: string;
  version_number: number;
  status: string;
  quality_score: number | null;
  qa_status: string | null;
  qa_auto_passed: boolean | null;
  qa_repair_count: number | null;
  edit_source: string | null;
  created_at: string;
  cost_usd: number;
  escalations: number;
}

// AA-802 — T2 silent correction (GET /tour-versions/{id}, POST …/sentences/suggest|apply).
export type TourProblem = SentenceProblem;
export interface TourSentence {
  key: string; start: number; end: number; text: string; sentence: string; problems: TourProblem[];
}
export interface TourSection { field: string; index: number | null; text: string; sentences: TourSentence[] }
export interface TourFlaggedSentence extends TourSentence {
  field: string; index: number | null; gates: string[]; blocking: boolean;
}
export interface TourVersionDetail extends Omit<VersionRow, "cost_usd" | "escalations"> {
  tenant_id: string;
  published_tour_id: string;
  latest_version_number: number | null;
  rewritten_content: unknown;
  reviews: { failure_summary: string | null; escalate_detail: unknown; review_status: string; created_at: string }[];
  cost_by_stage: CostRow[];
  sections: TourSection[];
  flagged_sentences: TourFlaggedSentence[];
  admin_edits: AdminEdit[];
  editable: boolean;
  edit_block_reason: string | null;
  tenant_copy: TenantCopy;
}
export interface TourSuggestResponse extends SuggestResponse { field: string; index: number | null }
export interface TourApplyBody extends ApplyBody { field: string; index: number | null }

export const TOUR_FIELD_LABELS: Record<string, string> = {
  subtitle: "Subtitle", summary: "Summary", highlights: "Highlight", itineraries: "Itinerary",
};
export const tourSectionLabel = (field: string, index: number | null) =>
  `${TOUR_FIELD_LABELS[field] ?? field}${index != null ? ` ${index + 1}` : ""}`;

export interface GateStat { gate: string; channel: string; runs: number; failed: number; blocking_failed: number }
export interface TenantStat { tenant_name: string; tenant_id: string; pieces: number; approved: number; held: number; failed: number; cost_usd: number }

export interface PlaceCard {
  card_id: string;
  display_name: string;
  locality: string | null;
  region: string | null;
  country: string;
  naming_note: string | null;
  facts: { text: string; kind: string }[];
  status: "auto_checked" | "verified" | "rejected";
  cost_usd: number;
  verified_by: string | null;
  verified_at: string | null;
  created_at: string;
  atoms: number;
}

// AA-774 (S235) — GET /place-cards/{id} and PATCH /place-cards/{id}/facts: one card with each fact's
// index in the stored array and whether the T9 writer seed shows it (what_it_is/history/setting/naming).
export interface PlaceCardFactRow { index: number; kind: string; text: string; in_writer_seed: boolean }
export interface PlaceCardDetail extends PlaceCard {
  updated_at: string;
  fact_rows: PlaceCardFactRow[];
  fact_kinds: string[];
}
export interface FactEdit { index: number; kind?: string; text?: string; drop?: boolean; expected_text?: string }

// S234 (AA-772) — GET /place-cards/duplicates: candidate clusters an admin may merge.
export interface DuplicateCard {
  card_id: string;
  display_name: string;
  locality: string | null;
  region: string | null;
  country: string;
  status: PlaceCard["status"];
  facts: number;
  atoms: number;
  cost_usd: number;
  created_at: string;
}
export interface DuplicateCluster {
  country: string;
  reasons: ("same_core_name" | "name_contains" | "alias")[];
  cards: DuplicateCard[];
}
export const DUPLICATE_REASON_LABELS: Record<string, string> = {
  same_core_name: "same name",
  name_contains: "one name contains the other",
  alias: "alias match",
};

export interface Paged<T> { data: T[]; total: number; limit: number; offset: number }

// AA-791 — human labels per piece (acp_shared.content_piece_label, migration 231; AA-783 criteria).
export type PieceLabelValue = "as_is" | "light" | "heavy" | "reject";
export type DefectLevel = "none" | "light" | "heavy";
export const LABEL_NAMES: Record<PieceLabelValue, string> = {
  as_is: "As-is", light: "Light edit", heavy: "Heavy edit", reject: "Reject",
};
export const LABEL_HINTS: Record<PieceLabelValue, string> = {
  as_is: "Publishable without a change",
  light: "≤10 min, ≤3 sentences, no research",
  heavy: "More than a light edit",
  reject: "Not worth fixing",
};
export const DEFECT_NAMES: Record<string, string> = {
  fact: "Fact", voice: "Voice", format: "Format", ai_phrasing: "AI phrasing",
};
export interface PieceLabel {
  labelled_by: string;
  label: PieceLabelValue;
  defects: Record<string, DefectLevel>;
  minutes_to_fix: number | null;
  reason: string | null;
  labelled_at: string;
  text_changed_since_label: boolean;
}
export interface PieceLabels {
  piece_id: string;
  labels: PieceLabel[];
  mine: PieceLabel | null;
  choices?: { label: PieceLabelValue[]; defect_classes: string[]; defect_levels: DefectLevel[] };
}
export interface PieceLabelBody {
  label: PieceLabelValue;
  defects: Record<string, DefectLevel>;
  minutes_to_fix: number | null;
  reason: string | null;
}
export type PiecesPage = Paged<PieceRow> & { labels_enabled?: boolean };

export const usd = (n: number | null | undefined) =>
  n == null ? "—" : n < 0.01 && n > 0 ? `$${n.toFixed(4)}` : `$${n.toFixed(3)}`;

export const fmtTime = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";

// AA-926 — gate labels and the retired set come from the quality registry (services/quality/taxonomy.yaml),
// generated into lib/quality-registry.generated.ts. Same 33 labels in the same order, plus labels for four
// active gates that showed as raw codes (F2_platform_truth, F2_superlatives, F2_style_warn, F19_authenticity).
export { GATE_LABELS, RETIRED_GATES };

export const gateLabel = (g: string) => {
  const label = GATE_LABELS[g] ?? g;
  return RETIRED_GATES.has(g) ? `${label} (retired)` : label;
};

/** Backend offsets are Python code-point indices; JS strings index UTF-16 units. Returns a mapper
 *  from code-point index to JS index (the identity when the text has no astral chars, e.g. emoji). */
export function codePointIndex(text: string): (cp: number) => number {
  if (!/[\uD800-\uDBFF]/.test(text)) return (i) => i;
  const map: number[] = [];
  let u = 0;
  for (const ch of text) { map.push(u); u += ch.length; }
  map.push(u);
  return (i) => map[Math.max(0, Math.min(i, map.length - 1))];
}
