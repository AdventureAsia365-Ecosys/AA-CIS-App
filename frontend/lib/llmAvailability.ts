// lib/llmAvailability.ts — AA-900: the LLM availability card (AI providers › Routing).
// Shape of GET /api/admin/llm-availability (backend api/services/llm_availability.py) and the pure helpers the card
// uses. The status is decided by the API from real outcomes (no probe call); this file only presents it.

export type AvailabilityStatus = "ok" | "degraded" | "blocked" | "unknown";
export type FailureClass = "access_denied" | "quota_exhausted" | "throttled";

export interface ProviderRow {
  provider: string;
  /** account key (never printed: the card names it through lib/awsAccounts accountName) */
  account: string | null;
  embed: boolean;
  calls: number;
  ok_calls: number;
  last_call_at: string | null;
  last_ok_at: string | null;
}

export interface FailureRow {
  error_class: FailureClass;
  jobs: number;
  last_at: string | null;
  last_kind?: string;
  last_job_id?: string;
  last_status?: string;
  last_error?: string;
}

export interface LlmAvailability {
  status: AvailabilityStatus;
  window_hours: number;
  generated_at: string;
  last_ok_at: string | null;
  cause: { error_class: FailureClass; at: string | null; kind?: string; job_id?: string; error?: string } | null;
  providers: ProviderRow[];
  failures: FailureRow[];
  /** AA-1002 S4 / AA-1018: per connection (absent on an older API). */
  connections?: { connection_key: string; enabled?: boolean; status?: string; failure_domain?: string | null }[];
  /** AA-1018: the failure-domain rule, report only (absent on an older API). */
  invariant?: { ready?: boolean; ok?: boolean; stages_checked?: number; breaches?: number;
    /** AA-642 A1: stages that pass through a declared degrade mode (not counted in breaches). */
    declared_degrade?: number; stages?: unknown[] };
  /** AA-1026 (S267): per active stage, the model that answers now (absent on an older API). */
  stage_coverage?: StageCoverage;
}

/** AA-1026 (S267): one deployment of a stage's route, as the stage coverage check sees it. */
export interface CoverageModel {
  model_key: string;
  connection_key: string | null;
  state: string;
  why?: string;
}

/** AA-1026 (S267): one active stage of the stage coverage report. */
export interface CoverageStage {
  stage: string;
  role?: string | null;
  status: "live" | "none" | string;
  live_model?: string | null;
  live_connection?: string | null;
  unproven?: number;
  models?: CoverageModel[];
}

/** AA-1026 (S267): the stage coverage report (shared/llm_client/stage_coverage.py). */
export interface StageCoverage {
  ready?: boolean;
  ok?: boolean;
  checked_at?: string;
  stages_checked?: number;
  uncovered?: string[];
  proof_max_age_hours?: number;
  stages?: CoverageStage[];
}

/** AA-1026 (S267): the stages of a coverage report with no live model first, then by stage name. A malformed row is
 *  dropped; an older API (no report) gives nothing. */
export function coverageRows(report: StageCoverage | null | undefined): CoverageStage[] {
  const rows = (report?.stages ?? []).filter(
    (s): s is CoverageStage => !!s && typeof s.stage === "string" && typeof s.status === "string",
  );
  return rows.sort((a, b) => Number(a.status === "live") - Number(b.status === "live")
    || a.stage.localeCompare(b.stage));
}

/** AA-1026 (S267): the reasons a stage has no live model, short ("model@connection state"), at most ``limit``. */
export function coverageReasons(stage: CoverageStage, limit = 3): string[] {
  return (stage.models ?? []).slice(0, limit).map((m) => `${m.model_key}@${m.connection_key ?? "-"} ${m.state}`);
}

/** AA-642 A5: one stage of the invariant report, as far as the role filter goes (other fields not read here). */
export interface RoleSkipStage {
  stage?: string;
  role?: string;
  /** "<model>@<connection>": a route model on a connection whose served roles leave the stage's role out. */
  role_skipped?: string[];
}

/** AA-642 A5: every stage whose route names a model on a connection that does not serve the stage's role, with what
 *  the filter left out (stage order kept). An older API (no role_skipped) or a malformed row gives nothing. */
export function roleSkips(stages: readonly unknown[] | undefined): { stage: string; role: string; skipped: string[] }[] {
  const out: { stage: string; role: string; skipped: string[] }[] = [];
  for (const raw of stages ?? []) {
    const s = (raw ?? {}) as RoleSkipStage;
    const skipped = Array.isArray(s.role_skipped) ? s.role_skipped.filter((x) => typeof x === "string") : [];
    if (typeof s.stage === "string" && skipped.length > 0) {
      out.push({ stage: s.stage, role: typeof s.role === "string" ? s.role : "", skipped });
    }
  }
  return out;
}

export type StatusTone = "success" | "warning" | "danger" | "neutral";

export const STATUS_TONE: Record<AvailabilityStatus, StatusTone> = {
  ok: "success",
  degraded: "warning",
  blocked: "danger",
  unknown: "neutral",
};

/** The badge tone of a status; an unexpected value reads as unknown. */
export function statusTone(status: string | null | undefined): StatusTone {
  return STATUS_TONE[(status ?? "unknown") as AvailabilityStatus] ?? "neutral";
}

/** Text routes first (they decide the status), embeddings after; inside each, the most recent call first. */
export function sortProviders(rows: readonly ProviderRow[]): ProviderRow[] {
  const at = (r: ProviderRow) => (r.last_call_at ? Date.parse(r.last_call_at) : 0);
  return [...rows].sort((a, b) => Number(a.embed) - Number(b.embed) || at(b) - at(a));
}

/** Jobs page link that finds one job (its search matches an id prefix). */
export function jobLink(jobId: string | null | undefined): string | null {
  return jobId ? `/admin/jobs?q=${encodeURIComponent(jobId)}` : null;
}
