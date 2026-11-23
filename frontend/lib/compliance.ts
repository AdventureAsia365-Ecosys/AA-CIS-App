// frontend/lib/compliance.ts
// AA-1170: types and pure helpers of Platform › Compliance (app/admin/platform/compliance). The API
// (api/routers/admin_platform_compliance.py, api/services/compliance.py) computes every number; this file only names,
// sorts and colours them. Dependency-free so lib/compliance.test.mjs runs it under node --test.

export const OVERVIEW_PATH = "/api/admin/platform/compliance";
export const CONTROLS_PATH = "/api/admin/platform/compliance/controls";
export const REFRESH_PATH = "/api/admin/platform/compliance/refresh";
export const OBLIGATIONS_PATH = "/api/admin/platform/compliance/obligations";
export const SETTINGS_HREF = "/admin/platform/settings?app=cis&area=compliance";

export type Tone = "neutral" | "accent" | "success" | "warning" | "danger" | "info" | "purple";
export type CheckStateName = "pass" | "fail" | "error" | "stale" | "manual" | "missing";
export type ControlStateName = "met" | "partial" | "stale" | "gap" | "no_evidence" | "inactive";
export type RequirementStateName = "met" | "partial" | "stale" | "gap" | "unmapped";

export interface FrameworkScore {
  framework: string;
  title?: string;
  kind?: "standard" | "law";
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
  scheduled?: boolean;
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

/** AA-1174: the result of collecting one of the API's own sources (cis_db, crm_ops) on Refresh. */
export interface LiveSourceState {
  state: "ok" | "recent" | "skipped" | "error";
  rows?: number;
  new?: number;
  by_status?: Record<string, number>;
  reason?: string;
  http?: number;
  last?: string;
  detail?: string;
}

export interface IngestState {
  state: "ok" | "missing" | "unavailable" | "invalid" | "skipped";
  read_at?: string;
  rows?: number;
  new?: number;
  dropped?: string[];
  detail?: string;
  reason?: string;
  live?: Record<string, LiveSourceState>;
}

/** AA-1174: a framework row of compliance.framework as the API returns it (names come from the table). */
export interface FrameworkInfo {
  framework: string;
  title: string;
  kind: "standard" | "law";
  scored: boolean;
  jurisdiction?: string | null;
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
  register: { requirements: number; controls: number; mappings: number; laws?: number; obligations?: number };
  ingest: IngestState | null;
  framework_info?: FrameworkInfo[];
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
  framework_info?: FrameworkInfo[];
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

/** The six framework keys of CIS 466 in display order: the English fallback only. Since AA-1174 the API returns every
 * framework with its name and kind (compliance.framework, `framework_info`); pass it to frameworkName / frameworkList. */
export const FRAMEWORKS: { key: string; name: string; legal?: boolean }[] = [
  { key: "iso27001:2022", name: "ISO/IEC 27001:2022" },
  { key: "soc2:2017", name: "SOC 2 (TSC common criteria)" },
  { key: "iso42001:2023", name: "ISO/IEC 42001:2023" },
  { key: "owasp-top10:2021", name: "OWASP Top 10 (2021)" },
  { key: "owasp-llm:2025", name: "OWASP Top 10 for LLM (2025)" },
  { key: "eu-ai-act:2024", name: "EU AI Act (deployer duties)", legal: true },
];

export function frameworkName(key: string, info?: FrameworkInfo[] | null): string {
  return info?.find((f) => f.framework === key)?.title ?? FRAMEWORKS.find((f) => f.key === key)?.name ?? key;
}

export function isLegal(key: string, info?: FrameworkInfo[] | null): boolean {
  const row = info?.find((f) => f.framework === key);
  if (row) return row.kind === "law";
  return FRAMEWORKS.some((f) => f.key === key && f.legal);
}

/** The framework filter options: the API list when present (laws included), else the fallback six. */
export function frameworkList(info?: FrameworkInfo[] | null): { key: string; name: string; legal: boolean }[] {
  if (info?.length) return info.map((f) => ({ key: f.framework, name: f.title, legal: f.kind === "law" }));
  return FRAMEWORKS.map((f) => ({ key: f.key, name: f.name, legal: !!f.legal }));
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

// ── AA-1174: obligations and the applicability matrix ─────────────────────────────────────────────────────────────

export type ObligationState = "met" | "not_met" | "not_checked";
export type Verdict = "applies" | "not_yet" | "not_applicable";

export interface LawSummary {
  framework: string;
  title: string;
  jurisdiction: string | null;
  citation: string | null;
  source_url: string | null;
  /** The date the text entered into force (one convention for every law). */
  in_force_on: string | null;
  /** The date its duties apply when later than in_force_on (GDPR: in force 2016-05-24, applies 2018-05-25). */
  applies_from?: string | null;
  scored: boolean;
  counts: { met: number; not_met: number; not_checked: number; partly: number; counsel_review: number; total: number };
  verdicts: Record<Verdict, number>;
}

export interface ObligationEvidence {
  check_id: string;
  state: CheckStateName;
  reason: string | null;
  collected_at: string | null;
  summary: string | null;
  link: string | null;
  doc_status: string | null;
}

export interface ObligationItem {
  framework: string;
  ref: string;
  section: string;
  title: string;
  sort_order: number;
  state: ObligationState;
  partly: boolean;
  counsel_review: boolean;
  applies_as: "controller" | "processor" | "both" | null;
  source_url: string | null;
  doc_url: string | null;
  controls: { control_id: string; coverage: "full" | "partial"; state: ControlStateName }[];
  evidence: ObligationEvidence[];
}

export interface ApplicabilityRow {
  market: string;
  segment: "traveller" | "agency" | "agency_client" | "staff";
  framework: string;
  test: string;
  threshold: string | null;
  verdict: Verdict;
  source_url: string;
  source_title: string;
  assessed_on: string;
  counsel_review: boolean;
  note: string | null;
  review_due: boolean;
}

export interface ObligationsResp {
  generated_at: string;
  environment: string;
  laws: LawSummary[];
  items: ObligationItem[];
  applicability: ApplicabilityRow[];
  settings: { applicability_review_max_age_days: number };
  ingest?: IngestState | null;
}

const OBLIGATION_TONE: Record<string, Tone> = { met: "success", not_met: "danger", not_checked: "neutral" };
const VERDICT_TONE: Record<string, Tone> = { applies: "accent", not_yet: "warning", not_applicable: "neutral" };

export function obligationTone(state: string | null | undefined): Tone {
  return OBLIGATION_TONE[state ?? ""] ?? "neutral";
}

export function verdictTone(verdict: string | null | undefined): Tone {
  return VERDICT_TONE[verdict ?? ""] ?? "neutral";
}

/** Obligations per law, worst first inside a law (not met, not checked, met), then by the law's own order. */
export function sortObligations(items: ObligationItem[]): ObligationItem[] {
  const rank: Record<string, number> = { not_met: 0, not_checked: 1, met: 2 };
  return [...items].sort((a, b) => a.framework.localeCompare(b.framework)
    || (rank[a.state] ?? 3) - (rank[b.state] ?? 3) || a.sort_order - b.sort_order);
}

/** The laws that have a checklist (at least one obligation), in the API's order. */
export function lawsWithItems(laws: LawSummary[]): LawSummary[] {
  return laws.filter((l) => l.counts.total > 0);
}

/** The matrix pivoted: one row per market x segment, one cell per law (framework key -> the matrix row). */
export function pivotApplicability(rows: ApplicabilityRow[]): {
  laws: string[];
  rows: { key: string; market: string; segment: ApplicabilityRow["segment"]; cells: Record<string, ApplicabilityRow> }[];
} {
  const laws: string[] = [];
  const byKey = new Map<string, { key: string; market: string; segment: ApplicabilityRow["segment"];
    cells: Record<string, ApplicabilityRow> }>();
  for (const r of rows) {
    if (!laws.includes(r.framework)) laws.push(r.framework);
    const key = `${r.market}|${r.segment}`;
    const row = byKey.get(key) ?? { key, market: r.market, segment: r.segment, cells: {} };
    row.cells[r.framework] = r;
    byKey.set(key, row);
  }
  const segOrder = ["traveller", "agency_client", "agency", "staff"];
  const out = [...byKey.values()].sort((a, b) => a.market.localeCompare(b.market)
    || segOrder.indexOf(a.segment) - segOrder.indexOf(b.segment));
  return { laws, rows: out };
}

/** The official hosts a legal source link may open (the loader's allowlist, mirrored as defence in depth). */
export const OFFICIAL_SOURCE_HOSTS = [
  "eur-lex.europa.eu", "edpb.europa.eu", "commission.europa.eu", "legislation.gov.uk", "ico.org.uk",
  "legislation.gov.au", "oaic.gov.au", "acma.gov.au", "leginfo.legislature.ca.gov", "cppa.ca.gov", "oag.ca.gov",
  "law.lis.virginia.gov", "uscode.house.gov", "ecfr.gov", "ftc.gov", "gesetze-im-internet.de", "legifrance.gouv.fr",
  "cnil.fr", "wetten.overheid.nl", "autoriteitpersoonsgegevens.nl", "vbpl.vn", "gov.vn", "chinhphu.vn", "quochoi.vn",
  "pcisecuritystandards.org",
];

/** A legal source link only when it is https on an official host (or a sub-domain of one); else null. */
export function safeSourceLink(href: string | null | undefined): string | null {
  if (!href || !href.startsWith("https://")) return null;
  let host: string;
  try {
    host = new URL(href).hostname.toLowerCase();
  } catch {
    return null;
  }
  return OFFICIAL_SOURCE_HOSTS.some((h) => host === h || host.endsWith(`.${h}`)) ? href : null;
}

export type RefreshTone = "ok" | "warning" | "error";

/** AA-1174: the counts the Refresh action reports (published file + the API's own sources) and its tone: ``ok`` only
 * when the published file was read and every live source was collected (or kept as collected minutes ago);
 * ``warning`` when nothing was published yet or a source was skipped (no evidence written: no Cognito session, a 401 /
 * 403 from the CRM, a read-only view); ``error`` when the file could not be read or was refused, or a source failed. */
export function refreshCounts(state: IngestState | null | undefined): {
  ok: boolean; tone: RefreshTone; read: number; fresh: number;
  sources: { source: string; state: string; rows: number; fresh: number }[];
} {
  if (!state) return { ok: false, tone: "error", read: 0, fresh: 0, sources: [] };
  const sources = Object.entries(state.live ?? {}).map(([source, s]) => ({
    source, state: s.state, rows: s.rows ?? 0, fresh: s.new ?? 0,
  }));
  const tone = refreshTone(state.state, sources.map((s) => s.state));
  return { ok: tone === "ok", tone, read: state.rows ?? 0, fresh: state.new ?? 0, sources };
}

function refreshTone(file: string, sources: string[]): RefreshTone {
  if (file === "unavailable" || file === "invalid" || sources.includes("error")) return "error";
  if (file === "missing" || file === "skipped" || sources.includes("skipped")) return "warning";
  return "ok";
}

export type SkipReason = "cognito_session" | "unauthorized" | "forbidden" | "view_as" | "unknown";

/** Why a source was skipped, from the API's reason code and the peer's HTTP status (plain message keys). */
export function skipReason(reason: string | null | undefined, http?: number | null): SkipReason {
  if (reason === "cognito_session_required") return "cognito_session";
  if (reason === "view_as_read_only") return "view_as";
  if (http === 401) return "unauthorized";
  if (http === 403) return "forbidden";
  return "unknown";
}
