// lib/writingLab.ts — AA-1039 R6 slice 3 (root ADR 0019 d13, TL-13): the Writing Lab stages and the shape of their
// read. A stage is a writing-memory writer (the four values every learning table CHECKs); the page is
// /admin/lab/<slug>, its data GET /api/admin/writing-memory/lab/<writer> (services/writing_memory/lab.py; admin only,
// $0, ids, labels, counts and hashes, never text). Dependency-free so `npm run test:unit` loads it directly.

import type { KpiReport, Release as WmRelease, ReleaseStops, WriterKpi } from "./writingMemory";

export const LAB_PATH = "/admin/lab";
export const LAB_API = "/api/admin/writing-memory/lab";
/** The Writing memory tools (the actions: approve, activate, roll back, keep) and their API. */
export const WRITING_MEMORY_PATH = "/admin/lab/writing-memory";
export const WM_RELEASES_API = "/api/admin/writing-memory/releases";
export const WM_METRICS_API = "/api/admin/writing-memory/metrics";

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

// S268 (rest of R6): Releases and Metrics per stage (IA section 6.4: the Releases and Metrics tabs of a Lab stage),
// over the existing Writing memory reads (GET /releases?writer=, GET /metrics), read-only here.
export const LAB_TABS = ["gate", "samples", "labels", "experiments", "prompts", "releases", "metrics"] as const;
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
  reverify_run_id: string | null; reverify: boolean; reverify_reasons: string[]; approved_at: string | null; activated_at: string | null;
  retired_at: string | null; created_at: string; updated_at: string;
}

export type Counts = Record<string, number>;

/** The short evaluation run the gate section returns (active_eval / last_eval). */
export interface GateEval {
  id: string; run_kind: string; verdict: string | null; stop_reason: string | null; regression_detected: boolean;
  cost_usd: number | null; tour_count: number; release_id: string | null; created_at: string;
  finished_at: string | null;
}

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
    tenant_releases_active: number; rules: Counts;
    /** The newest finished evaluation run of the ACTIVE release: what `state` reads. */
    active_eval: GateEval | null;
    /** The writer's newest finished evaluation run of any release: context only, never the state. */
    last_eval: (GateEval & { for_active: boolean }) | null;
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

/** Value tone of an evaluation tile: red when the run failed or saw a regression, green when it passed (or is the
 *  baseline import of release 0), grey when there is no run or another verdict. */
export function evalTone(run: Pick<GateEval, "verdict" | "regression_detected"> | null | undefined):
  "success" | "danger" | "neutral" {
  if (!run) return "neutral";
  if (run.verdict === "fail" || run.verdict === "invalid" || run.regression_detected) return "danger";
  return run.verdict === "pass" || run.verdict === "baseline" ? "success" : "neutral";
}

/** Share of `part` in `whole` as a whole percent, or "—" when there is nothing to divide. */
export function share(part: number, whole: number): string {
  return whole > 0 ? `${Math.round((part / whole) * 100)}%` : "—";
}

/** Count rows of a {value: n} map, largest first (the API already sorts; kept for safety). */
export function countRows(c: Counts | null | undefined): { key: string; n: number }[] {
  return Object.entries(c ?? {}).map(([key, n]) => ({ key, n })).sort((a, b) => b.n - a.n || a.key.localeCompare(b.key));
}


// ── Releases and Metrics per stage (S268) ─────────────────────────────────────────────────────────────────────────

/** The Writing memory page on one of its tabs (it reads `?tab=`). */
export function writingMemoryHref(tab?: "signals" | "candidates" | "releases" | "exemplars" | "metrics"): string {
  return tab ? `${WRITING_MEMORY_PATH}?tab=${tab}` : WRITING_MEMORY_PATH;
}

/** GET /releases of one writer: every scope and status, newest first (the API orders them), up to `limit`. */
export function releasesUrl(writer: LabWriter, limit = 50): string {
  return `${WM_RELEASES_API}?writer=${encodeURIComponent(writer)}&limit=${limit}`;
}

export type LabRelease = WmRelease;

/** "r0 pass · eval — · shadow fail": the evidence runs a release carries (a slot it does not need is not shown). */
export function evidenceSummary(r: Pick<WmRelease, "evidence" | "r0_run_id" | "eval_run_id" | "shadow_run_id">): {
  slot: "r0" | "eval" | "shadow"; verdict: string | null; finished: boolean;
}[] {
  const out: { slot: "r0" | "eval" | "shadow"; verdict: string | null; finished: boolean }[] = [];
  for (const slot of ["r0", "eval", "shadow"] as const) {
    const id = r[`${slot}_run_id` as const];
    const run = r.evidence?.[slot] ?? null;
    if (!id && !run) continue;
    out.push({ slot, verdict: run?.verdict ?? null, finished: !!run?.finished });
  }
  return out;
}

/** Badge tone of a release status. */
export function releaseTone(status: string): "success" | "danger" | "warning" | "info" | "neutral" {
  return status === "active" ? "success" : status === "rejected" ? "danger"
    : status === "approved" || status === "evaluated" ? "info" : status === "draft" ? "warning" : "neutral";
}

/** One weekly report row of a writer: its overall KPIs (null when the report did not measure it), the replay rate,
 *  the clean pairs and the soft stops / job actions of the writer's releases. */
export interface WriterWeek {
  week_start: string;
  trigger: string;
  kpi: WriterKpi | null;
  replay_rate: number | null;
  clean_pairs: number | null;
  stops: ReleaseStops[];
  actions: { release_no: number; scope: string; action: string; codes: string[] }[];
}

/** The stored weekly reports (newest first) seen from one writer. Reports keep their order. */
export function writerWeeks(reports: readonly KpiReport[] | null | undefined, writer: LabWriter): WriterWeek[] {
  return (reports ?? []).map((r) => {
    const block = r.kpis?.writers?.[writer] ?? null;
    return {
      week_start: r.week_start,
      trigger: r.trigger,
      kpi: block?.overall ?? null,
      replay_rate: block?.replay?.rate ?? null,
      clean_pairs: block ? block.clean_pairs : null,
      stops: (r.soft_stops ?? []).filter((s) => s.writer === writer && (s.stops?.length ?? 0) > 0),
      actions: (r.actions ?? []).filter((a) => a.writer === writer)
        .map((a) => ({ release_no: a.release_no, scope: a.scope, action: a.action, codes: [...(a.codes ?? [])] })),
    };
  });
}

/** A rate as a whole-number-and-one-decimal percent, "—" when not measured (never 0 %). */
export function pct(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : `${(v * 100).toFixed(1)}%`;
}
