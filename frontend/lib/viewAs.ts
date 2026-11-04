// frontend/lib/viewAs.ts — AA-1035 R2 slice 2 (root ADR 0019 d5): the pure parts of the read-only "view as tenant".
//
// The flow: Tenant 360 › "View as partner" (a reason, an optional ticket, 5-60 minutes) → POST /api/view-as (BFF,
// app/api/view-as/route.ts) → CIS POST /admin/tenants/{id}/view-as, which logs the session and returns a short-lived
// read-only tenant token. The BFF keeps that token in an httpOnly cookie scoped to /api/view-as (never readable by the
// page) and forwards only the allowed reads (GET /api/view-as/v1/<path>, app/api/view-as/v1/[...path]/route.ts). The
// page /admin/tenants/view-as shows a banner (tenant, time left, reason, End view) and the partner's own data.
//
// The backend is the authority (api/core/view_as.py READ_ROUTES, the gate refuses everything else and logs every
// request); VIEW_AS_READ_ROUTES mirrors it so the BFF never forwards anything else (tests/unit/test_aa1035_s2_view_as.py
// reads this file and keeps the two lists equal). Dependency-free: no React, no Node APIs.
//
// S268 fix pass: (1) the reads depend on the staff role (VIEW_AS_ROUTE_ROLES, the mirror of api/core/view_as.py
// ROUTE_CAPS): view-as never shows a role more than its own staff pages (support never sees the plan / usage read).
// (2) Only app/api/view-as/route.ts may start a session: the generic /api/admin proxy refuses the start path
// (isViewAsStartPath), and the backend answers the token only with the header VIEW_AS_BFF_HEADER.

/** The staff roles that may open a view (backend capability tenant.view_as). */
export const VIEW_AS_ROLES = ["admin", "customer_success", "support"] as const;

/** The httpOnly cookie that holds the view-as token, only ever sent to /api/view-as. */
export const VIEW_AS_COOKIE = "aa_view_as";
export const VIEW_AS_COOKIE_PATH = "/api/view-as";

export const VIEW_AS_PAGE = "/admin/tenants/view-as";
export const VIEW_AS_API = "/api/view-as";
export const VIEW_AS_QUERY_KEY = ["view-as", "current"] as const;

export const MIN_MINUTES = 5;
export const MAX_MINUTES = 60;
export const DEFAULT_MINUTES = 30;
export const MINUTE_CHOICES = [15, 30, 60] as const;
export const REASON_MIN = 3;
export const REASON_MAX = 500;
export const TICKET_MAX = 120;

// The tenant reads a view-as session may make (route templates; api/core/view_as.py READ_ROUTES, same order).
// No /v1/billing: no view-as page reads it, and support may never see billing.
export const VIEW_AS_READ_ROUTES = [
  "/v1/quota",
  "/v1/marketplace",
  "/v1/tours/my-versions",
  "/v1/tours/versions/{version_id}",
  "/v1/content-writing/reviews",
  "/v1/content-writing/pieces/{piece_id}",
  "/v1/publish-log",
  "/v1/publish-log/pending",
  "/v1/notifications",
  "/v1/facts",
  "/v1/account/members",
  "/v1/account/activity",
  "/v1/account/add-ons",
] as const;

// The reads that need a capability of their own on top of tenant.view_as: the staff roles that may make each one
// (api/core/view_as.py ROUTE_CAPS; /v1/quota carries the plan tier and usage, /v1/account/add-ons the add-on
// subscription statuses and the request decision notes). A read not listed: every view-as role.
export const VIEW_AS_ROUTE_ROLES: Readonly<Record<string, readonly string[]>> = {
  "/v1/quota": ["admin", "customer_success"],
  "/v1/account/add-ons": ["admin", "customer_success"],
};

const READ_PATTERNS = VIEW_AS_READ_ROUTES.map((t) =>
  new RegExp("^" + t.split(/(\{[^}]+\})/).map((p) => (p.startsWith("{") ? "[^/]+" : p.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))).join("") + "$"));

/** May a staff member of `role` make the view-as read `template` (a VIEW_AS_READ_ROUTES entry)? */
export function viewAsRoleMayRead(role: string | null | undefined, template: string): boolean {
  if (!role || !(VIEW_AS_ROLES as readonly string[]).includes(role)) return false;
  if (!(VIEW_AS_READ_ROUTES as readonly string[]).includes(template)) return false;
  const roles = VIEW_AS_ROUTE_ROLES[template];
  return !roles || roles.includes(role);
}

/** The reads `role` may make in a view-as session (the page shows only these). */
export function viewAsReadsForRole(role: string | null | undefined): string[] {
  return VIEW_AS_READ_ROUTES.filter((t) => viewAsRoleMayRead(role, t));
}

/** May the BFF forward GET `path` (e.g. "/v1/quota") for a view-as session of a staff member of `role`? The path is
 *  matched in full against the allowed reads, then the read must be one that role may make. */
export function viewAsPathAllowed(path: string, role: string | null | undefined): boolean {
  const i = READ_PATTERNS.findIndex((re) => re.test(path));
  return i >= 0 && viewAsRoleMayRead(role, VIEW_AS_READ_ROUTES[i]);
}

/** The request header the view-as BFF sets on every read: the signed-in member's verified staff role (requireAdmin).
 *  The API ends the session when it is not the role the session was started with (the token's `srl`). */
export const VIEW_AS_ROLE_HEADER = "x-view-as-staff-role";

/** Does the BFF forward GET `path` of a session started as `startRole` for a member now signed in as `role`?
 *  Same role: only the reads that role may make (viewAsPathAllowed). Role changed since the start: any allowed read
 *  path is forwarded with VIEW_AS_ROLE_HEADER so the API ends the session (cause role_changed) and logs it; the API
 *  never serves such a read. Never a path outside the allowed reads. */
export function viewAsForward(path: string, role: string | null | undefined, startRole: string | null | undefined): boolean {
  if (role && startRole && role !== startRole) return READ_PATTERNS.some((re) => re.test(path));
  return viewAsPathAllowed(path, role);
}

/** The request header the view-as BFF sets on the start call; the backend answers the token only with it. */
export const VIEW_AS_BFF_HEADER = "x-view-as-client";

/** Is `path` (a backend /admin sub-path, no leading slash, as the generic /api/admin proxy sees it) the view-as start
 *  route? The generic proxy refuses it for every method and role, so the raw token can only come back to
 *  app/api/view-as/route.ts, which keeps it in an httpOnly cookie. Generous on purpose: any case, any trailing part. */
export function isViewAsStartPath(path: string): boolean {
  const parts = path.split("/").filter((p) => p !== "");
  return parts.length >= 3 && parts[0].toLowerCase() === "tenants" && parts[2].toLowerCase() === "view-as";
}

export interface ViewAsSession {
  session_id: string;
  tenant_id: string;
  tenant_name: string;
  tenant_slug?: string;
  staff_name?: string | null;
  reason?: string;
  ticket_ref?: string | null;
  started_at: string | null;
  expires_at: string | null;
  ended_at?: string | null;
  end_reason?: string | null;
  open: boolean;
  read_only: true;
}

/** The answer of GET /api/view-as: the current session of this browser, or none. `allowed_reads`: the reads the
 *  signed-in staff member's role may make (viewAsReadsForRole). */
export type CurrentViewAs = { active: false } | ({ active: true; allowed_reads?: string[] } & ViewAsSession);

/** The claims of a view-as token the BFF reads (WITHOUT checking the signature: the backend verifies it on every
 *  request). Only used to know which session the cookie belongs to, whose it is and the staff role it was started
 *  with (`srl`). */
export function viewAsClaims(
  token: string | undefined | null,
): { sessionId: string; staffId: string; startRole: string; exp: number } | null {
  if (!token) return null;
  try {
    const part = token.split(".")[1];
    if (!part) return null;
    const b64 = part.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(part.length / 4) * 4, "=");
    const p = JSON.parse(atob(b64)) as { vas?: unknown; stf?: unknown; srl?: unknown; exp?: unknown };
    if (typeof p.vas !== "string" || typeof p.stf !== "string" || typeof p.srl !== "string"
      || typeof p.exp !== "number") return null;
    return { sessionId: p.vas, staffId: p.stf, startRole: p.srl, exp: p.exp };
  } catch {
    return null;
  }
}

const SESSION_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * The claims of the view-as cookie `token` when it is a session of the signed-in staff member `adminId` (its `stf`),
 * else null. The BFF reads, shows and ENDS only such a session: a cookie another member left in a shared browser is
 * never used, not even to end that member's session (an admin's end would revoke it).
 */
export function ownViewAsClaims(
  token: string | undefined | null,
  adminId: string | undefined | null,
): { sessionId: string; staffId: string; startRole: string; exp: number } | null {
  const claims = viewAsClaims(token);
  if (!claims || !adminId || claims.staffId !== adminId || !SESSION_ID_RE.test(claims.sessionId)) return null;
  return claims;
}

/**
 * The ids of the OPEN sessions of `adminId` in a GET /admin/view-as/sessions answer (an admin's list holds every
 * member's sessions; only the caller's own are ended at its sign-out).
 */
export function ownOpenSessionIds(answer: unknown, adminId: string | undefined | null): string[] {
  const sessions = (answer as { sessions?: unknown } | null)?.sessions;
  if (!adminId || !Array.isArray(sessions)) return [];
  return sessions
    .filter((s): s is { session_id: string; admin_user_id: string; open: boolean } =>
      !!s && typeof s === "object" && (s as { open?: unknown }).open === true
      && (s as { admin_user_id?: unknown }).admin_user_id === adminId
      && typeof (s as { session_id?: unknown }).session_id === "string"
      && SESSION_ID_RE.test((s as { session_id: string }).session_id))
    .map((s) => s.session_id);
}

/** Seconds until `expiresAt` (never below 0); 0 for a missing or bad date. */
export function secondsLeft(expiresAt: string | null | undefined, nowMs: number = Date.now()): number {
  const t = expiresAt ? Date.parse(expiresAt) : NaN;
  return Number.isFinite(t) ? Math.max(0, Math.floor((t - nowMs) / 1000)) : 0;
}

/** "mm:ss" (or "h:mm:ss" past an hour) for the banner countdown. */
export function formatCountdown(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s % 60)}` : `${pad(m)}:${pad(s % 60)}`;
}

export type StartProblem = "reason" | "ticket" | "minutes" | null;

/** The same checks the backend makes on the start body (a reason of 3-500 characters, a ticket of at most 120,
 *  5-60 minutes), so the dialog can say what is wrong before it sends. */
export function startProblem(reason: string, ticket: string, minutes: number): StartProblem {
  const r = reason.trim();
  if (r.length < REASON_MIN || r.length > REASON_MAX) return "reason";
  if (ticket.trim().length > TICKET_MAX) return "ticket";
  if (!Number.isInteger(minutes) || minutes < MIN_MINUTES || minutes > MAX_MINUTES) return "minutes";
  return null;
}

/** The cookie lifetime for a session: until its expiry, at most MAX_MINUTES, at least one second. */
export function cookieMaxAge(expiresAt: string | null | undefined, nowMs: number = Date.now()): number {
  return Math.min(MAX_MINUTES * 60, Math.max(1, secondsLeft(expiresAt, nowMs)));
}

/** The rows of a tenant list answer: `data`, else `items`, else `tours`, else the answer itself when it is a list. */
export function rowsOf(body: unknown): Record<string, unknown>[] {
  if (Array.isArray(body)) return body as Record<string, unknown>[];
  if (body && typeof body === "object") {
    for (const k of ["data", "items", "tours"]) {
      const v = (body as Record<string, unknown>)[k];
      if (Array.isArray(v)) return v as Record<string, unknown>[];
    }
  }
  return [];
}

/** The first of `keys` with a non-empty value in `row`, as text; null when none. */
export function pickText(row: Record<string, unknown>, keys: readonly string[]): string | null {
  for (const k of keys) {
    const v = row[k];
    if (v === null || v === undefined || v === "") continue;
    if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") return String(v);
  }
  return null;
}

/** The view-as read URL of a tenant path (BFF), e.g. "/v1/quota" → "/api/view-as/v1/quota". */
export function viewAsReadUrl(path: string): string {
  return `${VIEW_AS_API}${path}`;
}
