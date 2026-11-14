// frontend/lib/productCatalogue.ts
// AA-1106 — types and pure helpers of Platform › Product map (app/admin/platform/catalogue). The data is the root repo
// product catalogue bundle (docs/catalogue/*.yaml, built and checked by tools/catalogue/catalogue.py, published by the
// Docs sync workflow), served by GET /admin/platform/catalogue. No React, no fetch: unit tests in
// productCatalogue.test.mjs.

export type CatalogueStatus = "live" | "beta" | "planned" | "retired";
export type IntegrationKind = "db_view" | "http_api" | "event_outbox" | "webhook" | "m2m" | "provider";

export interface Conditions {
  entitlements?: string[];
  settings?: string[];
  connections?: string[];
  secrets?: string[];
  tenant_kinds?: string[];
  note?: string;
}

export interface CatalogueApp {
  id: string;
  name: string;
  name_vi?: string;
  repo?: string;
  summary?: string;
  summary_vi?: string;
  owner?: string;
  features?: number;
  live?: number;
  surface?: number;
}

export interface CatalogueFeature {
  id: string;
  app: string;
  module: string;
  name: string;
  name_vi?: string;
  summary: string;
  roles: string[];
  status: CatalogueStatus;
  owner: string;
  surface: string[];
  conditions: Conditions;
  depends_on: string[];
  guide?: string | null;
  uat: string[];
  integrations: string[];
}

export interface CatalogueIntegration {
  id: string;
  name: string;
  producer: string;
  consumers: string[];
  kind: IntegrationKind;
  contracts: { file: string; version: string }[];
  auth: string;
  conditions: Conditions;
  cadence: string;
  failure: string;
  kill_switch: string;
  personal_data: boolean;
  features: string[];
  owner: string;
  status: CatalogueStatus;
}

export interface JourneyStep {
  name: string;
  name_vi?: string;
  features?: string[];
  integrations?: string[];
}

export interface CatalogueJourney {
  id: string;
  name: string;
  name_vi?: string;
  summary?: string;
  summary_vi?: string;
  steps: JourneyStep[];
  uat: string[];
}

export interface CatalogueResp {
  format: number;
  generated_at: string | null;
  loaded_at: string;
  cache_ttl_s: number;
  source: { repo?: string | null; sha?: string | null; ref?: string | null };
  apps: CatalogueApp[];
  features: CatalogueFeature[];
  integrations: CatalogueIntegration[];
  journeys: CatalogueJourney[];
  scenarios: Record<string, { story?: string; status?: string; app?: string }>;
  settings: Record<string, { app?: string; area?: string }>;
  counts: Record<string, number>;
}

export const CATALOGUE_PATH = "/api/admin/platform/catalogue";
export const STATUSES: readonly CatalogueStatus[] = ["live", "beta", "planned", "retired"];
export const CONDITION_KINDS = ["entitlements", "settings", "connections", "secrets", "tenant_kinds"] as const;
export type ConditionKind = (typeof CONDITION_KINDS)[number];
/** The outside world in a link (a vendor, an agency's WordPress); not an app. */
export const EXTERNAL = "external";

/** The name in the UI language (the Vietnamese one when present). */
export function localName(row: { name: string; name_vi?: string }, lang: string): string {
  return lang === "vi" && row.name_vi ? row.name_vi : row.name;
}

export function localSummary(row: { summary?: string; summary_vi?: string }, lang: string): string {
  return (lang === "vi" && row.summary_vi ? row.summary_vi : row.summary) ?? "";
}

/** Which condition kinds a row has (non-empty lists only). */
export function conditionKinds(c: Conditions | undefined): ConditionKind[] {
  return CONDITION_KINDS.filter((k) => Array.isArray(c?.[k]) && (c?.[k] as string[]).length > 0);
}

export interface FeatureFilter {
  q?: string;
  app?: string;
  status?: string;
  role?: string;
  cond?: string;
}

/** Does a feature match the filter (empty fields match everything; q searches id, names, module, summary, surface
 *  and condition names, case-insensitive). */
export function matchesFeature(f: CatalogueFeature, filter: FeatureFilter): boolean {
  if (filter.app && f.app !== filter.app) return false;
  if (filter.status && f.status !== filter.status) return false;
  if (filter.role && !f.roles.includes(filter.role)) return false;
  if (filter.cond && !conditionKinds(f.conditions).includes(filter.cond as ConditionKind)) return false;
  const q = filter.q?.trim().toLowerCase();
  if (!q) return true;
  const hay = [f.id, f.name, f.name_vi ?? "", f.module, f.summary, ...f.surface, ...conditionNames(f.conditions)]
    .join(" ").toLowerCase();
  return hay.includes(q);
}

export interface LinkFilter {
  q?: string;
  app?: string;
  status?: string;
  kind?: string;
}

/** Does an integration match (app = producer or one of the consumers). */
export function matchesIntegration(i: CatalogueIntegration, filter: LinkFilter): boolean {
  if (filter.app && i.producer !== filter.app && !i.consumers.includes(filter.app)) return false;
  if (filter.status && i.status !== filter.status) return false;
  if (filter.kind && i.kind !== filter.kind) return false;
  const q = filter.q?.trim().toLowerCase();
  if (!q) return true;
  const hay = [i.id, i.name, i.auth, ...i.contracts.map((c) => c.file), ...conditionNames(i.conditions)]
    .join(" ").toLowerCase();
  return hay.includes(q);
}

/** Every name a condition holds, flat (for search). */
export function conditionNames(c: Conditions | undefined): string[] {
  return CONDITION_KINDS.flatMap((k) => (c?.[k] as string[] | undefined) ?? []);
}

/** Every role named by a feature, sorted (the role filter's options). */
export function roleOptions(features: CatalogueFeature[]): string[] {
  return [...new Set(features.flatMap((f) => f.roles))].sort();
}

/** The Platform › Settings URL that opens a key's app tab and area (null when the bundle does not know it). Booking
 *  keys open the Booking tab, which links to Pricing › Policy. */
export function settingHref(key: string, settings: CatalogueResp["settings"]): string | null {
  const meta = settings[key];
  if (!meta?.app) return null;
  const qs = new URLSearchParams({ app: meta.app });
  if (meta.area && meta.app !== "booking") qs.set("area", meta.area);
  return `/admin/platform/settings?${qs.toString()}`;
}

/** The Docs reader URL of a root repo page. */
export function docHref(path: string): string {
  return `/admin/docs?${new URLSearchParams({ path }).toString()}`;
}

export interface GraphEdge {
  from: string;
  to: string;
  ids: string[];
  live: number;
}

/** One edge per producer -> consumer pair (retired links left out), with the link ids on it. */
export function graphEdges(integrations: CatalogueIntegration[]): GraphEdge[] {
  const map = new Map<string, GraphEdge>();
  for (const i of integrations) {
    if (i.status === "retired") continue;
    for (const to of i.consumers) {
      const key = `${i.producer}->${to}`;
      const edge = map.get(key) ?? { from: i.producer, to, ids: [], live: 0 };
      edge.ids.push(i.id);
      if (i.status === "live") edge.live += 1;
      map.set(key, edge);
    }
  }
  return [...map.values()].sort((a, b) => (a.from + a.to).localeCompare(b.from + b.to));
}

/** Node positions on a circle (apps first, the outside world last), in a width x height box. */
export function graphLayout(nodes: string[], width: number, height: number): Record<string, { x: number; y: number }> {
  const out: Record<string, { x: number; y: number }> = {};
  const cx = width / 2;
  const cy = height / 2;
  const r = Math.min(width, height) / 2 - 60;
  nodes.forEach((n, idx) => {
    const angle = -Math.PI / 2 + (2 * Math.PI * idx) / Math.max(nodes.length, 1);
    out[n] = { x: Math.round(cx + r * Math.cos(angle)), y: Math.round(cy + r * Math.sin(angle)) };
  });
  return out;
}

/** Counts for the header tiles. */
export function summaryCounts(data: CatalogueResp) {
  return {
    apps: data.apps.length,
    features: data.features.length,
    live: data.features.filter((f) => f.status === "live").length,
    links: data.integrations.filter((i) => i.status !== "retired").length,
    journeys: data.journeys.length,
    uat: Object.keys(data.scenarios).length,
  };
}
