// lib/reports.ts — AA-1003: types and pure helpers of the Reports page (/admin/reports).
// Backend: api/routers/admin_reports.py (GET /admin/reports, GET /admin/reports/{id}/runs, GET /admin/reports/runs/{id},
// POST /admin/reports/{id}/run). Registry text (names, cadence, channel, sections, KPI definitions) comes from the
// root report registry; it may carry internal issue references, so the page shows it through stripIssueIds().

import type { Tone } from "../app/_kit/core/primitives";

export type DueState = "ok" | "due" | "overdue" | "manual" | "not_automated";
export type RunStatus = "queued" | "running" | "succeeded" | "failed" | "draft" | "approved" | "posted";

export interface RunSummary {
  id: string;
  status: RunStatus;
  trigger: string;
  period_start: string | null;
  period_end: string | null;
  finished_at: string | null;
  created_at: string | null;
  summary: string | null;
  error: string | null;
}

export interface ReportRow {
  id: string;
  family: string;
  name_en: string;
  name_vi: string;
  audience: string;
  owner_role: string;
  cadence: string;
  schedule: string;
  channel: string;
  automation: string;
  status: string;
  priority: string;
  template: string | null;
  generator: string | null;
  runnable: boolean;
  kpis: string[];
  last_run: RunSummary | null;
  covered_to: string | null;
  failed_7d: number;
  due: { state: DueState; expected_period_end: string | null };
}

export interface ReportList {
  registry: { version: number; imported_from: string; reports: number; kpis: number; snapshot_kpis: string[] };
  generated_at: string;
  snapshot_day: string;
  latest_kpi_day: string | null;
  states: Partial<Record<DueState, number>>;
  reports: ReportRow[];
}

export interface KpiDetail {
  id: string;
  name: string;
  definition: string;
  unit: string;
  threshold: string;
  sources: string[];
  snapshotted: boolean;
}

export interface Run extends RunSummary {
  report_id: string;
  triggered_by: string | null;
  job_id: string | null;
  started_at: string | null;
  s3_key: string | null;
}

export interface RunDetail extends Run {
  output: unknown;
  markdown: string | null;
}

export interface ReportRuns {
  report: Omit<ReportRow, "last_run" | "failed_7d"> & {
    decision: string;
    sections: string[];
    kpi_details: KpiDetail[];
    covered_to: string | null;
  };
  runs: Run[];
  total: number;
  page: number;
  page_size: number;
}

export interface ReportFilters {
  family: string;
  priority: string;
  generatedOnly: boolean;
  q: string;
}

export const EMPTY_FILTERS: ReportFilters = { family: "", priority: "", generatedOnly: false, q: "" };
export const RUNS_PAGE_SIZE = 10;
export const PRIORITIES = ["P0", "P1", "P2", "P3"] as const;

export const LIST_PATH = "/api/admin/reports";

export function runsPath(reportId: string, page: number): string {
  return `/api/admin/reports/${encodeURIComponent(reportId)}/runs?page=${page}&page_size=${RUNS_PAGE_SIZE}`;
}

export function runPath(runId: string): string {
  return `/api/admin/reports/runs/${encodeURIComponent(runId)}`;
}

export function generatePath(reportId: string): string {
  return `/api/admin/reports/${encodeURIComponent(reportId)}/run`;
}

/** The registry is small and returned whole (63 reports), so the page filters the full list. */
export function filterReports(rows: ReportRow[], f: ReportFilters): ReportRow[] {
  const q = f.q.trim().toLowerCase();
  return rows.filter((r) =>
    (!f.family || r.family === f.family) &&
    (!f.priority || r.priority === f.priority) &&
    (!f.generatedOnly || r.runnable) &&
    (!q || `${r.id} ${r.name_en} ${r.name_vi} ${r.owner_role}`.toLowerCase().includes(q)));
}

export function families(rows: ReportRow[]): string[] {
  return Array.from(new Set(rows.map((r) => r.family))).sort();
}

export const DUE_TONE: Record<DueState, Tone> = {
  ok: "success", due: "info", overdue: "danger", manual: "neutral", not_automated: "neutral",
};

export const RUN_TONE: Record<RunStatus, Tone> = {
  queued: "neutral", running: "info", succeeded: "success", failed: "danger", draft: "warning", approved: "success",
  posted: "success",
};

export const PRIORITY_TONE: Record<string, Tone> = { P0: "danger", P1: "warning", P2: "info", P3: "neutral" };

/** The run to open first in the drawer: the newest finished one with output, else the newest. */
export function defaultRun(runs: Run[]): Run | null {
  return runs.find((r) => r.status === "succeeded") ?? runs[0] ?? null;
}

/** A report period as one day or a range. */
export function periodText(start: string | null, end: string | null): string {
  if (!start && !end) return "";
  if (!end || start === end) return start ?? "";
  return `${start} – ${end}`;
}
