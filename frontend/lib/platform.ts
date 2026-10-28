// frontend/lib/platform.ts
// AA-884 (S241 GĐ7 / GĐ8 / GĐ10) — response types and small formatters for the Platform › Apps,
// Audit log and Cost per app pages. The shapes mirror the backend exactly:
//   GET /admin/platform/apps            api/services/platform_apps.py  collect_apps()
//   GET /admin/platform/audit           api/services/platform_audit.py list_audit()
//   GET /admin/platform/audit/verify    api/services/platform_audit.py verify_chain_cached()
//   GET /admin/platform/cost            api/services/platform_cost.py  cost_by_app()
//   POST /admin/cost-explorer/check     api/routers/admin_llm_ops.py   check_cost_explorer()
// Browser calls go through the same-origin BFF /api/admin/[...path] (lib/rbac.ts: "platform" is
// admin only; cost-explorer is open to staff at the BFF and re-checked by the backend, spend.*).

// ── Apps ──────────────────────────────────────────────────────────────────────────────────────

export type AppStatus = "ok" | "degraded" | "down" | "unknown";

/** S243 — the ONE wording of an app status, used by /admin/platform/apps and the /admin/overview tiles alike. */
export const APP_STATUS_LABEL: Record<AppStatus, { label: string; tone: "success" | "warning" | "danger" | "neutral" }> = {
  ok: { label: "Healthy", tone: "success" },
  degraded: { label: "Needs a look", tone: "warning" },
  down: { label: "Down", tone: "danger" },
  unknown: { label: "Unknown", tone: "neutral" },
};

/** api/services/app_health.py overall_status(): the status both pages show, and what it is based on. */
export interface AppHealthVerdict {
  status: AppStatus;
  reason: string;
  basis: string[];
}

/** "based on its health check and its traffic …" — one phrasing of `overall.basis` for both pages. */
// basis strings: api/services/app_health.py PROBE_BASIS / PROBE_NO_ANSWER_BASIS / TRAFFIC_BASIS.
export function basisText(basis: string[]): string {
  const noAnswer = basis.includes("Health check (did not answer)");
  const probe = basis.includes("Health check");
  const traffic = basis.some((b) => b.startsWith("Traffic"));
  if (noAnswer && traffic) return "its health check did not answer; its traffic of the last 24 hours is shown too";
  if (noAnswer) return "its health check did not answer (no traffic numbers right now)";
  if (probe && traffic) return "based on its health check and its traffic of the last 24 hours";
  if (traffic) return "based on its traffic of the last 24 hours (AWS CloudWatch); it has no health check";
  if (probe) return "based on its health check (no traffic numbers right now)";
  return "";
}

/** api/services/app_health.py traffic_summary(): CloudWatch traffic of the last 24 h (the overview's numbers). */
export interface AppTraffic {
  state: "ok" | "warn" | "alarm" | "unknown" | null;
  reason: string | null;
  has_data: boolean | null;
  requests: number | null;
  errors: number | null;
  error_rate_pct: number | null;
  throttles: number | null;
  p95_ms: number | null;
  alarms_total: number | null;
  alarms_firing: number;
  kind: "apigw" | "lambda" | null;
  resources: string[] | null;
  window_hours: number | null;
  as_of: string | null;
}

interface CardHealth {
  /** the app's own check (null: TripPlanner has none) */
  probe?: { status: AppStatus; reason: string } | null;
  traffic?: AppTraffic | null;
  /** the one status of the app (also on the Overview tile) */
  overall?: AppHealthVerdict;
}

export interface CisWorker {
  worker_id: string;
  git_sha: string | null;
  commit_url: string | null;
  task_revision: number | null;
  running_jobs: number | null;
  last_seen_at: string | null;
}

/** GET /admin/job-runner/deploy-gate (AA-993 G5): unsafe only while a rerun wave is open or a deploy-blocking
 * kind runs. `active` / `by_status` are the queued + running jobs a deploy releases and the new revision re-claims. */
export interface DeployGate {
  active: number;
  by_status: Record<string, number>;
  safe_to_deploy: boolean;
  exempt_kinds: string[];
  /** Absent on an API older than AA-993 G5. */
  open_waves?: number;
  blocking_kinds?: string[];
  blocking_running?: Record<string, number>;
  /** Queued rows of a deploy-blocking kind due within 15 min (they also block). */
  blocking_due?: Record<string, number>;
  blocked_by?: string[];
}

export interface CisCard extends CardHealth {
  id: "cis";
  label: string;
  repo_url: string;
  status: AppStatus;
  api: { git_sha: string; commit_url: string | null; task_revision: number | null };
  workers: CisWorker[] | null;
  /** true = every live worker runs the API's commit; null = nothing to compare (local / unreadable). */
  parity: boolean | null;
  deploy_gate: DeployGate | null;
  schema: { version: string | null; required: string | null; status: string | null; db: string | null };
  problems: string[];
}

export interface BookingCard extends CardHealth {
  id: "booking";
  label: string;
  repo_url: string;
  health_url: string;
  status: AppStatus;
  error?: string;
  git_sha?: string | null;
  commit_url?: string | null;
  health?: { http_status: number; status: string | null };
  ready?: { http_status: number; status: string | null; db: string | null; schema_version: string | null };
}

export interface TpSpendToday {
  cost_usd: number;
  calls: number;
  by_stage: { stage: string; calls: number; cost_usd: number }[];
  cap_usd: number;
  pct_of_cap: number | null;
  cap_label: string;
  level: "ok" | "warn" | "cap_reached";
  since: string;
  stages: string[];
}

export interface TripPlannerCard extends CardHealth {
  id: "tripplanner";
  label: string;
  repo_url: string;
  status: AppStatus;
  note?: string;
  error?: string;
  spend_today?: TpSpendToday | null;
}

/** AA-936 — the partner CRM, probed like Booking (/crm/health, /crm/health/ready). */
export interface CrmCard extends Omit<BookingCard, "id"> {
  id: "crm";
}

/** AA-936 — GET /api/admin/platform/apps/crm/jobs: the CRM's crm.job per kind (crm.ops.v1 /jobs/summary). */
export interface CrmJobKind {
  kind: string;
  queued: number;
  running: number;
  failed_24h: number;
  oldest_queued_age_s: number | null;
}

export interface CrmJobsResp {
  status: "ok" | "unavailable" | "refused" | "error";
  reason?: string | null;
  http_status?: number;
  generated_at?: string;
  kinds: CrmJobKind[];
}

export type AppCard = CisCard | BookingCard | TripPlannerCard | CrmCard;

export interface AppsResp {
  apps: AppCard[];
  generated_at: string;
  _cached: boolean;
}

// ── Audit ─────────────────────────────────────────────────────────────────────────────────────

export interface AuditRow {
  id: string;
  created_at: string | null;
  tenant_id: string | null;
  actor: string | null;
  actor_type: string | null;
  actor_name: string | null;
  actor_email: string | null;
  action: string | null;
  resource_type: string | null;
  resource_id: string | null;
  http_method: string | null;
  route: string | null;
  status: number | null;
  details: unknown;
  chain_seq: number | null;
  row_hash: string | null;
}

export interface AuditListResp {
  rows: AuditRow[];
  next_cursor: string | null;
  limit: number;
}

export interface AuditVerifyResp {
  checked: number;
  ok: boolean;
  first_bad_seq: number | null;
  problem: string | null;
  last_seq: number | null;
  from_seq: number | null;
  pre_chain_rows: number;
  unchained_after_genesis: number;
  unchained_after_genesis_ids: string[];
  chain_boundary: string | null;
  verified_at: string;
  _cached: boolean;
}

export interface AuditFilters {
  scope: "staff" | "all";
  actor: string;
  action: string;
  route: string;
  /** YYYY-MM-DD (UTC day, inclusive) or "". */
  since: string;
  /** YYYY-MM-DD (UTC day, inclusive) or "". */
  until: string;
}

export const EMPTY_AUDIT_FILTERS: AuditFilters = { scope: "staff", actor: "", action: "", route: "", since: "", until: "" };

/** The day after a YYYY-MM-DD date, as YYYY-MM-DD (UTC; the audit `until` bound is exclusive). */
export function nextDay(day: string): string {
  const d = new Date(`${day}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + 1);
  return d.toISOString().slice(0, 10);
}

/** Query string for GET /admin/platform/audit. Dates are whole UTC days: since 00:00Z, until the next day's 00:00Z. */
export function auditQuery(f: AuditFilters, cursor: string | null, limit = 100): string {
  const q = new URLSearchParams();
  q.set("scope", f.scope);
  q.set("limit", String(limit));
  if (f.actor.trim()) q.set("actor", f.actor.trim());
  if (f.action.trim()) q.set("action", f.action.trim());
  if (f.route.trim()) q.set("route", f.route.trim());
  if (f.since) q.set("since", `${f.since}T00:00:00Z`);
  if (f.until) q.set("until", `${nextDay(f.until)}T00:00:00Z`);
  if (cursor) q.set("cursor", cursor);
  return q.toString();
}

/** details.auth of a staff.request row written for a legacy password session (actor from an unverified header). */
export function isLegacyUnverified(details: unknown): boolean {
  return !!details && typeof details === "object" && (details as Record<string, unknown>).auth === "legacy_unverified";
}

// ── Cost ──────────────────────────────────────────────────────────────────────────────────────

export type CostRowKind = "app" | "llm_app" | "shared_tag" | "other_tag" | "untagged";

export interface CostService {
  service: string;
  amount_usd: number;
  is_bedrock: boolean;
}

export interface AiSource {
  provider: string | null;
  account: string | null;
  calls: number;
  cost_usd: number;
  billed_in_aws_acc2: boolean;
}

export interface CostRow {
  app: string;
  kind: CostRowKind;
  /** plain label from the backend cost module (the same on every page) */
  label: string;
  hint: string | null;
  aws_usd: number;
  aws_bedrock_usd: number;
  services: CostService[];
  aws_tags: string[];
  ai_usd: number;
  ai_in_aws_acc2_usd: number;
  /** ai_usd − ai_in_aws_acc2_usd: AI not already on the acc2 AWS bill */
  ai_outside_aws_usd: number;
  ai_calls: number;
  ai_by_source: AiSource[];
  total_usd: number;
}

export interface CostRefresh {
  attempted: boolean;
  ok: boolean | null;
  reason?: string;
  stale?: boolean;
  stale_reason?: string;
  skipped?: string;
  error?: string;
  row_count?: number;
  ce_requests?: number;
  est_cost_usd?: number;
}

export interface SatelliteLine {
  account_id: string | null;
  ce_bedrock_usd: number | null;
  llm_log_estimate_usd: number;
}

export interface CostResp {
  month: string;
  period_start: string;
  period_end_exclusive: string;
  aws: {
    account_id: string;
    has_data: boolean;
    total_usd: number | null;
    covered_from: string | null;
    covered_to: string | null;
    fetched_at: string | null;
    untagged_usd: number;
  };
  apps: CostRow[];
  totals: {
    aws_usd: number | null;
    ai_usd: number;
    ai_in_aws_acc2_usd: number;
    ai_outside_aws_usd: number;
    total_usd: number;
  };
  /** AA-998: acc3 is the only LLM satellite; `retired` is present only for a month that still holds spend of the
   * former fallback account (history, `account_id` null). */
  llm_satellites: { acc3?: SatelliteLine; retired?: SatelliteLine; ce_fetched_at?: string | null };
  /** AA-936: whether the CRM row's LLM spend (crm.ops.v1 /llm/usage, staff token) could be read. */
  crm_llm?: { status: string; reason?: string | null; http_status?: number | null };
  refresh: CostRefresh;
  sources: Record<string, string>;
}

export interface CeCheckResp {
  row_count: number;
  app_tag_row_count: number | null;
  errors: Record<string, string>;
  period_start: string;
  period_end_exclusive: string;
}

/** The current UTC month as YYYY-MM (the backend's month_window works in UTC days). */
export function currentUtcMonth(now: Date = new Date()): string {
  return now.toISOString().slice(0, 7);
}

/** The last day included in [start, endExclusive), as YYYY-MM-DD (the CE check takes an inclusive `end`). */
export function lastIncludedDay(endExclusive: string): string {
  const d = new Date(`${endExclusive}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() - 1);
  return d.toISOString().slice(0, 10);
}

// ── Formatters ────────────────────────────────────────────────────────────────────────────────

export function shortSha(sha: string | null | undefined): string {
  if (!sha) return "—";
  if (sha === "unknown") return "unknown";
  return sha.slice(0, 7);
}

/** USD with cents; sub-cent amounts keep 4 decimals so a few LLM calls do not show as $0.00. */
export function usd(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const abs = Math.abs(n);
  const digits = abs > 0 && abs < 0.01 ? 4 : 2;
  return `$${n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}
