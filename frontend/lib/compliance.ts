// frontend/lib/compliance.ts
// AA-1170: types and pure helpers of Platform › Compliance (app/admin/platform/compliance). The API
// (api/routers/admin_platform_compliance.py, api/services/compliance.py) computes every number; this file only names,
// sorts and colours them. Dependency-free so lib/compliance.test.mjs runs it under node --test.

export const OVERVIEW_PATH = "/api/admin/platform/compliance";
export const CONTROLS_PATH = "/api/admin/platform/compliance/controls";
export const REFRESH_PATH = "/api/admin/platform/compliance/refresh";
export const SETTINGS_HREF = "/admin/platform/settings?app=cis&area=compliance";

export type Tone = "neutral" | "accent" | "success" | "warning" | "danger" | "info" | "purple";
export type CheckStateName = "pass" | "fail" | "error" | "stale" | "manual" | "missing";
export type ControlStateName = "met" | "partial" | "stale" | "gap" | "no_evidence" | "inactive";
export type RequirementStateName = "met" | "partial" | "stale" | "gap" | "unmapped";

export interface FrameworkScore {
  framework: string;
  n_requirements: number;
  n_met: number;
  n_partial: number;
  n_gap: number;
  n_stale: number;
  n_unmapped: number;
  strict_pct: number;
  weighted_pct: number;
  reference_pct: number | null;
}

export interface CheckView {
  check_id: string;
  state: CheckStateName;
  reason?: string;
  source?: string | null;
  collected_at?: string | null;
  expires_at?: string | null;
  controls?: string[];
}

export interface Heartbeat {
  source: string;
  last_seen: string | null;
  age_hours: number | null;
  heartbeat_hours: number;
  ok: boolean;
  evidence_rows: number;
}

export interface TrendPoint {
  date: string;
  framework: string;
  strict_pct: number;
  weighted_pct: number;
  reference_pct: number | null;
}

export interface IngestState {
  state: "ok" | "missing" | "unavailable" | "invalid";
  read_at: string;
  rows?: number;
  new?: number;
  dropped?: string[];
  detail?: string;
}

export interface OverviewResp {
  environment: string;
  generated_at: string;
  frameworks: FrameworkScore[];
  controls: Record<string, number>;
  checks: Record<string, number>;
  attention: CheckView[];
  heartbeat: Heartbeat[];
  trend: TrendPoint[];
  settings: { validity_hours: Record<string, number>; heartbeat_hours: number; partial_weight: number };
  settings_changed_on: string | null;
  register: { requirements: number; controls: number; mappings: number };
  ingest: IngestState | null;
}

export interface ControlRow {
  control_id: string;
  title: string;
  domain: string;
  owner_role: string;
  frequency: string;
  test_type: string;
  scope: string;
  active: boolean;
  state: ControlStateName;
  frameworks: Record<string, number>;
  checks: CheckView[];
  last_evidence_at: string | null;
}

export interface ControlsResp {
  generated_at: string;
  controls: ControlRow[];
  frameworks: string[];
}

export interface EvidenceRow {
  source: string;
  status: string;
  collected_at: string;
  valid_until: string | null;
  collector: string;
  collector_version: string | null;
  run_ref: string | null;
  summary: string | null;
  detail: Record<string, unknown>;
  link: string | null;
}

export interface ControlDetail extends Omit<ControlRow, "frameworks" | "checks" | "last_evidence_at"> {
  intent: string | null;
  updated_at: string;
  requirements: { framework: string; ref: string; coverage: "full" | "partial"; note: string | null;
    title: string | null; section: string | null; state: RequirementStateName | null }[];
  checks: (CheckView & { required: boolean; history: EvidenceRow[] })[];
}

/** The framework keys of CIS 466 in display order, with their short display names. */
export const FRAMEWORKS: { key: string; name: string; legal?: boolean }[] = [
  { key: "iso27001:2022", name: "ISO/IEC 27001:2022" },
  { key: "soc2:2017", name: "SOC 2 (TSC common criteria)" },
  { key: "iso42001:2023", name: "ISO/IEC 42001:2023" },
  { key: "owasp-top10:2021", name: "OWASP Top 10 (2021)" },
  { key: "owasp-llm:2025", name: "OWASP Top 10 for LLM (2025)" },
  { key: "eu-ai-act:2024", name: "EU AI Act (deployer duties)", legal: true },
];

export function frameworkName(key: string): string {
  return FRAMEWORKS.find((f) => f.key === key)?.name ?? key;
}

export function isLegal(key: string): boolean {
  return FRAMEWORKS.some((f) => f.key === key && f.legal);
}

const CHECK_TONE: Record<string, Tone> = {
  pass: "success", fail: "danger", error: "danger", stale: "warning", manual: "info", missing: "neutral",
};
const CONTROL_TONE: Record<string, Tone> = {
  met: "success", partial: "info", stale: "warning", gap: "danger", no_evidence: "neutral", inactive: "neutral",
};

export function checkTone(state: string | null | undefined): Tone {
  return CHECK_TONE[state ?? ""] ?? "neutral";
}

export function controlTone(state: string | null | undefined): Tone {
  return CONTROL_TONE[state ?? ""] ?? "neutral";
}

/** Order of the control states in the table (worst first). */
export const CONTROL_ORDER: ControlStateName[] = ["gap", "stale", "partial", "no_evidence", "met", "inactive"];

export function sortControls(rows: ControlRow[]): ControlRow[] {
  const rank = (s: ControlStateName) => {
    const i = CONTROL_ORDER.indexOf(s);
    return i < 0 ? CONTROL_ORDER.length : i;
  };
  return [...rows].sort((a, b) => rank(a.state) - rank(b.state) || a.control_id.localeCompare(b.control_id));
}

/** One percentage as text ("45.8 %"); null -> "—". */
export function fmtPct(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${value.toFixed(1)} %`;
}

/** Signed difference between the evidenced score and the reference, in points ("−12.3 pts"); null when no reference. */
export function deltaToReference(score: FrameworkScore): string | null {
  if (score.reference_pct === null || score.reference_pct === undefined) return null;
  const d = score.weighted_pct - score.reference_pct;
  const sign = d > 0 ? "+" : d < 0 ? "−" : "±";
  return `${sign}${Math.abs(d).toFixed(1)} pts`;
}

/** Rows for one trend chart: one point per date with a key per framework (weighted %). */
export function trendSeries(points: TrendPoint[], metric: "weighted_pct" | "strict_pct" = "weighted_pct"):
  Record<string, string | number>[] {
  const byDate = new Map<string, Record<string, string | number>>();
  for (const p of points) {
    const row = byDate.get(p.date) ?? { date: p.date };
    row[p.framework] = p[metric];
    byDate.set(p.date, row);
  }
  return [...byDate.values()].sort((a, b) => String(a.date).localeCompare(String(b.date)));
}

/** Whether a control maps onto the framework filter ("all" passes every control). */
export function inFramework(row: ControlRow, framework: string): boolean {
  return framework === "all" || (row.frameworks[framework] ?? 0) > 0;
}

/** The evidence link only when it is a GitHub page (the API already filters; defence in depth for the href). */
export function safeLink(href: string | null | undefined): string | null {
  return href && href.startsWith("https://github.com/") ? href : null;
}
