// lib/spendControls.ts — AA-1126 (S283): the Spend controls section of AI gateway › Budgets (backend GET
// /admin/ai-gateway/spend-controls, api/services/ai_gateway_spend_controls.py; POST
// /admin/ai-gateway/anomaly-breaker/{app}/reset).
//
// One source of truth: the values are the settings registry's (Platform › Settings, app CIS, area Spend), the ones the
// gateway reads (shared/llm_client/caps_settings.py). This section only shows them and links each to its row there; it
// never edits a value itself. Pure helpers (no React): the value as text, the state of an app's breaker, what a
// connection at its hard budget does under the environment's connection cap mode.

export const SPEND_CONTROLS_API = "/api/admin/ai-gateway/spend-controls";
export const SPEND_CONTROLS_KEY = ["ai-gateway", "spend-controls"] as const;
/** Where every spend control is edited (the API sends the same link per key). */
export const SPEND_SETTINGS_HREF = "/admin/platform/settings?app=cis&area=spend";

export function anomalyResetPath(app: string): string {
  return `/api/admin/ai-gateway/anomaly-breaker/${encodeURIComponent(app)}/reset`;
}

export interface SpendControlSetting {
  key: string;
  label: string;
  description: string;
  value_type: string;
  unit: string | null;
  allowed_values: string[] | null;
  min_value: number | null;
  max_value: number | null;
  default_value: unknown;
  value: unknown;
  source: string;            // env | tenant | platform | default
  version: number | null;
  href: string;
}

export interface AnomalyVerdict {
  app: string;
  mode: string;
  since: string;
  spent_usd: number;
  median_usd: number;
  multiple: number;
  floor_usd: number;
  baseline_days: number;
  threshold_usd: number;
  tripped: boolean;
  refuses: boolean;
  reset_at: string | null;
  reset_by: string | null;
}

export interface SpendControlsResp {
  settings: SpendControlSetting[];
  /** Keys the server's compiled defaults do not have yet (the code shipped before its migration). */
  missing: string[];
  settings_href: string;
  connection_cap_mode: string;
  anomaly: { mode: string; policy: { multiple: number; floor_usd: number; baseline_days: number } | null;
    apps: AnomalyVerdict[] };
  errors: string[];
}

function num(v: unknown): string {
  if (typeof v !== "number" || !Number.isFinite(v)) return String(v ?? "—");
  return Number.isInteger(v) ? String(v) : String(Number(v.toFixed(4)));
}

/** A setting's value as people read it: an enum as is, money as $x.xx with its unit, a multiple as "4 ×". Pure. */
export function settingValueText(value: unknown, s: Pick<SpendControlSetting, "value_type" | "unit">): string {
  if (value == null) return "—";
  if (s.value_type === "enum" || typeof value !== "number") return String(value);
  if (s.value_type === "usd") return `$${value.toFixed(2)}${s.unit ? s.unit.replace(/^USD/, "").trim() : ""}`;
  if (s.unit === "x") return `${num(value)} ×`;
  return s.unit ? `${num(value)} ${s.unit}` : num(value);
}

/** True when the value in force is not the definition's default. Pure. */
export function changedFromDefault(s: Pick<SpendControlSetting, "value" | "default_value">): boolean {
  return JSON.stringify(s.value) !== JSON.stringify(s.default_value);
}

export type BreakerState = "ok" | "tripped_alert" | "refusing";

/** An app's breaker: under its line, over it but only alerting, or refusing its calls (mode enforce). Pure. */
export function breakerState(v: Pick<AnomalyVerdict, "tripped" | "refuses">): BreakerState {
  if (!v.tripped) return "ok";
  return v.refuses ? "refusing" : "tripped_alert";
}

export function breakerTone(state: BreakerState): "success" | "warning" | "danger" {
  return state === "ok" ? "success" : state === "refusing" ? "danger" : "warning";
}

/** The share of the trip line spent today (0 to 100+), null without a line. Pure. */
export function breakerUsedPct(v: Pick<AnomalyVerdict, "spent_usd" | "threshold_usd">): number | null {
  return v.threshold_usd > 0 ? (100 * v.spent_usd) / v.threshold_usd : null;
}

export type AtCap = "stops" | "fails_over" | "alert_only";

/** What a connection at its monthly budget does: its own cost policy (hard_stop) and the environment's connection cap
 *  mode (Platform › Settings cis.llm.connection_cap_mode). An alert-only policy stays alert only in every mode. Pure. */
export function connectionAtCap(hardStop: boolean, mode: string | null | undefined): AtCap {
  if (!hardStop) return "alert_only";
  return mode === "failover_continue" ? "fails_over" : "stops";
}

/** Only an administrator who may write (and is not a tighten-only role) resets a breaker, and only a tripped one. */
export function mayReset(v: Pick<AnomalyVerdict, "tripped">, canReset: boolean, tightenOnly: boolean): boolean {
  return v.tripped && canReset && !tightenOnly;
}
