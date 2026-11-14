// lib/routeTrace.ts — AA-1042 (S268): the AI gateway Trace tab (app/admin/ai-gateway/TraceTab.tsx): how each stage's
// route behaved. Types and pure helpers of the backend api/routers/admin_llm_route.py:
//   GET  /admin/ai-gateway/route-trace/requests            one line per gateway request (route trace)
//   GET  /admin/ai-gateway/route-trace/requests/{id}?at=   every attempt of one request
//   GET  /admin/ai-gateway/route-trace/rungs               per stage, the share of answers per rung
//   GET  /admin/ai-gateway/route-trace/fallback-content    A1 tour versions a fallback model wrote (marked / held)
//   GET  /admin/content-quality/fallback-writes            tenant T2 versions / T9 pieces a fallback model wrote
//                                                          (Tenant QA data: tenant_content.read only)
//   POST /admin/ai-gateway/route-trace/test                the route test (a few tiny real requests)
//   GET / PUT / DELETE /admin/ai-gateway/route-policy[/{stage}]   alert thresholds + the fallback hold switch
//   POST /admin/ai-gateway/route-watch/run                 run the route watch (alerts) or the digest now
// The page link of one request: ?tab=trace&rt=<trace id>&rt_at=<time of one of its rows> (a Calls row's link).

export const ROUTE_REQUESTS_API = "/api/admin/ai-gateway/route-trace/requests";
export const ROUTE_RUNGS_API = "/api/admin/ai-gateway/route-trace/rungs";
export const FALLBACK_CONTENT_API = "/api/admin/ai-gateway/route-trace/fallback-content";
export const FALLBACK_TENANT_CONTENT_API = "/api/admin/content-quality/fallback-writes";
export const ROUTE_TEST_API = "/api/admin/ai-gateway/route-trace/test";
export const ROUTE_POLICY_API = "/api/admin/ai-gateway/route-policy";
export const ROUTE_WATCH_API = "/api/admin/ai-gateway/route-watch/run";
export const ROUTE_KEY = ["ai-gateway", "route-trace"] as const;
export const ROUTE_WINDOWS = ["1h", "24h", "7d"] as const;
export type RouteWindow = (typeof ROUTE_WINDOWS)[number];
export const TEST_MAX_REQUESTS = 6;
export const DEFAULT_STAGE = "*";

export type RouteOutcome = "primary" | "fallback" | "no_answer";
export type Rung = "primary" | "fallback" | "unknown";

export interface RouteRequest {
  trace_id: string;
  stage: string | null;
  role: string | null;
  first_at: string | null;
  last_at: string | null;
  outcome: RouteOutcome;
  attempts: number | null;
  failed_attempts: number;
  served_model: string | null;
  served_connection: string | null;
  served_attempt: number | null;
  max_position: number | null;
  cost_usd: number | null;
  latency_ms: number | null;
  route_test: boolean;
  job_id: string | null;
  /** AA-1042 (S270): route rungs deferred behind an open refusal circuit (not called, no row of their own). */
  skipped_attempts?: number;
}

export interface RouteRequestsResp {
  window: RouteWindow;
  stage: string | null;
  fallback_only: boolean;
  requests: RouteRequest[];
}

export interface RouteAttempt {
  at: string | null;
  attempt_no: number | null;
  position: number | null;
  model: string | null;
  provider: string | null;
  connection: string | null;
  wire_id: string | null;
  status: string;
  error_class: string | null;
  error: string | null;
  latency_ms: number | null;
  cost_usd: number | null;
  tokens_in: number | null;
  tokens_out: number | null;
  billed_unusable: boolean;
}

export interface RouteTrace {
  trace_id: string;
  stage: string | null;
  role: string | null;
  job_id: string | null;
  tour_id: string | null;
  route_test: boolean;
  outcome: RouteOutcome;
  served: RouteAttempt | null;
  attempts: RouteAttempt[];
  cost_usd: number;
  latency_ms: number;
  route: string[] | null;
  first_at: string | null;
  last_at: string | null;
}

export interface RungRow {
  model: string | null;
  connection: string | null;
  rung: Rung;
  position: number | null;
  answered: number;
  share_pct: number | null;
  cost_usd: number | null;
  avg_latency_ms: number | null;
}

export interface StageRungs {
  stage: string;
  role: string | null;
  route: string[];
  active: boolean | null;
  answered: number;
  fallback_answered: number;
  fallback_pct: number | null;
  lost_requests: number;
  failed_attempts: number;
  rungs: RungRow[];
}

export interface RungsResp {
  window: RouteWindow;
  stages: StageRungs[];
}

export interface FallbackTour {
  id: string;
  tour_id: string;
  tour_name: string | null;
  version: number | null;
  status: string | null;
  at: string | null;
  held: boolean;
}

export interface FallbackPiece {
  piece_id: string;
  tenant_name: string | null;
  channel: string | null;
  status: string | null;
  held: boolean;
  at: string | null;
}

export interface FallbackContentResp {
  window: RouteWindow;
  tours: FallbackTour[];
}

export interface FallbackTenantVersion {
  id: string;
  tenant_name: string | null;
  tour_name: string | null;
  version: number | null;
  status: string | null;
  held: boolean;
  at: string | null;
}

export interface FallbackTenantContentResp {
  window: RouteWindow;
  versions: FallbackTenantVersion[];
  pieces: FallbackPiece[];
}

export interface PolicyValues {
  fallback_alert_pct: number;
  min_requests: number;
  alert_last_rung: boolean;
  cost_shift_usd: number;
  hold_fallback_writes: boolean;
}

export interface PolicyRow {
  stage: string;
  fallback_alert_pct: number | null;
  min_requests: number | null;
  alert_last_rung: boolean | null;
  cost_shift_usd: number | null;
  hold_fallback_writes: boolean | null;
  notes: string | null;
  updated_at: string | null;
  updated_by: string | null;
}

export interface StagePolicy {
  stage: string;
  row: PolicyRow | null;
  effective: PolicyValues & { stage: string };
}

export interface RoutePolicyResp {
  ready: boolean;
  defaults: PolicyValues & { stage: string };
  default_row: PolicyRow | null;
  stages: StagePolicy[];
}

export interface RouteTestLine {
  n: number;
  ok: boolean;
  trace_id?: string | null;
  model?: string | null;
  connection?: string | null;
  attempt_no?: number | null;
  route_position?: number | null;
  fallback_used?: boolean;
  cost_usd?: number | null;
  error?: string;
}

export interface RouteTestResp {
  stage: string;
  refuse_primary: boolean;
  route: string[];
  requests: RouteTestLine[];
  cost_usd: number;
}

/** The request list path (window, stage, fallback only). */
export function requestsPath(window: RouteWindow, stage: string | null, fallbackOnly: boolean): string {
  const q = new URLSearchParams({ window });
  if (stage) q.set("stage", stage);
  if (fallbackOnly) q.set("fallback_only", "true");
  return `${ROUTE_REQUESTS_API}?${q.toString()}`;
}

/** The path of one request; `at` (the time of one of its rows) narrows the search to +-15 minutes around it. */
export function tracePath(traceId: string, at?: string | null): string {
  const base = `${ROUTE_REQUESTS_API}/${encodeURIComponent(traceId)}`;
  return at ? `${base}?${new URLSearchParams({ at }).toString()}` : base;
}

export function rungsPath(window: RouteWindow): string {
  return `${ROUTE_RUNGS_API}?${new URLSearchParams({ window }).toString()}`;
}

export function policyPath(stage: string): string {
  return `${ROUTE_POLICY_API}/${encodeURIComponent(stage)}`;
}

export function parseRouteWindow(value: string | null | undefined): RouteWindow {
  return (ROUTE_WINDOWS as readonly string[]).includes(value ?? "") ? (value as RouteWindow) : "24h";
}

export interface RouteTarget {
  id: string;
  at: string | null;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** The request the address opens (`rt`, `rt_at`), or null for anything that is not a UUID. */
export function parseRouteTarget(rt: string | null | undefined, at: string | null | undefined): RouteTarget | null {
  if (!rt || !UUID_RE.test(rt)) return null;
  const when = at && !Number.isNaN(Date.parse(at)) ? at : null;
  return { id: rt.toLowerCase(), at: when };
}

/** The badge tone of a request's outcome. */
export function outcomeTone(o: RouteOutcome): "success" | "warning" | "danger" {
  return o === "primary" ? "success" : o === "fallback" ? "warning" : "danger";
}

/** The badge tone of one attempt's ledger status. */
/** AA-1042 (S270): the status of a route rung deferred behind an open refusal circuit (never called). */
export const SKIPPED_STATUS = "skipped";

export function attemptTone(status: string): "success" | "warning" | "danger" | "neutral" {
  if (status === "ok") return "success";
  if (status === "refused") return "warning";
  if (status === "error" || status === "billed_unusable" || status === "truncated") return "danger";
  return "neutral";
}

/** "model @ connection" of a rung or an answer ("—" when unknown). */
export function rungLabel(model: string | null | undefined, connection: string | null | undefined): string {
  if (!model) return "—";
  return connection ? `${model} @ ${connection}` : model;
}

/** A percentage with at most one decimal ("—" for null). */
export function pct(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return `${Math.round(v * 10) / 10} %`;
}

// ── the policy editor (strings in the form; empty = inherit) ─────────────────────────────────────────────────────────

export type Inherit = "inherit" | "on" | "off";

export interface PolicyDraft {
  fallback_alert_pct: string;
  min_requests: string;
  cost_shift_usd: string;
  alert_last_rung: Inherit;
  hold_fallback_writes: Inherit;
  notes: string;
}

const triOf = (v: boolean | null | undefined): Inherit => (v == null ? "inherit" : v ? "on" : "off");
const boolOf = (v: Inherit): boolean | null => (v === "inherit" ? null : v === "on");

export function policyDraft(row: PolicyRow | null): PolicyDraft {
  return {
    fallback_alert_pct: row?.fallback_alert_pct != null ? String(row.fallback_alert_pct) : "",
    min_requests: row?.min_requests != null ? String(row.min_requests) : "",
    cost_shift_usd: row?.cost_shift_usd != null ? String(row.cost_shift_usd) : "",
    alert_last_rung: triOf(row?.alert_last_rung),
    hold_fallback_writes: triOf(row?.hold_fallback_writes),
    notes: row?.notes ?? "",
  };
}

export type PolicyField = "fallback_alert_pct" | "min_requests" | "cost_shift_usd" | "notes";

export interface PolicyBody {
  fallback_alert_pct: number | null;
  min_requests: number | null;
  cost_shift_usd: number | null;
  alert_last_rung: boolean | null;
  hold_fallback_writes: boolean | null;
  notes: string | null;
}

/** The ranges of the route alert policy fields (the API's RoutePolicyBody); the editor's error messages quote them. */
export const POLICY_LIMITS = {
  pct: [1, 100], minRequests: [1, 10000], cost: [0.01, 10000], notes: 500,
} as const;

function wholeIn(s: string, lo: number, hi: number): number | null | "bad" {
  const t = s.trim();
  if (t === "") return null;
  if (!/^\d+$/.test(t)) return "bad";
  const n = Number(t);
  return n >= lo && n <= hi ? n : "bad";
}

/** The PUT body of a draft, or the first field that is not valid. */
export function policyBody(d: PolicyDraft): PolicyBody | PolicyField {
  const pctV = wholeIn(d.fallback_alert_pct, ...POLICY_LIMITS.pct);
  if (pctV === "bad") return "fallback_alert_pct";
  const minV = wholeIn(d.min_requests, ...POLICY_LIMITS.minRequests);
  if (minV === "bad") return "min_requests";
  let cost: number | null = null;
  const c = d.cost_shift_usd.trim();
  if (c !== "") {
    if (!/^\d+(\.\d{1,2})?$/.test(c)) return "cost_shift_usd";
    cost = Number(c);
    if (cost < POLICY_LIMITS.cost[0] || cost > POLICY_LIMITS.cost[1]) return "cost_shift_usd";
  }
  if (d.notes.length > POLICY_LIMITS.notes) return "notes";
  return {
    fallback_alert_pct: pctV, min_requests: minV, cost_shift_usd: cost,
    alert_last_rung: boolOf(d.alert_last_rung), hold_fallback_writes: boolOf(d.hold_fallback_writes),
    notes: d.notes.trim() || null,
  };
}
