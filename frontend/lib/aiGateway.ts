// lib/aiGateway.ts — AA-804 (UI part 1): the admin "AI providers" page (/admin/ai-gateway).
// Shapes of GET /api/admin/ai-gateway/providers and GET /api/admin/ai-gateway/trace?job_id= (backend
// api/services/ai_gateway.py) and the pure helpers the page uses. Every number comes from the API (database reads
// only, $0); this file only presents it. No account key or ID is written here: a Bedrock connection is named through
// lib/llmConnections connectionName + the account directory (useAwsAccounts).

export type ProviderStatus =
  | "ok" | "degraded" | "open" | "low" | "cap_reached" | "disabled" | "unknown";
export type ProviderGroup = "llm" | "decide" | "data";
export type Tone = "neutral" | "accent" | "success" | "warning" | "danger" | "info" | "purple";

export interface Spend {
  today_usd: number;
  month_usd: number;
  month_calls: number;
  last_call_at: string | null;
}

export interface DailyCap {
  budget_provider: string;
  per_day_usd: number;
  spent_today_usd: number;
  used_pct: number | null;
  hard_stop: boolean;
  alert_pct: number | null;
  state: "ok" | "alert" | "cap_reached";
}

export interface MonthlyBudget {
  monthly_budget_usd: number;
  month_spent_usd: number;
  used_pct: number | null;
  state: string;
  hard_stop: boolean;
}

export interface Balance {
  usd: number | null;
  /** true for Jev: recorded top-ups minus the logged spend (TypeSafe has no balance API) */
  estimated: boolean;
  threshold_usd: number;
  below_threshold: boolean;
  as_of: string | null;
  source: string;
}

export interface Credits {
  used_today: number;
  daily_limit: number;
  used_pct: number | null;
  window: string;
}

export interface ProviderHealth {
  // LLM connections
  last_ok_at?: string | null;
  last_error_at?: string | null;
  last_error_class?: string | null;
  open_models?: number;
  models?: number;
  failing_models?: number;
  // Jev (last 24 h of decision_log)
  window_hours?: number;
  calls?: number;
  errors?: number;
  error_pct?: number | null;
  mean_latency_ms?: number | null;
  last_at?: string | null;
}

export interface ProviderRow {
  key: string;
  group: ProviderGroup;
  provider: string;
  label: string;
  connection_key: string | null;
  account_id: string | null;
  enabled: boolean;
  data_scope: string | null;
  status: ProviderStatus;
  status_reason: string | null;
  health: ProviderHealth | null;
  spend: Spend | null;
  monthly_budget: MonthlyBudget | null;
  daily_cap: DailyCap | null;
  balance: Balance | null;
  credits: Credits | null;
  error: string | null;
  /** DataForSEO only: the month's calls that went to the live API (the rest were cache hits) */
  live_calls_month?: number;
}

export interface ProvidersResp {
  generated_at: string;
  day_start: string;
  month_start: string;
  providers: ProviderRow[];
  unattributed_llm: Spend | null;
  totals: { today_usd: number; month_usd: number };
  errors: { source: string; error: string }[];
}

export type TraceSource = "llm" | "jev" | "dfs" | "geocoder";

export interface TraceEvent {
  at: string;
  source: TraceSource;
  stage: string | null;
  what: string | null;
  model: string | null;
  provider: string | null;
  /** account key or vendor connection key (named through lib/awsAccounts accountName, never printed raw) */
  account: string | null;
  outcome: string | null;
  cost_usd: number | null;
  latency_ms: number | null;
  tokens_in: number | null;
  tokens_out: number | null;
  detail: Record<string, unknown>;
}

export interface TraceSourceSummary {
  events: number;
  shown: number;
  cost_usd: number;
  truncated: boolean;
  read: boolean;
  error: string | null;
}

export interface TraceResp {
  job: {
    id: string; kind: string; status: string; created_at: string | null; started_at: string | null;
    finished_at: string | null; cost_usd: number | null; tour_id: string | null; parent_job_id: string | null;
  };
  window: { from: string; to: string } | null;
  events: TraceEvent[];
  sources: Record<TraceSource, TraceSourceSummary>;
  totals: { events: number; cost_usd: number };
  limit_per_source: number;
  notes: string[];
}

export const PROVIDERS_API = "/api/admin/ai-gateway/providers";
export const TRACE_API = "/api/admin/ai-gateway/trace";
export const PAGE_PATH = "/admin/ai-gateway";
export const TRACE_SOURCES: readonly TraceSource[] = ["llm", "jev", "dfs", "geocoder"];

const STATUS_TONE: Record<ProviderStatus, Tone> = {
  ok: "success", degraded: "warning", open: "danger", low: "warning", cap_reached: "danger",
  disabled: "neutral", unknown: "neutral",
};

export function statusTone(status: string | null | undefined): Tone {
  return STATUS_TONE[(status ?? "unknown") as ProviderStatus] ?? "neutral";
}

/** Worst first: what needs a look sits at the top of the summary tile. */
const SEVERITY: Record<ProviderStatus, number> = {
  open: 0, cap_reached: 1, degraded: 2, low: 3, unknown: 4, ok: 5, disabled: 6,
};

export function severity(status: string | null | undefined): number {
  return SEVERITY[(status ?? "unknown") as ProviderStatus] ?? 4;
}

/** How many providers need a look (not ok, not switched off). */
export function needsLook(rows: readonly Pick<ProviderRow, "status">[]): number {
  return rows.filter((r) => r.status !== "ok" && r.status !== "disabled").length;
}

/** A USD amount: two decimals from one dollar up, more below (a call costs fractions of a cent). */
export function usd(v: number | null | undefined, digits?: number): string {
  if (v == null || Number.isNaN(v)) return "—";
  const d = digits ?? (Math.abs(v) >= 1 || v === 0 ? 2 : 4);
  return `$${v.toFixed(d)}`;
}

export function pct(v: number | null | undefined): string {
  return v == null ? "—" : `${v.toFixed(v >= 10 || v === 0 ? 0 : 1)} %`;
}

/** The page link that opens the trace drawer of a job (JobDrawer "Open trace"). */
export function traceHref(jobId: string): string {
  return `${PAGE_PATH}?trace=${encodeURIComponent(`job:${jobId}`)}`;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** The job id of a `?trace=job:<uuid>` value, or null (any other form is ignored). */
export function parseTrace(value: string | null | undefined): string | null {
  if (!value) return null;
  const m = /^job:(.+)$/.exec(value.trim());
  const id = m?.[1]?.trim() ?? "";
  return UUID_RE.test(id) ? id.toLowerCase() : null;
}

/** The trace API path of a job. */
export function tracePath(jobId: string): string {
  return `${TRACE_API}?job_id=${encodeURIComponent(jobId)}`;
}

/** Rows grouped as the page lists them: LLM connections, then Jev, then the data providers. */
export function groupRows(rows: readonly ProviderRow[]): Record<ProviderGroup, ProviderRow[]> {
  const out: Record<ProviderGroup, ProviderRow[]> = { llm: [], decide: [], data: [] };
  for (const r of rows) (out[r.group] ?? out.data).push(r);
  return out;
}
