// frontend/lib/crmConsole.ts
// AA-941 (card CRM-13a, screen S-01) — types, labels and formatters for Platform › Tenants › Agencies (CRM).
//
// The shapes mirror the partner CRM's crm.ops.v1 L1 response models exactly (AA-Partner api/app/routers/ops.py; CIS
// relays them unchanged: api/routers/admin_crm_console.py → api/services/crm_console.py). Money comes as a decimal
// STRING (Pydantic Decimal → "0.1234"), never a JSON number: read it with parseUsd() / formatUsd() only. L1 data only: agency name, state, meters,
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
  created_at: string;
  /** crm.ops.v1 1.2 (AA-978): who locked the agency, set exactly while `state` is "locked" (null otherwise). Absent
   * from a 1.1 producer. */
  lock_source?: LockSource | null;
  /** 1.2: the CIS membership feed the CRM pulls, platform-wide (the same value on every agency). Absent = unknown. */
  membership_feed_state?: MembershipFeedState;
  /** 1.2: seconds since the feed's last successful pull; null before the first one. */
  membership_feed_lag_s?: number | null;
}

/** aa_ops: the L1 console (and every lock older than the CRM's lock source); cis_tenant: CIS locked or deleted the
 * tenant; cis_membership: CIS removed the agency's last active owner. */
export type LockSource = "aa_ops" | "cis_tenant" | "cis_membership";
export type MembershipFeedState = "ok" | "pending" | "stale" | "failing" | "unknown";

export interface TenantPage {
  items: TenantSummary[];
  next_cursor: string | null;
}

export interface AgencyUsage {
  tenant_id: string;
  period_start: string;
  period_end: string;
  seats_used: number;
  seats_allowed: number;
  active_members_30d: number;
  storage_bytes: number;
  inbound_mail_30d: number;
  /** Decimal string in USD, 4 decimals ("0.1234"): AA's own platform AI spend for the agency this month. */
  ai_cost_usd_month: string;
  /** Decimal string in USD, 4 decimals ("2.0000"). */
  ai_budget_usd_month: string;
  failed_jobs_24h: number;
  last_activity_at: string | null;
}

export interface AgencyEntitlements {
  plan: "free" | "core" | "pro";
  seats_allowed: number;
  features_allowed: string[];
  insights_opt_out: boolean;
}

export type DataRequestKind = "agency_export" | "agency_deletion";
export type DataRequestStatus =
  | "awaiting_owner_confirmation"
  | "confirmed"
  | "export_downloaded"
  | "rejected"
  | "cancelled"
  | "failed"
  | "done";

export interface DataRequest {
  request_id: string;
  tenant_id: string;
  kind: DataRequestKind;
  status: DataRequestStatus;
  raised_at: string;
}

/** GET …/data-requests: the latest agency-scope requests, newest first. */
export interface DataRequestList {
  items: DataRequest[];
}

/** POST …/data-requests (202). */
export interface DataRequestRaised extends DataRequest {
  owners_notified: number;
}

export interface AgencyMember {
  member_id: string;
  role: "owner" | "agent" | "assistant";
  status: string;
  joined_at: string | null;
}

export interface PendingRecovery {
  request_id: string;
  eligible_at: string | null;
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

/** GET …/jobs: the agency's failed jobs of the last 7 days (no payload). */
export interface FailedJobList {
  items: FailedJob[];
}

// Write answers (the CRM's response models).
export interface OwnerInviteAccepted {
  tenant_id: string;
  invitation_id: string;
  status: "pending";
  expires_at: string;
}

export interface InsightsOptOut {
  opt_out: boolean;
}

export interface JobRetried {
  job_id: string;
  status: "queued";
}

export interface MemberDeactivated {
  member_id: string;
  status: "disabled";
  signed_out: boolean;
  active_owners: number;
}

export interface RecoveryOpened {
  request_id: string;
  status: "pending";
  eligible_at: string | null;
  owners_notified: number;
}

export interface RecoveryCompleted {
  request_id: string;
  status: "completed";
  owner_member_id: string;
}

// Request bodies (the CRM's models are closed: extra="forbid", so a field outside these is a 422).
export interface TicketBody {
  ticket_ref: string;
}

export interface InsightsOptOutBody {
  opt_out: boolean;
  owner_request_ref: string;
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

// AA-978 follow-up (crm.ops.v1 1.2): who locked a locked agency, and the CIS membership feed's health.
export const LOCK_SOURCE_LABEL: Record<LockSource, { label: string; short: string; detail: string }> = {
  aa_ops: {
    label: "Locked by AA",
    short: "by AA staff",
    detail: "Locked from this console (or before the CRM recorded who locked). An AA unlock lifts it.",
  },
  cis_tenant: {
    label: "Locked by CIS: tenant locked or deleted",
    short: "by CIS tenant lifecycle",
    detail: "CIS locked or deleted this tenant, and the membership feed passed it on. A CIS unlock lifts it.",
  },
  cis_membership: {
    label: "Locked by CIS: no active owner",
    short: "by CIS: no active owner",
    detail: "CIS removed the agency's last active owner. The owner recovery or an AA unlock lifts it.",
  },
};

/** Who locked the agency, or null when it is not locked. A locked agency from a 1.1 producer (no lock_source) reads
 * "source not reported"; a source this page does not know yet is shown raw. `short` is the few words the agency list
 * puts next to the State badge ("by AA staff", "by CIS tenant lifecycle", …). */
export function lockSourceText(
  summary: Pick<TenantSummary, "state" | "lock_source">,
): { code: string | null; label: string; short: string; detail: string } | null {
  if (summary.state !== "locked") return null;
  const src = summary.lock_source;
  if (!src) {
    return { code: null, label: "Locked (source not reported)", short: "source not reported",
      detail: "The CRM did not say who locked it." };
  }
  const known = Object.hasOwn(LOCK_SOURCE_LABEL, src) ? LOCK_SOURCE_LABEL[src as LockSource] : undefined;
  return known ? { code: src, ...known } : { code: src, label: `Locked (${src})`, short: `by ${src}`, detail: "" };
}

export const FEED_STATE_LABEL: Record<MembershipFeedState, { label: string; tone: Tone; detail: string }> = {
  ok: { label: "OK", tone: "success", detail: "The CRM is pulling CIS membership changes." },
  pending: {
    label: "Pending",
    tone: "info",
    detail: "No successful pull yet (also while the feed is not configured).",
  },
  stale: { label: "Stale", tone: "warning", detail: "No successful pull for 10 minutes." },
  failing: { label: "Failing", tone: "danger", detail: "The last pulls failed." },
  unknown: { label: "Unknown", tone: "neutral", detail: "The CRM cannot read the feed state." },
};

/** Seconds as "45 s", "12 min", "3 h 5 min", "2 d 4 h"; null → "no successful pull yet". */
export function formatLag(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "no successful pull yet";
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 24) return m % 60 ? `${h} h ${m % 60} min` : `${h} h`;
  const d = Math.floor(h / 24);
  return h % 24 ? `${d} d ${h % 24} h` : `${d} d`;
}

/** The CIS membership feed's health word, tone and last successful pull ("12 min ago" / "none yet"). An absent or
 * unrecognised state reads "unknown". */
export function membershipFeed(
  summary: Pick<TenantSummary, "membership_feed_state" | "membership_feed_lag_s">,
): { state: MembershipFeedState; label: string; tone: Tone; detail: string; lastPull: string } {
  const raw = summary.membership_feed_state;
  const state: MembershipFeedState = raw && Object.hasOwn(FEED_STATE_LABEL, raw) ? raw : "unknown";
  const lag = summary.membership_feed_lag_s;
  const lastPull = lag == null || !Number.isFinite(lag) ? "none yet" : `${formatLag(lag)} ago`;
  return { state, ...FEED_STATE_LABEL[state], lastPull };
}

/** The membership feed health for the whole page: the CRM reports the same platform-wide value on every agency, so the
 * first loaded agency carries it. Null when no agency is loaded (the CRM then reported nothing). */
export function platformMembershipFeed(
  items: ReadonlyArray<Pick<TenantSummary, "membership_feed_state" | "membership_feed_lag_s">>,
): ReturnType<typeof membershipFeed> | null {
  return items.length ? membershipFeed(items[0]) : null;
}

export const DATA_REQUEST_STATUS_LABEL: Record<DataRequestStatus, { label: string; tone: Tone }> = {
  awaiting_owner_confirmation: { label: "Waiting for the owner", tone: "warning" },
  confirmed: { label: "Confirmed by the owner", tone: "info" },
  // Still the agency's open request: a new export or deletion is refused (offboarding_open) until the owner closes it.
  export_downloaded: { label: "Downloaded; the owner has not closed it yet", tone: "warning" },
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
  tenant_exists: "An agency with this id already exists in the CRM. Nothing was changed.",
  owner_invite_pending: "An owner invitation is already pending.",
  member_limit_reached: "The agency has no free seat.",
  invalid_feature: "One of the features is not a known code.",
  offboarding_open:
    "The agency already has an open export or deletion request (a downloaded export stays open until the owner closes it).",
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

/** A CRM money value (a decimal string such as "0.1234"; a number is accepted too) as a number, or null when it is
 * missing or not a finite number. */
export function parseUsd(v: string | number | null | undefined): number | null {
  if (v == null) return null;
  if (typeof v === "string" && v.trim() === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

export function formatUsd(v: string | number | null | undefined): string {
  const n = parseUsd(v);
  if (n == null) return "—";
  return `$${n < 1 ? n.toFixed(3) : n.toFixed(2)}`;
}

export function pct(used: number, allowed: number): number {
  if (!allowed || allowed <= 0) return 0;
  return Math.max(0, Math.min(100, Math.round((100 * used) / allowed)));
}

/** The AI spend bar: cost / budget, both CRM decimal strings; 0 when either is missing. */
export function usdPct(cost: string | number | null | undefined, budget: string | number | null | undefined): number {
  return pct(parseUsd(cost) ?? 0, parseUsd(budget) ?? 0);
}

export const shortId = (id: string | null | undefined) => (id ? id.slice(0, 8) : "—");

/** May the owner recovery be completed now? (eligible_at has passed; the CRM re-checks it). */
export function recoveryDue(eligibleAt: string | null | undefined, now: number = Date.now()): boolean {
  if (!eligibleAt) return false;
  const t = Date.parse(eligibleAt);
  return Number.isFinite(t) && t <= now;
}
