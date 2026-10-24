// frontend/lib/crmConsole.ts
// AA-941 (card CRM-13a, screen S-01) — types, labels and formatters for Platform › Tenants › Agencies (CRM).
//
// The shapes mirror the partner CRM's crm.ops.v1 L1 responses exactly (CIS relays them unchanged:
// api/routers/admin_crm_console.py → api/services/crm_console.py). L1 data only: agency name, state, meters,
// entitlements, member ids/roles/statuses, request statuses and failed job ids. No trip, pipeline, revenue or
// traveller data exists in these shapes, and none is ever added here.

export const CRM_API = "/api/admin/platform/crm";

// ── responses ──────────────────────────────────────────────────────────────────────────────────

export interface CrmConsole {
  can_write: boolean;
  /** CIS's own ENVIRONMENT (dev | stg | prod): outside prod a new agency must be synthetic. */
  environment?: string;
}

export type AgencyState = "active" | "locked" | "offboarding" | "offboarded";

export interface TenantSummary {
  tenant_id: string;
  agency_name: string;
  state: AgencyState;
  is_synthetic: boolean;
  has_owner: boolean;
  holds_travellers: boolean;
  created_at: string | null;
}

export interface TenantPage {
  items: TenantSummary[];
  next_cursor: string | null;
}

export interface AgencyUsage {
  period_start: string;
  period_end: string;
  seats_used: number;
  seats_allowed: number;
  active_members_30d: number;
  storage_bytes: number;
  inbound_mail_30d: number;
  ai_cost_usd_month: number;
  ai_budget_usd_month: number;
  failed_jobs_24h: number;
  last_activity_at: string | null;
}

export interface AgencyEntitlements {
  plan: string;
  seats_allowed: number;
  features_allowed: string[];
  insights_opt_out: boolean;
}

export type DataRequestKind = "agency_export" | "agency_deletion";
export type DataRequestStatus =
  | "awaiting_owner_confirmation"
  | "confirmed"
  | "rejected"
  | "cancelled"
  | "failed"
  | "done";

export interface DataRequest {
  request_id: string;
  tenant_id: string;
  kind: DataRequestKind;
  status: DataRequestStatus;
  raised_at: string | null;
}

export interface AgencyMember {
  member_id: string;
  role: string;
  status: string;
  joined_at: string | null;
}

export interface PendingRecovery {
  request_id: string;
  eligible_at: string;
  candidate_member_id?: string;
}

export interface AgencyMembers {
  members: AgencyMember[];
  active_owners: number;
  recovery: PendingRecovery | null;
}

export interface FailedJob {
  job_id: string;
  kind: string;
  attempt: number;
  max_attempts: number;
  error_class: string | null;
  finished_at: string | null;
}

/** A list answer: the CRM returns `{items: [...]}` (TenantPage style); older drafts used a named key or a bare
 * array. Read whichever is there so the page does not break on that detail. */
export function listOf<T>(body: unknown, keys: string[]): T[] {
  if (Array.isArray(body)) return body as T[];
  if (body && typeof body === "object") {
    for (const k of ["items", ...keys]) {
      const v = (body as Record<string, unknown>)[k];
      if (Array.isArray(v)) return v as T[];
    }
  }
  return [];
}

// ── request vocabularies (exactly the CRM's codes) ─────────────────────────────────────────────

/** Ticket reference: a project key, a dash and a number (AA-941, KAN-20). The CRM uses the same pattern. */
export const TICKET_RE = /^[A-Za-z][A-Za-z0-9]{0,15}-[0-9]{1,10}$/;
export const isTicket = (v: string) => TICKET_RE.test(v.trim());

export const LOCK_REASONS = [
  { code: "owner_request", label: "The owner asked for it" },
  { code: "payment", label: "Payment problem" },
  { code: "abuse", label: "Abuse" },
  { code: "security", label: "Security incident" },
  { code: "test", label: "Test" },
] as const;

export const FEATURES = [
  { code: "ai_assist", label: "AI writing assist" },
  { code: "whatsapp_sms", label: "WhatsApp and SMS" },
  { code: "stripe_connect", label: "Stripe payments (Connect)" },
  { code: "campaigns", label: "E-mail campaigns" },
  { code: "custom_domain", label: "Custom domain" },
  { code: "insights_export", label: "Insights export" },
] as const;

export const RECOVERY_REASONS = [
  { code: "owner_left", label: "The owner left the agency" },
  { code: "owner_unreachable", label: "The owner cannot be reached" },
  { code: "no_owner", label: "The agency has no owner" },
] as const;

export const DATA_REQUEST_KINDS: { code: DataRequestKind; label: string }[] = [
  { code: "agency_export", label: "Export the agency's data" },
  { code: "agency_deletion", label: "Delete the agency (offboard)" },
];

export const LOCALES = ["en-AU", "en-GB", "en-US", "vi-VN"] as const;

export const RETRYABLE_KINDS = ["crm_intake_process", "crm_dispatch", "crm_offboard"] as const;

// ── labels ─────────────────────────────────────────────────────────────────────────────────────

export type Tone = "neutral" | "accent" | "success" | "warning" | "danger" | "info" | "purple";

export const STATE_LABEL: Record<AgencyState, { label: string; tone: Tone }> = {
  active: { label: "Active", tone: "success" },
  locked: { label: "Locked", tone: "warning" },
  offboarding: { label: "Offboarding", tone: "neutral" },
  offboarded: { label: "Offboarded", tone: "neutral" },
};

export function stateLabel(state: string): { label: string; tone: Tone } {
  return (STATE_LABEL as Record<string, { label: string; tone: Tone }>)[state] ?? { label: state, tone: "neutral" };
}

export const DATA_REQUEST_STATUS_LABEL: Record<DataRequestStatus, { label: string; tone: Tone }> = {
  awaiting_owner_confirmation: { label: "Waiting for the owner", tone: "warning" },
  confirmed: { label: "Confirmed by the owner", tone: "info" },
  rejected: { label: "Declined by the owner", tone: "neutral" },
  cancelled: { label: "Cancelled", tone: "neutral" },
  failed: { label: "Failed", tone: "danger" },
  done: { label: "Done", tone: "success" },
};

export function dataRequestStatus(s: string): { label: string; tone: Tone } {
  return (DATA_REQUEST_STATUS_LABEL as Record<string, { label: string; tone: Tone }>)[s] ?? { label: s, tone: "neutral" };
}

export function featureLabel(code: string): string {
  return FEATURES.find((f) => f.code === code)?.label ?? code;
}

export function kindLabel(code: string): string {
  return DATA_REQUEST_KINDS.find((k) => k.code === code)?.label ?? code;
}

// ── errors ─────────────────────────────────────────────────────────────────────────────────────

/** Plain words for the CRM's (and the relay's) error codes. The code itself is always shown next to them, so
 * "has_travellers" or "owner_exists" reaches the reader verbatim. */
const ERROR_TEXT: Record<string, string> = {
  cognito_session_required: "Sign in with your staff account (Cognito) to use the CRM console.",
  missing_group: "Your staff account is not in the crm-platform-admin group.",
  wrong_audience: "This sign-in cannot call the CRM console (wrong app client).",
  crm_unavailable: "The partner CRM did not answer. Try again in a minute.",
  crm_bad_response: "The partner CRM gave an unexpected answer.",
  crm_path_not_allowed: "This action is not part of the L1 console.",
  tenant_not_found: "This agency does not exist in the CRM.",
  tenant_locked: "The agency is locked. Unlock it first.",
  tenant_status: "Not possible in the agency's current state.",
  has_travellers: "The agency already holds travellers or inquiries, so AA cannot invite a first owner.",
  owner_exists: "The agency already has an owner.",
  owner_invite_pending: "An owner invitation is already pending.",
  member_limit_reached: "The agency has no free seat.",
  invalid_feature: "One of the features is not a known code.",
  offboarding_open: "The agency already has an open export or deletion request.",
  legal_hold: "A legal hold is active, so the agency cannot be deleted.",
  recovery_pending: "An owner recovery is already pending.",
  recovery_not_due: "The 14-day waiting period has not ended yet.",
  recovery_closed: "This recovery was cancelled or already completed.",
  recovery_not_found: "This recovery request does not exist.",
  candidate_inactive: "The candidate is no longer an active member.",
  candidate_is_owner: "The candidate is already an owner.",
  member_not_found: "This member does not exist.",
  job_not_found: "This job does not exist, is not failed, or belongs to another agency.",
  synthetic_required: "This environment only accepts synthetic (test) agencies.",
  synthetic_not_allowed: "Production does not accept synthetic agencies.",
  synthetic_flag_mismatch: "The agency already exists with the other synthetic setting.",
  invalid_display_name: "The agency name is not valid.",
  invalid_locale: "The locale must be en-AU, en-GB, en-US or vi-VN.",
  invalid_ticket_ref: "The ticket reference must look like AA-941.",
};

/** One readable line for a failed call: plain words + the code. `detail` may be a code string or FastAPI's 422
 * list of {loc, msg}. */
export function crmErrorText(status: number, detail: unknown, message?: unknown): string {
  if (Array.isArray(detail)) {
    const msgs = detail
      .map((d) => (d && typeof d === "object" && "msg" in d ? String((d as { msg: unknown }).msg) : ""))
      .filter(Boolean);
    return msgs.length ? `Check the form: ${msgs.join("; ")}` : `Request refused (${status}).`;
  }
  if (typeof detail === "string" && detail) {
    if (status === 403 && detail.startsWith("Role '")) return "Admin only: your CIS role cannot do this.";
    const plain = ERROR_TEXT[detail] ?? (typeof message === "string" && message ? message : null);
    return plain ? `${plain} (${detail})` : detail;
  }
  return `Request failed (${status}).`;
}

export type WriteResult<T = Record<string, unknown>> =
  | { ok: true; status: number; body: T }
  | { ok: false; status: number; message: string; code: string | null };

/** One console write through the BFF. Never throws. The body is not kept or logged anywhere. */
export async function crmWrite<T = Record<string, unknown>>(
  method: "POST" | "PUT",
  path: string,
  body?: unknown,
): Promise<WriteResult<T>> {
  let res: Response;
  try {
    res = await fetch(`${CRM_API}${path}`, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    return { ok: false, status: 0, message: "Network error: nothing was sent. Try again.", code: null };
  }
  let data: Record<string, unknown> = {};
  try {
    data = (await res.json()) as Record<string, unknown>;
  } catch {
    /* non-JSON body */
  }
  if (res.ok) return { ok: true, status: res.status, body: data as T };
  return {
    ok: false,
    status: res.status,
    message: crmErrorText(res.status, data.detail, data.message),
    code: typeof data.detail === "string" ? data.detail : null,
  };
}

// ── formatters ─────────────────────────────────────────────────────────────────────────────────

export function formatBytes(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}

export function formatUsd(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return `$${n < 1 ? n.toFixed(3) : n.toFixed(2)}`;
}

export function pct(used: number, allowed: number): number {
  if (!allowed || allowed <= 0) return 0;
  return Math.max(0, Math.min(100, Math.round((100 * used) / allowed)));
}

export const shortId = (id: string | null | undefined) => (id ? id.slice(0, 8) : "—");

/** May the owner recovery be completed now? (eligible_at has passed; the CRM re-checks it). */
export function recoveryDue(eligibleAt: string | null | undefined, now: number = Date.now()): boolean {
  if (!eligibleAt) return false;
  const t = Date.parse(eligibleAt);
  return Number.isFinite(t) && t <= now;
}
