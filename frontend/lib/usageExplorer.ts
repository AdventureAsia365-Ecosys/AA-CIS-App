// frontend/lib/usageExplorer.ts — AA-1045: the usage explorer page's pure helpers (no React): the address keys, the API
// paths, the drill-down link to the AI gateway Calls tab, the CSV rows and the number formats. Unit-tested in
// lib/usageExplorer.test.mjs. Backend: api/services/usage_explorer.py (GET /admin/usage-explorer/*).

import { CALL_PROVIDERS, PAGE_PATH as AI_GATEWAY_PATH } from "./aiGateway.ts";

export const API = "/api/admin/usage-explorer";
export const PAGE_PATH = "/admin/usage";

export const RANGES = ["today", "24h", "7d", "30d", "this_month", "last_month", "custom"] as const;
export type RangeKey = (typeof RANGES)[number];
export const GRANULARITIES = ["hour", "day", "week", "month"] as const;
export type Granularity = (typeof GRANULARITIES)[number];
/** Every dimension (api/services/usage_dims.py DIMENSIONS); `time` is groupable too. */
export const DIMENSIONS = ["source", "app", "product", "provider", "account", "model", "family", "stage", "role",
  "tenant", "status", "error_class", "traffic"] as const;
export type Dimension = (typeof DIMENSIONS)[number];
export type GroupKey = Dimension | "time";
export const METRICS = ["requests", "tokens_in", "tokens_out", "cache_read_tokens", "cache_write_tokens",
  "reasoning_tokens", "cost_usd", "avg_latency_ms", "p95_latency_ms", "error_rate", "fallback_rate", "cost_per_tour",
  "cost_per_piece"] as const;
export type Metric = (typeof METRICS)[number];
export const NONE = "(none)";
export const DEFAULTS = { range: "7d", granularity: "day", group: "provider", sort: "cost_usd", dir: "desc" } as const;

/** The address keys of the explorer (also the saved view's query and the API query). */
export const QUERY_KEYS = ["range", "from", "to", "granularity", "group", "sort", "dir", "per_unit",
  ...DIMENSIONS.map((d) => `f_${d}`)] as const;

/** A time bucket; the API clips start / end to the query range (partial: the range cuts the bucket). */
export interface Bucket { start: string; end: string; label: string; partial?: boolean }

export interface MetricsRow {
  requests: number;
  ledger_rows: number;
  tokens_in: number;
  tokens_out: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  reasoning_tokens: number;
  cost_usd: number;
  avg_latency_ms: number | null;
  p95_latency_ms: number | null;
  error_rate: number | null;
  fallback_rate: number | null;
  cost_per_tour: number | null;
  cost_per_piece: number | null;
  tours?: number;
  pieces?: number;
}

export interface ExplorerRow extends MetricsRow {
  key: string;
  dims: Partial<Record<GroupKey, string>>;
  bucket?: Bucket;
}

export interface SeriesPoint extends Bucket {
  requests: number;
  tokens: number;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
}

export interface ExplorerResp {
  generated_at: string;
  timezone: string;
  range: { key: RangeKey; from: string; to: string; from_utc: string; to_utc: string };
  granularity: Granularity;
  group: GroupKey[];
  filters: Partial<Record<Dimension, string[]>>;
  sort: { key: string; dir: "asc" | "desc" };
  rows: ExplorerRow[];
  truncated: boolean;
  groups: number;
  series: SeriesPoint[];
  totals: MetricsRow;
  labels: { tenant: Record<string, string> };
  crm: { status: string; reason?: string | null; rows?: number };
  rollup: { job_id: string; started_at: string | null; finished_at: string | null; from?: string; to?: string } | null;
  notes: string[];
}

export interface OptionsResp {
  range: { from: string; to: string };
  values: Partial<Record<Dimension, string[]>>;
  labels: { tenant: Record<string, string> };
}

export interface SavedView { id: string; name: string; query: Record<string, string>; created_at: string;
  updated_at: string }

export interface InvoiceRow {
  provider: string;
  account: string | null;
  requests: number;
  ledger_usd: number;
  provider_usd: number | null;
  provider_days: number;
  provider_source: string[];
  invoice_usd: number | null;
}

export interface InvoicesResp { month: string; from: string; to: string; rows: InvoiceRow[]; missing: string }

/** The explorer's query from the address (`get` returns "" for a missing key): only the keys that are set. */
export function readQuery(get: (key: string) => string): Record<string, string> {
  const q: Record<string, string> = {};
  for (const k of QUERY_KEYS) {
    const v = get(k).trim();
    if (v) q[k] = v;
  }
  return q;
}

/** The group keys of a query (default provider), at most two. */
export function groupOf(q: Record<string, string>): GroupKey[] {
  const raw = (q.group || DEFAULTS.group).split(",").map((s) => s.trim()).filter(Boolean);
  return [...new Set(raw)].slice(0, 2) as GroupKey[];
}

/** The filter values of one dimension. */
export function filterValues(q: Record<string, string>, d: Dimension): string[] {
  return (q[`f_${d}`] || "").split(",").map((s) => s.trim()).filter(Boolean);
}

/** The new address value of a filter after toggling one value (null = no filter). */
export function toggleValue(q: Record<string, string>, d: Dimension, value: string): string | null {
  const now = filterValues(q, d);
  const next = now.includes(value) ? now.filter((v) => v !== value) : [...now, value];
  return next.length ? next.join(",") : null;
}

function qs(q: Record<string, string>, keys?: readonly string[]): string {
  const p = new URLSearchParams();
  for (const k of keys ?? QUERY_KEYS) if (q[k]) p.set(k, q[k]);
  return p.toString();
}

export function queryPath(q: Record<string, string>): string {
  const s = qs(q);
  return s ? `${API}/query?${s}` : `${API}/query`;
}

export function optionsPath(q: Record<string, string>): string {
  const s = qs(q, ["range", "from", "to"]);
  return s ? `${API}/options?${s}` : `${API}/options`;
}

export function invoicesPath(month?: string): string {
  return month ? `${API}/invoices?month=${encodeURIComponent(month)}` : `${API}/invoices`;
}

export const VIEWS_API = `${API}/views`;

/** The page address of a saved view's query. */
export function viewHref(query: Record<string, string>): string {
  const s = qs(query);
  return s ? `${PAGE_PATH}?${s}` : PAGE_PATH;
}

// Explorer dimension -> Calls filter key (lib/aiGateway CALL_PARAM keys, without the `c_` prefix). family and traffic
// have none: the Calls list cannot narrow on them.
const CALL_KEY: Partial<Record<Dimension, string>> = {
  source: "source", app: "app", provider: "provider", stage: "stage", status: "status", tenant: "tenant",
  model: "model", account: "account", role: "role", error_class: "error_class", product: "product",
};
// The Calls filters that take "(none)" (rows without a value).
const CALL_NONE_OK = new Set(["tenant", "model", "account", "role", "error_class"]);
const CALL_PREFIX = "c_";

const later = (a: string, b: string) => (Date.parse(a) >= Date.parse(b) ? a : b);
const earlier = (a: string, b: string) => (Date.parse(a) <= Date.parse(b) ? a : b);

export interface DrillLink {
  href: string;
  /** dimensions the Calls list cannot narrow on (it then shows a wider set) */
  lost: string[];
}

/**
 * The Calls tab link of one explorer cell: the cell's time range (its bucket clipped to the query's range, else the
 * query's range), the cell's dimension values and every single-valued filter. A dimension the Calls list cannot carry
 * (family, traffic, a multi-valued filter, an empty value it has no "(none)" for, a provider that is not a
 * spend-budget provider) is listed in `lost`.
 */
export function drillHref(
  range: { from: string; to: string },
  dims: Partial<Record<GroupKey, string>>,
  filters: Partial<Record<Dimension, string[]>>,
  bucket?: Bucket,
): DrillLink {
  const p = new URLSearchParams({ tab: "calls" });
  // The cell counted rows inside the range only: a day / week / month bucket the range cuts must not widen the list.
  p.set(`${CALL_PREFIX}since`, bucket ? later(bucket.start, range.from) : range.from);
  p.set(`${CALL_PREFIX}until`, bucket ? earlier(bucket.end, range.to) : range.to);
  const lost: string[] = [];
  const fixed: Partial<Record<Dimension, string>> = {};
  for (const [d, v] of Object.entries(dims)) if (d !== "time") fixed[d as Dimension] = v ?? "";
  for (const d of DIMENSIONS) {
    if (d in fixed) continue;
    const vals = filters[d] ?? [];
    if (vals.length === 1) fixed[d] = vals[0] === NONE ? "" : vals[0];
    else if (vals.length > 1) lost.push(d);
  }
  for (const [d, raw] of Object.entries(fixed) as [Dimension, string][]) {
    const key = CALL_KEY[d];
    const value = raw === NONE ? "" : raw;
    if (!key) { lost.push(d); continue; }
    if (value === "") {
      if (CALL_NONE_OK.has(key)) p.set(CALL_PREFIX + key, NONE);
      else lost.push(d);
      continue;
    }
    if (d === "provider" && !(CALL_PROVIDERS as readonly string[]).includes(value)) { lost.push(d); continue; }
    if (d === "source" && value === "crm") { p.set(`${CALL_PREFIX}source`, "crm"); continue; }
    p.set(CALL_PREFIX + key, value);
  }
  return { href: `${AI_GATEWAY_PATH}?${p.toString()}`, lost: [...new Set(lost)] };
}

/** The CSV columns of the table: the grouped dimensions (tenant with its name), then every metric (raw numbers). */
export function csvColumns(group: GroupKey[], tenantNames: Record<string, string>):
  { header: string; value: (r: ExplorerRow) => unknown }[] {
  const dims = group.flatMap((g) => {
    const col = { header: g, value: (r: ExplorerRow) => r.dims[g] ?? "" };
    return g === "tenant"
      ? [col, { header: "tenant_name", value: (r: ExplorerRow) => tenantNames[r.dims.tenant ?? ""] ?? "" }]
      : [col];
  });
  return [...dims, ...METRICS.map((m) => ({ header: m, value: (r: ExplorerRow) => r[m] ?? "" }))];
}

/** The CSV file name of a view: usage-<range or dates>-<group>.csv */
export function csvName(q: Record<string, string>): string {
  const range = q.range === "custom" ? `${q.from ?? ""}_${q.to ?? ""}` : (q.range || DEFAULTS.range);
  return `usage-${range}-${groupOf(q).join("-")}.csv`;
}

export function fmtInt(n: number | null | undefined): string {
  return n == null ? "—" : Math.round(n).toLocaleString("en-GB");
}

export function fmtUsd(n: number | null | undefined): string {
  if (n == null) return "—";
  const digits = Math.abs(n) >= 100 ? 2 : Math.abs(n) >= 1 ? 3 : 4;
  return `$${n.toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: digits })}`;
}

export function fmtPct(n: number | null | undefined): string {
  return n == null ? "—" : `${(n * 100).toLocaleString("en-GB", { maximumFractionDigits: 1 })}%`;
}

export function fmtMs(n: number | null | undefined): string {
  if (n == null) return "—";
  return n >= 10000 ? `${(n / 1000).toLocaleString("en-GB", { maximumFractionDigits: 1 })} s` : `${Math.round(n)} ms`;
}

/** The text of one metric value. */
export function fmtMetric(m: Metric, v: number | null | undefined): string {
  if (m === "cost_usd" || m === "cost_per_tour" || m === "cost_per_piece") return fmtUsd(v);
  if (m === "error_rate" || m === "fallback_rate") return fmtPct(v);
  if (m === "avg_latency_ms" || m === "p95_latency_ms") return fmtMs(v);
  return fmtInt(v);
}

export const TIME_ZONE = "Asia/Ho_Chi_Minh";

/** An instant in Vietnam time (the page shows every time in ICT; the data stays UTC): "4 Nov 2026, 09:00 ICT". */
export function fmtIct(iso: string | null | undefined, withTime = true): string {
  if (!iso) return "—";
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return "—";
  const text = t.toLocaleString("en-GB", {
    timeZone: TIME_ZONE, day: "numeric", month: "short", year: "numeric",
    ...(withTime ? { hour: "2-digit", minute: "2-digit", hour12: false } : {}),
  });
  return withTime ? `${text} ICT` : text;
}

/** The last six UTC months as YYYY-MM, newest first (the invoice card's month list). */
export function lastMonths(now: Date = new Date(), n = 6): string[] {
  const out: string[] = [];
  for (let i = 0; i < n; i++) {
    const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - i, 1));
    out.push(`${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`);
  }
  return out;
}
