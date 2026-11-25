// lib/opsStatusP2.ts — AA-1142 (S292): the API shapes and pure helpers of the P2 features of the Ecosystem status page
// (app/admin/overview): availability objective with error budget and burn rates (sections.slo), external synthetic
// checks (sections.external), the incident record (/api/admin/overview/incidents) and the per-agency drill-down
// (/api/admin/overview/status/tenants). Kept apart from lib/opsStatus.ts so the P1 helpers stay as they are. Every
// limit comes from the API (cis.ops.status_slo_* / burn_* / cert_* / tenant_top_n settings), never from here.

import type { Section, StatusResp } from "./opsStatus";

export interface BurnWindow { burn: number | null; events: number; bad: number; factor: number;
  state: "ok" | "alert" | "few" | "none" }
export interface SloRow {
  app: string; label: string; measured: boolean; reason?: string; basis?: "requests" | "health samples";
  availability_pct?: number | null; target_pct?: number; budget_left_pct?: number | null; events?: number;
  bad?: number; below_target?: boolean; fast?: BurnWindow; slow?: BurnWindow;
}
export interface SloRules {
  target_pct: number; window_days: number; budget_warn_pct: number; budget_warn_key: string;
  fast: { window_min: number; factor: number };
  slow: { window_hours: number; factor: number }; min_requests: number; target_key: string; window_key: string;
  fast_window_key: string; fast_factor_key: string; slow_window_key: string; slow_factor_key: string;
}
export interface Slo { rules: SloRules; rows: SloRow[]; sample_minutes: number }

export interface ExternalRow {
  target: string; app: string; host: string; state: string; checked_at: string; http_status: number | null;
  latency_ms: number | null; dns_ok: boolean | null; tls_ok: boolean | null; error: string | null;
  cert_not_after: string | null; cert_days_left: number | null; cert_level: "ok" | "warn" | "crit" | "none";
  cell_minutes: number; cells: string[]; uptime_pct: number | null;
}
export interface External { rows: ExternalRow[]; newest: string; schedule_minutes: number; stale: boolean;
  cert_days: { warn: number; crit: number; warn_key: string; crit_key: string } }

export type IncidentStatus = "investigating" | "identified" | "monitoring" | "resolved";
export interface Incident {
  incident_id: number; title: string; severity: "sev1" | "sev2" | "sev3"; status: IncidentStatus; app: string | null;
  item_id: string | null; owner: string; owner_name: string | null; summary: string | null; declared_by: string;
  declared_by_name: string | null; declared_at: string; updated_at: string; resolved_at: string | null;
}
export interface TimelineRow {
  t: string; t2?: string | null; kind: "update" | "event" | "ack"; type: string; from?: string | null;
  to?: string | null; note?: string | null; by?: string; by_name?: string | null; severity?: string;
  app?: string | null; text?: string; id?: string; until?: string | null; cleared_at?: string | null;
}
export interface IncidentDetail extends Incident {
  window: { start: string; end: string; lookback_min: number; lookback_key: string };
  timeline: TimelineRow[]; lesson_draft: string; lessons_url: string;
}
export interface IncidentList { incidents: Incident[]; open: number; lessons_url: string }

export interface AgencyRef { tenant_id: string; name: string | null; slug: string | null; active: boolean | null }
export interface TenantsResp {
  generated_at: string; tenant: string | null; top_n: number; top_n_key: string; options: AgencyRef[]; not_measured: string[];
  ai_spend?: (AgencyRef & { cost_usd: number; requests: number })[];
  failed_jobs?: (AgencyRef & { failed: number; last: string | null })[];
  ai_errors?: (AgencyRef & { failed: number; requests: number; failed_pct: number | null })[];
  platform?: { cost_usd: number; requests: number; failed: number };
  agencies_seen?: number;
  agency?: AgencyRef & { known: boolean; cost_usd: number; requests: number; ai_failed: number; jobs_failed: number;
    by_stage: { app: string; stage: string | null; cost_usd: number; requests: number; failed: number }[];
    ai_errors: { error_class: string; requests: number }[];
    failed_jobs: { kind: string; failed: number; last: string | null }[] };
}

export const INCIDENT_STATUSES: readonly IncidentStatus[] = ["investigating", "identified", "monitoring", "resolved"];
export const SEVERITIES = ["sev1", "sev2", "sev3"] as const;
export const INCIDENTS_QUERY_KEY = "admin-ecosystem-incidents";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** The P2 sections of a status answer (absent before the P2 API ships: undefined, the cards then say nothing). */
export function p2Sections(data: StatusResp): { slo?: Section<Slo>; external?: Section<External> } {
  const s = data.sections as unknown as Record<string, unknown>;
  return { slo: s.slo as Section<Slo> | undefined, external: s.external as Section<External> | undefined };
}

/** The level of a burn window for its colour: alert = crit; few / none = grey (never green when not judged). */
export function burnLevel(w: BurnWindow | undefined): "crit" | "ok" | "none" {
  if (!w) return "none";
  if (w.state === "alert") return "crit";
  return w.state === "ok" ? "ok" : "none";
}

/** Budget left: crit when spent (≤ 0), warn at or under the warn line (`warnPct`, the API's
 * rules.budget_warn_pct = cis.ops.status_slo_budget_warn_pct), ok otherwise; none when unknown. */
export function budgetLevel(left: number | null | undefined, warnPct: number): "crit" | "warn" | "ok" | "none" {
  if (left == null) return "none";
  if (left <= 0) return "crit";
  return left <= warnPct ? "warn" : "ok";
}

/** A bar width (0-100) for the budget left; a spent budget draws an empty bar. */
export function budgetBar(left: number | null | undefined): number {
  if (left == null || !Number.isFinite(left)) return 0;
  return Math.max(0, Math.min(100, left));
}

/** "2.1×" (one decimal under 10, none above); "—" without a rate. */
export function fmtBurn(b: number | null | undefined): string {
  if (b == null || !Number.isFinite(b)) return "—";
  return `${b < 10 ? b.toFixed(1) : Math.round(b)}×`;
}

/** The state colour of an external target, with its certificate level folded in. */
export function externalLevel(r: Pick<ExternalRow, "state" | "cert_level">): "crit" | "warn" | "ok" | "none" {
  if (r.state === "down" || r.state === "degraded" || r.cert_level === "crit") return "crit";
  if (r.state === "warn" || r.cert_level === "warn") return "warn";
  return r.state === "ok" ? "ok" : "none";
}

/** Tone of an incident status: resolved is done, monitoring a warning, the others need someone. */
export function incidentTone(s: IncidentStatus): "success" | "warning" | "danger" {
  if (s === "resolved") return "success";
  return s === "monitoring" ? "warning" : "danger";
}

/** A title prefilled from an alert: its own title, cut to the 300 characters the API takes. */
export function titleFromItem(title: string | null | undefined): string {
  return (title ?? "").trim().slice(0, 300);
}

/** The title rule of the API (3-300 characters after trimming). */
export function titleOk(title: string): boolean {
  const n = title.trim().length;
  return n >= 3 && n <= 300;
}

/** The tenant filter of the URL: an agency id, else none. */
export function tenantParam(raw: string | null | undefined): string | null {
  const v = (raw ?? "").trim().toLowerCase();
  return UUID.test(v) ? v : null;
}

/** The query of the per-agency read: the page's range (custom with from / to) and the agency. */
export function tenantsQuery(range: string, from: string, to: string, tenant: string | null): string {
  const q = new URLSearchParams({ range });
  if (range === "custom") {
    if (from) q.set("from", from);
    if (to) q.set("to", to);
  }
  if (tenant) q.set("tenant", tenant);
  return q.toString();
}

/** An agency's label: its name, else its slug, else the start of its id. */
export function agencyLabel(a: Pick<AgencyRef, "name" | "slug" | "tenant_id">): string {
  return a.name || a.slug || `${a.tenant_id.slice(0, 8)}…`;
}
