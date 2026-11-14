// frontend/lib/capTighten.ts — S280 (AA-1096): the client side of the S279 tighten-only rule and of the evidence a
// gated route change needs, for the cap / threshold editors outside Platform › Settings (AI & Spend › Budgets, central
// caps, Tenant 360 › Cost, app profiles, connections, route editors).
//
// Since S279 the API refuses any loosening from a role without settings.write (settings_registry.loosened, a 403
// "Your role may only tighten this cap…"). These helpers let an editor show what its role may not loosen before the
// API refuses it, start a new cap on the side the role may add, and read the refusal as the page's own sentence. The
// API stays the authority: everything here is a UI hint. Pure.

import type { AppProfile, BudgetDraft, CentralDraft, EvalRunRow, PageAccess } from "./aiGateway.ts";

/** GET /api/admin/ai-gateway/access with the S280 field: the role may only tighten caps (absent from an older API). */
export type PageAccessHint = PageAccess & { tighten_only?: boolean };

/** A new provider / job cap starts with the hard stop on: the API's own default for a provider row
 *  (admin_budgets.upsert_budget: hard_stop omitted = stop) and the only new cap a tighten-only role may add. */
export const NEW_BUDGET_DRAFT: Readonly<BudgetDraft> = { per_run: "", per_day: "", hard_stop: true, alert_pct: "80" };

/** A new central cap: alert only for the administrator (tech lead T-8: Dev caps never stop normal traffic), the hard
 *  stop on for a tighten-only role (settings_registry.loosened: a new row without the stop loosens). */
export function newCentralDraft(tightenOnly: boolean): CentralDraft {
  return { kind: "stage", target: "", per_run: "", per_day: "", per_month: "", hard_stop: tightenOnly, alert_pct: "80" };
}

/** True when any of the conditions holds (a Save button's "blocked"). */
export function anyOf(...conditions: unknown[]): boolean {
  return conditions.some(Boolean);
}

/** A tighten-only role's hint for a field (its current value as its limit), else the field's own hint. */
export function tightHint<V>(on: boolean, now: V | null | undefined, tight: (v: V) => string, dflt: string): string {
  return on && now != null ? tight(now) : dflt;
}

type Fields = Readonly<Record<string, unknown>>;

/** The field groups of `loosenedFields`: `caps` (null = no cap; removing it or a higher value loosens), `stops`
 *  (true is the strict side), `upIsLooser` (a percentage where a higher number acts later), `downIsLooser` (an alert
 *  floor where a lower number, or none, acts later). */
export interface LooseKinds {
  caps?: readonly string[];
  stops?: readonly string[];
  upIsLooser?: readonly string[];
  downIsLooser?: readonly string[];
}

const num = (v: unknown) => (v == null || v === "" ? null : Number(v));

function capsLoosened(before: Fields, after: Fields, fields: readonly string[]): string[] {
  return fields.filter((f) => {
    const old = num(before[f]);
    const now = num(after[f]);
    return old != null && (now == null || now > old);
  });
}

function upLoosened(before: Fields, after: Fields, fields: readonly string[]): string[] {
  return fields.filter((f) => {
    const old = num(before[f]);
    const now = num(after[f]);
    return old != null && now != null && now > old;
  });
}

function downLoosened(before: Fields, after: Fields, fields: readonly string[]): string[] {
  return fields.filter((f) => {
    const old = num(before[f]);
    const now = num(after[f]);
    return old != null && (now == null || now < old);
  });
}

/** The fields `after` loosens against `before` (null = no row yet): the client copy of the backend's
 *  settings_registry.loosened. No row before: only a stop flag left off counts. */
export function loosenedFields(beforeRow: object | null, afterRow: object, kinds: LooseKinds): string[] {
  const after = afterRow as Fields;
  const stops = kinds.stops ?? [];
  if (!beforeRow) return stops.filter((f) => after[f] === false);
  const before = beforeRow as Fields;
  return [
    ...capsLoosened(before, after, kinds.caps ?? []),
    ...stops.filter((f) => before[f] === true && after[f] === false),
    ...upLoosened(before, after, kinds.upIsLooser ?? []),
    ...downLoosened(before, after, kinds.downIsLooser ?? []),
  ];
}

/** The field groups of a spend cap (admin_budgets.upsert_budget's loosened call). */
export const SPEND_CAP_KINDS = {
  caps: ["per_run_usd", "per_day_usd", "per_month_usd"], stops: ["hard_stop"], upIsLooser: ["alert_pct"],
} as const;

/** The field groups of a tenant AI budget (tenant_ai_budget.upsert's loosened call). */
export const TENANT_BUDGET_KINDS = {
  caps: ["monthly_usd"], stops: ["hard_stop"], upIsLooser: ["alert_pct", "approval_pct"],
} as const;

/** The field groups of an app profile (admin_ai_gateway._check_profile_tighten_only over _profile_limits): AI off and
 *  the bulk pause are the strict sides, the default daily budget a cap. */
export const APP_PROFILE_KINDS = { caps: ["default_daily_budget_usd"], stops: ["ai_off", "bulk_paused"] } as const;

/** An app profile (or its PATCH body over it) as the backend's _profile_limits view. */
export function profileLimits(p: Pick<AppProfile, "ai_enabled" | "bulk_paused" | "default_daily_budget_usd">,
  body: Readonly<Record<string, unknown>> = {}): Record<string, unknown> {
  const pick = <K extends keyof typeof p>(k: K) => (k in body ? body[k] : p[k]) as (typeof p)[K];
  return { ai_off: !pick("ai_enabled"), bulk_paused: !!pick("bulk_paused"),
    default_daily_budget_usd: pick("default_daily_budget_usd") };
}

/** The field groups of a connection and its cost policy (llm_connections TIGHTEN_CAPS / _STOPS / _UP_IS_LOOSER /
 *  _DOWN_IS_LOOSER). */
export const CONNECTION_KINDS = {
  caps: ["rpm_limit", "max_concurrency", "monthly_budget_usd", "daily_credit_limit"], stops: ["budget_hard_stop"],
  upIsLooser: ["max_retries", "budget_alert_pct"], downIsLooser: ["low_balance_usd"],
} as const;

/** The limits of a connection form as llm_connections._refuse_loosening reads them (no policy row: the default alert
 *  percent, the stop on), with a PATCH body over them. Only the fields the settings form edits; timeout and notes are not limits. */
export function connectionLimits(c: {
  max_retries: number | null; max_concurrency: number | null; rpm_limit: number | null;
  monthly_budget_usd: number | null; cost_policy?: { alert_pct: number; hard_stop: boolean } | null;
}, defaultAlertPct: number, body: Readonly<Record<string, unknown>> = {}): Record<string, unknown> {
  const before: Record<string, unknown> = {
    max_retries: c.max_retries, max_concurrency: c.max_concurrency, rpm_limit: c.rpm_limit,
    monthly_budget_usd: c.monthly_budget_usd, budget_alert_pct: c.cost_policy?.alert_pct ?? defaultAlertPct,
    budget_hard_stop: c.cost_policy?.hard_stop ?? true,
  };
  for (const f of Object.keys(before)) if (f in body) before[f] = body[f];
  return before;
}

/** The backend's tighten-only refusals (403): settings_registry.TIGHTEN_ONLY ("Your role may only tighten this cap…")
 *  and admin.py's tenant-limit sentence ("Your role may only lower this limit…"). The page shows its own sentence. */
export const TIGHTEN_ONLY_REFUSAL = "Your role may only tighten this cap";
export const TIGHTEN_ONLY_REFUSALS: readonly string[] = [TIGHTEN_ONLY_REFUSAL, "Your role may only lower this limit"];

export function isTightenOnlyRefusal(e: unknown): boolean {
  if (!(e instanceof Error)) return false;
  const status = (e as Error & { status?: number }).status;
  return TIGHTEN_ONLY_REFUSALS.some((p) => e.message.startsWith(p)) && (status === undefined || status === 403);
}

/** The eval runs that can back a gated route change, newest first (the client copy of
 *  shared/llm_client/route_evidence.eval_run_problem: a writing-memory run must be finished with verdict "pass" and
 *  stop reason "completed"; any other run must not have failed; none may have detected a regression). The API still
 *  checks the run (it also refuses an INVALID run, which this list cannot see). */
export function qualifyingEvalRuns(rows: readonly EvalRunRow[]): EvalRunRow[] {
  return rows
    .filter((r) => !r.regression_detected)
    .filter((r) => (r.pipeline === "writing_memory"
      ? r.finished_at != null && r.verdict === "pass" && r.stop_reason === "completed"
      : r.verdict !== "fail" && r.verdict !== "invalid"))
    .slice()
    .sort((a, b) => (b.finished_at ?? b.created_at ?? "").localeCompare(a.finished_at ?? a.created_at ?? ""));
}
