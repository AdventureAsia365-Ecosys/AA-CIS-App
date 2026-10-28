// lib/writingMemory.ts — AA-917 WM-14: types and helpers of Quality › Writing memory (/admin/writing-memory).
// Backend: api/routers/admin_writing_memory.py (admin only: tenant.read reads, ai.admin writes), through the BFF.
// AA-994: the text helpers below are English by default and take the UI language's words as an optional last
// argument (lib/writingMemory.messages.ts, the same keys), so `npm run test:unit` keeps loading this file directly.

export const WM_API = "/api/admin/writing-memory";

export type Scope = "platform" | "tenant";
export type FactStatus = "pending" | "grey" | "pass" | "fail";
export type RuleStatus = "candidate" | "rejected" | "approved" | "active" | "expired";

export interface Summary {
  signals: { total: number } & Partial<Record<"writer" | "scope" | "fact_status" | "action" | "actor_type",
    Record<string, number>>>;
  rules: Record<string, Record<string, number>>;
  budget: { month_cap_usd: number; month_spent_usd: number; run_cap_usd: number; max_calls_per_run: number };
  mine_trigger: {
    auto_refused: string | null; runs_this_month: number; auto_runs_this_month: number; new_pass: number;
    open_waves: number; last_run_at: string | null; auto_enabled?: boolean;
    thresholds: { min_new_pass: number; max_auto_runs_per_month: number; max_runs_per_month: number };
  };
  last_jobs: Record<string, { job_id: string; status: string; created_at: string | null; finished_at: string | null }>;
}

export interface Example { before: string; after: string | null; problem: string | null }

export interface Cluster {
  cluster_key: string; writer: string; scope: Scope; tenant_id: string | null; label: string; meaning: string;
  basis: string; channels: string[]; support: { signals: number; pieces: number; tenants: number; reviewers: number };
  has_open_rule: boolean; draftable: boolean; samples: Example[]; signal_ids: string[];
}

export interface ClustersResponse {
  clusters: Cluster[]; pass_signals: number; a1_pieces_30d: number;
  weeks: { writer: string; week: string; n: number }[];
  thresholds: Record<string, number>;
}

export interface Signal {
  signal_id: string; writer: string; channel: string | null; scope: Scope; tenant_id: string | null;
  field: string | null; before_text: string | null; after_text: string | null; gate: string | null;
  problem: string | null; reason_code: string | null; action: string; actor_type: string; actor_id: string | null;
  fact_status: FactStatus; source_table: string; source_id: string; created_at: string;
}

export interface SignalsPage { items: Signal[]; total: number; limit: number; offset: number }

export interface LintCheck { ok: boolean; detail?: string; explained?: number; held_out?: number }

export interface Rule {
  rule_id: string; kind: string; scope: Scope; tenant_id: string | null; writer: string; applies_to: string[];
  reason_code: string | null; gate_class: string | null; cluster_key: string; rule_text: string;
  replacement: string | null; match_phrases: string[]; token_estimate: number; status: RuleStatus;
  signal_ids: string[]; support_signals: number; support_pieces: number; support_tenants: number;
  support_reviewers: number; lint: Record<string, LintCheck | string | boolean | object>; drafted_by: string | null;
  expires_at: string; approved_by?: string | null; approved_at?: string | null; rejected_by: string | null;
  rejected_reason: string | null; created_at: string;
}

/** AA-918 — the R0 replay result kept on a rule (`lint.r0`): counts and ids only, $0. */
export interface R0Result {
  version: number; computed_at: string; rule_sha: string; cost_usd: number;
  signals: { window_days: number; read: number; usable: number; matched: number; explained_edits: number; system: number };
  recurrence: { window_days: number; basis: string | null;
    k: Record<"signals" | "pieces" | "tenants" | "reviewers", number>;
    have: Record<"signals" | "pieces" | "tenants" | "reviewers", number> };
  r2_eligible: boolean; r2_reason: string | null;
  pieces: { pieces: number; with_writer_input: number; texts: number; with_phrase: number; source_exempt: number;
    fires: number; errors: number };
  prevented: { by_gate: Record<string, number>; blocking: number; total: number; piece_ids: string[] };
  phrase: { before: number; after: number };
  new_hard_stops: Record<string, number>;
  baseline: { pieces_compared: number; pieces_agree: number; gate_checks: number; gate_agree: number;
    agreement: number | null;
    by_gate: Record<string, { compared: number; agree: number; stored_fail: number; replay_fail: number }> };
  l0_applicable: boolean; l0_ok: boolean; l0_reason: string | null;
}

export function r0Of(rule: Rule): R0Result | null {
  const v = rule.lint?.r0 as unknown;
  return v && typeof v === "object" ? (v as R0Result) : null;
}

/** The writers with an L0 reader (backend services/writing_memory/l0.py L0_WRITERS). */
export const L0_WRITERS = ["t9_blog", "t9_social"];

export interface RulesPage { items: Rule[]; total: number; limit: number; offset: number }
export interface RuleDetail extends Rule {
  provenance: Signal[];
  l0_activation: { code: string; detail: string } | null;   // AA-918: null = the L0 path may activate it now
  r0_fresh: boolean;
}

export const LINT_CHECKS = ["shape", "constraint_only", "lexicon_specifics", "five_gram_overlap", "predict"] as const;

export const LINT_LABEL: Record<(typeof LINT_CHECKS)[number], string> = {
  shape: "Size",
  constraint_only: "Only constrains",
  lexicon_specifics: "Replacement adds nothing",
  five_gram_overlap: "No tenant text",
  predict: "Explains held-out edits",
};

export function lintCheck(rule: Rule, key: string): LintCheck | null {
  const v = rule.lint?.[key];
  return v && typeof v === "object" ? (v as LintCheck) : null;
}

export function factTone(s: FactStatus): "success" | "danger" | "warning" | "neutral" {
  return s === "pass" ? "success" : s === "fail" ? "danger" : s === "grey" ? "warning" : "neutral";
}

export function ruleTone(s: RuleStatus): "success" | "danger" | "warning" | "neutral" | "info" {
  return s === "active" ? "success" : s === "approved" ? "info" : s === "rejected" ? "danger"
    : s === "candidate" ? "warning" : "neutral";
}

/** The words owner() prints (WM_TEXT in lib/writingMemory.messages.ts has the Vietnamese ones). */
export const OWNER_TEXT = {
  platform: "Platform",
  tenantShort: (id: string) => `Tenant ${id}`,
};
export type OwnerText = typeof OWNER_TEXT;

/** "Platform", else the tenant's printed name when the caller has one (the Releases tab, AA-919), else "Tenant" plus
 *  the first 8 characters of its id (the Signals / Candidates / Rules lists never print tenant names). */
export function owner(scope: Scope, tenantId: string | null, tenantName?: string, text: OwnerText = OWNER_TEXT): string {
  return scope === "platform" ? text.platform : tenantName || text.tenantShort((tenantId || "").slice(0, 8));
}

export function usd(n: number | null | undefined): string {
  return `$${(n ?? 0).toFixed(2)}`;
}

/** The two messages wmSend() makes itself (the API's own refusal text is passed through as sent). */
export const SEND_TEXT = {
  requestFailed: (status: number) => `Request failed (${status})`,
  noAnswer: "No answer from the server; reload before trying again.",
};
export type SendText = typeof SEND_TEXT;

/** Send a write to the BFF; resolves to { ok, data | message } and never throws. */
export async function wmSend<T>(path: string, method: string, body?: unknown, text: SendText = SEND_TEXT):
  Promise<{ ok: true; data: T } | { ok: false; message: string }> {
  try {
    const res = await fetch(`${WM_API}${path}`, {
      method, headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok) {
      const d = (json as { detail?: unknown }).detail;
      const msg = typeof d === "string" ? d : (d as { detail?: string } | undefined)?.detail;
      return { ok: false, message: msg || text.requestFailed(res.status) };
    }
    return { ok: true, data: json as T };
  } catch {
    return { ok: false, message: text.noAnswer };
  }
}

// ── AA-919 WM-22 — Releases tab (backend services/writing_memory/releases.py, CIS 287) ──────────────────────────────

export const WRITERS = ["a1", "t2", "t9_blog", "t9_social"] as const;
export type Writer = (typeof WRITERS)[number];
export const RELEASE_STATUSES = ["draft", "evaluated", "approved", "active", "retired", "rejected"] as const;
export type ReleaseStatus = (typeof RELEASE_STATUSES)[number];
export type ContentClass = "empty" | "lexicon_only" | "repair_hint_only" | "rules";

/** One evidence slot of a list row: the named run's kind and verdict (null = the release names no run). */
export interface EvidenceRun { run_kind: string | null; verdict: string | null; finished: boolean; missing?: boolean }

export interface Release {
  release_id: string; writer: Writer; scope: Scope; tenant_id: string | null; release_no: number;
  train_kind: string; content_class: ContentClass; rule_ids: string[]; items_sha256: string | null;
  status: ReleaseStatus; r0_run_id: string | null; eval_run_id: string | null; shadow_run_id: string | null;
  previous_release_id: string | null; train_month: string | null; emergency_reason: string | null;
  emergency_ref: string | null; reverify: boolean; reverify_reasons: string[]; reverify_at: string | null;
  fingerprint: { golden_set?: string | null; gate_taxonomy?: string | number | null; prompt_sha256?: string;
    writer_route?: { stage: string; model_id: string } | null; judge_route?: { stage: string; model_id: string } | null };
  approved_by: string | null; approved_at: string | null; approval_ref: string | null; activated_at: string | null;
  retired_at: string | null; retired_reason: string | null; rejected_by: string | null; rejected_reason: string | null;
  notes: string | null; created_by: string | null; created_at: string; updated_at: string | null;
  evidence?: Record<"r0" | "eval" | "shadow", EvidenceRun | null>;
}

export interface ReleasesPage { items: Release[]; total: number; limit: number; offset: number }

/** A named evidence run in the release drawer: ids, verdict and a whitelisted summary of its details. */
export interface ReleaseRun {
  id: string; pipeline: string; writer: string; run_kind: string; verdict: string | null; stop_reason: string | null;
  finished_at: string | null; tour_count: number | null; cost_usd: number | string | null;
  summary: { inputs?: number; errors?: number; byte_diffs?: number; byte_diffs_vs_pre?: number;
    compared_with_pre?: number; stored_hashes?: number; stored_hash_match?: number; prompt_sha256?: string;
    git_sha?: string; source?: string; results?: Record<string, number>; reasons?: string[] };
}

export interface ReleaseDetail extends Release {
  runs: Record<"r0" | "eval" | "shadow", ReleaseRun | null>;
  activation: { code: string; detail: string } | null;   // null on an approved release = Activate may run now
}

export interface WriterSwitch {
  writer: Writer; memory_enabled: boolean; train_closed_month: string | null; updated_by: string | null;
  updated_at: string | null;
}

export interface ActiveReleases { items: Release[]; writers: WriterSwitch[] }

/** The evidence runs each content class needs before it becomes `evaluated` (releases.attach_evidence). */
export const EVIDENCE_NEEDED: Record<ContentClass, ("r0" | "eval" | "shadow")[]> = {
  empty: ["r0", "eval"], lexicon_only: ["r0"], repair_hint_only: ["eval", "shadow"], rules: ["eval", "shadow"],
};

/** The run verdict each evidence slot accepts (releases.evidence_problem; `baseline` only for an empty release). */
export function evidenceOk(slot: "r0" | "eval" | "shadow", run: EvidenceRun | null | undefined,
  cls: ContentClass): boolean {
  if (!run || run.missing || !run.finished) return false;
  if (slot === "r0") return run.run_kind === "r0_replay" && run.verdict === "pass";
  if (slot === "shadow") return run.run_kind === "shadow" && run.verdict === "pass";
  return (run.run_kind === "r2" && run.verdict === "pass")
    || (cls === "empty" && run.run_kind === "baseline_import" && run.verdict === "baseline");
}

/** One word per evidence slot for the list: "passed", "failed", "missing" (named, not found), or "—" (not named).
 *  The word is also the key of its UI-language text (EVIDENCE_WORD_MESSAGES). */
export type EvidenceWord = "—" | "missing" | "running" | "baseline" | "passed" | "failed";

export function evidenceWord(slot: "r0" | "eval" | "shadow", run: EvidenceRun | null | undefined,
  cls: ContentClass): EvidenceWord {
  if (!run) return "—";
  if (run.missing) return "missing";
  if (!run.finished) return "running";
  return evidenceOk(slot, run, cls) ? (run.verdict === "baseline" ? "baseline" : "passed") : "failed";
}

export function releaseTone(s: ReleaseStatus): "success" | "danger" | "warning" | "neutral" | "info" | "purple" {
  return s === "active" ? "success" : s === "approved" ? "info" : s === "evaluated" ? "purple"
    : s === "rejected" ? "danger" : s === "draft" ? "warning" : "neutral";
}

/** Who did it, short: "admin 1a2b3c4d" for a staff id (bare or admin:-prefixed), else the actor string as stored. */
export function actorLabel(actor: string | null | undefined): string {
  if (!actor) return "—";
  const id = actor.startsWith("admin:") ? actor.slice(6) : actor;
  return /^[0-9a-f]{8}-[0-9a-f]{4}-/i.test(id) ? `admin ${id.slice(0, 8)}` : actor;
}

/** The actions the backend allows from a status (releases.TRANSITIONS and the route guards). */
export function releaseActions(r: Pick<Release, "status" | "previous_release_id">): {
  evidence: boolean; approve: boolean; activate: boolean; rollback: boolean; reject: boolean } {
  return {
    evidence: r.status === "draft" || r.status === "evaluated",
    approve: r.status === "evaluated",
    activate: r.status === "approved",
    rollback: r.status === "active" && !!r.previous_release_id,
    reject: r.status === "draft" || r.status === "evaluated" || r.status === "approved",
  };
}

/** First day of the month of an <input type="month"> value ("2026-10" → "2026-10-01"), or null. */
export function monthStart(v: string): string | null {
  return /^\d{4}-(0[1-9]|1[0-2])$/.test(v) ? `${v}-01` : null;
}

export function isUuid(v: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(v.trim());
}

// ── post-activation check (POST /releases/{id}/post-activation-check, DRY RUN) ──────────────────────────────────────

export type CheckVerdict = "pass" | "pending" | "violation" | "not_applicable";

/** The report of services/writing_memory/post_activation.check_release: counts, codes and ids, never text. */
export interface PostActivationReport {
  release_id: string; writer: Writer; release_no: number; status: ReleaseStatus; verdict: CheckVerdict;
  n: number; checked: number; carried?: number; not_carried?: number; not_rendered?: number;
  fingerprint_mismatch?: number; violations?: number; violation_codes?: Record<string, number>;
  violating_refs?: { source: string; ref: string; codes: string[] }[]; action: string; reason?: string;
  cost_usd: number;
}

export function checkTone(v: CheckVerdict): "success" | "danger" | "warning" | "neutral" {
  return v === "pass" ? "success" : v === "violation" ? "danger" : v === "pending" ? "warning" : "neutral";
}

/** The sentences checkSentence() prints (CHECK_SENTENCE_MESSAGES has the Vietnamese ones, same keys). */
export const CHECK_TEXT = {
  neverActive: "This release was never active, so it wrote nothing to check.",
  emptyRelease: "An empty release adds nothing to the prompt, so there is nothing to check.",
  violation: "At least one write made with this release breaks a hard stop. The hourly check rolls it back; "
    + "this dry run moved nothing.",
  pass: (carried: number) => `${carried} writes carry this release and none breaks a hard stop.`,
  pending: (carried: number, n: number, checked: number) =>
    `${carried} of the ${n} writes needed carry this release so far, none breaking a hard stop (${checked} writes read).`,
};
export type CheckText = typeof CHECK_TEXT;

/** One plain sentence for the verdict of a dry-run check. */
export function checkSentence(r: Pick<PostActivationReport, "verdict" | "n" | "checked" | "carried" | "reason">,
  text: CheckText = CHECK_TEXT): string {
  const carried = r.carried ?? 0;
  if (r.verdict === "not_applicable") return r.reason === "never activated" ? text.neverActive : text.emptyRelease;
  if (r.verdict === "violation") return text.violation;
  if (r.verdict === "pass") return text.pass(carried);
  return text.pending(carried, r.n, r.checked);
}

/** Labels of the violation codes the check reports (post_activation.check_write). */
export const CHECK_CODE_LABELS: Record<string, string> = {
  qa_trace: "QA trace in the text", tenant_leak: "another tenant's text", fingerprint_mismatch: "rules hash mismatch",
};
