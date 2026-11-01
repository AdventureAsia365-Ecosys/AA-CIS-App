// lib/llmAvailability.ts — AA-900: the LLM availability card (AI providers › Routing).
// Shape of GET /api/admin/llm-availability (backend api/services/llm_availability.py) and the pure helpers the card
// uses. The status is decided by the API from real outcomes (no probe call); this file only presents it.

export type AvailabilityStatus = "ok" | "degraded" | "blocked" | "unknown";
export type FailureClass = "access_denied" | "quota_exhausted" | "throttled";

export interface ProviderRow {
  provider: string;
  /** account key (never printed: the card names it through lib/awsAccounts accountName) */
  account: string | null;
  embed: boolean;
  calls: number;
  ok_calls: number;
  last_call_at: string | null;
  last_ok_at: string | null;
}

export interface FailureRow {
  error_class: FailureClass;
  jobs: number;
  last_at: string | null;
  last_kind?: string;
  last_job_id?: string;
  last_status?: string;
  last_error?: string;
}

export interface LlmAvailability {
  status: AvailabilityStatus;
  window_hours: number;
  generated_at: string;
  last_ok_at: string | null;
  cause: { error_class: FailureClass; at: string | null; kind?: string; job_id?: string; error?: string } | null;
  providers: ProviderRow[];
  failures: FailureRow[];
  /** AA-1002 S4 / AA-1018: per connection (absent on an older API). */
  connections?: { connection_key: string; enabled?: boolean; status?: string; failure_domain?: string | null }[];
  /** AA-1018: the failure-domain rule, report only (absent on an older API). */
  invariant?: { ready?: boolean; ok?: boolean; stages_checked?: number; breaches?: number;
    /** AA-642 A1: stages that pass through a declared degrade mode (not counted in breaches). */
    declared_degrade?: number; stages?: unknown[] };
}

export type StatusTone = "success" | "warning" | "danger" | "neutral";

export const STATUS_TONE: Record<AvailabilityStatus, StatusTone> = {
  ok: "success",
  degraded: "warning",
  blocked: "danger",
  unknown: "neutral",
};

/** The badge tone of a status; an unexpected value reads as unknown. */
export function statusTone(status: string | null | undefined): StatusTone {
  return STATUS_TONE[(status ?? "unknown") as AvailabilityStatus] ?? "neutral";
}

/** Text routes first (they decide the status), embeddings after; inside each, the most recent call first. */
export function sortProviders(rows: readonly ProviderRow[]): ProviderRow[] {
  const at = (r: ProviderRow) => (r.last_call_at ? Date.parse(r.last_call_at) : 0);
  return [...rows].sort((a, b) => Number(a.embed) - Number(b.embed) || at(b) - at(a));
}

/** Jobs page link that finds one job (its search matches an id prefix). */
export function jobLink(jobId: string | null | undefined): string | null {
  return jobId ? `/admin/jobs?q=${encodeURIComponent(jobId)}` : null;
}
