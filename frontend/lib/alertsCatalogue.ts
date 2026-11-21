// lib/alertsCatalogue.ts — AA-1171: Platform › Alerts › Catalogue (types + pure helpers).
//
// Mirrors GET /admin/platform/alerts/catalogue (backend api/services/alerts_catalogue.py). One row per alert
// condition: CloudWatch alarms, EventBridge rules that notify a topic, budgets and cost anomaly alerts, and the
// in-app checks of CIS, Booking and the partner CRM. Pure, so the node tests and a Playwright spec can import it.

export const CATALOGUE_PATH = "/api/admin/platform/alerts/catalogue";

export type AlertType =
  | "metric" | "metric_math" | "composite" | "eventbridge" | "budget" | "cost_anomaly" | "in_app_check";
export type EditableVia = "settings" | "terraform" | "none";
export type Severity = "page" | "ticket";

export interface CatalogueRow {
  id: string;
  name: string;
  app: string | null;
  app_label: string;
  component: string | null;
  type: AlertType;
  source: string;
  severity: Severity | null;
  mutable: boolean | null;
  condition: string | null;
  threshold: string | null;
  threshold_source: string | null;
  editable_via: EditableVia;
  edit_href: string | null;
  channel: string[];
  runbook: string | null;
  owner_role: string;
  state: string | null;
  state_since: string | null;
  last_alert_at: string | null;
  account_id: string | null;
  freshness: string | null;
  coverage_gap: string | null;
  description: string | null;
}

export interface SourceFreshness {
  status: string | null;
  as_of?: string | null;
  reason?: string | null;
  message?: string | null;
}

export interface CatalogueSummary {
  total: number;
  by_type: Record<string, number>;
  by_app: Record<string, number>;
  by_severity: { page: number; ticket: number; untagged: number };
  firing: number;
  firing_by_app: Record<string, number>;
  page_firing: number;
  gaps: number;
}

export interface CatalogueResp {
  as_of: string;
  environment: string;
  summary: CatalogueSummary;
  freshness: Record<string, SourceFreshness>;
  rows: CatalogueRow[];
  gaps: { key: string; text: string }[];
}

export const TYPES: AlertType[] = [
  "metric", "metric_math", "composite", "eventbridge", "budget", "cost_anomaly", "in_app_check",
];
export const SOURCES = ["aws", "cis", "booking", "crm", "budgets"] as const;

export interface CatalogueFilter {
  q: string;
  app: string;
  type: string;
  severity: string;
  state: string;
  edit: string;
  gaps: string;
}

/** "ALARM" rows are firing; budgets and rows of other accounts have no live state ("not_live"). */
export function isFiring(r: Pick<CatalogueRow, "state">): boolean {
  return r.state === "ALARM";
}

/** Does a row pass the page's filters (all strings from the URL; empty = no filter)? */
export function matchesRow(r: CatalogueRow, f: CatalogueFilter): boolean {
  if (f.app && (r.app ?? "") !== f.app) return false;
  if (f.type && r.type !== f.type) return false;
  if (f.severity === "untagged" ? r.severity !== null : f.severity && r.severity !== f.severity) return false;
  if (f.state === "firing" && !isFiring(r)) return false;
  if (f.state === "quiet" && isFiring(r)) return false;
  if (f.edit && r.editable_via !== f.edit) return false;
  if (f.gaps === "1" && !r.coverage_gap) return false;
  if (f.q) {
    const hay = `${r.name} ${r.condition ?? ""} ${r.description ?? ""} ${r.component ?? ""} ${r.threshold_source ?? ""}`
      .toLowerCase();
    if (!hay.includes(f.q.trim().toLowerCase())) return false;
  }
  return true;
}

/** The app filter's options, in the order the catalogue lists them. */
export function appOptions(rows: CatalogueRow[]): { value: string; label: string }[] {
  const seen = new Map<string, string>();
  for (const r of rows) if (r.app && !seen.has(r.app)) seen.set(r.app, r.app_label);
  return [...seen.entries()].sort((a, b) => a[1].localeCompare(b[1])).map(([value, label]) => ({ value, label }));
}

/** A source that did not answer, for the notice above the table. */
export function staleSources(freshness: Record<string, SourceFreshness>): string[] {
  return Object.entries(freshness).filter(([, f]) => f && f.status && f.status !== "ok").map(([k]) => k);
}
