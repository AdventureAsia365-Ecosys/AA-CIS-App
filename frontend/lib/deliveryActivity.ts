// frontend/lib/deliveryActivity.ts
// AA-1125: types and pure helpers of the Merge queue and Git map tabs of Platform › Delivery
// (app/admin/platform/delivery). The sections are built by the root repo workflow "Delivery metrics"
// (tools/delivery/delivery_metrics.py, `queue` and `git` per app) and served by the CIS API
// (GET /admin/platform/delivery?section=queue|git) with the merge-queue colours from the settings
// cis.delivery.queue_* (CIS 445). Definitions: root docs/guides/delivery-metrics.md.

import type { MetricStatus } from "./deliveryMetrics";
import type { HistBin, QueueWeek, RangeMeta } from "./deliveryRange";

export const QUEUE_PATH = "/api/admin/platform/delivery?section=queue";
export const GIT_PATH = "/api/admin/platform/delivery?section=git";

export type QueueMetric = "queue_wait" | "ejection_rate" | "flaky_rate";
export const QUEUE_METRICS: QueueMetric[] = ["queue_wait", "ejection_rate", "flaky_rate"];
export type QueueOutcome = "merged" | "ejected" | "removed" | "queued";
export const QUEUE_OUTCOMES: Exclude<QueueOutcome, "queued">[] = ["merged", "ejected", "removed"];
export type CommitKind = "agent" | "ci_bot" | "release_please" | "dependabot" | "bot" | "human";
export const COMMIT_KINDS: CommitKind[] = ["agent", "ci_bot", "release_please", "dependabot", "bot", "human"];

export interface QueueEntry {
  pr: number; title: string | null; url: string; enqueued_at: string | null; left_at: string | null;
  outcome: QueueOutcome; reason: string | null; failed_workflows: string[]; wait_h: number | null; attempt: number;
  attempts: number; runs: number; rebuilds: number; reruns: number; minutes: number;
  flaky: "rerun_green" | "reentry_green" | null; new_commit: boolean; run_url: string | null;
}

export interface QueueReason {
  reason: string; n: number; workflows: Record<string, number>; last_pr: number | null; last_url: string | null;
  last_at: string | null;
}

export interface QueueWorkflow { name: string; runs: number; failures: number; minutes: number; reruns: number }

export interface QueueSection {
  window: { start: string; end: string };
  complete_since: string | null;
  entries: number; prs: number; merged: number; ejected: number; removed: number; in_queue: number;
  wait: { p50_h: number | null; p95_h: number | null; n: number };
  to_merge: { p50_h: number | null; p95_h: number | null; n: number };
  ejection_rate: { value: number | null; n: number; ejected: number };
  flaky: { value: number | null; n: number; flaky: number; rerun_green: number; reentry_green: number };
  reasons: QueueReason[];
  ci: { runs: number; failures: number; failure_rate: number | null; p50_s: number | null; p95_s: number | null;
    reruns: number; minutes: number; workflows: QueueWorkflow[] };
  throughput: { date: string; merged: number; ejected: number; removed: number }[];
  recent: QueueEntry[];
  flaky_entries: QueueEntry[];
  /** AA-1135: collectors from 18/11/2026 on */
  wait_histogram?: HistBin[];
  weekly?: QueueWeek[];
}

export interface AppHead {
  id: string; name: string; repo: string; environment: string;
  window: { start: string; end: string; days: number } | null; complete_since: string | null;
}

export interface QueueApp extends AppHead { queue: QueueSection; queue_status: Record<QueueMetric, MetricStatus> }

export interface Band { green: number; amber: number; higher_is_better: boolean; keys: string[] }

export interface QueueResp {
  section: "queue"; generated_at: string | null; apps: QueueApp[];
  thresholds: Record<QueueMetric, Band> & { min_sample: number };
  range?: RangeMeta;
}

export interface ChurnRow {
  path: string; additions: number; deletions: number; churn: number; commits: number; files?: number;
  by_kind?: Partial<Record<CommitKind, number>>; last_at?: string | null; children?: ChurnRow[];
}

export interface GitWindow {
  start: string; end: string; commits: number; files_read: number; additions: number; deletions: number;
  by_kind: Partial<Record<CommitKind, number>>; merges: number; merges_by_prefix: Record<string, number>;
  dirs: ChurnRow[]; files: ChurnRow[];
}

export interface GitNode {
  sha: string; parents: string[]; title: string | null; pr: number | null; url: string; pr_url: string | null;
  kind: CommitKind | null; at: string | null; additions: number | null; deletions: number | null;
  files: number | null;
}

export interface GitSection {
  branch: string; days: number; complete_since: string | null; commits: number; files_coverage: number | null;
  daily: { date: string; n: number }[];
  windows: Record<string, GitWindow>;
  graph: GitNode[];
  /** AA-1135: the window of the chosen range (preset or custom) */
  range?: GitWindow & { complete_since: string | null; files_coverage: number | null;
    daily: { date: string; n: number }[] };
}

export interface GitApp extends AppHead { git: GitSection }

export interface GitResp { section: "git"; generated_at: string | null; apps: GitApp[]; range?: RangeMeta }

/** The value of a merge-queue metric as shown (wait in hours or minutes, rates as whole percents). */
export function queueValue(q: QueueSection, m: QueueMetric): number | null {
  if (m === "queue_wait") return q.wait?.p95_h ?? null;
  if (m === "ejection_rate") return q.ejection_rate?.value ?? null;
  return q.flaky?.value ?? null;
}

/** The sample size shown next to a merge-queue metric. */
export function queueN(q: QueueSection, m: QueueMetric): number {
  if (m === "queue_wait") return q.wait?.n ?? 0;
  if (m === "ejection_rate") return q.ejection_rate?.n ?? 0;
  return q.flaky?.n ?? 0;
}

/** The worst colour among an app's merge-queue metrics (its card border). */
export function worstQueueStatus(s: Partial<Record<QueueMetric, MetricStatus>> | undefined): MetricStatus {
  const order: MetricStatus[] = ["red", "amber", "green", "few", "none"];
  const seen = QUEUE_METRICS.map((k) => s?.[k] ?? "none");
  return order.find((x) => seen.includes(x)) ?? "none";
}

export function outcomeTone(o: QueueOutcome): "success" | "danger" | "neutral" | "info" {
  return o === "merged" ? "success" : o === "ejected" ? "danger" : o === "queued" ? "info" : "neutral";
}

const DAY_MS = 86_400_000;

function utcDay(iso: string): number {
  return Date.parse(`${iso}T00:00:00Z`);
}

function isoDay(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10);
}

export interface HeatCell { date: string; n: number; level: number }

/** Heat level 0..4 of a day's commit count against the busiest day (0 = none). */
export function heatLevel(n: number, max: number): number {
  if (!n || n <= 0 || !max) return 0;
  return Math.min(4, Math.max(1, Math.ceil((n / max) * 4)));
}

/** The calendar heatmap: one column per ISO week (Monday first), seven rows; days outside the series are null. */
export function calendarWeeks(daily: { date: string; n: number }[]): (HeatCell | null)[][] {
  const days = (daily ?? []).filter((d) => /^\d{4}-\d{2}-\d{2}$/.test(d.date));
  if (!days.length) return [];
  const byDay = new Map(days.map((d) => [d.date, d.n]));
  const max = Math.max(0, ...days.map((d) => d.n));
  const first = utcDay(days[0].date);
  const last = utcDay(days[days.length - 1].date);
  const weekday = (ms: number) => (new Date(ms).getUTCDay() + 6) % 7; // Monday 0
  const start = first - weekday(first) * DAY_MS;
  const weeks: (HeatCell | null)[][] = [];
  for (let w = start; w <= last; w += 7 * DAY_MS) {
    const col: (HeatCell | null)[] = [];
    for (let i = 0; i < 7; i++) {
      const ms = w + i * DAY_MS;
      const key = isoDay(ms);
      col.push(ms < first || ms > last || !byDay.has(key) ? null : { date: key, n: byDay.get(key) ?? 0,
        level: heatLevel(byDay.get(key) ?? 0, max) });
    }
    weeks.push(col);
  }
  return weeks;
}

/** Commits per ISO week (the Monday of each week), oldest first. */
export function weeklyCommits(daily: { date: string; n: number }[]): { week: string; n: number }[] {
  const out = new Map<string, number>();
  for (const d of daily ?? []) {
    const ms = utcDay(d.date);
    if (!Number.isFinite(ms)) continue;
    const monday = isoDay(ms - ((new Date(ms).getUTCDay() + 6) % 7) * DAY_MS);
    out.set(monday, (out.get(monday) ?? 0) + (d.n || 0));
  }
  return [...out.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([week, n]) => ({ week, n }));
}

/** Each author kind's count and share of a window's commits, in COMMIT_KINDS order, kinds with no commit left out. */
export function kindShares(byKind: Partial<Record<CommitKind, number>> | undefined): {
  kind: CommitKind; n: number; share: number }[] {
  const total = COMMIT_KINDS.reduce((s, k) => s + (byKind?.[k] ?? 0), 0);
  return COMMIT_KINDS.filter((k) => (byKind?.[k] ?? 0) > 0)
    .map((k) => ({ kind: k, n: byKind?.[k] ?? 0, share: total ? (byKind?.[k] ?? 0) / total : 0 }));
}

/** AA-1135: the git window shown — the chosen range when the API served one, else the summary's fixed window. */
export function gitWindow(g: GitSection | undefined, windowKey: string): GitWindow | undefined {
  return g?.range ?? g?.windows?.[windowKey];
}

/** One chart row per app: commits of each author kind in a window. */
export function kindRows(apps: GitApp[], windowKey: string): ({ name: string } & Record<CommitKind, number>)[] {
  return apps.map((a) => {
    const by = gitWindow(a.git, windowKey)?.by_kind ?? {};
    const row = { name: a.name } as { name: string } & Record<CommitKind, number>;
    for (const k of COMMIT_KINDS) row[k] = by[k] ?? 0;
    return row;
  });
}

/** The top-level directory picked for a drill-down, or null when the path is not one of the window's dirs. */
export function pickDir(win: GitWindow | undefined, path: string | null): ChurnRow | null {
  if (!win || !path) return null;
  return win.dirs.find((d) => d.path === path) ?? null;
}

/** Which entry of its pull request this is ("2/3"), or "1" for a single entry. */
export function attemptLabel(e: QueueEntry): string {
  return e.attempts > 1 ? `${e.attempt}/${e.attempts}` : "1";
}

/** Window keys of the git section ("d30", "d90") in day order. */
export function windowKeys(g: GitSection | undefined): string[] {
  return Object.keys(g?.windows ?? {}).sort((a, b) => Number(a.slice(1)) - Number(b.slice(1)));
}

/** Lines changed as "+a / −d". */
export function fmtLines(add: number | null | undefined, del: number | null | undefined): string {
  if (add === null || add === undefined || del === null || del === undefined) return "—";
  return `+${add} / −${del}`;
}
