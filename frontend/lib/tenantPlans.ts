// lib/tenantPlans.ts — AA-1028 R1 slice 3 (root ADR 0019 d2): the plan catalogue (Tenants › Plans & add-ons) and the
// Tenant 360 › Subscription tab. Pure parts (types, drafts, validation, labels) so node --test can load them, plus the
// BFF helpers that keep the API's refusal code (the backend answers {detail: {detail: code, message}}).
//
// Backend: api/routers/admin_plans.py (GET/POST /admin/plans, PATCH /admin/plans/{key}, POST …/retire, GET
// /admin/plans/features), api/routers/admin_tenant_subscription.py (GET /admin/tenants/{id}/subscription, PUT/DELETE
// …/entitlement-overrides/{key}) and the tenant_provision saga (POST /admin/tenants/{id}/provision[/cancel], GET
// /admin/tenants/provision/{job}). Apps read the resolved entitlements, never a plan name; the UI shows the keys with
// their catalogue description.

export type Product = "content" | "crm" | "tripplanner";
export type FeatureProduct = Product | "platform";
export type ValueKind = "flag" | "limit" | "seats" | "policy";

export interface Feature {
  feature_key: string;
  product: FeatureProduct;
  value_kind: ValueKind;
  unit: string | null;
  description: string;
}

export interface PlanFeatureRow { feature_key: string; limit_value: string | null }

export interface Plan {
  plan_key: string;
  product: Product;
  name: string;
  tier: string | null;
  price_usd_monthly: string | null;
  price_unit: "tenant" | "seat" | "site";
  seats_included: number | null;
  price_extra_seat_usd: string | null;
  is_public: boolean;
  version: number;
  notes: string | null;
  updated_at: string | null;
  updated_by: string;
  retired_at: string | null;
  live_subscriptions: number;
  features: PlanFeatureRow[];
}

export interface PlansResp { plans: Plan[]; assignable_tiers: string[] }

export const PRODUCTS: Product[] = ["content", "crm", "tripplanner"];
export const TIERS = ["starter", "growth", "business", "enterprise", "internal"] as const;
export const PLANS_API = "/api/admin/plans";
export const FEATURES_API = "/api/admin/plans/features";
export const PLANS_KEY = ["admin-plans"] as const;
export const DEFAULT_UNIT: Record<Product, Plan["price_unit"]> = { content: "tenant", crm: "seat", tripplanner: "site" };

const DECIMAL = /^\d{1,12}(\.\d{1,2})?$/;
const PLAN_KEY = /^[a-z]+\.[a-z][a-z0-9_]*$/;

/** The content tiers staff may put a tenant on: the live public content plans with a tier, cheapest first, plus the
 * tenant's current tier (so a tenant on a hidden tier still shows its own). Replaces the UI's PLAN_OPTIONS. */
export function tierChoices(plans: Plan[] | undefined, current?: string | null): string[] {
  const out = (plans ?? [])
    .filter((p) => p.product === "content" && p.tier && !p.retired_at && p.is_public)
    .sort((a, b) => Number(a.price_usd_monthly ?? Infinity) - Number(b.price_usd_monthly ?? Infinity))
    .map((p) => p.tier as string);
  if (current && !out.includes(current)) out.unshift(current);
  return out;
}

/** The features a plan of `product` may give: the product's keys, then the platform keys. */
export function featuresFor(product: Product, catalogue: Feature[]): Feature[] {
  return catalogue.filter((f) => f.product === product).concat(catalogue.filter((f) => f.product === "platform"));
}

// ── Plan drafts ───────────────────────────────────────────────────────────────────────────────────────────────

export interface PlanDraft {
  plan_key: string;
  product: Product;
  tier: string;               // "" = none
  name: string;
  price: string;              // "" = no price
  price_unit: Plan["price_unit"];
  seats_included: string;
  price_extra_seat: string;
  is_public: boolean;
  notes: string;
  /** feature_key -> "" (on, no limit) | a decimal; a key left out is off. */
  features: Record<string, string>;
}

export function planDraftOf(plan: Plan | null, product: Product = "content"): PlanDraft {
  if (!plan) {
    return { plan_key: `${product}.`, product, tier: "", name: "", price: "", price_unit: DEFAULT_UNIT[product],
             seats_included: "", price_extra_seat: "", is_public: false, notes: "", features: {} };
  }
  return {
    plan_key: plan.plan_key, product: plan.product, tier: plan.tier ?? "", name: plan.name,
    price: plan.price_usd_monthly ?? "", price_unit: plan.price_unit,
    seats_included: plan.seats_included == null ? "" : String(plan.seats_included),
    price_extra_seat: plan.price_extra_seat_usd ?? "", is_public: plan.is_public, notes: plan.notes ?? "",
    features: Object.fromEntries(plan.features.map((f) => [f.feature_key, f.limit_value ?? ""])),
  };
}

export type PlanDraftError =
  | "key" | "name" | "price" | "seats" | "extraSeat" | "notes" | "limit" | "rpmRequired";

export interface PlanBody {
  plan_key?: string; product?: Product; tier?: string | null; version?: number;
  name: string; price_usd_monthly: string | null; price_unit: Plan["price_unit"]; seats_included: number | null;
  price_extra_seat_usd: string | null; is_public: boolean; notes: string | null;
  features: { feature_key: string; limit_value: string | null }[];
}

/** The request body of a create (``plan`` null) or an edit, or the first error (field) the API would refuse. */
export function planBody(d: PlanDraft, catalogue: Feature[], plan: Plan | null):
    { body: PlanBody | null; error: PlanDraftError | null; field?: string } {
  if (!plan && (!PLAN_KEY.test(d.plan_key) || d.plan_key.split(".")[0] !== d.product || d.plan_key.length > 64)) {
    return { body: null, error: "key" };
  }
  const name = d.name.trim();
  if (name.length < 1 || name.length > 100) return { body: null, error: "name" };
  const price = d.price.trim();
  if (price && !DECIMAL.test(price)) return { body: null, error: "price" };
  const seats = d.seats_included.trim();
  if (seats && (!/^\d{1,6}$/.test(seats) || Number(seats) > 100000)) return { body: null, error: "seats" };
  const extra = d.price_extra_seat.trim();
  if (extra && !DECIMAL.test(extra)) return { body: null, error: "extraSeat" };
  if (d.notes.length > 1000) return { body: null, error: "notes" };
  const kinds = new Map(catalogue.map((f) => [f.feature_key, f.value_kind]));
  const features: PlanBody["features"] = [];
  for (const [key, raw] of Object.entries(d.features).sort(([a], [b]) => a.localeCompare(b))) {
    const kind = kinds.get(key);
    if (!kind) continue;                                  // a retired key is dropped
    const v = raw.trim();
    if (kind !== "flag" && v && !DECIMAL.test(v)) return { body: null, error: "limit", field: key };
    features.push({ feature_key: key, limit_value: kind === "flag" || !v ? null : v });
  }
  const tier = plan ? plan.tier : (d.product === "content" && d.tier ? d.tier : null);
  if (tier) {
    const rpm = features.find((f) => f.feature_key === "content.rate_limit_rpm");
    if (!rpm || rpm.limit_value == null || Number(rpm.limit_value) < 1) {
      return { body: null, error: "rpmRequired", field: "content.rate_limit_rpm" };
    }
  }
  const body: PlanBody = {
    name, price_usd_monthly: price || null, price_unit: d.price_unit, seats_included: seats ? Number(seats) : null,
    price_extra_seat_usd: extra || null, is_public: d.is_public, notes: d.notes.trim() || null, features,
  };
  if (plan) body.version = plan.version;
  else Object.assign(body, { plan_key: d.plan_key, product: d.product, tier });
  return { body, error: null };
}

// ── Subscription tab ──────────────────────────────────────────────────────────────────────────────────────────

export interface SubscriptionRow {
  product: Product; plan_key: string; plan_name: string; status: string; seats: number | null;
  trial_ends_at: string | null; paid_through: string | null; started_at: string | null;
  canceled_at: string | null; updated_at: string | null; updated_by: string; plan_retired: boolean;
}
export interface EntitlementRow {
  feature_key: string; product: FeatureProduct; value_kind: ValueKind; unit: string | null;
  limit_value: string | null; source: "plan" | "trial" | "override"; plan_key: string | null;
  expires_at: string | null; description: string;
}
export interface OverrideRow {
  feature_key: string; product: FeatureProduct; value_kind: ValueKind; limit_value: string | null; enabled: boolean;
  reason: string; granted_by: string; expires_at: string | null; updated_at: string | null; active: boolean;
}
export interface SubscriptionResp {
  tenant: { tenant_id: string; name: string; plan_tier: string; account_status: string;
            suspend_reason: string | null; kind: string; is_pilot: boolean; pilot_ends_at: string | null;
            in_service: boolean };
  subscriptions: SubscriptionRow[];
  entitlements: EntitlementRow[];
  overrides: OverrideRow[];
  add_on_plans: { plan_key: string; product: Product; name: string; seats_included: number | null }[];
  override_rules?: OverrideRules;
}

/** What an override may set (the API refuses the rest, services/tenant_account/subscription.py): keys another column
 * enforces (the request rate: the tenant's settings), and keys whose limit must be a number. "Off" is for on/off
 * features only: for a limit it would remove the limit, so 0 is the way to stop it. */
export interface OverrideRules { not_overridable: string[]; limit_required: string[] }
export const DEFAULT_OVERRIDE_RULES: OverrideRules = {
  not_overridable: ["content.rate_limit_rpm"], limit_required: ["content.rewrite"],
};

/** The features the override drawer offers. */
export function overrideFeatures(catalogue: Feature[], rules: OverrideRules = DEFAULT_OVERRIDE_RULES): Feature[] {
  return catalogue.filter((f) => !rules.not_overridable.includes(f.feature_key));
}

/** The effects the drawer offers for a feature: on/off for a flag; a limit (and "no limit" unless one is required). */
export function overrideModes(f: Feature | undefined, rules: OverrideRules = DEFAULT_OVERRIDE_RULES):
    OverrideDraft["mode"][] {
  if (!f || f.value_kind === "flag") return ["off", "on"];
  return rules.limit_required.includes(f.feature_key) ? ["limit"] : ["limit", "on"];
}

/** The effect a new override of ``f`` starts with. */
export function defaultOverrideMode(f: Feature | undefined): OverrideDraft["mode"] {
  return !f || f.value_kind === "flag" ? "off" : "limit";
}

export const subscriptionPath = (tenantId: string) => `/api/admin/tenants/${tenantId}/subscription`;
export const overridePath = (tenantId: string, key: string) =>
  `/api/admin/tenants/${tenantId}/entitlement-overrides/${encodeURIComponent(key)}`;
export const subscriptionKey = (tenantId: string) => ["tenant360", tenantId, "subscription"] as const;

/** Live = the account and the products the tenant uses now. */
export const LIVE_STATUSES = ["trialing", "active", "past_due"];

export function statusTone(status: string): "success" | "warning" | "danger" | "info" | "neutral" {
  if (status === "active") return "success";
  if (status === "trialing" || status === "trial") return "info";
  if (status === "past_due" || status === "suspended" || status === "paused" || status === "unpaid") return "warning";
  if (status === "canceled" || status === "offboarding" || status === "closed") return "danger";
  return "neutral";
}

export function sourceTone(source: EntitlementRow["source"]): "neutral" | "info" | "purple" {
  return source === "override" ? "purple" : source === "trial" ? "info" : "neutral";
}

/** "200 a month", "USD 15.00 a month", "300 a minute", "4 seats", "7 days", "on" / "no limit". */
export function limitText(kind: ValueKind, unit: string | null, limit: string | null,
                          w: { on: string; noLimit: string; perMonth: (v: string) => string;
                               usdMonth: (v: string) => string; perMinute: (v: string) => string;
                               seats: (v: string) => string; days: (v: string) => string }): string {
  if (kind === "flag") return w.on;
  if (limit == null) return w.noLimit;
  const n = Number(limit);
  const v = Number.isInteger(n) ? n.toLocaleString("en-US") : n.toLocaleString("en-US", { minimumFractionDigits: 2 });
  switch (unit) {
    case "usd_month": return w.usdMonth(n.toLocaleString("en-US", { minimumFractionDigits: 2 }));
    case "rpm": return w.perMinute(v);
    case "seats": return w.seats(v);
    case "days": return w.days(v);
    default: return w.perMonth(v);
  }
}

// ── Override drafts ───────────────────────────────────────────────────────────────────────────────────────────

export interface OverrideDraft { feature_key: string; mode: "off" | "on" | "limit"; limit: string; reason: string;
                                 expires: string }

export function overrideDraftOf(o: OverrideRow | null, key = ""): OverrideDraft {
  if (!o) return { feature_key: key, mode: "off", limit: "", reason: "", expires: "" };
  return { feature_key: o.feature_key, mode: !o.enabled ? "off" : o.limit_value == null ? "on" : "limit",
           limit: o.limit_value ?? "", reason: o.reason, expires: o.expires_at ? o.expires_at.slice(0, 10) : "" };
}

export type OverrideDraftError = "feature" | "limit" | "reason" | "expires" | "flagLimit" | "offLimit"
  | "limitRequired" | "notOverridable";

/** The PUT body, or the first error. ``expires`` is a date (YYYY-MM-DD, end of that day UTC); ``today`` for tests. */
export function overrideBody(d: OverrideDraft, catalogue: Feature[], today = new Date().toISOString().slice(0, 10),
                             rules: OverrideRules = DEFAULT_OVERRIDE_RULES):
    { body: { enabled: boolean; limit_value: string | null; reason: string; expires_at: string | null } | null;
      error: OverrideDraftError | null } {
  const f = catalogue.find((x) => x.feature_key === d.feature_key);
  if (!f) return { body: null, error: "feature" };
  if (rules.not_overridable.includes(f.feature_key)) return { body: null, error: "notOverridable" };
  if (d.mode === "limit" && f.value_kind === "flag") return { body: null, error: "flagLimit" };
  if (d.mode === "off" && f.value_kind !== "flag") return { body: null, error: "offLimit" };
  const limit = d.limit.trim();
  if (d.mode === "limit" && limit && !DECIMAL.test(limit)) return { body: null, error: "limit" };
  if (rules.limit_required.includes(f.feature_key) && (d.mode !== "limit" || !limit)) {
    return { body: null, error: "limitRequired" };
  }
  const reason = d.reason.trim();
  if (reason.length < 1 || reason.length > 500) return { body: null, error: "reason" };
  if (d.expires && (!/^\d{4}-\d{2}-\d{2}$/.test(d.expires) || d.expires < today)) return { body: null, error: "expires" };
  return {
    body: { enabled: d.mode !== "off", limit_value: d.mode === "limit" && limit ? limit : null, reason,
            expires_at: d.expires ? `${d.expires}T23:59:59Z` : null },
    error: null,
  };
}

// ── BFF calls that keep the refusal code ──────────────────────────────────────────────────────────────────────

export class PlanApiError extends Error {
  code: string;
  status: number;
  constructor(message: string, code: string, status: number) {
    super(message);
    this.name = "PlanApiError";
    this.code = code;
    this.status = status;
  }
}

/** The message and code of an API refusal body (FastAPI 422 lists, our {detail: {detail, message}}, a string). */
export function refusalOf(body: unknown, status: number): { code: string; message: string } {
  const d = (body as { detail?: unknown } | null)?.detail;
  if (d && typeof d === "object" && !Array.isArray(d)) {
    const o = d as { detail?: unknown; message?: unknown };
    return { code: String(o.detail ?? `http_${status}`), message: String(o.message ?? o.detail ?? "") };
  }
  if (Array.isArray(d)) {
    const first = d[0] as { msg?: string; loc?: unknown[] } | undefined;
    return { code: "invalid_request", message: first ? `${(first.loc ?? []).slice(1).join(".")}: ${first.msg}` : "" };
  }
  return { code: `http_${status}`, message: typeof d === "string" ? d : `Request failed (${status})` };
}

export async function sendJson<T>(path: string, method: string, body?: unknown): Promise<T> {
  const res = await fetch(path, { method, headers: { "Content-Type": "application/json" },
                                  body: body === undefined ? undefined : JSON.stringify(body) });
  let parsed: unknown = null;
  try { parsed = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const r = refusalOf(parsed, res.status);
    throw new PlanApiError(r.message || r.code, r.code, res.status);
  }
  return parsed as T;
}

export interface ProvisionJob { job_id: string; status: string; steps: string[]; error: string | null;
                                result: Record<string, unknown> | null }

export function newRequestKey(prefix = "admin-ui"): string {
  const rnd = typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID() : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
  return `${prefix}-${rnd}`.slice(0, 64);
}

const TERMINAL = new Set(["succeeded", "failed", "cancelled", "dead", "stopped_budget"]);

/** Enqueue a saga request and wait for its job (at most ``timeoutMs``); returns the last job state. */
export async function runSaga(path: string, body: unknown, { timeoutMs = 30000, everyMs = 1000,
                              sleep = (ms: number) => new Promise((r) => setTimeout(r, ms)) } = {}): Promise<ProvisionJob> {
  let job = await sendJson<ProvisionJob>(path, "POST", body);
  const until = Date.now() + timeoutMs;
  while (!TERMINAL.has(job.status) && Date.now() < until) {
    await sleep(everyMs);
    job = await sendJson<ProvisionJob>(`/api/admin/tenants/provision/${job.job_id}`, "GET");
  }
  return job;
}

export function sagaFailed(job: ProvisionJob): boolean {
  return job.status !== "succeeded";
}
