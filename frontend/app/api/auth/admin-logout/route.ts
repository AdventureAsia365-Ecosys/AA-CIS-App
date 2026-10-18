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
import { NextRequest, NextResponse } from "next/server";
import { adminVerifyCache } from "@/lib/auth-server";
import {
  allowedOriginForRequest,
  clearStaffSessionCookies,
  cognitoLogoutUrl,
  readRefreshCookie,
  revokeRefreshToken,
  sessionClientKind,
} from "@/lib/cognito";
import { STAFF_TOKEN_COOKIE, isCognitoToken } from "@/lib/staffSession";

export async function POST(request: NextRequest) {
  const token = request.cookies.get(STAFF_TOKEN_COOKIE)?.value;
  const rt = readRefreshCookie(request);
  const kind = sessionClientKind(request);

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
  return response;
}
