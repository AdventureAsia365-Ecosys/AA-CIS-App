// lib/llmConnections.ts — Settings › LLM Models › Connections (gateway slice S4).
// Shapes of GET /api/admin/llm-connections (backend api/services/llm_connections.py), the per-connection health in
// GET /api/admin/llm-availability (api/services/llm_availability.py), PATCH …/llm-connections/{key} and
// POST …/llm-connections/{key}/test, plus the pure helpers the tab uses (unit-tested in llmConnections.test.mjs).
// A Bedrock connection is named by its AWS account ID (lib/awsAccounts), never by its internal key.

import { accountName, isLiveAccount, type AccountText, type AwsAccounts } from "./awsAccounts.ts";

export interface Deployment {
  model_key: string;
  wire_id: string;
  priority: number;
  enabled: boolean;
  origin: string;
  batch_capable: boolean;
  price_in_override: number | null;
  price_out_override: number | null;
  notes: string | null;
}

export interface Connection {
  connection_key: string;
  kind: string;
  auth_mode: string;
  label: string;
  enabled: boolean;
  data_scope: string;
  account_id: string | null;
  region: string | null;
  role_name: string | null;
  credential_source: string;
  secret_name: string | null;
  credential_env: string | null;
  credential_env_set: boolean | null;
  base_url: string | null;
  timeout_s: number | null;
  max_retries: number | null;
  max_concurrency: number | null;
  rpm_limit: number | null;
  monthly_budget_usd: number | null;
  policy_problems: string[];
  notes: string | null;
  updated_at: string | null;
  updated_by: string | null;
  deployments: Deployment[];
  // gateway slice S10 (absent from an older API: the defaults apply)
  cost_policy?: CostPolicy;
  reconcile_source?: string;
  reconcile_configured?: boolean;
  usage_key_supported?: boolean;
}

/** The budget policy of a connection (shared.llm_connection_cost_policy, migration 317; defaults when not stored). */
export interface CostPolicy {
  alert_pct: number;
  hard_stop: boolean;
  usage_secret_name: string | null;
  usage_scope_id: string | null;
  stored: boolean;
  updated_at: string | null;
  updated_by: string | null;
}

export interface ConnectionsResp {
  schema_ready: boolean;
  routing_uses_registry: boolean;
  connections: Connection[];
  cost_policy_ready?: boolean;
}

// ── gateway slice S10: GET /api/admin/llm-connections/costs ──────────────────────────────────────────────────────

export type BudgetState = "ok" | "alert" | "stopped";
export type ReconcileStatus = "ok" | "mismatch" | "not_configured" | "unsupported" | "no_data" | "error";

export interface BudgetSummary {
  connection_key: string;
  monthly_budget_usd: number;
  month_spent_usd: number;
  month_calls: number;
  used_pct: number | null;
  alert_pct: number;
  hard_stop: boolean;
  state: BudgetState;
}

export interface ReconcileRow {
  day: string;
  source: string;
  status: ReconcileStatus;
  logged_usd: number | null;
  logged_calls: number;
  provider_usd: number | null;
  diff_usd: number | null;
  diff_pct: number | null;
  details: Record<string, unknown>;
  checked_at: string | null;
}

export interface ConnectionCost {
  connection_key: string;
  kind: string;
  label: string;
  enabled: boolean;
  budget: BudgetSummary | null;
  reconcile: { source: string; configured: boolean; last: ReconcileRow | null; days: ReconcileRow[] };
}

export interface CostsResp {
  schema_ready: boolean;
  connections: ConnectionCost[];
  tolerance: { usd: number; pct: number } | null;
  window_days?: number;
}

export const BUDGET_TONE: Record<BudgetState, "success" | "warning" | "danger"> = {
  ok: "success", alert: "warning", stopped: "danger",
};

export const RECONCILE_TONE: Record<ReconcileStatus, "success" | "warning" | "danger" | "neutral"> = {
  ok: "success", mismatch: "warning", error: "danger", no_data: "neutral", not_configured: "neutral",
  unsupported: "neutral",
};

export function budgetTone(state: string | null | undefined): "success" | "warning" | "danger" | "neutral" {
  return BUDGET_TONE[(state ?? "") as BudgetState] ?? "neutral";
}

export function reconcileTone(status: string | null | undefined): "success" | "warning" | "danger" | "neutral" {
  return RECONCILE_TONE[(status ?? "") as ReconcileStatus] ?? "neutral";
}

/** "$12.34" (2 decimals; 4 below one cent so a small spend is not shown as $0.00). */
export function usd(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  const abs = Math.abs(v);
  const digits = abs > 0 && abs < 0.01 ? 4 : 2;
  return `${v < 0 ? "-" : ""}$${abs.toFixed(digits)}`;
}

/** Whether a reconcile row is provisional (a Cost Explorer day not settled yet: never alerted). */
export function isProvisional(r: Pick<ReconcileRow, "details"> | null | undefined): boolean {
  return !!r && r.details?.provisional === true;
}

export type CircuitState = "closed" | "open" | "half_open";
export type HealthStatus = "ok" | "open" | "degraded" | "disabled" | "unknown";

export interface ModelHealth {
  model_key: string;
  state: CircuitState;
  consecutive_failures: number;
  last_ok_at: string | null;
  last_error_at: string | null;
  last_error_class: string | null;
  last_error: string | null;
  circuit_open_until: string | null;
  probe_until: string | null;
  open_streak: number;
}

export interface ConnectionHealth {
  connection_key: string;
  kind: string | null;
  label: string | null;
  enabled: boolean;
  status: HealthStatus;
  open_models: number;
  last_ok_at: string | null;
  last_error_at: string | null;
  last_error_class: string | null;
  models: ModelHealth[];
}

/** The part of GET /api/admin/llm-availability this tab reads. */
export interface AvailabilityHealth {
  health_ready?: boolean;
  connections?: ConnectionHealth[];
}

export interface TestResult {
  connection_key: string;
  kind: string;
  model_key: string;
  wire_id: string;
  ok: boolean;
  reply_usable: boolean;
  error_class: string | null;
  error: string | null;
  cost_usd: number;
  latency_ms: number;
  tokens_in?: number;
  tokens_out?: number;
  provider_model?: string | null;
  reply?: string;
}

export type Tone = "success" | "warning" | "danger" | "neutral";

export const HEALTH_TONE: Record<HealthStatus, Tone> = {
  ok: "success",
  open: "danger",
  degraded: "warning",
  disabled: "neutral",
  unknown: "neutral",
};

export const CIRCUIT_TONE: Record<CircuitState, Tone> = { closed: "success", open: "danger", half_open: "warning" };

export function healthTone(status: string | null | undefined): Tone {
  return HEALTH_TONE[(status ?? "unknown") as HealthStatus] ?? "neutral";
}

export function circuitTone(state: string | null | undefined): Tone {
  return CIRCUIT_TONE[(state ?? "closed") as CircuitState] ?? "neutral";
}

/** How the page names a connection: a Bedrock one by its AWS account ID (from the row, else the account directory),
 *  any other by its key (e.g. "anthropic-direct"). */
export function connectionName(c: Pick<Connection, "connection_key" | "kind" | "account_id">,
  dir?: AwsAccounts | null, text?: AccountText): string {
  if (c.kind !== "bedrock") return c.connection_key;
  if (c.account_id) return c.account_id;
  if (isLiveAccount(c.connection_key)) return accountName(c.connection_key, dir, text);
  return c.connection_key;
}

/** The kinds whose on/off switch the gateway reads (api/services/llm_connections.TOGGLE_KINDS). */
export const TOGGLE_KINDS: readonly string[] = ["bedrock", "anthropic", "openai"];
/** The kinds that take a base URL (shared/llm_client/connections.BASE_URL_ALLOWLIST). */
export const BASE_URL_KINDS: readonly string[] = ["anthropic", "openai", "azure_openai"];

export const canSwitchOn = (c: Pick<Connection, "kind">) => TOGGLE_KINDS.includes(c.kind);
export const takesSecret = (c: Pick<Connection, "auth_mode">) =>
  c.auth_mode === "api_key" || c.auth_mode === "bedrock_api_key";
export const takesBaseUrl = (c: Pick<Connection, "kind">) => BASE_URL_KINDS.includes(c.kind);

/** The edit form: every field a string as typed. */
export interface EditForm {
  secret_name: string;
  base_url: string;
  timeout_s: string;
  max_retries: string;
  max_concurrency: string;
  rpm_limit: string;
  monthly_budget_usd: string;
  notes: string;
  // gateway slice S10: the budget policy
  budget_alert_pct: string;
  budget_hard_stop: "true" | "false";
  usage_secret_name: string;
  usage_scope_id: string;
}

export type EditField = keyof EditForm;

type TextField = "secret_name" | "base_url" | "notes" | "budget_hard_stop" | "usage_secret_name" | "usage_scope_id";

const NUMERIC: Record<Exclude<EditField, TextField>, [number, number, boolean]> = {
  timeout_s: [1, 900, true],
  max_retries: [0, 5, true],
  max_concurrency: [1, 1000, true],
  rpm_limit: [1, 100000, true],
  monthly_budget_usd: [0, 1_000_000, false],
  budget_alert_pct: [1, 100, true],
};

/** Same defaults as shared/cost_guard.py (no policy row). */
export const DEFAULT_ALERT_PCT = 80;
const POLICY_FIELDS: readonly EditField[] = ["budget_alert_pct", "budget_hard_stop", "usage_secret_name", "usage_scope_id"];
const USAGE_FIELDS: readonly EditField[] = ["usage_secret_name", "usage_scope_id"];
const SCOPE_ID_RE = /^[A-Za-z0-9_-]{1,128}$/;

const str = (v: string | number | null | undefined) => (v == null ? "" : String(v));

export const takesUsageKey = (c: Pick<Connection, "usage_key_supported">) => !!c.usage_key_supported;

export function formOf(c: Connection): EditForm {
  const p = c.cost_policy;
  return {
    secret_name: str(c.secret_name), base_url: str(c.base_url), timeout_s: str(c.timeout_s),
    max_retries: str(c.max_retries), max_concurrency: str(c.max_concurrency), rpm_limit: str(c.rpm_limit),
    monthly_budget_usd: str(c.monthly_budget_usd), notes: str(c.notes),
    budget_alert_pct: str(p?.alert_pct ?? DEFAULT_ALERT_PCT),
    budget_hard_stop: (p?.hard_stop ?? true) ? "true" : "false",
    usage_secret_name: str(p?.usage_secret_name), usage_scope_id: str(p?.usage_scope_id),
  };
}

// Same pattern as the table CHECK (migration 304) and connections.SECRET_NAME_RE.
const SECRET_NAME_RE = /^aa-llm\/[a-z0-9-]+\/[a-z0-9][a-z0-9-]*$/;

export type FieldError = "secretPrefix" | "httpsOnly" | "wholeNumber" | "range" | "scopeId";

/** The PATCH body (only the fields that changed; an emptied field is null) and the client-side errors per field.
 *  The API checks everything again (secret prefix, host allowlist, ranges): this only saves a round trip.
 *  S10: an emptied alert percent goes back to the default (80); the hard stop is a boolean; the usage key fields
 *  exist only on a connection whose provider has a usage API (usage_key_supported). */
export function patchBody(c: Connection, form: EditForm): {
  body: Record<string, string | number | boolean | null>;
  errors: Partial<Record<EditField, { code: FieldError; min?: number; max?: number }>>;
} {
  const body: Record<string, string | number | boolean | null> = {};
  const errors: Partial<Record<EditField, { code: FieldError; min?: number; max?: number }>> = {};
  const before = formOf(c);
  for (const f of Object.keys(form) as EditField[]) {
    if (f === "secret_name" && !takesSecret(c)) continue;
    if (f === "base_url" && !takesBaseUrl(c)) continue;
    if (USAGE_FIELDS.includes(f) && !takesUsageKey(c)) continue;
    if (POLICY_FIELDS.includes(f) && !c.cost_policy) continue;   // an older API without the policy fields
    const typed = form[f].trim();
    const raw = f === "budget_alert_pct" && typed === "" ? String(DEFAULT_ALERT_PCT) : typed;
    if (raw === before[f].trim()) continue;
    if (f === "budget_hard_stop") {
      body[f] = raw === "true";
      continue;
    }
    if (raw === "") {
      body[f] = null;
      continue;
    }
    if (f === "secret_name" || f === "usage_secret_name") {
      if (!SECRET_NAME_RE.test(raw)) errors[f] = { code: "secretPrefix" };
      body[f] = raw;
    } else if (f === "usage_scope_id") {
      if (!SCOPE_ID_RE.test(raw)) errors[f] = { code: "scopeId" };
      body[f] = raw;
    } else if (f === "base_url") {
      if (!raw.startsWith("https://")) errors[f] = { code: "httpsOnly" };
      body[f] = raw;
    } else if (f === "notes") {
      body[f] = raw;
    } else {
      const [min, max, integer] = NUMERIC[f];
      const n = Number(raw);
      if (!Number.isFinite(n) || (integer && !Number.isInteger(n))) errors[f] = { code: "wholeNumber" };
      else if (n < min || n > max) errors[f] = { code: "range", min, max };
      body[f] = n;
    }
  }
  return { body, errors };
}

/** Health of one deployment, if the breaker has a row for it. */
export function modelHealth(health: ConnectionHealth | undefined, modelKey: string): ModelHealth | undefined {
  return health?.models.find((m) => m.model_key === modelKey);
}

/** The deployments the test can pick: text models only (no embeddings), enabled ones first, then by priority. */
export function testChoices(c: Connection, apiStyle: (modelKey: string) => string | undefined): Deployment[] {
  return c.deployments
    .filter((d) => apiStyle(d.model_key) !== "embed")
    .slice()
    .sort((a, b) => Number(b.enabled) - Number(a.enabled) || a.priority - b.priority
      || a.model_key.localeCompare(b.model_key));
}
