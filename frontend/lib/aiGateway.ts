// lib/aiGateway.ts — AA-804 (UI parts 1 to 3): the admin "AI providers" page (/admin/ai-gateway).
// Shapes of GET /api/admin/ai-gateway/providers, /trace?job_id= | ?tour_id=, /budgets (backend
// api/services/ai_gateway.py) and /calls (api/services/ai_gateway_calls.py),
// the page's tab and view keys, the links that retired Settings / Cost Operations tabs now point to, and the pure
// helpers the page uses. Every number comes from the API (database reads only, $0); this file only presents it. No
// account key or ID is written here: a Bedrock connection is named through lib/llmConnections connectionName + the
// account directory (useAwsAccounts); the only key used is the LLM account constant of lib/awsAccounts.

import { LLM_ACCOUNT, VENDOR_CONNECTION_LABELS } from "./awsAccounts.ts";

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
  // Registry v2 (CIS migration 322, AA-1017): on the Jev / DataForSEO / Geoapify rows once the registry has them.
  // Absent on an older API: the page then hides the line (never shows a guess).
  failure_domain?: string | null;
  access_state?: string | null;
  last_probe_at?: string | null;
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
/** A Calls row's ledger: the four CIS ledgers plus the CRM's own (crm.ops.v1 GET /llm/calls). */
export type CallSource = TraceSource | "crm";
/** Ledger v2 status of an attempt (null on a row written before 31 Oct 2026). */
export type CallStatus = "ok" | "refused" | "error" | "billed_unusable" | "truncated";

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
  /** Ledger v2 status; null on an older row (the outcome still says ok / shadow / fallback / cache / live). */
  status: CallStatus | null;
  error_class: string | null;
  job_id: string | null;
  app: string | null;
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

export interface TraceJob {
  id: string; kind: string; status: string; created_at: string | null; started_at: string | null;
  finished_at: string | null; cost_usd: number | null;
}

export interface TraceResp {
  kind: "job" | "tour";
  job: (TraceJob & { tour_id: string | null; parent_job_id: string | null }) | null;
  tour: { id: string; jobs: TraceJob[]; jobs_total: number; jobs_error: string | null } | null;
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

/** The word a row's status badge shows (live finding F8). An LLM connection that is "unknown" with no reason has made
 *  no call in the window, so it says "No recent calls" like Routing. Any other row keeps its status: a Jev or
 *  DataForSEO row is "unknown" because no top-up or balance is known (its reason line says so), and an unreadable
 *  source stays "unknown". */
export function statusWordKey(row: Pick<ProviderRow, "group" | "status" | "status_reason">): ProviderStatus | "no_recent_calls" {
  return row.group === "llm" && row.status === "unknown" && !row.status_reason ? "no_recent_calls" : row.status;
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

/** What a trace drawer shows: one job, or one tour (every job of the tour and the calls marked with it). */
export interface TraceTarget {
  kind: "job" | "tour";
  id: string;
}

/** The page link that opens the trace drawer of a job (JobDrawer "Open trace", the decisions log). */
export function traceHref(jobId: string): string {
  return `${PAGE_PATH}?trace=${encodeURIComponent(`job:${jobId}`)}`;
}

/** The page link that opens the trace drawer of a tour (the tour detail panel). */
export function tourTraceHref(tourId: string): string {
  return `${PAGE_PATH}?trace=${encodeURIComponent(`tour:${tourId}`)}`;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isUuid(value: string | null | undefined): boolean {
  return !!value && UUID_RE.test(value.trim());
}

/** The target of a `?trace=job:<uuid>` or `?trace=tour:<uuid>` value, or null (any other form is ignored). */
export function parseTraceTarget(value: string | null | undefined): TraceTarget | null {
  if (!value) return null;
  const m = /^(job|tour):(.+)$/.exec(value.trim());
  const id = m?.[2]?.trim() ?? "";
  return m && UUID_RE.test(id) ? { kind: m[1] as TraceTarget["kind"], id: id.toLowerCase() } : null;
}

/** The job id of a `?trace=job:<uuid>` value, or null (a tour or any other form gives null). */
export function parseTrace(value: string | null | undefined): string | null {
  const t = parseTraceTarget(value);
  return t?.kind === "job" ? t.id : null;
}

/** The trace API path of a job (a string) or of a target. */
export function tracePath(target: string | TraceTarget): string {
  const t = typeof target === "string" ? { kind: "job" as const, id: target } : target;
  return `${TRACE_API}?${t.kind === "tour" ? "tour_id" : "job_id"}=${encodeURIComponent(t.id)}`;
}

/** The `?trace=` value of a target (the page keeps it in the address). */
export function traceParam(t: TraceTarget): string {
  return `${t.kind}:${t.id}`;
}

/** Rows grouped as the page lists them: LLM connections, then Jev, then the data providers. */
export function groupRows<R extends Pick<ProviderRow, "group">>(rows: readonly R[]): Record<ProviderGroup, R[]> {
  const out: Record<ProviderGroup, R[]> = { llm: [], decide: [], data: [] };
  for (const r of rows) (out[r.group] ?? out.data).push(r);
  return out;
}

// ── tabs, views and the links of retired tabs (AA-804 UI part 2) ──────────────────────────────────────────────────

/** The page's tabs, in order. `providers` is the default (no `tab` in the address). */
export const TAB_KEYS = ["providers", "routing", "models", "budgets", "calls", "guide"] as const;
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
/** The Cost Operations page and its merged "AI & data spend" tab (UI part 3: the LLM, DataForSEO and Jev tabs became
 *  views of one tab). */
export const COST_OPS_PATH = "/admin/cost-ops";
export const SPEND_TAB = "spend";
export const SPEND_VIEWS = ["llm", "dfs", "jev"] as const;
export type SpendView = (typeof SPEND_VIEWS)[number];

export function parseSpendView(value: string | null | undefined): SpendView {
  return (SPEND_VIEWS as readonly string[]).includes(value ?? "") ? (value as SpendView) : "llm";
}

/** The link of the merged tab: exactly one `tab` value, `view` only when it is not the LLM view. */
export function costOpsSpendHref(view?: SpendView): string {
  const q = new URLSearchParams({ tab: SPEND_TAB });
  if (view && view !== "llm") q.set("view", view);
  return `${COST_OPS_PATH}?${q.toString()}`;
}

/** Cost Operations tabs that moved: Budgets to this page, LLM / DataForSEO / Jev into the merged spend tab. */
export const RETIRED_COST_OPS_TABS: Readonly<Record<string, string>> = {
  budgets: aiGatewayHref("budgets"),
  llm: costOpsSpendHref("llm"),
  dfs: costOpsSpendHref("dfs"),
  jev: costOpsSpendHref("jev"),
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

/** Who last changed a cap (backend ai_gateway.actor_view): a staff member by name, a migration, an older
 *  "<username>:<reference>" login value (C4 polish N4), other text. */
export type ActorKind = "staff" | "migration" | "login" | "other" | "none";

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
  /** C4 (F5): absent on an older API (the page then never prints a raw id). */
  updated_by_kind?: ActorKind;
  updated_by_name?: string | null;
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
  /** AA-1016 (C4): false before CIS migration 321 (no tenant budget table yet); absent on an older API. */
  tenant_ready?: boolean;
  tiers: {
    provider: SpendBudgetRow[];
    job: SpendBudgetRow[];
    connection: ConnectionBudgetRow[];
    tenant: TenantBudgetRow[];
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

// ── calls (GET /api/admin/ai-gateway/calls, UI part 3) ─────────────────────────────────────────────────────────────

export const CALLS_API = "/api/admin/ai-gateway/calls";
export const CALL_SOURCES: readonly CallSource[] = ["llm", "jev", "dfs", "geocoder", "crm"];
export const CALL_STATUSES: readonly CallStatus[] = ["ok", "refused", "error", "billed_unusable", "truncated"];
export const CALL_WINDOWS = ["1h", "24h", "7d", "30d"] as const;
export type CallWindow = (typeof CALL_WINDOWS)[number];
/** The spend-budget providers (shared/cost_guard.py PROVIDERS), the provider filter's values. */
export const CALL_PROVIDERS = ["bedrock", "openai", "anthropic", "azure_openai", "jev", "dfs", "geocoder"] as const;
export const CALL_APPS = ["cis", "tripplanner", "crm"] as const;

export interface CallRow extends Omit<TraceEvent, "source"> {
  /** `<source>:<ledger row id>`, unique across the page */
  id: string;
  source: CallSource;
  tenant_id: string | null;
}

export interface CrmCallsState {
  status: "ok" | "not_asked" | "unavailable" | "refused" | "error";
  reason?: string | null;
  rows?: number;
  http_status?: number | null;
  /** The CRM's totals (crm.ops.v1 GET /llm/usage) when its rows could not be read: whole UTC days from .. to,
   *  so they can cover more than the window. */
  aggregate?: { calls: number; cost_usd: number; from: string; to: string;
    by_model: { model: string; calls: number; cost_usd: number }[] } | null;
}

export interface CallsResp {
  generated_at: string;
  window: { key: CallWindow; from: string; to: string };
  filters: Record<string, string>;
  sources: CallSource[];
  rows: CallRow[];
  next_cursor: string | null;
  limit: number;
  crm: CrmCallsState;
  errors: { source: string; error: string }[];
}

/** The Calls tab's filters (the address keeps them under CALL_PARAM keys, `c_` prefixed, apart from the page's own). */
export interface CallFilters {
  job_id?: string;
  tour_id?: string;
  app?: string;
  provider?: string;
  status?: string;
  stage?: string;
  source?: string;
  window?: string;
}

export const CALL_FILTER_KEYS = ["job_id", "tour_id", "app", "provider", "status", "stage", "source",
  "window"] as const;

/** The API path of one page of calls: only the filters that are set, then the cursor. */
export function callsPath(f: CallFilters, cursor?: string | null, limit?: number): string {
  const q = new URLSearchParams();
  for (const k of CALL_FILTER_KEYS) {
    const v = f[k]?.trim();
    if (v) q.set(k, v);
  }
  if (cursor) q.set("cursor", cursor);
  if (limit) q.set("limit", String(limit));
  const qs = q.toString();
  return qs ? `${CALLS_API}?${qs}` : CALLS_API;
}

/** The tone of a call's status / outcome badge. */
export function callTone(outcome: string | null | undefined): Tone {
  if (outcome === "error" || outcome === "reject" || outcome === "refused") return "danger";
  if (outcome === "billed_unusable" || outcome === "truncated" || outcome === "grey" || outcome === "fallback") {
    return "warning";
  }
  if (outcome === "shadow" || outcome === "cache") return "info";
  if (outcome === "ok" || outcome === "accept" || outcome === "live") return "success";
  return "neutral";
}

/** The address keys of the Calls tab's filters. */
export const CALL_PARAM: Readonly<Record<(typeof CALL_FILTER_KEYS)[number], string>> = {
  job_id: "c_job", tour_id: "c_tour", app: "c_app", provider: "c_provider", status: "c_status", stage: "c_stage",
  source: "c_source", window: "c_window",
};

/** The Calls filters read from the address (`get` returns "" for a missing key); a bad job / tour id is dropped. */
export function callFiltersFrom(get: (key: string) => string): CallFilters {
  const f: CallFilters = {};
  for (const k of CALL_FILTER_KEYS) {
    const v = get(CALL_PARAM[k]).trim();
    if (!v) continue;
    if ((k === "job_id" || k === "tour_id") && !UUID_RE.test(v)) continue;
    f[k] = k === "job_id" || k === "tour_id" ? v.toLowerCase() : v;
  }
  return f;
}

/** The link of the Calls tab filtered to one job (a job drawer, a trace). */
export function callsHref(f: CallFilters): string {
  const q = new URLSearchParams({ tab: "calls" });
  for (const k of CALL_FILTER_KEYS) {
    const v = f[k]?.trim();
    if (v) q.set(CALL_PARAM[k], v);
  }
  return `${PAGE_PATH}?${q.toString()}`;
}

// ── C4 (S262 wave 4): actor names, tenant AI budgets, warm paths, access state, the failure-domain rule,
//    the Experiments section and the re-queue of refused jobs ──────────────────────────────────────────────────────

const UUID_ANYWHERE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

/** "<username>:<reference>" (backend ai_gateway._LOGIN_RE): an older hand-set actor, e.g. "mai:ops12". */
const LOGIN_ACTOR = /^([a-z][a-z0-9._-]{0,99}):([a-z0-9._-]{1,64})$/i;

/** What the "Last changed" column prints (live finding F5): a staff member's name, "Migration NNN", other text; a
 *  staff id with no name (a former member, or an older API) is `staffFallback`, never the raw id. C4 polish (N4): a
 *  "<username>:<reference>" value the staff directory could not name reads `loginText(reference)` ("staff (ops12)"),
 *  never the raw login value. */
export function actorLabel(r: { updated_by?: string | null; updated_by_kind?: ActorKind; updated_by_name?: string | null },
  staffFallback: string, loginText?: (reference: string) => string): string | null {
  if (r.updated_by_name && r.updated_by_kind !== "other") return r.updated_by_name;
  const raw = (r.updated_by ?? r.updated_by_name ?? "").trim();
  if (!raw) return null;
  if (r.updated_by_kind === "staff" || UUID_ANYWHERE.test(raw)) return staffFallback;
  const m = /^migration[-_ ]?(\d{3})(?:[-_].*)?$/i.exec(raw);
  if (m) return `Migration ${m[1]}`;
  const login = LOGIN_ACTOR.exec(raw);
  if (login) return loginText ? loginText(login[2]) : staffFallback;
  return r.updated_by_name ?? raw;
}

export type TenantBudgetState = "unlimited" | "ok" | "alert" | "approval" | "over";

/** One tenant's monthly AI budget (backend api/services/tenant_ai_budget.py budget_view). */
export interface TenantBudgetRow {
  tenant_id: string;
  tenant_name: string | null;
  monthly_usd: number | null;
  alert_pct: number;
  approval_pct: number;
  hard_stop: boolean;
  notes: string | null;
  updated_at: string | null;
  updated_by: string | null;
  updated_by_kind?: ActorKind;
  updated_by_name?: string | null;
  month_spent_usd: number;
  month_calls: number;
  used_pct: number | null;
  state: TenantBudgetState;
}

/** GET /api/admin/ai-gateway/tenant-budgets/{tenant_id} (Tenant 360 › Cost). */
export interface TenantBudgetResp {
  ready: boolean;
  tenant_id: string;
  tenant_name: string | null;
  budget: TenantBudgetRow | null;
  month_spent_usd: number;
  month_calls: number;
  month_start: string;
  defaults: { monthly_usd: number | null; alert_pct: number; approval_pct: number; hard_stop: boolean;
    notes: string | null };
}

export function tenantBudgetPath(tenantId: string): string {
  return `/api/admin/ai-gateway/tenant-budgets/${encodeURIComponent(tenantId)}`;
}

const TENANT_BUDGET_TONE: Record<TenantBudgetState, Tone> = {
  unlimited: "neutral", ok: "success", alert: "warning", approval: "danger", over: "warning",
};

export function tenantBudgetTone(state: string | null | undefined): Tone {
  return TENANT_BUDGET_TONE[(state ?? "") as TenantBudgetState] ?? "neutral";
}

/** The tenant budget editor's draft: every field a string, like the spend budget editor. */
export interface TenantBudgetDraft {
  monthly: string;
  alert_pct: string;
  approval_pct: string;
  hard_stop: boolean;
  notes: string;
}

export interface TenantBudgetBody {
  monthly_usd: number | null;
  alert_pct: number;
  approval_pct: number;
  hard_stop: boolean;
  notes: string | null;
}

export type TenantBudgetError = "monthly" | "alertPct" | "approvalPct" | "order" | "notes";

export function tenantBudgetDraftOf(b: Pick<TenantBudgetRow, "monthly_usd" | "alert_pct" | "approval_pct"
  | "hard_stop" | "notes"> | null): TenantBudgetDraft {
  return {
    monthly: b?.monthly_usd == null ? "" : String(b.monthly_usd),
    alert_pct: String(b?.alert_pct ?? 100),
    approval_pct: String(b?.approval_pct ?? 150),
    hard_stop: b?.hard_stop ?? true,   // a new budget holds jobs at the approval threshold by default
    notes: b?.notes ?? "",
  };
}

function wholePct(v: string): number | undefined {
  const n = Number(v.trim());
  return v.trim() !== "" && Number.isInteger(n) && n >= 1 && n <= 1000 ? n : undefined;
}

/** The PUT body of a draft (backend TenantBudgetBody: amount >= 0 or empty = no amount, whole % 1–1000, approval at
 *  least the alert %, notes up to 2,000 characters), or the first problem. The API checks again. */
export function tenantBudgetBody(d: TenantBudgetDraft): { body?: TenantBudgetBody; error?: TenantBudgetError } {
  const m = d.monthly.trim();
  const monthly = m === "" ? null : Number(m);
  if (monthly !== null && (!Number.isFinite(monthly) || monthly < 0 || monthly > 1_000_000)) return { error: "monthly" };
  const alert = wholePct(d.alert_pct);
  if (alert === undefined) return { error: "alertPct" };
  const approval = wholePct(d.approval_pct);
  if (approval === undefined) return { error: "approvalPct" };
  if (approval < alert) return { error: "order" };
  if (d.notes.length > 2000) return { error: "notes" };
  return { body: { monthly_usd: monthly === null ? null : Math.round(monthly * 100) / 100, alert_pct: alert,
    approval_pct: approval, hard_stop: d.hard_stop, notes: d.notes.trim() || null } };
}

export function sameTenantDraft(a: TenantBudgetDraft, b: TenantBudgetDraft): boolean {
  return a.monthly.trim() === b.monthly.trim() && a.alert_pct.trim() === b.alert_pct.trim()
    && a.approval_pct.trim() === b.approval_pct.trim() && a.hard_stop === b.hard_stop
    && a.notes.trim() === b.notes.trim();
}

/** The Tenant 360 link of a tenant's Cost tab (the Budgets tab's tenant rows). */
export function tenantCostHref(tenantId: string): string {
  return `/admin/tenants?tenant=${encodeURIComponent(tenantId)}&tab=cost`;
}

/** The warm independent paths of root ADR 0009 (decision 2, accepted 31/10/2026): both direct vendor APIs. A row is a
 *  warm path by its connection kind (the API key connection of that vendor), never by an account key. */
export type WarmPath = "anthropic" | "openai";

export function warmPathOf(row: { group?: string; provider?: string | null; data_scope?: string | null }):
  WarmPath | null {
  if (row.group !== undefined && row.group !== "llm") return null;
  if (row.data_scope && row.data_scope !== "vendor_direct") return null;
  return row.provider === "anthropic" || row.provider === "openai" ? row.provider : null;
}

/** Access state of a registry row (migration 322): unknown | ok | access_denied | quota_exhausted | unconfigured. */
export const ACCESS_STATES = ["ok", "unknown", "access_denied", "quota_exhausted", "unconfigured"] as const;
export type AccessState = (typeof ACCESS_STATES)[number];

const ACCESS_TONE: Record<AccessState, Tone> = {
  ok: "success", unknown: "neutral", access_denied: "danger", quota_exhausted: "danger", unconfigured: "warning",
};

export function accessTone(state: string | null | undefined): Tone {
  return ACCESS_TONE[(state ?? "unknown") as AccessState] ?? "neutral";
}

export function isAccessState(state: string | null | undefined): state is AccessState {
  return (ACCESS_STATES as readonly string[]).includes(state ?? "");
}

// ── C4 polish (N4): raw codes in plain words ─────────────────────────────────────────────────────────────────────

/** The ledger's error classes (shared/llm_client/call_log.py ERROR_CLASSES, migration 319 CHECK; a unit test keeps
 *  the two lists equal). The page prints each one in words (messages.ts ERROR_CLASS), never the code. */
export const ERROR_CLASSES = [
  "access_denied", "quota_exhausted", "throttled", "credential", "other", "deadline", "budget", "truncated",
  "disabled",                     // AA-642 A1 (migration 326): the stage kill switch refused the call
] as const;
export type ErrorClass = (typeof ERROR_CLASSES)[number];

/** The words key of an error class; a class this build does not know reads as "other" (never the raw code). */
export function errorClassKey(cls: string | null | undefined): ErrorClass | null {
  if (!cls) return null;
  return (ERROR_CLASSES as readonly string[]).includes(cls) ? (cls as ErrorClass) : "other";
}

/** The backend's refusal of a write by the read-only E2E session (api/core/e2e_staff.py E2E_READ_ONLY): 403
 *  `e2e_read_only` before the route runs. The page says "read-only session" instead of the code. */
export const READ_ONLY_REFUSAL = "e2e_read_only";

export function isReadOnlyRefusal(e: unknown): boolean {
  if (!(e instanceof Error)) return false;
  const status = (e as Error & { status?: number }).status;
  return e.message.trim() === READ_ONLY_REFUSAL && (status === undefined || status === 403);
}

/** The account part of a failure domain `<kind>:<account>:<scope>` as people read it (C4, polish N4): a 12-digit AWS
 *  account ID as is, a direct vendor API's name, a registry connection's label when the account part is a connection
 *  key (`labels` {connection_key: label}), a Bedrock row's own name (`ownName`, ignored when it is the key itself),
 *  else "this platform's AWS account" (Bedrock) or "its own vendor account". Never a connection key or an environment variable name. */
export function domainAccountText(kind: string, account: string, words: { own: string; vendor: string },
  opts: { ownName?: string | null; labels?: Readonly<Record<string, string>> } = {}): string {
  if (/^\d{12}$/.test(account)) return account;
  if (Object.prototype.hasOwnProperty.call(VENDOR_CONNECTION_LABELS, account)) return VENDOR_CONNECTION_LABELS[account];
  const label = opts.labels && Object.prototype.hasOwnProperty.call(opts.labels, account) ? opts.labels[account] : null;
  if (label && label !== account) return label;
  // the caller's own name for the row, unless it is just the same key again
  const own = opts.ownName && opts.ownName !== account ? opts.ownName : null;
  return kind.startsWith("bedrock") ? own ?? words.own : words.vendor;
}

/** The registry facts of one LLM connection the Providers overview adds to its row: from GET /api/admin/llm-connections
 *  (access_state, last_probe_at, failure_domain once migration 322 is applied) and GET /api/admin/llm-availability
 *  (failure_domain derived today). Any field the API does not send stays undefined, and its line is hidden. */
export interface RegistryFacts {
  failure_domain?: string | null;
  access_state?: string | null;
  last_probe_at?: string | null;
}

export function registryFacts(
  key: string | null | undefined,
  connections: readonly (RegistryFacts & { connection_key: string })[] | undefined,
  availability: readonly { connection_key: string; failure_domain?: string | null }[] | undefined,
): RegistryFacts {
  if (!key) return {};
  const c = connections?.find((x) => x.connection_key === key);
  const a = availability?.find((x) => x.connection_key === key);
  const out: RegistryFacts = {};
  const domain = c?.failure_domain ?? a?.failure_domain;
  if (domain !== undefined) out.failure_domain = domain;
  if (c && "access_state" in c) out.access_state = c.access_state;
  if (c && "last_probe_at" in c) out.last_probe_at = c.last_probe_at;
  return out;
}

/** A failure domain `<kind>:<account>:<scope>` as the page prints it: the account part named by the caller (an AWS
 *  account ID or the vendor API's name, never a key), then the kind and the scope. */
export function domainParts(domain: string | null | undefined): { kind: string; account: string; scope: string } | null {
  if (!domain) return null;
  const parts = domain.split(":");
  if (parts.length < 3) return null;
  return { kind: parts[0], account: parts.slice(1, -1).join(":"), scope: parts[parts.length - 1] };
}

/** The failure-domain rule's answer for one stage (GET /api/admin/llm-availability `invariant.stages`). */
export interface InvariantStage {
  stage: string;
  role: string;
  ok: boolean;
  primary: string | null;
  primary_domain: string | null;
  other_domains: string[];
  reason: string | null;
  /** AA-642 A1: ok (another failure domain), degrade (none, but a declared degrade mode) or breach; absent before. */
  status?: "ok" | "degrade" | "breach";
  degrade_mode?: string | null;
}

export interface Invariant {
  ready?: boolean;
  report_only?: boolean;
  ok?: boolean;
  stages_checked?: number;
  breaches?: number;
  /** AA-642 A1: stages that pass through a declared degrade mode, and the switched-off stages (not checked). */
  declared_degrade?: number;
  disabled?: string[];
  stages?: InvariantStage[];
}

/** The rule's answer per stage, or an empty map when the API has no report (older API, or before migration 304). */
export function invariantByStage(inv: Invariant | null | undefined): Record<string, InvariantStage> {
  if (!inv || inv.ready === false || !Array.isArray(inv.stages)) return {};
  return Object.fromEntries(inv.stages.map((s) => [s.stage, s]));
}

// ── Experiments (GET /api/admin/ai-gateway/experiments) ──────────────────────────────────────────────────────────

export const EXPERIMENTS_API = "/api/admin/ai-gateway/experiments";

export interface PromptVersionRow {
  stage: string;
  version: string;
  prompt_sha256: string;
  sha_short: string;
  source_path: string | null;
  notes: string | null;
  created_at: string | null;
  created_by: string | null;
}

export interface EvalRunRow {
  id: string;
  pipeline: string;
  prompt_version: string;
  baseline_prompt_version: string | null;
  run_kind: string;
  writer: string | null;
  tour_count: number;
  avg_quality_score: number | null;
  baseline_avg_quality_score: number | null;
  gate_pass_count: number | null;
  gate_fail_count: number | null;
  cost_usd: number | null;
  cost_cap_usd: number | null;
  regression_detected: boolean;
  verdict: string | null;
  stop_reason: string | null;
  judge_kappa: number | null;
  created_at: string | null;
  finished_at: string | null;
}

export interface ExperimentList<T> {
  ready: boolean;
  rows: T[];
  limit: number;
  error: string | null;
}

export interface ExperimentsResp {
  prompt_versions: ExperimentList<PromptVersionRow>;
  eval_runs: ExperimentList<EvalRunRow>;
}

export function verdictTone(verdict: string | null | undefined, regression?: boolean): Tone {
  if (regression || verdict === "fail" || verdict === "invalid") return "danger";
  if (verdict === "pass") return "success";
  if (verdict === "baseline" || verdict === "informative") return "info";
  return "neutral";
}

// ── re-queue the jobs an LLM account block failed (POST /api/admin/job-runner/jobs/retry-llm-denied) ─────────────

export const RETRY_LLM_DENIED_API = "/api/admin/job-runner/jobs/retry-llm-denied";

export interface RetryJob {
  id: string;
  kind: string;
  error: string;
  finished_at: string | null;
  held?: string;
}

export interface RetryLlmDeniedResp {
  dry_run: boolean;
  prefix: string;
  since: string;
  kinds: string[];
  limit: number;
  matched: number;
  eligible: number;
  held_count: number;
  requeued: number;
  requeued_ids: string[];
  jobs: RetryJob[];
  held: RetryJob[];
  blocked_connections: Record<string, string>;
  more: boolean;
}

/** Rows one real re-queue reads at most (backend shared/jobs/queue.py RETRY_REASON_APPLY_LIMIT). The dry run asks
 *  for the same number, so its "eligible" is exactly what the apply right after it re-queues. */
export const REQUEUE_BATCH = 50;

/** The POST body: a batch of REQUEUE_BATCH rows from `since` (the server's default window, 14 days, when absent).
 *  The apply passes the dry run's own `since`, so both read the same rows. */
export function requeueBody(dryRun: boolean, since?: string | null): { dry_run: boolean; limit: number; since?: string } {
  return since ? { dry_run: dryRun, limit: REQUEUE_BATCH, since } : { dry_run: dryRun, limit: REQUEUE_BATCH };
}

/** Where the next batch starts, or null when there is none: the latest `finished_at` of the rows this call read (the
 *  rows are read oldest first). Its re-queued rows have left the failed set and its held rows before that time are
 *  skipped, so held rows can not fill every batch. Null as well when the cursor would not move (every row of a full
 *  batch finished in the same instant), so the page never offers the same batch again. */
export function nextRequeueSince(r: Pick<RetryLlmDeniedResp, "more" | "since" | "jobs" | "held">): string | null {
  if (!r.more) return null;
  let best: string | null = null;
  let bestMs = -Infinity;
  for (const j of [...r.jobs, ...r.held]) {
    const ms = j.finished_at ? Date.parse(j.finished_at) : NaN;
    if (!Number.isNaN(ms) && ms > bestMs) { bestMs = ms; best = j.finished_at; }
  }
  if (best === null || bestMs <= Date.parse(r.since)) return null;
  return best;
}

/** Why the dry run holds a job, as the page groups it (backend shared/jobs/queue.py `retry_by_reason` / `llm_hold`):
 *  its connection is still refused, it names no connection but one is refused with the same class, its job type
 *  refuses a retry, or its job type is not registered. */
export type HeldType = "connection" | "same_class" | "kind_refuses" | "kind_unknown";

export interface HeldGroup {
  type: HeldType;
  /** Connection keys (for the page to name, never to print), only for the two connection types. */
  keys: string[];
  /** The refusal classes of those connections (access_denied, quota_exhausted…). */
  classes: string[];
  /** The job kind, only for the two job-type types. */
  kind: string | null;
  jobs: number;
}

const HELD_ORDER: Record<HeldType, number> = { connection: 0, same_class: 1, kind_refuses: 2, kind_unknown: 3 };

function namedKeys(text: string, keys: readonly string[]): string[] {
  const esc = (k: string) => k.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return keys.filter((k) => new RegExp(`(?<![A-Za-z0-9-])${esc(k)}(?![A-Za-z0-9-])`).test(text)).sort();
}

/** The dry run's held jobs grouped by why, biggest group first. The backend's reason text is only parsed here, never
 *  shown: it names connections by key and classes by code. */
export function heldGroups(held: readonly RetryJob[], blocked: Readonly<Record<string, string>>): HeldGroup[] {
  const keys = Object.keys(blocked);
  const m = new Map<string, HeldGroup>();
  for (const j of held) {
    const why = j.held ?? "";
    let g: Omit<HeldGroup, "jobs">;
    if (why.startsWith("kind is not registered")) {
      g = { type: "kind_unknown", keys: [], classes: [], kind: j.kind };
    } else if (why.startsWith("connection still refused") || why.startsWith("a connection is still refused")) {
      const named = namedKeys(why, keys);
      g = { type: why.startsWith("connection") ? "connection" : "same_class", keys: named, kind: null,
        classes: [...new Set(named.map((k) => blocked[k]).filter(Boolean))].sort() };
    } else {
      g = { type: "kind_refuses", keys: [], classes: [], kind: j.kind };
    }
    const id = `${g.type}|${g.keys.join(",")}|${g.kind ?? ""}`;
    const cur = m.get(id);
    if (cur) cur.jobs += 1; else m.set(id, { ...g, jobs: 1 });
  }
  return [...m.values()].sort((a, b) => b.jobs - a.jobs || HELD_ORDER[a.type] - HELD_ORDER[b.type]);
}

// ── Models & trials: catalog text without internal names (live finding F4) ─────────────────────────────────────

const SESSION_TAG = /\b(?:S\d{3}|AA-\d{2,5})\b[:,]?\s*/g;
const INTERNAL_PART = /\.py\b|\bmigration\s*\d{3}\b|\b[a-z_]+\.[a-z_]+_(?:log|config|catalog)\b/i;

function tidy(text: string): string {
  return text.replace(/\s{2,}/g, " ").replace(/\s+([,.;:)])/g, "$1").replace(/\(\s*\)/g, "")
    .replace(/^[\s,;:.-]+|[\s,;:-]+$/g, "").trim();
}

function sentenceCase(text: string): string {
  if (!text) return text;
  const lower = text === text.toUpperCase() ? text.toLowerCase() : text;
  return lower.charAt(0).toUpperCase() + lower.slice(1);
}

/** A model's blocked reason for people: no session or issue tag, no shouting (the kit badge stays short: the page
 *  shows the badge "Blocked" and this sentence under it). */
export function blockedReasonText(reason: string | null | undefined): string | null {
  if (!reason) return null;
  const out = sentenceCase(tidy(reason.replace(SESSION_TAG, " ")));
  return out || null;
}

/** A price source for people: parts naming a code file, a migration or a table are left out, session tags removed;
 *  nothing left = `fallback` (e.g. "Built-in price table"). */
export function priceSourceText(source: string | null | undefined, fallback: string): string | null {
  if (!source) return null;
  const parts = source.replace(SESSION_TAG, " ").split(/;\s*|\.\s+(?=[A-Z])/)
    .map((p) => tidy(p)).filter((p) => p && !INTERNAL_PART.test(p));
  return parts.length ? parts.join("; ") : fallback;
}
