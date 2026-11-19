// frontend/lib/connectionBudgets.ts — AA-1143 (tech lead decision G3): AI gateway › Budgets, "Connection budgets".
// One budget per LLM connection: its total (the provider budget) and one fixed-USD share per app; CIS takes what is
// left (the remainder), which the CIS gateway enforces. Backend: GET / PUT /admin/ai-gateway/connection-budgets
// (api/services/llm_connection_budgets.py; the rows shared.llm_connection_budget, CIS 455 / 456). The CRM gets its
// copy over crm.ops.v1; TripPlanner reads catalog.v_llm_connection_cap_v2.

export const CONNECTION_BUDGETS_API = "/api/admin/ai-gateway/connection-budgets";
export const BUDGETS_TAB_HREF = "/admin/ai-gateway?tab=budgets";

export interface BudgetRowVersion {
  usd: number;
  version: number;
  reason: string;
  actor: string;
  valid_from: string;
}

export type CrmState = "not_sent" | "in_sync" | "differs" | "unknown";

export interface ConnectionBudgetRow {
  connection_key: string;
  label: string;
  kind: string;
  enabled: boolean;
  total: BudgetRowVersion | null;
  /** Every share app (TripPlanner, CRM, Booking): null = no share of its own. */
  shares: Record<string, BudgetRowVersion | null>;
  cis_remainder_usd: number | null;
  /** The cap the CIS gateway enforces (the rows' trigger keeps it equal to the remainder). */
  cis_cap_usd: number | null;
  cis_cap_in_sync: boolean;
  /** This UTC month's CIS-ledger spend per app (the CRM keeps its own ledger). */
  spend: Record<string, number>;
  cis_pool_spent_usd: number;
  alert_pct: number;
  hard_stop: boolean;
  crm_expected_usd: number | null;
  crm_copy_usd: number | null;
  crm_setting_usd: number | null;
  crm_state: CrmState;
}

export interface ConnectionBudgetsResp {
  ready: boolean;
  apps: string[];
  rows: ConnectionBudgetRow[];
  crm: { copy_error: string | null; setting_error: string | null; sent_at: string | null } | null;
  errors: { source: string; error: string }[];
  message?: string;
}

export interface HistoryResp {
  connection_key: string;
  versions: (BudgetRowVersion & { app: string | null })[];
}

/** The editor's text fields: the total and each share app ("" = no share). */
export interface BudgetDraft {
  total: string;
  shares: Record<string, string>;
  reason: string;
}

export const REASON_MIN = 20;
export const REASON_MAX = 500;
const AMOUNT_RE = /^\d{1,7}(\.\d{1,2})?$/;

export function draftOf(row: ConnectionBudgetRow, apps: string[]): BudgetDraft {
  const shares: Record<string, string> = {};
  for (const a of apps) shares[a] = row.shares[a] == null ? "" : String(row.shares[a]!.usd);
  return { total: row.total == null ? "" : String(row.total.usd), shares, reason: "" };
}

function amount(text: string): number | null | "bad" {
  const s = text.trim();
  if (s === "") return null;
  if (!AMOUNT_RE.test(s)) return "bad";
  const v = Number(s);
  return v > 1_000_000 ? "bad" : v;
}

export type DraftError = "amount" | "totalRequired" | "overAllocated" | "reason";

export interface DraftCheck {
  /** The PUT body; null when nothing changed or the draft has errors. */
  body: { total_usd?: number; shares: Record<string, number | null>; reason: string } | null;
  errors: Partial<Record<string, DraftError>>;
  /** The CIS remainder the draft leaves (null without a total). */
  remainder: number | null;
  changed: boolean;
}

const cents = (v: number) => Math.round(v * 100);
const sameCents = (a: number | null, b: number | null) => (a == null ? null : cents(a)) === (b == null ? null : cents(b));

interface ShareScan {
  shares: Record<string, number | null>;
  sum: number;
  typed: boolean;
  errors: Partial<Record<string, DraftError>>;
}

/** The share fields: the changed ones (null = removed), their sum, and the bad amounts. Pure. */
function scanShares(row: ConnectionBudgetRow, apps: string[], d: BudgetDraft): ShareScan {
  const out: ShareScan = { shares: {}, sum: 0, typed: false, errors: {} };
  for (const a of apps) {
    const v = amount(d.shares[a] ?? "");
    if (v === "bad") {
      out.errors[a] = "amount";
      out.typed = true;
      continue;
    }
    if (!sameCents(v, row.shares[a]?.usd ?? null)) out.shares[a] = v;
    out.sum += v ?? 0;
  }
  return out;
}

function totalError(total: number | null | "bad", remainder: number | null): DraftError | undefined {
  if (total === "bad") return "amount";
  if (total == null) return "totalRequired";
  return remainder != null && remainder < 0 ? "overAllocated" : undefined;
}

function reasonOk(reason: string): boolean {
  return reason.length >= REASON_MIN && reason.length <= REASON_MAX;
}

function totalChange(row: ConnectionBudgetRow, total: number | null | "bad"): number | null {
  return typeof total === "number" && !sameCents(total, row.total?.usd ?? null) ? total : null;
}

function errorsOf(scan: ShareScan, tErr: DraftError | undefined, changed: boolean, reason: string) {
  const errors: Partial<Record<string, DraftError>> = { ...scan.errors };
  if (tErr) errors.total = tErr;
  if (changed && !reasonOk(reason)) errors.reason = "reason";
  return errors;
}

/** Check a draft against the row the way the API does (amounts 0..1000000 with cents, a total first, shares never
 * above the total, a reason of 20..500 characters) and build the PUT body with only what changed. Pure. */
export function checkDraft(row: ConnectionBudgetRow, apps: string[], d: BudgetDraft): DraftCheck {
  const total = amount(d.total);
  const scan = scanShares(row, apps, d);
  const remainder = typeof total === "number" ? (cents(total) - cents(scan.sum)) / 100 : null;
  const newTotal = totalChange(row, total);
  const changed = newTotal != null || scan.typed || total === "bad" || Object.keys(scan.shares).length > 0;
  const reason = d.reason.trim();
  const errors = errorsOf(scan, totalError(total, remainder), changed, reason);
  const ok = changed && Object.keys(errors).length === 0;
  const totalPart = newTotal != null ? { total_usd: newTotal } : {};
  return { body: ok ? { ...totalPart, shares: scan.shares, reason } : null, errors, remainder, changed };
}

export type BudgetTone = "success" | "warning" | "danger" | "neutral";

/** The row's state in one word: no total yet, the CRM copy not in step, the CIS cap not equal to the remainder (a
 * hand edit before the rows), the remainder used up, else ok. */
export function rowState(r: ConnectionBudgetRow): { key: string; tone: BudgetTone } {
  if (r.total == null) return { key: "noTotal", tone: "neutral" };
  if (!r.cis_cap_in_sync) return { key: "capOutOfSync", tone: "danger" };
  if (r.crm_state === "differs" || r.crm_state === "not_sent") return { key: "crmNotInStep", tone: "warning" };
  if (r.cis_remainder_usd != null && r.cis_pool_spent_usd >= r.cis_remainder_usd && r.hard_stop) {
    return { key: "cisStopped", tone: "danger" };
  }
  if (r.crm_state === "unknown") return { key: "crmUnknown", tone: "warning" };
  return { key: "ok", tone: "success" };
}

/** Connections with a total first (by key), then the ones without. */
export function budgetOrder(rows: ConnectionBudgetRow[]): ConnectionBudgetRow[] {
  return [...rows].sort((a, b) => Number(a.total == null) - Number(b.total == null)
    || a.connection_key.localeCompare(b.connection_key));
}

/** The app names shown in the UI (never an internal id). */
export function appLabel(app: string): string {
  return ({ cis: "CIS", tripplanner: "TripPlanner", crm: "CRM", booking: "Booking" } as Record<string, string>)[app]
    ?? app;
}
