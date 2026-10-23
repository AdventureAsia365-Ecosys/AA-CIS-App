// lib/dataErasure.ts — AA-839: Jobs › Data erasure (types + pure helpers).
//
// Mirrors the backend bodies of GET /admin/job-runner/erasure, POST /admin/job-runner/erasure/run and the
// data_erasure job result (AA-CIS-App api/routers/admin_job_runner.py, services/jobs/erasure_job.py,
// services/erasure/plan.py). Pure (no React), so a test or a Playwright spec can import it.
//
// Privacy rule: a traveller is only ever sent as the sha256 hex of the trimmed, lowercased address (runbook
// docs/legal/dsar-erasure-runbook.md section 2 step 2). The address never leaves the browser and is never stored.

export const ERASURE_KIND = "data_erasure";
export const ERASURE_API = "/api/admin/job-runner/erasure";
export const JOBS_API = "/api/admin/job-runner/jobs";

/** First characters of the fingerprint the operator types to confirm an apply. */
export const FINGERPRINT_CONFIRM_CHARS = 8;

export type Subject = "tenant" | "traveller";

export interface ErasureSettings {
  kind: string;
  apply_enabled: boolean;
  tenant_retention_days: number;
  dry_run_max_age_hours: number;
  tenant_tables: { table: string; action: string; reason: string }[];
  traveller_scans: { table: string; column: string; action: string; reason: string }[];
  tenant_checklist: ChecklistItem[];
  traveller_checklist: ChecklistItem[];
}

export interface ChecklistItem {
  owner: string;
  item: string;
  how: string;
}

export interface PlanRow {
  table: string;
  action: string;
  rows: number;
  reason?: string;
  via?: string[];
  ids?: string[];
}

export interface Blocker {
  kind: string;
  table: string;
  rows: number | null;
  detail: string;
}

export interface S3Prefix {
  prefix: string;
  objects: number | null;
  error?: string;
}

export interface SecretRef {
  name: string;
  exists: boolean | null;
  error?: string;
}

export interface S3Ref {
  column: string;
  rows: number | null;
  outside_prefixes?: number;
  outside_keys?: string[];
  outside_dirs?: string[];
  error?: string;
}

/** The data_erasure job result: a DRY RUN, an apply, or the redacted shape (counts only). */
export interface ErasureResult {
  subject?: Subject;
  mode?: "dry_run" | "apply";
  fingerprint?: string;
  totals?: Record<string, number>;
  rows?: PlanRow[];
  blockers?: Blocker[];
  blocked?: boolean;
  checklist?: ChecklistItem[];
  redacted?: boolean;
  // tenant
  tenant?: string;
  tenant_state?: "offboarded" | "active" | "suspended" | null;
  offboarded_at?: string | null;
  checks_passed?: Blocker[];
  unregistered?: string[];
  missing_tables?: string[];
  delete_order?: string[];
  null_first?: string[];
  s3_refs?: S3Ref[];
  s3?: S3Prefix[];
  secrets?: SecretRef[];
  // traveller
  skipped_tables?: string[];
  manual?: PlanRow[];
  hash_stripped?: boolean;
  // DRY RUN only
  apply?: { enabled: boolean; tenant_retention_days: number | null; needs: string[] };
  used_by_apply?: string;
  // apply only
  applied?: PlanRow[];
  verified_left?: Record<string, number>;
  verified?: boolean;
}

export interface ErasureJob {
  id: string;
  kind: string;
  status: string;
  payload: Record<string, unknown> | null;
  progress: Record<string, unknown> | null;
  result: ErasureResult | null;
  error: string | null;
  created_by: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface JobsPage {
  jobs: ErasureJob[];
  total?: number;
}

export interface RunResponse {
  job_id: string;
  kind: string;
  subject: Subject;
  mode: "dry_run" | "apply";
}

export interface TenantOption {
  tenant_id: string;
  name: string;
  slug: string;
  status: string;
  cancelled_at: string | null;
}

const HEX64 = /^[0-9a-f]{64}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const PLATFORM_TENANT_ID = "00000000-0000-0000-0000-000000000001";
// RDS DB snapshot identifier: a letter first, then letters, digits and single hyphens; no trailing hyphen.
const SNAPSHOT_ID = /^[A-Za-z](?:[A-Za-z0-9]|-(?!-))*[A-Za-z0-9]$/;

export function isHex64(v: unknown): v is string {
  return typeof v === "string" && HEX64.test(v);
}

/** A tenant id the API accepts (uuid, never the AA internal tenant). */
export function tenantIdProblem(raw: string): string | null {
  const v = raw.trim().toLowerCase();
  if (!v) return "Pick a tenant or paste its id.";
  if (!UUID.test(v)) return "A tenant id is a uuid (8-4-4-4-12 hex characters).";
  if (v === PLATFORM_TENANT_ID) return "The AA internal tenant can never be erased.";
  return null;
}

/** Same normalisation as the runbook's python one-liner: strip, lowercase. */
export function normaliseEmail(raw: string): string {
  return raw.trim().toLowerCase();
}

export function emailLooksValid(raw: string): boolean {
  const v = normaliseEmail(raw);
  return /^[^\s@<>,;]+@[^\s@<>,;]+\.[^\s@<>,;]+$/.test(v);
}

/** sha256 hex of the normalised address, computed in the browser (Web Crypto). */
export async function emailSha256(raw: string): Promise<string> {
  const bytes = new TextEncoder().encode(normaliseEmail(raw));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

export function isTerminal(status: string | undefined | null): boolean {
  return !!status && status !== "queued" && status !== "running";
}

export function jobSubject(job: ErasureJob): Subject | null {
  const s = job.payload?.subject ?? job.result?.subject;
  return s === "tenant" || s === "traveller" ? s : null;
}

export function jobMode(job: ErasureJob): "dry_run" | "apply" {
  return job.payload?.mode === "apply" || job.result?.mode === "apply" ? "apply" : "dry_run";
}

export function jobTenant(job: ErasureJob): string | null {
  const t = job.payload?.tenant ?? job.result?.tenant;
  return typeof t === "string" ? t : null;
}

/** The traveller hash held by the job row, or null once the job stripped it (after an apply / a failure). */
export function jobHash(job: ErasureJob): string | null {
  const h = job.payload?.email_sha256;
  return isHex64(h) ? h : null;
}

export function isVerifyRun(job: ErasureJob): boolean {
  return job.payload?.verify === true;
}

/** Totals in a fixed, readable order: destructive first. */
const TOTAL_ORDER = ["delete", "cascade", "null_first", "redact", "detach", "merge", "manual", "keep"];

export function orderedTotals(totals: Record<string, number> | undefined): [string, number][] {
  const entries = Object.entries(totals ?? {});
  return entries.sort(([a], [b]) => {
    const ia = TOTAL_ORDER.indexOf(a);
    const ib = TOTAL_ORDER.indexOf(b);
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib) || a.localeCompare(b);
  });
}

export const ACTION_HELP: Record<string, string> = {
  delete: "rows deleted",
  cascade: "child rows deleted with their parent (found through foreign keys)",
  null_first: "a foreign key nulled first to break a cycle",
  redact: "the hash or address removed from the row; the row stays",
  detach: "row kept, tenant link and soft keys nulled",
  merge: "added into the platform (no tenant) row of the same day",
  keep: "kept (append-only audit trail)",
  manual: "a person handles it (not changed by the job)",
};

export type ActionTone = "danger" | "warning" | "info" | "purple" | "neutral" | "accent";

export function actionTone(action: string): ActionTone {
  const base = action.split(":", 1)[0];
  switch (base) {
    case "delete":
    case "cascade":
      return "danger";
    case "null_first":
    case "redact":
      return "warning";
    case "detach":
      return "info";
    case "merge":
      return "purple";
    case "manual":
      return "accent";
    default:
      return "neutral";
  }
}

/** "delete 12 · cascade 40 · detach 3" — the sentence the operator ticks to confirm. */
export function totalsSentence(totals: Record<string, number> | undefined): string {
  const parts = orderedTotals(totals).filter(([, n]) => n > 0).map(([k, n]) => `${k} ${n.toLocaleString("en-US")}`);
  return parts.length ? parts.join(" · ") : "no rows";
}

export function rowsTouched(totals: Record<string, number> | undefined): number {
  return orderedTotals(totals).filter(([k]) => k !== "keep").reduce((s, [, n]) => s + n, 0);
}

export function hoursSince(iso: string | null | undefined, now: number): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : (now - t) / 3_600_000;
}

export function snapshotIdProblem(raw: string): string | null {
  const v = raw.trim();
  if (!v) return "Type the id of the manual RDS snapshot you took for this erasure.";
  if (v.length > 255 || !SNAPSHOT_ID.test(v)) {
    return "An RDS snapshot id starts with a letter and has only letters, digits and single hyphens.";
  }
  return null;
}

export interface ApplyGate {
  /** Can the apply panel take input at all? */
  open: boolean;
  /** Why not (shown to the operator); empty when open. */
  reasons: string[];
  /** Soft warnings (the job re-checks them): e.g. the retention window. */
  warnings: string[];
}

/** Whether a DRY RUN may be approved and applied from the page. The job re-checks everything itself. */
export function applyGate(
  job: ErasureJob | null | undefined,
  settings: Pick<ErasureSettings, "apply_enabled" | "dry_run_max_age_hours" | "tenant_retention_days"> | null | undefined,
  now: number,
): ApplyGate {
  const reasons: string[] = [];
  const warnings: string[] = [];
  if (!job) return { open: false, reasons: ["Run a DRY RUN first."], warnings };
  const res = job.result;
  if (jobMode(job) !== "dry_run") reasons.push("This job is an apply, not a DRY RUN.");
  if (job.status !== "succeeded") reasons.push("The DRY RUN has not succeeded.");
  if (isVerifyRun(job)) reasons.push("This is a verification DRY RUN after an apply; it cannot be applied.");
  if (res?.redacted) reasons.push("Your role sees only the counts of this job.");
  if (res && !isHex64(res.fingerprint)) reasons.push("The DRY RUN has no fingerprint.");
  if (res?.blocked) reasons.push("The DRY RUN has blockers: resolve them and run a new DRY RUN.");
  if (res?.used_by_apply) reasons.push(`This DRY RUN was already used by apply job ${res.used_by_apply}; run a new one.`);
  if (jobSubject(job) === "traveller" && !jobHash(job)) {
    reasons.push("The job no longer holds the traveller hash; run a new DRY RUN.");
  }
  const age = hoursSince(job.finished_at, now);
  const maxAge = settings?.dry_run_max_age_hours ?? 24;
  if (age !== null && age > maxAge) reasons.push(`The DRY RUN is older than ${maxAge} h; run a new one and approve it.`);
  if (!settings?.apply_enabled) {
    reasons.push("Apply is off on this environment (ERASURE_APPLY_ENABLED is not true on the API and worker).");
  }
  if (jobSubject(job) === "tenant" && res) {
    if (res.tenant_state !== "offboarded") {
      reasons.push(`The tenant is ${res.tenant_state ?? "missing"}, not offboarded.`);
    } else if (settings) {
      const days = hoursSince(res.offboarded_at, now);
      if (days !== null && days / 24 < settings.tenant_retention_days) {
        warnings.push(
          `Offboarded ${Math.floor(days / 24)} day(s) ago; the retention window is ${settings.tenant_retention_days} days, so the job will refuse.`,
        );
      }
    }
  }
  return { open: reasons.length === 0, reasons, warnings };
}

export interface ApplyForm {
  fingerprintPrefix: string;
  snapshotId: string;
  approvedBy: string;
  confirmed: boolean;
}

/** What still stops the "Apply erasure" button, for a DRY RUN whose gate is open. */
export function applyFormProblems(fingerprint: string, form: ApplyForm): string[] {
  const out: string[] = [];
  const want = fingerprint.slice(0, FINGERPRINT_CONFIRM_CHARS).toLowerCase();
  if (form.fingerprintPrefix.trim().toLowerCase() !== want) {
    out.push(`Type the first ${FINGERPRINT_CONFIRM_CHARS} characters of the fingerprint.`);
  }
  const snap = snapshotIdProblem(form.snapshotId);
  if (snap) out.push(snap);
  if (!form.approvedBy.trim()) out.push("Type who approved this fingerprint.");
  else if (form.approvedBy.trim().length > 120) out.push("The approver name is at most 120 characters.");
  if (!form.confirmed) out.push("Tick the confirmation.");
  return out;
}

/** The POST body of an apply, built only from the approved DRY RUN (never from free input but the form). */
export function applyBody(job: ErasureJob, form: ApplyForm): Record<string, unknown> {
  const subject = jobSubject(job);
  const body: Record<string, unknown> = {
    subject,
    dry_run: false,
    dry_run_job_id: job.id,
    approved_fingerprint: job.result?.fingerprint,
    approved_by: form.approvedBy.trim(),
    snapshot_id: form.snapshotId.trim(),
  };
  if (subject === "tenant") body.tenant_id = jobTenant(job);
  else body.email_sha256 = jobHash(job);
  return body;
}

/** Suggested snapshot id (runbook naming), dated today in UTC. */
export function suggestedSnapshotId(subject: Subject, now: number): string {
  const d = new Date(now).toISOString().slice(0, 10).replace(/-/g, "");
  return subject === "tenant" ? `tenant-erase-${d}-pre` : `dsar-${d}-pre`;
}

/** A readable message from a failed BFF / API call (FastAPI `detail` string or validation list). */
export function detailMessage(status: number, detail: unknown): string {
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail)) {
    const first = detail[0] as { msg?: string; loc?: unknown[] } | undefined;
    if (first?.msg) return `The server refused the form: ${first.msg}`;
  }
  return `Request failed (${status})`;
}
