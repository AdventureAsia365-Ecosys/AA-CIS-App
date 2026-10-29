// frontend/lib/dataCoverage.ts
// AA-992 — types, labels and formatters for Platform › Overview › Data coverage (/admin/data-coverage).
// AA-994 — the words they return are DC_WORDS (English default); the page passes the UI language's words.
//
// The shapes mirror the backend response model exactly (api/schemas/data_coverage.py, GET /admin/data-coverage). The
// page computes nothing: the status, the missing count and the expected gap all come from the API (colour rule =
// `status` + `kind`). Numbers only: no id, name or sample is ever in this response.

export const DATA_COVERAGE_PATH = "/api/admin/data-coverage";
export const REFRESH_THROTTLE_MS = 15_000;

export type RowKind = "must" | "backlog" | "info";
export type RowStatus = "ok" | "gap" | "info" | "na";
export type Polarity = "coverage" | "defect" | "threshold" | "equal";

export interface CoverageRow {
  id: string;
  label: string;
  unit: string;
  kind: RowKind;
  polarity: Polarity;
  value: number | null;
  denominator: number | null;
  denominator_label: string | null;
  pct: number | null;
  missing: number | null;
  expected_gap: number | null;
  expected_note: string | null;
  limit: number | null;
  status: RowStatus;
  reason: string | null;
  issue: string | null;
  drill: string | null;
  source: string;
  timing_ms: number;
  as_of: string | null;
}

export interface CoverageGroup {
  id: string;
  label: string;
  note: string | null;
  rows: CoverageRow[];
}

export interface SourceStatus {
  status: "ok" | "error" | "timeout" | "unavailable";
  reason: string | null;
  timing_ms: number;
  tier: "fast" | "slow" | "crm";
  reads: string;
}

export interface DataCoverageResponse {
  as_of: string;
  generated_ms: number;
  cached: boolean;
  stale: boolean;
  live_tours: number | null;
  summary: { rows: number; gaps: number; backlog: number; na: number };
  groups: CoverageGroup[];
  sources: Record<string, SourceStatus>;
}

export type BadgeTone = "success" | "danger" | "warning" | "neutral";

/**
 * The words the helpers below put into the page (English; the Vietnamese side is lib/dataCoverage.messages.ts
 * DC_MESSAGES). Every helper takes them as an optional last argument, so `node --test` loads this file as it is.
 */
export const DC_WORDS = {
  badgeOk: "OK",
  badgeBacklog: "Backlog",
  badgeGap: "Gap",
  badgeInfo: "Info",
  badgeNa: "n/a",
  ageSeconds: (n: number) => `${n} s`,
  ageMinutes: (n: number) => `${n} min`,
  ageHours: (n: number) => `${n} h`,
  notAvailable: "not available",
  csvStage: "Stage",
  csvMetric: "Metric",
  csvMetricId: "Metric id",
  csvCount: "Count",
  csvOf: "Of",
  csvOfLabel: "Of (label)",
  csvLimit: "limit",
  csvMissing: "Missing",
  csvExpectedGap: "Expected gap",
  csvExpectedNote: "Expected note",
  csvKind: "Kind",
  csvStatus: "Status",
  csvReason: "Reason",
  csvIssue: "Issue",
  csvSource: "Source",
  csvAsOf: "As of",
};
export type DcWords = { [K in keyof typeof DC_WORDS]: (typeof DC_WORDS)[K] };

/** The status badge: green OK, red Gap (a must row), amber Backlog (a backlog row over its gap), grey Info / n/a. */
export function statusBadge(
  row: Pick<CoverageRow, "status" | "kind">, w: DcWords = DC_WORDS,
): { tone: BadgeTone; label: string } {
  if (row.status === "ok") return { tone: "success", label: w.badgeOk };
  if (row.status === "gap") return row.kind === "backlog" ? { tone: "warning", label: w.badgeBacklog } : { tone: "danger", label: w.badgeGap };
  if (row.status === "info") return { tone: "neutral", label: w.badgeInfo };
  return { tone: "neutral", label: w.badgeNa };
}

const NUM = new Intl.NumberFormat("en-US");

export function formatCount(n: number | null | undefined): string {
  return n == null ? "—" : NUM.format(n);
}

export function formatPct(p: number | null | undefined): string {
  return p == null ? "—" : `${p.toFixed(1)}%`;
}

/** "892 live tours" / "≤ 30" for a threshold row / "—". */
export function formatOf(row: Pick<CoverageRow, "denominator" | "denominator_label" | "polarity" | "limit">): string {
  if (row.polarity === "threshold" && row.limit != null) return `≤ ${NUM.format(row.limit)}`;
  if (row.denominator == null) return "—";
  return row.denominator_label ? `${NUM.format(row.denominator)} ${row.denominator_label}` : NUM.format(row.denominator);
}

export function formatExpected(row: Pick<CoverageRow, "expected_gap" | "expected_note">): string {
  if (row.expected_gap == null) return row.expected_note ?? "—";
  return row.expected_note ? `${NUM.format(row.expected_gap)} · ${row.expected_note}` : NUM.format(row.expected_gap);
}

/** Seconds since `iso`, as "23 s" / "4 min" / "2 h"; "" when unknown. */
export function formatAge(iso: string | null | undefined, now: number = Date.now(), w: DcWords = DC_WORDS): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 90) return w.ageSeconds(s);
  if (s < 5400) return w.ageMinutes(Math.round(s / 60));
  return w.ageHours(Math.round(s / 3600));
}

/** "10:42:13 UTC" for the header's "As of". */
export function formatUtcTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return `${d.toISOString().slice(11, 19)} UTC`;
}

/** Rows of a group whose status is a gap (must or backlog), for the stage header "n rows · k gaps". */
export function groupGaps(group: CoverageGroup): number {
  return group.rows.filter((r) => r.status === "gap").length;
}

/** True when every row of a group is n/a with one reason: the page then shows that reason once. */
export function groupUnavailableReason(group: CoverageGroup, w: DcWords = DC_WORDS): string | null {
  if (!group.rows.length || group.rows.some((r) => r.status !== "na")) return null;
  const reasons = new Set(group.rows.map((r) => r.reason ?? ""));
  return reasons.size === 1 ? [...reasons][0] || w.notAvailable : w.notAvailable;
}

export interface CsvRow {
  stage: string;
  row: CoverageRow;
}

/** Every row of every stage, flattened in page order, for the CSV export. */
export function csvRows(data: DataCoverageResponse): CsvRow[] {
  return data.groups.flatMap((g) => g.rows.map((row) => ({ stage: g.label, row })));
}

/** The CSV columns with headers in the given words (`CSV_COLUMNS` = English). */
export function csvColumns(w: DcWords = DC_WORDS): { header: string; value: (r: CsvRow) => unknown }[] {
  return [
    { header: w.csvStage, value: (r) => r.stage },
    { header: w.csvMetric, value: (r) => r.row.label },
    { header: w.csvMetricId, value: (r) => r.row.id },
    { header: w.csvCount, value: (r) => r.row.value },
    { header: w.csvOf, value: (r) => (r.row.polarity === "threshold" ? r.row.limit : r.row.denominator) },
    { header: w.csvOfLabel, value: (r) => (r.row.polarity === "threshold" ? w.csvLimit : r.row.denominator_label) },
    { header: "%", value: (r) => r.row.pct },
    { header: w.csvMissing, value: (r) => r.row.missing },
    { header: w.csvExpectedGap, value: (r) => r.row.expected_gap },
    { header: w.csvExpectedNote, value: (r) => r.row.expected_note },
    { header: w.csvKind, value: (r) => r.row.kind },
    { header: w.csvStatus, value: (r) => r.row.status },
    { header: w.csvReason, value: (r) => r.row.reason },
    { header: w.csvIssue, value: (r) => r.row.issue },
    { header: w.csvSource, value: (r) => r.row.source },
    { header: w.csvAsOf, value: (r) => r.row.as_of },
  ];
}

export const CSV_COLUMNS = csvColumns();
