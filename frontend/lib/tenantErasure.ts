// lib/tenantErasure.ts — the tenant offboarding erasure saga (types + pure helpers).
//
// Mirrors the backend bodies of /admin/job-runner/erasure/requests (AA-CIS-App api/routers/admin_tenant_erasure.py,
// services/tenant_erasure/saga.py). Ids, phases, hashes, counts and staff actor ids only. Pure (no React).

export const ERASURE_REQUESTS_API = "/api/admin/job-runner/erasure/requests";

export const APPS = ["cis", "booking", "tripplanner", "crm"] as const;
export type App = (typeof APPS)[number];
export const MODES = ["dry_run", "apply", "verify"] as const;
export type StepMode = (typeof MODES)[number];

export type Phase = "dry_run" | "awaiting_approval" | "apply" | "verify" | "done" | "cancelled" | "failed";

/** Phases in which the saga job has work to do ("Run now" is offered). */
export const WORKING_PHASES: readonly Phase[] = ["dry_run", "apply", "verify"];
/** Phases in which a new DRY RUN round may start (the backend also refuses once the CIS apply started). */
export const NEW_ROUND_PHASES: readonly Phase[] = ["dry_run", "awaiting_approval", "apply"];

export interface SagaBlocker { app: string; kind: string; detail: string }

/** The newest report of one app for one round and mode (tenant.erasure.v1 ErasureReport). */
export interface SagaStep {
  request_id: string;
  round: number;
  app: App;
  mode: StepMode;
  /** ok | blocked | refused | failed, plus pending (a CIS job running) and app_not_ready (no report view yet). */
  status: string;
  report_id: string | null;
  error_code: string | null;
  plan_hash: string | null;
  approved_plan_hash: string | null;
  tables: { table: string; action: string; rows: number; digest: string | null }[];
  blockers: { code: string; count: number }[];
  census: { pii_columns_checked: number; pii_rows_found: number } | null;
  job_id: string | null;
  reported_at: string | null;
  updated_at: string;
}

export interface SagaRequest {
  request_id: string;
  tenant_id: string;
  tenant_name?: string | null;
  mode: "live" | "drill";
  phase: Phase;
  round: number;
  opened_at: string;
  is_pilot: boolean;
  is_synthetic: boolean;
  /** The apps active in the current round (stage 1: CIS, Booking, TripPlanner; stage 2: the CRM). */
  round_apps: App[];
  /** The apps whose erasure is verified. */
  done_apps: App[];
  not_before_cis: string;
  not_before_booking: string;
  not_before_tripplanner: string;
  not_before_crm: string;
  next_due_at?: string | null;
  combined_fingerprint: string | null;
  fingerprint_at: string | null;
  approved_fingerprint: string | null;
  approved_by: string | null;
  approved_at: string | null;
  approval_expires_at: string | null;
  snapshot_id: string | null;
  blockers: SagaBlocker[];
  receipt: Record<string, unknown> | null;
  created_by: string;
  created_at: string;
  updated_at: string;
  cancelled_at: string | null;
  done_at: string | null;
  steps?: SagaStep[];
  job?: { id: string; status: string; run_after: string | null } | null;
}

export interface SagaSettings {
  apply_enabled: boolean;
  apply_switch: string;
  approval_valid_hours: number;
  window_days: Record<App, number>;
  environment: string | null;
}

export interface SagaList { requests: SagaRequest[]; settings: SagaSettings }
export interface SagaDetail { request: SagaRequest; settings: SagaSettings }

/** The contract's step status of one app: done, failed, active (in the current round) or waiting. */
export function stepStatus(req: SagaRequest, app: App): "done" | "failed" | "active" | "waiting" {
  if (req.done_apps.includes(app)) return "done";
  if (!req.round_apps.includes(app)) return "waiting";
  const stopped = (req.steps ?? []).some((s) => s.round === req.round && s.app === app
    && ["blocked", "refused", "failed"].includes(s.status));
  return stopped ? "failed" : "active";
}

/** Badge tone of a phase. */
export function phaseTone(phase: Phase): "neutral" | "info" | "warning" | "success" | "danger" {
  if (phase === "done") return "success";
  if (phase === "failed") return "danger";
  if (phase === "awaiting_approval") return "warning";
  if (phase === "cancelled") return "neutral";
  return "info";
}

/** The step of (app, mode) in the request's current round, or undefined. */
export function stepOf(req: SagaRequest, app: App, mode: StepMode): SagaStep | undefined {
  return (req.steps ?? []).find((s) => s.round === req.round && s.app === app && s.mode === mode);
}

export function notBefore(req: SagaRequest, app: App): string {
  return req[`not_before_${app}` as const];
}

/** Why the approval form is closed (empty = open). Mirrors the backend refusals the page can know in advance. */
export function approvalBlockers(req: SagaRequest, settings: SagaSettings | undefined, nowMs: number,
  words: { notAwaiting: string; switchOff: string; tooOld: (h: number) => string }): string[] {
  const out: string[] = [];
  if (req.phase !== "awaiting_approval" || !req.combined_fingerprint) out.push(words.notAwaiting);
  if (!settings?.apply_enabled) out.push(words.switchOff);
  // The validity comes from the settings registry (cis.erasure.saga_approval_valid_hours); no local default.
  const hours = settings?.approval_valid_hours;
  if (hours !== undefined && req.fingerprint_at && nowMs - Date.parse(req.fingerprint_at) > hours * 3_600_000) {
    out.push(words.tooOld(hours));
  }
  return out;
}

/** The typed fingerprint prefix must match the request's combined fingerprint. */
export const FINGERPRINT_PREFIX = 8;
export function prefixMatches(fingerprint: string | null, typed: string): boolean {
  return !!fingerprint && typed.trim().toLowerCase() === fingerprint.slice(0, FINGERPRINT_PREFIX);
}

export function shortHash(h: string | null | undefined): string {
  return h ? h.slice(0, 8) : "—";
}
