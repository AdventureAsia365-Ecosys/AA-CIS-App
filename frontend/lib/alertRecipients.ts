// lib/alertRecipients.ts — AA-913: Platform › People & access › Alert recipients (types + pure helpers).
//
// Mirrors the backend body of GET /admin/platform/alert-recipients (api/services/alert_recipients.page and
// api/routers/admin_alert_recipients.py). Pure, so a test or a Playwright spec can import it.

export type RecipientStatus = "pending" | "confirmed" | "removed";
export type RemovedReason = "admin" | "unsubscribed" | "expired";
export type SnsState = "confirmed" | "pending" | "missing";

export interface AlertRecipient {
  id: string;
  email: string;
  note: string | null;
  status: RecipientStatus;
  removed_reason: RemovedReason | null;
  has_subscription: boolean;
  created_at: string | null;
  created_by: string | null;
  confirmed_at: string | null;
  removed_at: string | null;
  removed_by: string | null;
  last_confirmation_sent_at: string | null;
  last_seen_state: SnsState | null;
  last_reconciled_at: string | null;
  resend_available_at: string | null;
}

export interface AlertRecipientsPage {
  configured: boolean;
  topic_name: string | null;
  bootstrap: { email: string; locked: true; state: SnsState | null } | null;
  ops_fallback_email: string | null;
  ops_mail_configured: boolean;
  recipients: AlertRecipient[];
  unmanaged: { email: string; state: SnsState }[];
  history: AlertRecipient[];
  limits: { max_active: number; active: number; adds_left_today: number; max_note: number };
  reconciled_at: string | null;
  reconcile_error: string | null;
  viewer?: { auth: "cognito" | "legacy"; can_write: boolean };
}

export type StatusTone = "success" | "warning" | "neutral" | "danger";

/** The status badge text. */
export function statusLabel(r: Pick<AlertRecipient, "status" | "removed_reason">): string {
  if (r.status === "confirmed") return "Confirmed";
  if (r.status === "pending") return "Waiting for confirmation";
  switch (r.removed_reason) {
    case "unsubscribed":
      return "Unsubscribed";
    case "expired":
      return "Expired (not confirmed)";
    default:
      return "Removed";
  }
}

export function statusTone(r: Pick<AlertRecipient, "status" | "removed_reason">): StatusTone {
  if (r.status === "confirmed") return "success";
  if (r.status === "pending") return "warning";
  return r.removed_reason === "admin" ? "neutral" : "danger";
}

/** What SNS says about a subscription the page does not manage (bootstrap, console-added). */
export function snsStateLabel(state: SnsState | null | undefined): string {
  if (state === "confirmed") return "Confirmed";
  if (state === "pending") return "Waiting for confirmation";
  if (state === "missing") return "Not subscribed";
  return "Unknown";
}

// Same rule as the backend (shared/alert_recipients.normalize_email) and the table CHECK (migration 251).
const EMAIL_RE = /^[^@\s,;<>"]+@[^@\s,;<>"]+\.[^@\s,;<>"]+$/;
export const MAX_EMAIL_LEN = 254;

/** One mailbox, as the backend will accept it (the backend re-checks). */
export function emailLooksValid(raw: string): boolean {
  const v = raw.trim().toLowerCase();
  return v.length > 0 && v.length <= MAX_EMAIL_LEN && v.split("@").length === 2 && EMAIL_RE.test(v);
}

export const COMPANY_DOMAIN = "adventure.asia";

/** Alerts carry security details (who read a secret, from which IP): warn before adding an outside mailbox. */
export function isCompanyDomain(raw: string): boolean {
  const v = raw.trim().toLowerCase();
  return v.endsWith(`@${COMPANY_DOMAIN}`);
}

/** Seconds until `iso` (0 when past or unknown). For the Resend countdown. */
export function secondsUntil(iso: string | null | undefined, now: number = Date.now()): number {
  if (!iso) return 0;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return 0;
  return Math.max(0, Math.ceil((t - now) / 1000));
}

export function formatWait(seconds: number): string {
  if (seconds <= 0) return "now";
  if (seconds < 60) return `${seconds}s`;
  const m = Math.ceil(seconds / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
}
