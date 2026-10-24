// lib/writingMemory.ts — AA-917 WM-14: types and helpers of Quality › Writing memory (/admin/writing-memory).
// Backend: api/routers/admin_writing_memory.py (admin only: tenant.read reads, ai.admin writes), through the BFF.

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
    open_waves: number; last_run_at: string | null;
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
  support_reviewers: number; lint: Record<string, LintCheck | string | boolean>; drafted_by: string | null;
  expires_at: string; rejected_by: string | null; rejected_reason: string | null; created_at: string;
}

export interface RulesPage { items: Rule[]; total: number; limit: number; offset: number }
export interface RuleDetail extends Rule { provenance: Signal[] }

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

export function owner(scope: Scope, tenantId: string | null): string {
  return scope === "platform" ? "Platform" : `Tenant ${(tenantId || "").slice(0, 8)}`;
}

export function usd(n: number | null | undefined): string {
  return `$${(n ?? 0).toFixed(2)}`;
}

/** Send a write to the BFF; resolves to { ok, data | message } and never throws. */
export async function wmSend<T>(path: string, method: string, body?: unknown):
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
      return { ok: false, message: msg || `Request failed (${res.status})` };
    }
    return { ok: true, data: json as T };
  } catch {
    return { ok: false, message: "No answer from the server; reload before trying again." };
  }
}
