// app/admin/tenants/_components/tenant360.ts
// AA-766 — shared types, formatters and validation for the Tenant 360 tabs (Settings, Cost,
// Quality, Markets). All data comes from admin-only routes (/admin/tenants/{id}/...): cost, gate
// verdicts and research coverage are never part of a tenant API response (AA-613).

export const usd = (v: number | null | undefined, digits = 2) =>
  v == null ? "—" : `$${v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;

export const pct = (ratio: number | null | undefined, digits = 0) =>
  ratio == null ? "—" : `${(ratio * 100).toFixed(digits)}%`;

// ── Settings ──────────────────────────────────────────────────────────────────

/** Mirrors api/routers/admin.py _VALID_CHANNELS (services/acp_planning/models.py Channel). */
export const PLANNING_CHANNELS = [
  "blog", "facebook", "instagram", "tiktok", "linkedin", "email", "landing_page", "ads",
] as const;

/** Markets the SEO research pipeline supports today (seed_builder.DFS_LOCATION_MAP keys). */
export const SEO_SUPPORTED_MARKETS = ["US", "UK", "AU", "DE", "FR", "NL"];

/** Mirrors api/routers/admin.py RATE_LIMIT_RPM_MAX (a typo guard, not a commercial limit). */
export const RATE_LIMIT_RPM_MAX = 10000;

export interface TenantConfig {
  tenant_id: string;
  markets: string[];
  channels: string[];
  posts_per_week: number;
}

/** "us, de ,UK" -> ["US", "DE", "UK"] (trimmed, upper-cased, de-duplicated, order kept). */
export function parseMarkets(input: string): string[] {
  const out: string[] = [];
  for (const raw of input.split(/[,\s]+/)) {
    const m = raw.trim().toUpperCase();
    if (m && !out.includes(m)) out.push(m);
  }
  return out;
}

/** Field errors for the planning-config form; an empty object means valid. */
export function validateConfig(markets: string[], channels: string[], postsPerWeek: string): Record<string, string> {
  const errors: Record<string, string> = {};
  if (markets.length === 0) errors.markets = "At least one market is required.";
  else {
    const bad = markets.filter(m => !/^[A-Z]{2}$/.test(m));
    if (bad.length) errors.markets = `Use 2-letter country codes: ${bad.join(", ")}`;
  }
  if (channels.length === 0) errors.channels = "Pick at least one channel.";
  const n = Number(postsPerWeek);
  if (!Number.isInteger(n) || n < 1 || n > 14) errors.posts_per_week = "Posts/week must be an integer from 1 to 14.";
  return errors;
}

export function validateRpm(rpm: string): string | null {
  const n = Number(rpm);
  if (!Number.isInteger(n) || n < 1 || n > RATE_LIMIT_RPM_MAX) return `Rate limit must be an integer from 1 to ${RATE_LIMIT_RPM_MAX}.`;
  return null;
}

/** AA-819 — mirrors the code default in api/routers/v1_publish.py publish() (used while
 *  shared.tenants.daily_publish_limit is NULL, or before migration 230 is applied). */
export const DAILY_PUBLISH_LIMIT_DEFAULT = 20;
/** AA-819 — a typo guard for the override, not a commercial limit. */
export const DAILY_PUBLISH_LIMIT_MAX = 1000;

/** "" means "no override" (send null → the code default applies). */
export function validateDailyPublishLimit(value: string): string | null {
  if (value.trim() === "") return null;
  const n = Number(value);
  if (!Number.isInteger(n) || n < 1 || n > DAILY_PUBLISH_LIMIT_MAX) {
    return `Daily publish limit must be an integer from 1 to ${DAILY_PUBLISH_LIMIT_MAX}, or empty for the default.`;
  }
  return null;
}

/** Form text -> PATCH value: "" -> null (clear the override), "15" -> 15. Call only when valid. */
export function parseDailyPublishLimit(value: string): number | null {
  return value.trim() === "" ? null : Number(value);
}

/** "20/day (default)" when there is no override, "5/day" when there is. */
export function fmtDailyPublishLimit(limit: number | null | undefined): string {
  return limit == null ? `${DAILY_PUBLISH_LIMIT_DEFAULT}/day (default)` : `${limit}/day`;
}

// ── Cost ──────────────────────────────────────────────────────────────────────

export interface TenantCost {
  tenant_id: string;
  billing_month: string | null;
  total_cost_usd: number;
  by_stage: { stage: string; calls: number; cost_usd: number }[];
  by_channel: { channel: string; kind: string; pieces: number; cost_usd: number; cost_per_piece_usd: number | null }[];
  by_kind: { kind: string; pieces: number; cost_usd: number; cost_per_piece_usd: number | null }[];
  unattributed_to_piece_usd: number;
  per_tour: { t2_cost_usd: number; tours_rewritten: number; cost_per_tour_usd: number | null };
  margin: {
    plan_attached: boolean; plan_price_usd: number | null; overage_usd: number | null;
    revenue_usd: number | null; llm_cost_usd: number; margin_usd: number | null;
    margin_pct: number | null; revenue_basis: string;
  };
  definition: string;
}

// ── Quality ───────────────────────────────────────────────────────────────────

export interface TenantQuality {
  tenant_id: string;
  window_days: number;
  pieces: { by_status: Record<string, number>; total: number; shipped: number; held_rate: number | null };
  gates: {
    gate: string; evaluated: number; passed: number; failed_blocking: number;
    failed_warn: number; held_pieces: number; pass_rate: number | null;
  }[];
  held_reasons: { held_reason: string; count: number }[];
  edits: {
    ai_versions: number; tenant_edits: number; tours_generated: number; tours_edited: number;
    edit_rate: number | null; edits_per_version: number | null;
  };
}

// ── Markets ───────────────────────────────────────────────────────────────────

export interface TenantMarkets {
  tenant_id: string;
  fresh_days: number;
  seo: {
    declared: string[]; countries: string[]; markets: string[]; unsupported: string[];
    defaulted_to_us: boolean; configured: boolean;
  };
  planning_markets: string[] | null;
  coverage: {
    market: string; places_in_scope: number; researched: number; fresh: number;
    fresh_pct: number | null; last_researched: string | null;
  }[];
  unmapped_requests: { country_code: string; first_seen_at: string | null; requested_at: string | null; resolved_at: string | null }[];
}
