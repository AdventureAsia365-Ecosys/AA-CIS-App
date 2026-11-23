// frontend/lib/staffViewAs.ts — AA-1157 (root ADR 0019 d5 as amended): the pure parts of the owner's read-only
// "view as a lower staff role".
//
// The flow: Platform › Roles & permissions › "View as" on a CIS role (a reason, 15 / 30 / 60 minutes) → POST
// /api/view-as/roles (app/api/view-as/roles/route.ts) → CIS POST /admin/view-as/roles (owner only, downward only,
// logged with the role's full capability list), which answers a short-lived signed token. The BFF keeps it in the
// httpOnly cookie STAFF_VIEW_AS_COOKIE (never readable by the page) and forwards it as STAFF_VIEW_AS_HEADER on every
// staff API call (lib/auth-server.ts staffCredentialHeaders). The API then runs the request as the target role and
// refuses every write (403 view_as_read_only). The Cognito session cookies are never touched.
//
// The page sees only the display cookie STAFF_VIEW_DISPLAY_COOKIE ({role, label, caps, until}): a UI hint for the nav
// and the banner, never a credential. The BFF honours the httpOnly cookie only for the signed-in member's own session
// (claim stf), only on a verified Cognito admin session (the only role an owner can start a view from: a forged cookie
// of anyone else is ignored), and its capability claim is intersected with the verified ones (effectiveSession).
// Dependency-free: no React, no Node APIs.

export const STAFF_VIEW_AS_COOKIE = "aa_staff_view_as";
export const STAFF_VIEW_DISPLAY_COOKIE = "cis_view";
export const STAFF_VIEW_AS_HEADER = "x-staff-view-as";
export const STAFF_VIEW_AS_API = "/api/view-as/roles";
/** The backend start route (lib/viewAs.ts VIEW_AS_BFF_HEADER is required on it). */
export const STAFF_VIEW_AS_START_PATH = "/admin/view-as/roles";
export const STAFF_VIEW_AS_ENDED = "staff_view_as_ended";
export const VIEW_AS_READ_ONLY = "view_as_read_only";
export const ROLES_PAGE = "/admin/platform/roles";
export const DEFAULT_ROLE_VIEW_REASON = "Check what this role sees";
/** The verified role an owner starts a view from (backend: staff.view_as_role needs the admin role). */
export const VIEW_FROM_ROLE = "admin";

const SESSION_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const ROLE_RE = /^[a-z][a-z0-9_]{1,62}$/;

export interface StaffViewClaims {
  sessionId: string;
  staffId: string;
  role: string;
  capabilities: string[];
  exp: number;
}

/** The claims of a view-as-a-role token, decoded WITHOUT checking the signature (the API verifies it on every
 *  request); null when it is not that kind of token. */
export function staffViewClaims(token: string | undefined | null): StaffViewClaims | null {
  const p = jwtPayload(token);
  if (!p || p.typ !== "staff_view_as" || !Array.isArray(p.vcp) || typeof p.exp !== "number") return null;
  const { svs, stf, vrl } = p;
  if (!isText(svs, SESSION_ID_RE) || typeof stf !== "string" || !isText(vrl, ROLE_RE)) return null;
  return { sessionId: svs, staffId: stf, role: vrl, exp: p.exp,
    capabilities: (p.vcp as unknown[]).filter((c): c is string => typeof c === "string") };
}

function isText(v: unknown, re: RegExp): v is string {
  return typeof v === "string" && re.test(v);
}

/** A JWT's payload, decoded WITHOUT checking the signature; null when it is not one. */
function jwtPayload(token: string | undefined | null): Record<string, unknown> | null {
  try {
    const part = (token ?? "").split(".")[1];
    if (!part) return null;
    const b64 = part.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(part.length / 4) * 4, "=");
    const p = JSON.parse(atob(b64));
    return p && typeof p === "object" ? (p as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

/** The claims of `token` when it is an unexpired view of the signed-in member `adminId`, else null. */
export function ownStaffView(
  token: string | undefined | null, adminId: string | undefined | null, nowMs: number = Date.now(),
): StaffViewClaims | null {
  const c = staffViewClaims(token);
  if (!c || !adminId || c.staffId !== adminId || c.exp * 1000 <= nowMs) return null;
  return c;
}

/** The claims of `token` when it is a view of the signed-in member `adminId` that is PAST its expiry, else null. The
 *  token cookie outlives the view by STAFF_VIEW_CLOSE_GRACE_S so the banner's last check can still name the session:
 *  the BFF then reads it as the owner and the API closes it as 'expired' (its end row). Never honoured as a view. */
export function expiredOwnStaffView(
  token: string | undefined | null, adminId: string | undefined | null, nowMs: number = Date.now(),
): StaffViewClaims | null {
  const c = staffViewClaims(token);
  if (!c || !adminId || c.staffId !== adminId || c.exp * 1000 > nowMs) return null;
  return c;
}

/** How long the httpOnly token cookie outlives the view (seconds; the display cookie ends with the view). */
export const STAFF_VIEW_CLOSE_GRACE_S = 300;

/** The log line of a write the BFF refused inside a view (structured, one line, no body, no query string): the
 *  refusal never reaches the API, so this line is its trace (the API's own gate logs a write that reaches it). */
export function refusedWriteLog(
  app: string, view: { sessionId: string; role: string; staffId: string }, method: string, path: string,
): string {
  const clean = path.split("?")[0].replace(/[^\w\-./\[\]]/g, "_").slice(0, 200);
  return `[view-as] staff_view_as_write_refused app=${app} session=${view.sessionId} staff=${view.staffId} `
    + `view_role=${view.role} method=${method.toUpperCase()} path=${clean}`;
}

/** The session a request is checked with: the verified one, or inside an own view started from a verified admin
 *  Cognito session, the view role with its claimed capabilities intersected with the verified ones (never wider). */
export function effectiveSession(
  verified: { role: string; capabilities: readonly string[]; cognito: boolean },
  view: StaffViewClaims | null,
): { role: string; capabilities: string[]; viewing: boolean } {
  if (!view || !verified.cognito || verified.role !== VIEW_FROM_ROLE) {
    return { role: verified.role, capabilities: [...verified.capabilities], viewing: false };
  }
  const held = new Set(verified.capabilities);
  return { role: view.role, capabilities: view.capabilities.filter((c) => held.has(c)), viewing: true };
}

/** How a staff BFF request runs, given the signed-in member's own unexpired view (`view`, from ownStaffView):
 *  - "none": no own view, the verified session;
 *  - "owner": the route acts as the owner (requireAdmin allowDuringViewAs): verified rights, token not forwarded;
 *  - "refuse": a write, 403 view_as_read_only. Fails closed: refused whether or not the view is honoured here (a
 *    custom-role or stale cookie never lets a write through with the owner's own rights);
 *  - "read": the effective session (the view role when honoured, else the verified one). The token is forwarded either
 *    way, so the API ends a session it no longer honours (409) and the proxy clears the cookies. */
export type StaffViewStep =
  | { kind: "none" }
  | { kind: "owner" }
  | { kind: "refuse" }
  | { kind: "read"; role: string; capabilities: string[]; viewing: boolean };

export function staffViewStep(
  isRead: boolean,
  verified: { role: string; capabilities: readonly string[]; cognito: boolean },
  view: StaffViewClaims | null,
  allowDuringViewAs: boolean,
): StaffViewStep {
  if (!view) return { kind: "none" };
  if (allowDuringViewAs) return { kind: "owner" };
  if (!isRead) return { kind: "refuse" };
  return { kind: "read", ...effectiveSession(verified, view) };
}

/** Is `path` (a backend /admin sub-path, no leading slash, as the generic /api/admin proxy sees it) the view-as-a-role
 *  start? The generic proxy refuses it: only app/api/view-as/roles/route.ts starts one (the token stays httpOnly). */
export function isStaffViewAsStartPath(path: string): boolean {
  const parts = path.split("/").filter((p) => p !== "").map((p) => p.toLowerCase());
  return parts.length >= 2 && parts[0] === "view-as" && parts[1] === "roles";
}

export interface StaffViewDisplay {
  role: string;
  label: string;
  caps: string[];
  /** ISO end time of the session. */
  until: string;
}

/** The display cookie's value: plain JSON. `res.cookies.set` URI-encodes it once on the wire; encoding it here as
 *  well made the browser hold a twice-encoded value the banner and the nav could not read (S288 live check). */
export function displayCookieValue(d: StaffViewDisplay): string {
  return JSON.stringify(d);
}

/** The display cookie read back; null when absent, malformed or past its end (`nowMs`). */
export function parseDisplayCookie(raw: string | undefined | null, nowMs: number = Date.now()): StaffViewDisplay | null {
  const d = decodeDisplay(raw);
  if (!d || !isText(d.role, ROLE_RE) || typeof d.label !== "string" || typeof d.until !== "string"
    || !Array.isArray(d.caps)) return null;
  const end = Date.parse(d.until);
  if (!(end > nowMs)) return null;
  return { role: d.role, label: d.label, until: d.until, caps: d.caps.filter((c) => typeof c === "string") };
}

/** Reads the plain JSON (server: `req.cookies` already decoded it), the once-encoded wire value (`document.cookie`)
 *  and the twice-encoded value an older BFF wrote. At most two decodes, so the work stays bounded. */
function decodeDisplay(raw: string | undefined | null): Partial<StaffViewDisplay> | null {
  let text = raw ?? "";
  for (let step = 0; step < 3 && text; step++) {
    if (text.startsWith("{")) {
      try {
        const d = JSON.parse(text);
        return d && typeof d === "object" ? (d as Partial<StaffViewDisplay>) : null;
      } catch {
        return null;
      }
    }
    try {
      text = decodeURIComponent(text);
    } catch {
      return null;
    }
  }
  return null;
}

/** Is a 409 body from the API the end of a view-as-a-role session (the BFF then clears its cookies)? */
export function isViewEnded(status: number, body: unknown): boolean {
  return status === 409 && !!body && typeof body === "object"
    && (body as { detail?: unknown }).detail === STAFF_VIEW_AS_ENDED;
}
