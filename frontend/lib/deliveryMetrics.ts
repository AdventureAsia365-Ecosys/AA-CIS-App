// frontend/lib/deliveryMetrics.ts
// AA-1108: types and pure helpers of Platform › Delivery (app/admin/platform/delivery). The summary is built by the
// root repo workflow "Delivery metrics" (tools/delivery/delivery_metrics.py) and served by the CIS API
// (api/services/delivery_metrics.py, GET /admin/platform/delivery) with each metric's colour and the thresholds in
// force (settings cis.delivery.*). Definitions of every number: root docs/guides/delivery-metrics.md.

import type { RangeMeta } from "./deliveryRange";

export const DELIVERY_PATH = "/api/admin/platform/delivery";
export const GUIDE_PATH = "docs/guides/delivery-metrics.md";
export const SETTINGS_HREF = "/admin/platform/settings?app=cis&area=delivery";

export type MetricStatus = "green" | "amber" | "red" | "few" | "none";
export type MetricKey =
  | "deployment_frequency" | "lead_time" | "change_failure_rate" | "recovery_time" | "rework_rate" | "ci_p95" | "main";
export const DORA_KEYS: MetricKey[] = [
  "deployment_frequency", "lead_time", "change_failure_rate", "recovery_time", "rework_rate",
];
export type SegmentKey = "to_first_green" | "to_queue" | "to_merge" | "to_deploy" | "to_verify";
export const SEGMENTS: SegmentKey[] = ["to_first_green", "to_queue", "to_merge", "to_deploy", "to_verify"];
export type DeployOutcome = "success" | "verify_failed" | "deploy_failed" | "unclassified";

export interface Dora {
  deployment_frequency: { value: number | null; n: number; attempts: number; unit: string };
  lead_time: { p50_h: number | null; p90_h: number | null; n: number };
  change_failure_rate: { value: number | null; n: number; failed: number; by_reason: Record<string, number> };
  recovery_time: { p50_h: number | null; p90_h: number | null; n: number; open: number };
  rework_rate: { value: number | null; n: number; rework: number };
}

export interface SegmentStat {
  segment: SegmentKey; p50_h: number | null; p90_h: number | null; n: number;
  /** AA-1135: collectors from 18/11/2026 on */
  p95_h?: number | null;
}

export interface WorkflowRow {
  name: string; file: string; runs: number; failures: number; failure_rate: number | null; p50_s: number | null;
  p95_s: number | null; reruns: number; minutes: number; kind: "ci" | "deploy" | "other"; last_url: string | null;
  last_result: string | null;
}

export interface CiBlock {
  workflows: WorkflowRow[];
  slowest: { name: string; p95_s: number; n: number } | null;
  main: { state: "green" | "red" | "unknown"; red_since: string | null; red_hours: number | null; url: string | null;
    failure_rate: number | null; n: number };
  reruns: number;
  minutes: { total: number; coverage: number | null; n: number };
}

export interface DeployRun { id: number; url: string; workflow: string; outcome: string; attempt: number }

export interface DeployRow {
  sha: string; started_at: string | null; deployed_at: string | null; finished_at: string | null;
  verified_at: string | null; outcome: DeployOutcome; failed: string[]; rework: string[]; redeploy: boolean;
  failed_at: string | null; recovered_at: string | null; recovery_h: number | null; deployment_records: number;
  runs: DeployRun[]; changes: number[];
}

export interface FlakyRow { workflow: string; job: string; flips: number; last_at: string | null; url: string | null }

export interface WeekRow {
  week: string; start: string; end: string; partial: boolean; complete: boolean; deploys: number;
  deploy_attempts: number; lead_time_p50_h: number | null; lead_time_n: number; cfr: number | null; failed: number;
  recovery_p50_h: number | null; recovery_n: number; rework_rate: number | null; rework: number;
  main_failure_rate: number | null; main_n: number; ci_p95_s: number | null; minutes: number;
}

export interface ChangeRow {
  number: number; title: string; url: string; kind: "agent" | "human" | "bot"; login: string | null;
  excluded: "bot" | "docs_only" | null; merged_at: string | null; lead_time_h: number | null;
  segments: Partial<Record<SegmentKey, number>>;
}

export interface DeliveryApp {
  id: string; name: string; repo: string; environment: string;
  window: { start: string; end: string; days: number; covered_days?: number; requested_start?: string };
  complete_since: string | null;
  dora: Dora;
  lead_time_segments: SegmentStat[];
  ci: CiBlock;
  merge_queue: { wait_p50_h: number | null; wait_p95_h: number | null; n: number; ejections: number };
  flaky: FlakyRow[];
  verify: { pass_rate: number | null; n: number; red: number; last_red_at: string | null; last_red_url: string | null };
  deploys: DeployRow[];
  failed_deploys: DeployRow[];
  changes: { merged: number; by_kind: Record<"agent" | "human" | "bot", number>; docs_only: number;
    awaiting_deploy: number; bot: ChangeRow[]; slowest: ChangeRow[] };
  trend: { weeks: WeekRow[] };
  status: Record<MetricKey, MetricStatus>;
}

export interface Band { green: number; amber: number; higher_is_better: boolean; keys: string[] }

export interface DeliveryResp {
  format: number;
  generated_at: string | null;
  environment: string | null;
  source: { repo: string | null; sha: string | null; run_id: string | null };
  definitions: { version?: string; headline_days?: number; rework_hours?: number; trend_weeks?: number;
    guide?: string } | null;
  collection: { requests?: number | null; stopped?: string | null; errors?: string[] } | null;
  apps: DeliveryApp[];
  thresholds: Record<Exclude<MetricKey, "main">, Band> & { main_red_hours: number; min_sample: number };
  /** AA-1135: the range served and what the date picker may offer */
  range?: RangeMeta;
  cache_ttl_s: number;
  loaded_at: string;
}

const ROOT_REPO_URL = "https://github.com/AdventureAsia365-Ecosys";

/** Kit tone of a metric colour (few / none are grey). */
export function statusTone(s: MetricStatus | undefined): "success" | "warning" | "danger" | "neutral" {
  return s === "green" ? "success" : s === "amber" ? "warning" : s === "red" ? "danger" : "neutral";
}

const round1 = (v: number) => Math.round(v * 10) / 10;

/** Hours as minutes under one hour, hours under two days, days above. */
export function fmtHours(h: number | null | undefined): string {
  if (h === null || h === undefined || !Number.isFinite(h)) return "—";
  if (h < 1) return `${Math.max(1, Math.round(h * 60))} min`;
  if (h < 48) return `${round1(h)} h`;
  return `${round1(h / 24)} d`;
}

/** Seconds as seconds under 90 s, else minutes. */
export function fmtSeconds(s: number | null | undefined): string {
  if (s === null || s === undefined || !Number.isFinite(s)) return "—";
  return s < 90 ? `${Math.round(s)} s` : `${round1(s / 60)} min`;
}

/** A 0..1 ratio as a whole percent. */
export function fmtRatio(r: number | null | undefined): string {
  if (r === null || r === undefined || !Number.isFinite(r)) return "—";
  return `${Math.round(r * 100)} %`;
}

/** A number with at most one decimal (deploys a week). */
export function fmtNum(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  return String(round1(v));
}

/** The headline value of a DORA key, formatted. */
export function doraValue(app: DeliveryApp, key: MetricKey): string {
  const d = app.dora;
  switch (key) {
    case "deployment_frequency": return fmtNum(d.deployment_frequency.value);
    case "lead_time": return fmtHours(d.lead_time.p50_h);
    case "change_failure_rate": return fmtRatio(d.change_failure_rate.value);
    case "recovery_time": return fmtHours(d.recovery_time.p50_h);
    case "rework_rate": return fmtRatio(d.rework_rate.value);
    case "ci_p95": return fmtSeconds(app.ci.slowest?.p95_s);
    default: return app.ci.main.state;
  }
}

/** The sample size shown next to a metric. */
export function doraN(app: DeliveryApp, key: MetricKey): number {
  const d = app.dora;
  switch (key) {
    case "deployment_frequency": return d.deployment_frequency.attempts;
    case "lead_time": return d.lead_time.n;
    case "change_failure_rate": return d.change_failure_rate.n;
    case "recovery_time": return d.recovery_time.n;
    case "rework_rate": return d.rework_rate.n;
    case "ci_p95": return app.ci.slowest?.n ?? 0;
    default: return app.ci.main.n;
  }
}

/** The 12-week series of a DORA key for a sparkline (null where the week has no value). */
export function sparkSeries(app: DeliveryApp, key: MetricKey): { week: string; v: number | null }[] {
  const pick: Record<string, (w: WeekRow) => number | null> = {
    deployment_frequency: (w) => w.deploys,
    lead_time: (w) => w.lead_time_p50_h,
    change_failure_rate: (w) => (w.cfr === null ? null : w.cfr * 100),
    recovery_time: (w) => w.recovery_p50_h,
    rework_rate: (w) => (w.rework_rate === null ? null : w.rework_rate * 100),
    ci_p95: (w) => (w.ci_p95_s === null ? null : w.ci_p95_s / 60),
    main: (w) => (w.main_failure_rate === null ? null : w.main_failure_rate * 100),
  };
  const f = pick[key];
  return (app.trend?.weeks ?? []).map((w) => ({ week: w.week, v: w.complete === false ? null : f(w) }));
}

/** Rows of the stacked lead-time chart: the median of each segment, then the slowest changes one per row. */
export function segmentRows(app: DeliveryApp, medianLabel: string): (Partial<Record<SegmentKey, number>> & {
  label: string; url: string | null })[] {
  const median: Partial<Record<SegmentKey, number>> & { label: string; url: string | null } = {
    label: medianLabel, url: null };
  for (const s of app.lead_time_segments ?? []) if (s.p50_h !== null) median[s.segment] = s.p50_h;
  const changes = (app.changes?.slowest ?? []).map((c) => ({ label: `#${c.number}`, url: c.url, ...c.segments }));
  return [median, ...changes];
}

/** The segment that takes the longest median time (where to look first); null without data. */
export function slowestSegment(app: DeliveryApp): SegmentKey | null {
  const have = (app.lead_time_segments ?? []).filter((s) => s.p50_h !== null && s.n > 0);
  if (!have.length) return null;
  return have.reduce((a, b) => ((b.p50_h ?? 0) > (a.p50_h ?? 0) ? b : a)).segment;
}

/** The worst colour among the five DORA keys (the app card's border). */
export function worstStatus(app: DeliveryApp): MetricStatus {
  const order: MetricStatus[] = ["red", "amber", "green", "few", "none"];
  const seen = [...DORA_KEYS, "main" as MetricKey].map((k) => app.status?.[k] ?? "none");
  return order.find((s) => seen.includes(s)) ?? "none";
}

export function shortSha(sha: string | null | undefined): string {
  return (sha ?? "").slice(0, 8) || "—";
}

export function repoUrl(repo: string): string {
  return `${ROOT_REPO_URL}/${encodeURIComponent(repo)}`;
}

export function commitUrl(repo: string, sha: string): string {
  return `${repoUrl(repo)}/commit/${encodeURIComponent(sha)}`;
}

export function prUrl(repo: string, n: number): string {
  return `${repoUrl(repo)}/pull/${n}`;
}

/** Only links to github.com are rendered as links (the summary is data, not trusted markup). */
export function safeGithubUrl(url: string | null | undefined): string | null {
  return typeof url === "string" && url.startsWith("https://github.com/") ? url : null;
}

/** Hours since an ISO time, or null. */
export function hoursSince(iso: string | null | undefined, now: number = Date.now()): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isFinite(t) ? Math.max(0, (now - t) / 3_600_000) : null;
}

export function docHref(path: string): string {
  return `/admin/docs?${new URLSearchParams({ path }).toString()}`;
}
