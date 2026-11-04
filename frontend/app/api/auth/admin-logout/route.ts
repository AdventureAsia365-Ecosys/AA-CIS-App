// frontend/app/api/auth/admin-logout/route.ts
// AA-521 — mirrors AA-427's /api/auth/tenant-logout for the admin session.
//
// AdminSidebar.tsx's logout() used to only clear the display cookies
// (cis_role/cis_user/cis_api_token) via `document.cookie = "...max-age=0"`.
// cis_admin_token (the real JWT proxy.ts/verifyAdminToken() checks) has
// been httpOnly since AA-232 — client JS can't see or overwrite it, so
// clicking Logout redirected to /login but left the real session alive until
// its natural 24h expiry (JWT_EXPIRY_H). This route clears cis_admin_token
// server-side (cookies().delete() works on httpOnly cookies; client JS still can't).
//
// AA-816 (C4) — Cognito sessions: revoke the refresh token first (/oauth2/revoke also invalidates
// the access tokens issued from it), drop the BFF verify-cache entry, clear every session cookie
// (cis_staff_rt at its own /api/auth path), then answer with the Cognito /logout URL. The browser must
// navigate there (Topbar uses window.location): it clears the managed-login session cookie, which
// otherwise signs the user straight back in for up to an hour (ADR 0006 §7). Cognito then returns to
// /login, which never auto-redirects. A legacy session gets { redirect: "/login" }.
//
// AA-816 (cutover prep, S244) — same-origin check first, like AA-Booking /api/auth/logout: another site must
// not be able to sign staff out (logout CSRF). A cross-site POST gets 403 and changes nothing (no revoke, no
// cookie cleared). The Topbar only follows a redirect of the two shapes above (lib/staffLogout.ts).
//
// AA-1035 follow-up (S268) — signing out ends the member's own open view-as sessions at once (CIS GET
// /admin/view-as/sessions?open_only=true, then POST .../end for each of the member's own: one tenant.view_as_ended
// row each), BEFORE the refresh token is revoked (the revoke also invalidates the access token those calls use), and
// clears the view-as cookie (its own /api/view-as path). Best effort and bounded (VIEW_AS_END_TIMEOUT_MS): a failure
// never blocks the sign-out; an open session then ends at its next request or its expiry (at most 60 minutes).
import { NextRequest, NextResponse } from "next/server";
import { adminVerifyCache, requireAdmin, staffCredentialHeaders } from "@/lib/auth-server";
import {
  allowedOriginForRequest,
  clearStaffSessionCookies,
  cognitoLogoutUrl,
  readRefreshCookie,
  revokeRefreshToken,
  sessionClientKind,
} from "@/lib/cognito";
import { isSameOrigin } from "@/lib/sameOrigin";
import { STAFF_TOKEN_COOKIE, isCognitoToken } from "@/lib/staffSession";
import type { StaffRole } from "@/lib/rbac";
import { VIEW_AS_COOKIE, VIEW_AS_COOKIE_PATH, VIEW_AS_ROLES, ownOpenSessionIds } from "@/lib/viewAs";

const API_URL = process.env.API_URL ?? "https://api-cis.lumiguides.it.com";
const ADMIN_SECRET = process.env.ADMIN_SECRET ?? "";
const VIEW_AS_END_TIMEOUT_MS = 4000;

/** End the signed-in member's own open view-as sessions (best effort; the number ended). */
async function endOwnViewAsSessions(request: NextRequest): Promise<number> {
  const auth = await requireAdmin(request, VIEW_AS_ROLES as unknown as StaffRole[]);
  if (!auth.ok) return 0;
  const credential = staffCredentialHeaders(auth, ADMIN_SECRET, "admin");
  if (!credential) return 0;
  const headers = { "x-admin-user-id": auth.adminId, "x-admin-role": auth.role, ...credential };
  try {
    const signal = AbortSignal.timeout(VIEW_AS_END_TIMEOUT_MS);
    const res = await fetch(`${API_URL}/admin/view-as/sessions?open_only=true`, { headers, signal });
    if (!res.ok) return 0;
    const ids = ownOpenSessionIds(await res.json(), auth.adminId);
    const ended = await Promise.all(ids.map((id) =>
      fetch(`${API_URL}/admin/view-as/sessions/${id}/end`, { method: "POST", headers, signal })
        .then((r) => r.ok, () => false)));
    return ended.filter(Boolean).length;
  } catch {
    console.warn("[auth] logout: ending the open view-as sessions failed");
    return 0;
  }
}

export async function POST(request: NextRequest) {
  if (!isSameOrigin(request, { strict: true })) {
    const refused = NextResponse.json({ ok: false, message: "Cross-site sign-out refused." }, { status: 403 });
    refused.headers.set("Cache-Control", "no-store");
    return refused;
  }
  const token = request.cookies.get(STAFF_TOKEN_COOKIE)?.value;
  const rt = readRefreshCookie(request);
  const kind = sessionClientKind(request);

  await endOwnViewAsSessions(request);
  if (rt) {
    const revoked = await revokeRefreshToken(rt.kind, rt.refreshToken);
    if (!revoked) console.warn(`[auth] logout: refresh token revoke failed client=${rt.kind}`);
  }
  if (token) adminVerifyCache.delete(token);

  let redirect = "/login";
  if (kind && (rt || isCognitoToken(token))) {
    const origin = allowedOriginForRequest(request, kind);
    if (origin) redirect = cognitoLogoutUrl(kind, origin);
  }

  const response = NextResponse.json({ ok: true, redirect });
  response.headers.set("Cache-Control", "no-store");
  clearStaffSessionCookies(response);
  response.cookies.set(VIEW_AS_COOKIE, "", { httpOnly: true, secure: process.env.NODE_ENV === "production",
    sameSite: "strict", path: VIEW_AS_COOKIE_PATH, maxAge: 0 });
  return response;
}
