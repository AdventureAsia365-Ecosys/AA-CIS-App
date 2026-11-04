// lib/writingLab.ts — AA-1039 R6 slice 3 (root ADR 0019 d13, TL-13): the Writing Lab stages and the shape of their
// read. A stage is a writing-memory writer (the four values every learning table CHECKs); the page is
// /admin/lab/<slug>, its data GET /api/admin/writing-memory/lab/<writer> (services/writing_memory/lab.py; admin only,
// $0, ids, labels, counts and hashes, never text). Dependency-free so `npm run test:unit` loads it directly.

export const LAB_PATH = "/admin/lab";
export const LAB_API = "/api/admin/writing-memory/lab";

export type LabWriter = "a1" | "t2" | "t9_blog" | "t9_social";

export interface LabStage {
  /** URL segment of /admin/lab/<slug>. */
  slug: string;
  writer: LabWriter;
}

/** The stage bar, in pipeline order: the A1 master rewrite (S1), the T2 tenant rewrite, the T9 article and social
 *  writers. Labels live in app/admin/lab/messages.ts (EN / VI). */
export const LAB_STAGES: readonly LabStage[] = [
  { slug: "a1-s1", writer: "a1" },
  { slug: "t2-rewrite", writer: "t2" },
  { slug: "t9-blog", writer: "t9_blog" },
  { slug: "t9-social", writer: "t9_social" },
];

export const DEFAULT_STAGE = LAB_STAGES[0].slug;

export function stageBySlug(slug: string | null | undefined): LabStage | null {
  return LAB_STAGES.find((s) => s.slug === slug) ?? null;
}

export function stageHref(slug: string, tab?: string | null): string {
  return tab && tab !== "gate" ? `${LAB_PATH}/${slug}?tab=${encodeURIComponent(tab)}` : `${LAB_PATH}/${slug}`;
}

export const LAB_TABS = ["gate", "samples", "labels", "experiments", "prompts"] as const;
export type LabTab = (typeof LAB_TABS)[number];

export function parseLabTab(v: string | null | undefined): LabTab {
  return (LAB_TABS as readonly string[]).includes(v ?? "") ? (v as LabTab) : "gate";
}

export type GateCode = "no_release" | "memory_off" | "reverify" | "regression" | "passing";

export interface SectionStatus { ready: boolean; error: string | null }

export interface GoldenItem {
  item_id: string; sample_id: string; set_name: string; split: string; kind: string; trap: boolean; tiers: string[];
  channel: string | null; scope: string; country: string | null; days: number | null; post_edit: boolean;
  label_verdict: string; label_source: string; pick_reason: string | null; created_at: string;
}

export interface EvalRun {
  id: string; pipeline: string; run_kind: string; prompt_version: string; baseline_prompt_version: string | null;
  tour_count: number; avg_quality_score: number | null; gate_pass_count: number | null; gate_fail_count: number | null;
  cost_usd: number | null; cost_cap_usd: number | null; regression_detected: boolean; verdict: string | null;
  stop_reason: string | null; judge_kappa: number | null; release_id: string | null; golden_set_id: string | null;
  tiers: string[]; created_at: string; finished_at: string | null;
}

export interface ShadowRun {
  run_id: string; release_id: string; items: number; pass: number; fail: number; errors: number; probes: number;
  cost_usd: number; started_at: string; last_at: string;
}

export interface ModelShadow {
  stage: string; primary_model: string; shadow_model: string; calls: number; errors: number;
  primary_cost_usd: number; shadow_cost_usd: number; shadow_p50_ms: number | null; last_at: string;
}

export interface PromptVersion {
  stage: string; version: string; prompt_sha256: string; sha_short: string; source_path: string;
  notes: string | null; created_at: string; created_by: string;
}

export interface Release {
  release_id: string; release_no: number; scope: string; train_kind: string; content_class: string; status: string;
  rules: number; r0_run_id: string | null; eval_run_id: string | null; shadow_run_id: string | null;
  reverify: boolean; reverify_reasons: string[]; approved_at: string | null; activated_at: string | null;
  retired_at: string | null; created_at: string; updated_at: string;
}

export type Counts = Record<string, number>;

export interface LabStageView {
  writer: LabWriter;
  prompt_stages: string[];
  sections: Record<"samples" | "labels" | "experiments" | "prompts" | "gate", SectionStatus>;
  samples: { golden_sets: { set_id: string; name: string; version: string; created_at: string; items: number }[];
    total: number; by_split: Counts; by_kind: Counts; items: GoldenItem[]; limit: number } | null;
  labels: { golden: { by_verdict: Counts; by_source: Counts };
    signals: { total: number; last_30d: number; by_fact_status: Counts; by_reason_code: Counts; by_action: Counts;
      by_actor_type: Counts; by_scope: Counts } } | null;
  experiments: { eval_runs: EvalRun[]; shadow_runs: ShadowRun[]; model_shadow: ModelShadow[];
    model_shadow_days: number } | null;
  prompts: { stages: string[]; versions: PromptVersion[]; limit: number } | null;
  gate: { switch: { memory_enabled: boolean; train_closed_month: string | null; updated_at: string } | null;
    active_release: Release | null; latest_release: Release | null; releases: Counts;
    tenant_releases_active: number; rules: Counts; last_eval: EvalRun | null;
    state: { code: GateCode; ok: boolean; reasons?: string[] } } | null;
}

/** Badge tone of a gate state. */
export function gateTone(code: GateCode): "success" | "warning" | "danger" | "neutral" {
  return code === "passing" ? "success" : code === "regression" ? "danger"
    : code === "reverify" || code === "memory_off" ? "warning" : "neutral";
}

/** Badge tone of an evaluation / shadow verdict. */
export function verdictTone(v: string | null | undefined): "success" | "danger" | "warning" | "neutral" | "info" {
  return v === "pass" ? "success" : v === "fail" || v === "invalid" ? "danger" : v === "baseline" ? "info"
    : v == null ? "warning" : "neutral";
}

/** Share of `part` in `whole` as a whole percent, or "—" when there is nothing to divide. */
export function share(part: number, whole: number): string {
  return whole > 0 ? `${Math.round((part / whole) * 100)}%` : "—";
}

/** Count rows of a {value: n} map, largest first (the API already sorts; kept for safety). */
export function countRows(c: Counts | null | undefined): { key: string; n: number }[] {
  return Object.entries(c ?? {}).map(([key, n]) => ({ key, n })).sort((a, b) => b.n - a.n || a.key.localeCompare(b.key));
}
