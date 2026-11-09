// frontend/app/api/view-as/route.ts — AA-1035 R2 slice 2 (root ADR 0019 d5): start, read and end the read-only
// "view as tenant" session of this browser.
//
//   POST   { tenant_id, reason, ticket_ref?, minutes? } → CIS POST /admin/tenants/{id}/view-as (staff credential, the
//          backend checks tenant.view_as + the tenant scope and logs the session). The read-only tenant token it returns
//          goes into the httpOnly cookie aa_view_as (path /api/view-as, SameSite=Strict, Secure, at most 60 minutes) and
//          is NEVER sent to the page; the page gets the session without it.
//          The start call carries "x-view-as-client": "bff" (VIEW_AS_BFF_HEADER): the backend answers the token only
//          with it, and the generic /api/admin proxy (which never sets it) refuses the start path too.
//   GET    → the current session of this browser ({ active: false } when none, ended or not this staff member's),
//          with `allowed_reads`: the reads the signed-in member's role may make (lib/viewAs.ts viewAsReadsForRole).
//   DELETE → CIS POST /admin/view-as/sessions/{id}/end for the signed-in member's OWN session (the token's stf, like
//          GET), then the cookie is cleared (also when the API call fails). A cookie of another member (a shared
//          browser) is only cleared: never ended through this member (an admin's end would revoke it).
//
// The token's reads go through app/api/view-as/v1/[...path]/route.ts. Every staff check is the backend's; this route
// adds the verified staff session (requireAdmin) and that the cookie belongs to the signed-in staff member.
import { NextRequest, NextResponse } from "next/server";
import { refuseCrossSiteWrite } from "@/lib/crossSiteGuard";
import { requireAdmin, secretNotConfigured, staffCredentialHeaders } from "@/lib/auth-server";
import {
  VIEW_AS_BFF_HEADER, VIEW_AS_COOKIE, VIEW_AS_COOKIE_PATH, VIEW_AS_ROLES, cookieMaxAge, ownViewAsClaims, viewAsClaims,
  viewAsReadsForRole, type CurrentViewAs,
} from "@/lib/viewAs";
import type { StaffRole } from "@/lib/rbac";

const API_URL = process.env.API_URL ?? "https://api-cis.lumiguides.it.com";
const ADMIN_SECRET = process.env.ADMIN_SECRET ?? "";
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const ROLES = VIEW_AS_ROLES as unknown as StaffRole[];
// Same rule as the staff session cookie (app/api/auth/login/route.ts): Secure outside local development.
const SECURE = process.env.NODE_ENV === "production";

function clearCookie(res: NextResponse): NextResponse {
  res.cookies.set(VIEW_AS_COOKIE, "", { httpOnly: true, secure: SECURE, sameSite: "strict", path: VIEW_AS_COOKIE_PATH,
    maxAge: 0 });
  return res;
}

async function staffHeaders(req: NextRequest, path: string) {
  const auth = await requireAdmin(req, ROLES);
  if (!auth.ok) return { auth, headers: null as Record<string, string> | null };
  const credential = staffCredentialHeaders(auth, ADMIN_SECRET, path);
  if (!credential) return { auth, headers: null };
  return {
    auth,
    headers: { "x-admin-user-id": auth.adminId, "x-admin-role": auth.role, ...credential } as Record<string, string>,
  };
}

export async function POST(req: NextRequest) {
  const refused = refuseCrossSiteWrite(req);
  if (refused) return refused;
  const { auth, headers } = await staffHeaders(req, "admin");
  if (!auth.ok) return auth.response;
  if (!headers) return secretNotConfigured();
  let body: { tenant_id?: unknown; reason?: unknown; ticket_ref?: unknown; minutes?: unknown };
  try { body = await req.json(); } catch { return NextResponse.json({ detail: "Invalid body" }, { status: 400 }); }
  const tenantId = typeof body.tenant_id === "string" ? body.tenant_id : "";
  if (!UUID_RE.test(tenantId)) return NextResponse.json({ detail: "Invalid tenant" }, { status: 400 });
  let res: Response;
  try {
    res = await fetch(`${API_URL}/admin/tenants/${tenantId}/view-as`, {
      method: "POST",
      headers: { ...headers, "Content-Type": "application/json", [VIEW_AS_BFF_HEADER]: "bff" },
      body: JSON.stringify({ reason: body.reason, ticket_ref: body.ticket_ref ?? null, minutes: body.minutes ?? 30 }),
    });
  } catch {
    return NextResponse.json({ detail: "Upstream connection error" }, { status: 502 });
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return NextResponse.json(data, { status: res.status });
  const { token, ...session } = data as { token?: string; expires_at?: string };
  const claims = viewAsClaims(token);
  if (!token || !claims || claims.staffId !== auth.adminId) {
    return NextResponse.json({ detail: "The view-as session could not be opened" }, { status: 502 });
  }
  const out = NextResponse.json(
    { active: true, open: true, ...session, allowed_reads: viewAsReadsForRole(auth.role) }, { status: 201 });
  out.cookies.set(VIEW_AS_COOKIE, token, {
    httpOnly: true, secure: SECURE, sameSite: "strict", path: VIEW_AS_COOKIE_PATH,
    maxAge: cookieMaxAge(session.expires_at),
  });
  return out;
}

export async function GET(req: NextRequest) {
  const { auth, headers } = await staffHeaders(req, "admin");
  if (!auth.ok) return auth.response;
  if (!headers) return secretNotConfigured();
  const cookie = req.cookies.get(VIEW_AS_COOKIE)?.value;
  const claims = ownViewAsClaims(cookie, auth.adminId);
  const none: CurrentViewAs = { active: false };
  // A cookie of another staff member (a shared browser) is never used: cleared.
  if (!claims) return viewAsClaims(cookie) ? clearCookie(NextResponse.json(none)) : NextResponse.json(none);
  try {
    const res = await fetch(`${API_URL}/admin/view-as/sessions/${claims.sessionId}`, { headers });
    if (!res.ok) return clearCookie(NextResponse.json(none));
    const s = await res.json();
    if (!s.open) return clearCookie(NextResponse.json(none));
    return NextResponse.json({ active: true, ...s, allowed_reads: viewAsReadsForRole(auth.role) });
  } catch {
    return NextResponse.json({ detail: "Upstream connection error" }, { status: 502 });
  }
}

export async function DELETE(req: NextRequest) {
  const refused = refuseCrossSiteWrite(req);
  if (refused) return refused;
  const { auth, headers } = await staffHeaders(req, "admin");
  if (!auth.ok) return auth.response;
  if (!headers) return secretNotConfigured();
  const claims = ownViewAsClaims(req.cookies.get(VIEW_AS_COOKIE)?.value, auth.adminId);
  if (!claims) return clearCookie(NextResponse.json({ ended: false }));
  let ended = false;
  try {
    const res = await fetch(`${API_URL}/admin/view-as/sessions/${claims.sessionId}/end`, { method: "POST", headers });
    ended = res.ok;
  } catch { /* the cookie is cleared anyway; the session expires on its own */ }
  return clearCookie(NextResponse.json({ ended }));
}
