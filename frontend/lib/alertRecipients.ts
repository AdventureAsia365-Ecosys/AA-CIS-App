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
  /** Removed on this page while unconfirmed: AWS still lists the pending subscription until it expires (3 days). */
  leaving?: { email: string; state: SnsState }[];
  history: AlertRecipient[];
  limits: { max_active: number; active: number; adds_left_today: number; max_note: number };
  reconciled_at: string | null;
  reconcile_error: string | null;
  viewer?: { auth: "cognito" | "legacy"; can_write: boolean };
}

export type StatusTone = "success" | "warning" | "neutral" | "danger";

/** AA-994: the words of the helpers below (English). The page passes the UI language's words
 * (lib/alertRecipients.messages.ts ALERT_WORDS_MESSAGES[lang]); a call without them stays English. */
export const ALERT_WORDS = {
  confirmed: "Confirmed",
  pending: "Waiting for confirmation",
  unsubscribed: "Unsubscribed",
  expired: "Expired (not confirmed)",
  removed: "Removed",
  notSubscribed: "Not subscribed",
  unknown: "Unknown",
  waitNow: "now",
  waitS: (s: number) => `${s}s`,
  waitMin: (m: number) => `${m} min`,
  waitHMin: (h: number, m: number) => `${h} h ${m} min`,
};
export type AlertWords = typeof ALERT_WORDS;

/** The status badge text. */
export function statusLabel(r: Pick<AlertRecipient, "status" | "removed_reason">, w: AlertWords = ALERT_WORDS): string {
  if (r.status === "confirmed") return w.confirmed;
  if (r.status === "pending") return w.pending;
  switch (r.removed_reason) {
    case "unsubscribed":
      return w.unsubscribed;
    case "expired":
      return w.expired;
    default:
      return w.removed;
  }
}

export function statusTone(r: Pick<AlertRecipient, "status" | "removed_reason">): StatusTone {
  if (r.status === "confirmed") return "success";
  if (r.status === "pending") return "warning";
  return r.removed_reason === "admin" ? "neutral" : "danger";
}

/** What SNS says about a subscription the page does not manage (bootstrap, console-added). */
export function snsStateLabel(state: SnsState | null | undefined, w: AlertWords = ALERT_WORDS): string {
  if (state === "confirmed") return w.confirmed;
  if (state === "pending") return w.pending;
  if (state === "missing") return w.notSubscribed;
  return w.unknown;
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

/** Edit distance with adjacent swaps (optimal string alignment), small inputs only. */
function editDistance(a: string, b: string): number {
  const d: number[][] = Array.from({ length: a.length + 1 }, (_, i) => [i, ...Array(b.length).fill(0)]);
  for (let j = 1; j <= b.length; j++) d[0][j] = j;
  for (let i = 1; i <= a.length; i++) {
    for (let j = 1; j <= b.length; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      d[i][j] = Math.min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost);
      if (i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) d[i][j] = Math.min(d[i][j], d[i - 2][j - 2] + 1);
    }
  }
  return d[a.length][b.length];
}

/** The company address the admin probably meant when the domain is a near miss of adventure.asia (e.g. "aisa",
 *  "adventrue"), else null. A mistyped domain never receives the AWS confirmation e-mail, and nothing on the page
 *  says why (S245: admin@adventure.aisa). */
export function companyDomainSuggestion(raw: string): string | null {
  const v = raw.trim().toLowerCase();
  const at = v.lastIndexOf("@");
  if (at <= 0) return null;
  const domain = v.slice(at + 1);
  if (!domain || domain === COMPANY_DOMAIN) return null;
  return editDistance(domain, COMPANY_DOMAIN) <= 2 ? `${v.slice(0, at)}@${COMPANY_DOMAIN}` : null;
}

/** Seconds to wait from a refused (429) change. The body's `retry_after_s` comes first: the admin BFF proxy forwards
 *  only Content-Type, so the Retry-After header normally never reaches the browser. */
export function retryAfterSeconds(body: { retry_after_s?: unknown } | null | undefined, header: string | null): number | undefined {
  for (const raw of [body?.retry_after_s, header]) {
    const n = typeof raw === "number" ? raw : Number(raw ?? "");
    if (raw !== null && raw !== undefined && raw !== "" && Number.isFinite(n) && n > 0) return Math.ceil(n);
  }
  return undefined;
}

/** Seconds until `iso` (0 when past or unknown). For the Resend countdown. */
export function secondsUntil(iso: string | null | undefined, now: number = Date.now()): number {
  if (!iso) return 0;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return 0;
  return Math.max(0, Math.ceil((t - now) / 1000));
}

export function formatWait(seconds: number, w: AlertWords = ALERT_WORDS): string {
  if (seconds <= 0) return w.waitNow;
  if (seconds < 60) return w.waitS(seconds);
  const m = Math.ceil(seconds / 60);
  return m < 60 ? w.waitMin(m) : w.waitHMin(Math.floor(m / 60), m % 60);
}
