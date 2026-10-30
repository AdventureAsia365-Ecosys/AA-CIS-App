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
}

export interface ConnectionsResp {
  schema_ready: boolean;
  routing_uses_registry: boolean;
  connections: Connection[];
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
}

export type EditField = keyof EditForm;

const NUMERIC: Record<Exclude<EditField, "secret_name" | "base_url" | "notes">, [number, number, boolean]> = {
  timeout_s: [1, 900, true],
  max_retries: [0, 5, true],
  max_concurrency: [1, 1000, true],
  rpm_limit: [1, 100000, true],
  monthly_budget_usd: [0, 1_000_000, false],
};

const str = (v: string | number | null | undefined) => (v == null ? "" : String(v));

export function formOf(c: Connection): EditForm {
  return {
    secret_name: str(c.secret_name), base_url: str(c.base_url), timeout_s: str(c.timeout_s),
    max_retries: str(c.max_retries), max_concurrency: str(c.max_concurrency), rpm_limit: str(c.rpm_limit),
    monthly_budget_usd: str(c.monthly_budget_usd), notes: str(c.notes),
  };
}

// Same pattern as the table CHECK (migration 304) and connections.SECRET_NAME_RE.
const SECRET_NAME_RE = /^aa-llm\/[a-z0-9-]+\/[a-z0-9][a-z0-9-]*$/;

export type FieldError = "secretPrefix" | "httpsOnly" | "wholeNumber" | "range";

/** The PATCH body (only the fields that changed; an emptied field is null) and the client-side errors per field.
 *  The API checks everything again (secret prefix, host allowlist, ranges): this only saves a round trip. */
export function patchBody(c: Connection, form: EditForm): {
  body: Record<string, string | number | null>;
  errors: Partial<Record<EditField, { code: FieldError; min?: number; max?: number }>>;
} {
  const body: Record<string, string | number | null> = {};
  const errors: Partial<Record<EditField, { code: FieldError; min?: number; max?: number }>> = {};
  const before = formOf(c);
  for (const f of Object.keys(form) as EditField[]) {
    if (f === "secret_name" && !takesSecret(c)) continue;
    if (f === "base_url" && !takesBaseUrl(c)) continue;
    const raw = form[f].trim();
    if (raw === before[f].trim()) continue;
    if (raw === "") {
      body[f] = null;
      continue;
    }
    if (f === "secret_name") {
      if (!SECRET_NAME_RE.test(raw)) errors[f] = { code: "secretPrefix" };
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
