// frontend/app/api/view-as/roles/route.ts — AA-1157 (root ADR 0019 d5 as amended): start, read and end the owner's
// read-only "view as a lower staff role" session of this browser.
//
//   POST   { role, reason, minutes? } → CIS POST /admin/view-as/roles (owner only, downward only, logged with the
//          role's full capability list). The signed token it returns goes into the httpOnly cookie aa_staff_view_as
//          (path /, SameSite=Strict, Secure, at most 60 minutes) and is NEVER sent to the page; the page gets the
//          display cookie cis_view ({role, label, caps, until}) and `landing`, the view role's first page. The call
//          carries "x-view-as-client": "bff": the API answers the token only with it, and the generic /api/admin
//          proxy refuses the start path. Refused inside a view (requireAdmin: a write) and without a Cognito session.
//   GET    → the current view of this browser ({ active: false } when none, ended or not this member's), read from
//          CIS GET /admin/view-as/sessions/{id} AS THE OWNER (no view header: the view role may not read sessions).
//          A view of this member past its expiry (the token cookie outlives it by STAFF_VIEW_CLOSE_GRACE_S) is read
//          once more the same way: the API then closes it as 'expired' and writes its end row.
//   DELETE → the "Back" button: CIS POST /admin/view-as/sessions/{id}/end as the owner (no view header), then both
//          cookies are cleared (also when the API call fails: the session then ends at its expiry). The Cognito
//          session cookies are never touched, so the sign-in session is the same before and after.
import { NextRequest, NextResponse } from "next/server";
import { refuseCrossSiteWrite } from "@/lib/crossSiteGuard";
import { requireAdmin, secretNotConfigured, staffCredentialHeaders } from "@/lib/auth-server";
import { landingPage } from "@/lib/rbac";
import { VIEW_AS_BFF_HEADER, cookieMaxAge } from "@/lib/viewAs";
import {
  DEFAULT_ROLE_VIEW_REASON, STAFF_VIEW_AS_COOKIE, STAFF_VIEW_AS_START_PATH, STAFF_VIEW_DISPLAY_COOKIE,
  expiredOwnStaffView, ownStaffView, parseDisplayCookie, staffViewClaims,
} from "@/lib/staffViewAs";
import { clearStaffViewCookies, setStaffViewCookies } from "@/lib/staffViewCookies";

const API_URL = process.env.API_URL ?? "https://api-cis.lumiguides.it.com";
const ADMIN_SECRET = process.env.ADMIN_SECRET ?? "";
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

type StartAnswer = { token?: string; view_role?: string; view_label?: string; expires_at?: string;
  capabilities?: string[]; [k: string]: unknown };

export async function POST(req: NextRequest) {
  const refused = refuseCrossSiteWrite(req);
  if (refused) return refused;
  const auth = await requireAdmin(req);
  if (!auth.ok) return auth.response;
  if (!auth.bearer) return NextResponse.json({ detail: "staff_view_as_needs_cognito" }, { status: 403 });
  const credential = staffCredentialHeaders(auth, ADMIN_SECRET, "admin");
  if (!credential) return secretNotConfigured();
  let body: { role?: unknown; reason?: unknown; minutes?: unknown };
  try { body = await req.json(); } catch { return NextResponse.json({ detail: "Invalid body" }, { status: 400 }); }
  let res: Response;
  try {
    res = await fetch(`${API_URL}${STAFF_VIEW_AS_START_PATH}`, {
      method: "POST",
      headers: { "x-admin-user-id": auth.adminId, "x-admin-role": auth.role, ...credential,
        "Content-Type": "application/json", [VIEW_AS_BFF_HEADER]: "bff" },
      body: JSON.stringify({ role: body.role, reason: body.reason ?? DEFAULT_ROLE_VIEW_REASON,
        minutes: body.minutes ?? 30 }),
    });
  } catch {
    return NextResponse.json({ detail: "Upstream connection error" }, { status: 502 });
  }
  const data = (await res.json().catch(() => ({}))) as StartAnswer;
  return res.ok ? started(data, auth.adminId) : NextResponse.json(data, { status: res.status });
}

/** The page's answer to a started view: the session without the token, the view role's first page; the token goes
 *  into the httpOnly cookie, the display hint next to it. */
function started(data: StartAnswer, adminId: string): NextResponse {
  const { token, ...session } = data;
  const claims = staffViewClaims(token);
  const expires = typeof session.expires_at === "string" ? session.expires_at : null;
  if (!token || !claims || claims.staffId !== adminId || !expires) {
    return NextResponse.json({ detail: "The view could not be opened" }, { status: 502 });
  }
  const caps = Array.isArray(session.capabilities) ? session.capabilities : [];
  const display = { role: claims.role, label: String(session.view_label ?? claims.role), caps, until: expires };
  const landing = landingPage({ role: claims.role, capabilities: caps });
  const out = NextResponse.json({ active: true, ...session, landing }, { status: 201 });
  return setStaffViewCookies(out, token, display, cookieMaxAge(expires));
}

/** The owner's own session headers (no view header) for a session read or end. */
async function ownerHeaders(req: NextRequest) {
  const auth = await requireAdmin(req, undefined, { allowDuringViewAs: true });
  if (!auth.ok) return { auth, headers: null as Record<string, string> | null };
  const credential = staffCredentialHeaders(auth, ADMIN_SECRET, "admin");
  return { auth, headers: credential && { "x-admin-user-id": auth.adminId, "x-admin-role": auth.role, ...credential } };
}

export async function GET(req: NextRequest) {
  const { auth, headers } = await ownerHeaders(req);
  if (!auth.ok) return auth.response;
  if (!headers) return secretNotConfigured();
  const token = req.cookies.get(STAFF_VIEW_AS_COOKIE)?.value;
  const claims = ownStaffView(token, auth.adminId);
  if (!claims) return closeExpired(token, auth.adminId, headers);
  let s: SessionRead | null;
  try {
    s = await readSession(claims.sessionId, headers);
  } catch {
    return NextResponse.json({ detail: "Upstream connection error" }, { status: 502 });
  }
  if (!s?.open || s.kind !== "role") return inactive();
  return NextResponse.json(current(s, req.cookies.get(STAFF_VIEW_DISPLAY_COOKIE)?.value));
}

/** No own view (none, ended, expired, another member's): inactive, both cookies cleared. */
function inactive(): NextResponse {
  return clearStaffViewCookies(NextResponse.json({ active: false }));
}

/** No running own view. An own view past its expiry is read once more as the owner first: the API closes it as
 *  'expired' (its end row). Best effort: the next start or sign-out closes it otherwise. */
async function closeExpired(token: string | undefined, adminId: string, headers: Record<string, string>) {
  const expired = expiredOwnStaffView(token, adminId);
  if (expired) await readSession(expired.sessionId, headers).catch(() => null);
  return inactive();
}

type SessionRead = { open?: boolean; kind?: string; session_id?: string; view_role?: string; expires_at?: string };

/** CIS GET /admin/view-as/sessions/{id} as the owner: the session, or null when the API refused (404, ...). Throws on
 *  a connection error. */
async function readSession(sessionId: string, headers: Record<string, string>): Promise<SessionRead | null> {
  if (!UUID_RE.test(sessionId)) return null;
  const res = await fetch(`${API_URL}/admin/view-as/sessions/${sessionId}`, { headers, cache: "no-store" });
  return res.ok ? ((await res.json()) as SessionRead) : null;
}

/** The page's answer for an open role session (the label comes from the display hint; the API stores no label). */
function current(s: { session_id?: string; view_role?: string; expires_at?: string }, rawDisplay?: string) {
  const display = parseDisplayCookie(rawDisplay);
  return { active: true, kind: "role", session_id: s.session_id, view_role: s.view_role,
    view_label: display?.label ?? s.view_role, expires_at: s.expires_at, read_only: true };
}

export async function DELETE(req: NextRequest) {
  const refused = refuseCrossSiteWrite(req);
  if (refused) return refused;
  const { auth, headers } = await ownerHeaders(req);
  if (!auth.ok) return auth.response;
  if (!headers) return secretNotConfigured();
  // Expired or not: the end route closes an expired session as 'expired' and writes its end row.
  const claims = staffViewClaims(req.cookies.get(STAFF_VIEW_AS_COOKIE)?.value);
  if (claims && claims.staffId !== auth.adminId) return clearStaffViewCookies(NextResponse.json({ ended: false }));
  let ended = false;
  if (claims && UUID_RE.test(claims.sessionId)) {
    try {
      const res = await fetch(`${API_URL}/admin/view-as/sessions/${claims.sessionId}/end`, { method: "POST", headers });
      ended = res.ok;
    } catch { /* the cookies are cleared anyway; the session expires on its own */ }
  }
  return clearStaffViewCookies(NextResponse.json({ ended }));
}
