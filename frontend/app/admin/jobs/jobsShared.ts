// app/admin/jobs/jobsShared.ts — types and small helpers shared by the Jobs page, its drawer and the
// worker health panel (AA-650 / AA-687). API: /admin/job-runner/* via /api/admin proxy.

import { formatDateTime } from "../../_kit";

export interface Job {
  id: string;
  kind: string;
  status: string;
  attempt: number;
  max_attempts: number;
  payload: Record<string, unknown> | null;
  progress: Record<string, unknown> | null;
  result: Record<string, unknown> | null;
  cost_usd: number;
  llm_cost_usd?: number;
  error: string | null;
  cancel_requested: boolean;
  created_by: string | null;
  locked_by?: string | null;
  locked_until?: string | null;
  run_after: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  links?: JobLinks | null;
}
export interface JobLinks {
  tour_versions: { version_id: string; published_tour_id: string; tenant_id: string; version_number: number; status: string }[];
  content_pieces: { piece_id: string; tenant_id: string; status: string }[];
  tour_id?: string | null;
  version_id?: string | null;
}
export interface Count { kind: string; status: string; n: number; cost_usd: number }
export interface Kind { kind: string; concurrency: number; max_attempts: number; expected_seconds?: number }

export interface Worker {
  worker_id: string;
  host: string;
  started_at: string;
  last_seen_at: string;
  stopped_at: string | null;
  max_parallel: number;
  caps: Record<string, number> | null;
  task_revision: number | null;
  running_jobs: number;
  reaped_requeued: number;
  reaped_failed: number;
  last_reap_at: string | null;
  last_reaped: { requeued?: string[]; failed?: string[] } | null;
  alive: boolean;
}
export interface WorkerHealthResp {
  workers: Worker[];
  running: { worker_id: string | null; kind: string; n: number; oldest_started_at: string | null }[];
  queued: { kind: string; n: number; ready: number; oldest_created_at: string | null }[];
  alive_seconds: number;
}

// The six job-runner statuses (queue.TERMINAL + queued/running). The kit StatusBadge owns the
// status→tone mapping now (AA-721), so the page no longer keeps a colour map here.
export const STATUSES = ["queued", "running", "succeeded", "failed", "stopped_budget", "cancelled"] as const;

export function fmtTime(iso: string | null | undefined): string {
  return formatDateTime(iso);
}

export function fmtSeconds(s: number): string {
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

export function ago(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "—";
  return `${fmtSeconds((now - new Date(iso).getTime()) / 1000)} ago`;
}

export function runSeconds(job: Job, now = Date.now()): number | null {
  if (!job.started_at) return null;
  const end = job.finished_at ? new Date(job.finished_at).getTime() : now;
  return Math.max(0, (end - new Date(job.started_at).getTime()) / 1000);
}

export function releases(job: Job): number {
  const r = job.progress?.releases;
  return typeof r === "number" ? r : 0;
}

/** `progress.{step,done,total}` reported by long kinds (a3_atomize since AA-688). */
export function stepProgress(job: Job): { step: string; done: number; total: number } | null {
  const p = job.progress ?? {};
  if (typeof p.done === "number" && typeof p.total === "number" && p.total > 0) {
    return { step: typeof p.step === "string" ? p.step : "", done: p.done, total: p.total };
  }
  return null;
}

export function usd(v: number | null | undefined, digits = 4): string {
  return `$${(v ?? 0).toFixed(digits)}`;
}
