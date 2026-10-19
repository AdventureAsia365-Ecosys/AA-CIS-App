// portal/proxy.ts
// AA-885 — the tenant portal's page gate (Next 16 Proxy, Node.js runtime; no `runtime` export).
// The tenant half of the admin app's old gate (frontend/middleware.ts → proxy.ts, AA-232 / AA-252 /
// AA-573), with no staff or Cognito logic: the portal has no staff session.
//
//   - /tenant-login is public.
//   - /portal/* needs a cis_tenant_token that POST /auth/verify-tenant accepts (3 s timeout, shared
//     verify cache with the route handlers, lib/auth-server.ts).
//   - A missing or refused token → the 5 tenant cookies are cleared and the browser goes to
//     /tenant-login. A bare or forged `cis_role` cookie proves nothing (AA-252).
//   - Backend unreachable → fail CLOSED outside development (redirect to /tenant-login, cookies kept,
//     so the session works again once the backend answers); development lets the page through.
import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { TENANT_COOKIES, TENANT_TOKEN_COOKIE, verifyTenantToken } from "./lib/auth-server";

const LOGIN_PATH = "/tenant-login";

function toLogin(request: NextRequest, clearCookies: boolean): NextResponse {
  const response = NextResponse.redirect(new URL(LOGIN_PATH, request.url));
  if (clearCookies) {
    for (const name of TENANT_COOKIES) response.cookies.delete(name);
  }
  return response;
}

export async function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;

  if (pathname === LOGIN_PATH || pathname.startsWith(LOGIN_PATH + "/")) {
    return NextResponse.next();
  }

  const token = request.cookies.get(TENANT_TOKEN_COOKIE)?.value;
  if (!token) return toLogin(request, true);

  const verified = await verifyTenantToken(token);
  if (verified.ok) return NextResponse.next();

  if (verified.status === 0) {
    // Verification service unreachable — not a refusal of the token.
    if (process.env.NODE_ENV === "development") {
      console.warn("[portal proxy] tenant JWT verify unreachable — dev mode, allowing through");
      return NextResponse.next();
    }
    return toLogin(request, false);
  }
  if (verified.status >= 500) return toLogin(request, false);
  // 401/403: invalid or expired token → clear the session and log in again.
  return toLogin(request, true);
}

export const config = {
  matcher: ["/portal/:path*", "/tenant-login"],
};
