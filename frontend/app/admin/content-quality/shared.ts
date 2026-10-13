// app/admin/content-quality/shared.ts — AA-769. Types + helpers for the Content Quality page
// (GET /admin/content-quality/*). Cross-tenant, admin only.

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
}

export interface Gate {
  gate: string;
  passed: boolean;
  blocking?: boolean;
  repairable?: boolean;
  violations: string[];
}

export interface Highlight { start: number; end: number; gate: string; blocking: boolean; problem: string }

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
}

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

export const usd = (n: number | null | undefined) =>
  n == null ? "—" : n < 0.01 && n > 0 ? `$${n.toFixed(4)}` : `$${n.toFixed(3)}`;

export const fmtTime = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";

/** Plain-language gate names for admins (the codes stay visible next to them). */
export const GATE_LABELS: Record<string, string> = {
  F0_complete: "Text cut off",
  F1_grounding: "Number not in source",
  F2_banned_patterns: "Cliché / banned phrase",
  F2_brand_forbidden: "Brand-forbidden word",
  F11_brand_name: "Wrong brand name",
  F10_cannibalization_cross_tenant: "Too close to another tenant",
  promises_an_option: "Optional item stated as included",
  F4_extreme_length: "Length out of range",
  F4_seo_surface: "SEO title / meta",
  F5_atom_density: "Few source citations",
  F3_structural_variance: "Flat structure",
  F7_faq_dedup: "FAQ restates body",
  F7_faq_autofix: "FAQ pairs removed",
  F6_cta_present: "No CTA",
  F8_framework: "Goal framework",
  F9_cta_fact: "Unsupported claim / CTA",
  F9_brand_style: "Off-brand / AI wording",
  F12_scope: "Place outside the piece's scope",
  F13_hygiene: "Leak / social proof / repeat / hashtag",
  F13_autofix: "Auto-fixed sentences",
  F14_social_shape: "Social post shape",
  F15_voice: "Stock phrase / voice",
  within_tenant_reuse: "Repeats this tenant",
};

export const gateLabel = (g: string) => GATE_LABELS[g] ?? g;
