// lib/aiGateway.ts — AA-804 (UI parts 1 and 2): the admin "AI providers" page (/admin/ai-gateway).
// Shapes of GET /api/admin/ai-gateway/providers, /trace?job_id= and /budgets (backend api/services/ai_gateway.py),
// the page's tab and view keys, the links that retired Settings / Cost Operations tabs now point to, and the pure
// helpers the page uses. Every number comes from the API (database reads only, $0); this file only presents it. No
// account key or ID is written here: a Bedrock connection is named through lib/llmConnections connectionName + the
// account directory (useAwsAccounts); the only key used is the LLM account constant of lib/awsAccounts.

import { LLM_ACCOUNT } from "./awsAccounts.ts";

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

// ── tabs, views and the links of retired tabs (AA-804 UI part 2) ──────────────────────────────────────────────────

/** The page's tabs, in order. `providers` is the default (no `tab` in the address). */
export const TAB_KEYS = ["providers", "routing", "models", "budgets", "guide"] as const;
export type TabKey = (typeof TAB_KEYS)[number];
/** The views inside the Providers tab: the read-only overview, the connection settings, the Jev credit ledger. */
export const PROVIDER_VIEWS = ["overview", "connections", "jev-credit"] as const;
export type ProviderView = (typeof PROVIDER_VIEWS)[number];

export function parseTab(value: string | null | undefined): TabKey {
  return (TAB_KEYS as readonly string[]).includes(value ?? "") ? (value as TabKey) : "providers";
}

export function parseView(value: string | null | undefined): ProviderView {
  return (PROVIDER_VIEWS as readonly string[]).includes(value ?? "") ? (value as ProviderView) : "overview";
}

/** The page link of a tab (and a Providers view): exactly one `tab` value, `view` only when it is not the overview. */
export function aiGatewayHref(tab: TabKey, view?: ProviderView): string {
  const q = new URLSearchParams({ tab });
  if (tab === "providers" && view && view !== "overview") q.set("view", view);
  return `${PAGE_PATH}?${q.toString()}`;
}

/** Settings tabs that moved here: the old `?tab=` links (bookmarks, mails already sent) are sent on to these. */
export const RETIRED_SETTINGS_TABS: Readonly<Record<string, string>> = {
  models: aiGatewayHref("routing"),
  "jev-credit": aiGatewayHref("providers", "jev-credit"),
};
/** Cost Operations tabs that moved here. */
export const RETIRED_COST_OPS_TABS: Readonly<Record<string, string>> = {
  budgets: aiGatewayHref("budgets"),
};

/** Where a retired `?tab=` value now lives, or null (the tab still exists, or no tab was asked). */
export function retiredTabTarget(map: Readonly<Record<string, string>>, wanted: string | null | undefined): string | null {
  return wanted && Object.prototype.hasOwnProperty.call(map, wanted) ? map[wanted] : null;
}

/** Show the per-stage account select only while a stage still routes to an account other than the LLM account.
 *  Every row NULL or the LLM account: nothing to choose, so the select is hidden (the column stays; the PATCH keeps
 *  sending the row's own value). */
export function accountRouteMatters(rows: readonly { account_route: string | null | undefined }[]): boolean {
  return rows.some((r) => r.account_route != null && r.account_route !== "" && r.account_route !== LLM_ACCOUNT);
}

// ── budgets (GET /api/admin/ai-gateway/budgets; edits through PUT /api/admin/budgets/{provider}/{scope}) ───────────

export type BudgetState = "ok" | "alert" | "cap_reached" | "no_daily_cap";
export type ConnectionBudgetState = "ok" | "alert" | "stopped" | "no_budget";

export interface SpendBudgetRow {
  provider: string;
  scope: string;
  job_kind: string | null;
  /** "kind": the guard measures this job row's daily cap against that job kind's own spend today; "provider": against
   *  the provider's whole day. `spent_today_usd` is that same figure. */
  day_scope: "provider" | "kind";
  per_run_usd: number | null;
  per_day_usd: number | null;
  hard_stop: boolean;
  alert_pct: number | null;
  updated_at: string | null;
  updated_by: string | null;
  spent_today_usd: number;
  used_pct: number | null;
  state: BudgetState;
}

export interface ConnectionBudgetRow {
  connection_key: string;
  kind: string | null;
  label: string;
  enabled: boolean;
  account_id: string | null;
  monthly_budget_usd: number | null;
  month_spent_usd: number;
  month_calls: number;
  used_pct: number | null;
  alert_pct: number;
  hard_stop: boolean;
  state: ConnectionBudgetState;
}

export interface CoverageRow {
  tier: "provider" | "connection";
  key: string;
  label?: string;
  kind?: string | null;
  account_id?: string | null;
  capped: boolean;
  hard_stop: boolean | null;
}

export interface BudgetsResp {
  generated_at: string;
  day_start: string;
  month_start: string;
  providers: string[];
  tiers: {
    provider: SpendBudgetRow[];
    job: SpendBudgetRow[];
    connection: ConnectionBudgetRow[];
    tenant: unknown[];
  };
  coverage: CoverageRow[];
  spent_today_usd: Record<string, number>;
  errors: { source: string; error: string }[];
}

export const BUDGETS_API = "/api/admin/ai-gateway/budgets";

/** The spend_budget providers (shared/cost_guard.py PROVIDERS) and their product names. */
export const BUDGET_PROVIDER_LABEL: Readonly<Record<string, string>> = {
  dfs: "DataForSEO", bedrock: "Bedrock", openai: "OpenAI API", jev: "Jev", geocoder: "Geocoder (Geoapify)",
  anthropic: "Anthropic API", azure_openai: "Azure OpenAI",
};

export function budgetProviderLabel(provider: string): string {
  return BUDGET_PROVIDER_LABEL[provider] ?? provider;
}

const BUDGET_TONE: Record<BudgetState | ConnectionBudgetState, Tone> = {
  ok: "success", alert: "warning", cap_reached: "danger", stopped: "danger", no_daily_cap: "neutral",
  no_budget: "neutral",
};

export function budgetTone(state: string | null | undefined): Tone {
  return BUDGET_TONE[(state ?? "") as BudgetState] ?? "neutral";
}

/** One spend_budget row as the editor types it: every field a string. */
export interface BudgetDraft {
  per_run: string;
  per_day: string;
  hard_stop: boolean;
  alert_pct: string;
}

export interface BudgetBody {
  per_run_usd: number | null;
  per_day_usd: number | null;
  hard_stop: boolean;
  alert_pct: number;
}

export type BudgetError = "cap" | "alertPct" | "scope";

export function budgetDraftOf(r: Pick<SpendBudgetRow, "per_run_usd" | "per_day_usd" | "hard_stop" | "alert_pct">):
  BudgetDraft {
  return {
    per_run: r.per_run_usd == null ? "" : String(r.per_run_usd),
    per_day: r.per_day_usd == null ? "" : String(r.per_day_usd),
    hard_stop: r.hard_stop,
    alert_pct: String(r.alert_pct ?? 80),
  };
}

export function sameDraft(a: BudgetDraft, b: BudgetDraft): boolean {
  return a.per_run.trim() === b.per_run.trim() && a.per_day.trim() === b.per_day.trim()
    && a.hard_stop === b.hard_stop && a.alert_pct.trim() === b.alert_pct.trim();
}

function cap(v: string): number | null | undefined {
  const s = v.trim();
  if (s === "") return null;            // no cap
  const n = Number(s);
  return Number.isFinite(n) && n >= 0 ? n : undefined;
}

/** The PUT body of a draft (the backend BudgetUpdate: caps >= 0 or null, alert % a whole number 1–100), or the first
 *  problem. The API checks again; this only saves a round trip. */
export function budgetBody(d: BudgetDraft): { body?: BudgetBody; error?: BudgetError } {
  const run = cap(d.per_run);
  const day = cap(d.per_day);
  if (run === undefined || day === undefined) return { error: "cap" };
  const pct = Number(d.alert_pct.trim());
  if (!Number.isInteger(pct) || pct < 1 || pct > 100) return { error: "alertPct" };
  return { body: { per_run_usd: run, per_day_usd: day, hard_stop: d.hard_stop, alert_pct: pct } };
}

const SCOPE_RE = /^job:[a-z0-9_]+$/;

/** A new row's scope: `global` or `job:<kind>` (the backend _valid rule, kind in lower snake case). */
export function validScope(scope: string): boolean {
  const s = scope.trim();
  return s === "global" || SCOPE_RE.test(s);
}

/** The PUT path of one spend_budget row. */
export function budgetPutPath(provider: string, scope: string): string {
  return `/api/admin/budgets/${encodeURIComponent(provider)}/${encodeURIComponent(scope)}`;
}
