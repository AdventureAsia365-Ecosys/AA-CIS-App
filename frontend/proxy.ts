// frontend/proxy.ts
// S243 (AA-816 chore): Next 16 file convention — was middleware.ts (deprecated in Next 16). The function
// is exported as `proxy`; it runs on the Node.js runtime (middleware ran on Edge), and a `runtime`
// export is not allowed here (Next throws). `config.matcher` and the gate logic are unchanged.
//
// AA-232: ADMIN_PATHS/INTERNAL_PATHS verify a real JWT (cis_admin_token) via
// /auth/verify-admin, mirroring the tenant-portal verify (moved to portal/proxy.ts by AA-885). The old check
// (cookie value === "admin"/present) is a role LABEL a client could set by
// hand in devtools — it gated the page, but not the identity. This adds
// actual signature verification on top of the same cookie-role gate (kept
// as a fast pre-check to avoid a network call on every request for
// obviously-wrong roles).
//
// AA-252 (security fix — rewrite from an exclude-list to an allow-list).
// Audit of the previous exclude-list shape found FIVE bypasses, all rooted
// in the same mistake (trusting a plain, client-writable cis_role cookie as
// if it were proof of identity) or in gaps in the exclude-lists themselves:
//   #1 verifyAdminToken() treated a MISSING JWT cookie as "pass" (interim
//      carve-out for the retired ADMIN_SECRET fallback shape) — anyone could
//      set `cis_role=admin` by hand with no token at all. FIXED: missing
//      token now always fails closed.
//   #2 the internal-paths block only verified when role was exactly "admin"
//      or "reviewer" — any OTHER role value (e.g. `cis_role=banana`) skipped
//      verification entirely and passed straight through.
//   #3 the tenant-portal block special-cased `role === "admin"` to
//      `return NextResponse.next()` immediately, without ever calling
//      verifyAdminToken — a forged admin cookie reached /portal with zero
//      verification, independent of #1's fix.
//   #4 /admin/run-health, /admin/s1-rewrite, /admin/settings existed as real
//      pages but were never listed in INTERNAL_PATHS/ADMIN_PATHS, so they
//      fell through to the unconditional `NextResponse.next()` at the end —
//      zero auth of any kind, even with no cookies at all.
//   #5 /brand (app/(internal)/brand — a route group, not under /admin) was
//      missing from config.matcher entirely, so middleware never ran for it.
// Root cause of #2/#4/#5: an exclude-list only blocks what it explicitly
// lists; anything it forgets to list is open by default. This file now uses
// an allow-list instead — every protected prefix declares exactly which
// roles may reach it, and anything not listed (or reached with a role not on
// its list) is denied by default. See PAGE_RULES in lib/rbac.ts.
//
// AA-656: every staff role (admin, reviewer, content) now carries a signed JWT and the allow-list
// lives in lib/rbac.ts (PAGE_RULES), shared with the BFF proxies and the nav. The role that is
// checked comes from the VERIFIED token, never from the `cis_role` cookie — before this, a reviewer
// with a valid token could set `cis_role=admin` by hand and open admin-only pages, and the content
// role had no token at all.
//
// AA-816 (C4): `cis_admin_token` holds either the legacy HS256 JWT or a Cognito access token (it has
// an `iss`). A Cognito token that is expired (or about to be) is refreshed by redirecting to the
// single-flight /api/auth/refresh route — the refresh cookie is scoped to /api/auth and never reaches
// a page request — which rotates the tokens and sends the browser back here. The Cognito path never
// fails open, not even in development.
//
// AA-885: the tenant portal is its own app (portal/, host portal.lumiguides.it.com) with its own
// portal/proxy.ts. This gate no longer matches /portal or /tenant-login: next.config.ts answers those
// with a 308 to the portal host before this file runs, and no tenant session is checked here.
import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { adminVerifyCache, verifiedStaffOf } from "./lib/auth-server";
import { canSeePage, isStaffSubject, landingPage, pageRule, type StaffSession } from "./lib/rbac";
import { NONCE_HEADER, contentSecurityPolicy, newNonce, sentryOrigin } from "./lib/securityHeaders";
import { STAFF_VIEW_AS_COOKIE, effectiveSession, ownStaffView } from "./lib/staffViewAs";
import {
  CAPS_COOKIE,
  REFRESH_PATH,
  STAFF_REFRESH_COOKIE,
  STAFF_REFRESH_COOKIE_PATH,
  STAFF_TOKEN_COOKIE,
  isCognitoToken,
  tokenExpiresSoon,
} from "./lib/staffSession";

const PUBLIC_PATHS = ["/login"];

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "https://api-cis.lumiguides.it.com";

// AA-1111 (S284): the browser posts Sentry events to the DSN's origin (a public build-time value; none = no Sentry).
const SENTRY_ORIGIN = sentryOrigin(process.env.NEXT_PUBLIC_SENTRY_DSN);

/** The staff gate runs on these paths only (the matcher before AA-1111: "/admin/:path*" and "/login"). */
function isGatedPath(pathname: string): boolean {
  return pathname === "/login" || pathname === "/admin" || pathname.startsWith("/admin/");
}

/**
 * AA-1111 (S284, ZAP baseline): every response the proxy handles carries the full CSP with a fresh nonce. The policy
 * also rides on the request (Next takes the nonce from it for its own scripts; app/layout.tsx reads x-nonce for the
 * restore scripts). Then the staff gate below decides, as before, for /admin/* and /login only.
 */
export async function proxy(request: NextRequest) {
  const nonce = newNonce();
  const csp = contentSecurityPolicy({
    nonce,
    dev: process.env.NODE_ENV === "development",
    connectSrc: [SENTRY_ORIGIN],
  });
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set(NONCE_HEADER, nonce);
  requestHeaders.set("Content-Security-Policy", csp);
  const pass = () => NextResponse.next({ request: { headers: requestHeaders } });
  const response = isGatedPath(request.nextUrl.pathname) ? await staffGate(request, pass) : pass();
  response.headers.set("Content-Security-Policy", csp);
  return response;
}

/** The staff gate (AA-232 / AA-252 / AA-656 / AA-816, see the header). `pass` lets the request through. */
async function staffGate(request: NextRequest, pass: () => NextResponse): Promise<NextResponse> {
  const { pathname } = request.nextUrl;

  if (PUBLIC_PATHS.some(p => pathname.startsWith(p))) {
    return pass();
  }

  // AA-722 — the legacy /admin/dashboard page is retired. Route old bookmarks
  // and the login redirect to the role's landing page: admin → Overview
  // (admin-only), reviewer/content → Review Queue. This must run BEFORE the
  // allow-list lookup below: /admin/dashboard is not in PAGE_RULES,
  // so it would otherwise hit the deny-by-default branch and bounce to /login.
  // A static next.config redirect to Overview would 403/redirect reviewer and
  // content back to /login (Overview is admin-only), hence the cookie-aware
  // branch here. No token verification needed — the redirect only picks a
  // destination; the destination's own PAGE_RULES entry still enforces
  // auth on the follow-up request.
  if (pathname === "/admin/dashboard" || pathname.startsWith("/admin/dashboard/")) {
    // Destination only; the landing page's own rule enforces auth on the follow-up request.
    const dashRole = request.cookies.get("cis_role")?.value;
    const dashCaps = (request.cookies.get(CAPS_COOKIE)?.value ?? "").split(",").filter(Boolean);   // AA-1155
    return NextResponse.redirect(new URL(landingPage({ role: dashRole, capabilities: dashCaps }), request.url));
  }

  const route = pageRule(pathname);
  if (!route) {
    // AA-252 #4/#5: reached middleware (config.matcher matched) but isn't in
    // the allow-list — fail closed instead of silently falling through.
    return NextResponse.redirect(new URL("/login", request.url));
  }

  // AA-656: identity comes from whichever session token is present, verified server-side.
  const staffToken = request.cookies.get(STAFF_TOKEN_COOKIE)?.value;
  if (staffToken) return sessionGate(request, staffToken, pass);

  // No token (a bare or forged cis_role cookie proves nothing) → log in.
  const response = NextResponse.redirect(new URL("/login", request.url));
  response.cookies.delete("cis_role");
  return response;
}

/** A request that carries a staff token: refresh, verify, then the page's capability check (AA-656 / AA-816;
 *  AA-1155: lib/rbac.ts canSeePage by the verified capability list). */
async function sessionGate(
  request: NextRequest,
  staffToken: string,
  pass: () => NextResponse,
): Promise<NextResponse> {
  if (isCognitoToken(staffToken) && tokenExpiresSoon(staffToken)) {
    // AA-816: refresh first (no verify round trip for a token that is about to die). The route
    // validates `next` as a same-site path before redirecting back to it.
    const refreshUrl = new URL(REFRESH_PATH, request.url);
    refreshUrl.searchParams.set("next", request.nextUrl.pathname + request.nextUrl.search);
    return NextResponse.redirect(refreshUrl);
  }
  const verified = await verifyAdminToken(request);
  if (verified instanceof NextResponse) return verified; // invalid/expired token
  if (!isStaffSubject(verified)) {
    return toStaffLogin(request);
  }
  // AA-1157: inside the owner's own "view as a lower role" session, the pages are the view role's (never wider than
  // the verified session: lib/staffViewAs.ts effectiveSession). The API decides every call anyway.
  const view = ownStaffView(request.cookies.get(STAFF_VIEW_AS_COOKIE)?.value, verified.adminId);
  const subject = effectiveSession({ role: String(verified.role ?? ""), capabilities: verified.capabilities ?? [],
    cognito: isCognitoToken(staffToken) }, view);
  if (!canSeePage(subject, request.nextUrl.pathname)) {
    // Real staff session, page not allowed for its capabilities → its own landing page, not /login.
    // Every role's landing page is allowed for that role (lib/rbacCaps.test.mjs 4), so this cannot loop.
    return NextResponse.redirect(new URL(landingPage(subject), request.url));
  }
  return pass();
}

// ── AA-232 helper — verify cis_admin_token ──────────────────────────────────
// Returns a NextResponse (redirect) if verification fails, or the verified
// role if it passed (caller checks it against the page rule). A missing token always fails closed (AA-252
// #1). Only the /auth/verify-admin network-failure case below keeps the
// fail-open-in-dev / fail-closed-in-prod policy (as the tenant gate had; now portal/proxy.ts) — and only for a legacy
// HS256 token: a Cognito token (AA-816) always fails closed.
async function verifyAdminToken(request: NextRequest): Promise<NextResponse | (StaffSession & { adminId?: string })> {
  const token = request.cookies.get(STAFF_TOKEN_COOKIE)?.value;
  if (!token) {
    // AA-252: no JWT present → fail closed. The legacy ADMIN_SECRET fallback
    // (login/route.ts) that used to leave sessions with a role cookie but no
    // JWT has been retired — cis_role alone is client-writable and proves
    // nothing. Every admin/reviewer session must carry a verified JWT now.
    return toStaffLogin(request);
  }

  // AA-573: shares lib/auth-server.ts's cache (AA-551). Without this, the
  // very first protected-page navigation right after login re-ran this
  // same 3s-timeout /auth/verify-admin round trip for a token issued
  // milliseconds earlier — under any transient backend latency this could
  // lose the race and bounce the fresh login straight back to /login.
  const cached = adminVerifyCache.get(token);
  if (cached) return { role: cached.role, capabilities: cached.capabilities, adminId: cached.adminId };

  try {
    const res = await fetch(`${API_URL}/auth/verify-admin`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
      signal: AbortSignal.timeout(3000),
    });

    if (res.status >= 500) {
      // AA-816: a backend error is not a refusal. Fail closed, but keep the session cookies so the user
      // can carry on once the backend is back (a Cognito refresh that just rotated the tokens lands
      // here when verify-admin is still down; wiping would throw the new refresh token away).
      return NextResponse.redirect(new URL("/login?error=backend", request.url));
    }
    if (!res.ok) {
      return toStaffLogin(request);
    }
    const verified = verifiedStaffOf(await res.json());   // AA-1155: with the capability list
    adminVerifyCache.set(token, verified);
    return { role: verified.role, capabilities: verified.capabilities, adminId: verified.adminId };
  } catch {
    const isDev = process.env.NODE_ENV === "development";
    if (!isDev || isCognitoToken(token)) {
      return NextResponse.redirect(new URL("/login", request.url));
    }
    console.warn("[proxy] Admin JWT verify failed — dev mode, allowing through");
    return { role: request.cookies.get("cis_role")?.value ?? "" };
  }
}

/** Redirect to /login and drop the staff session cookies (refresh cookie included, at its own path). */
function toStaffLogin(request: NextRequest): NextResponse {
  const response = NextResponse.redirect(new URL("/login", request.url));
  response.cookies.delete("cis_role");
  response.cookies.delete(CAPS_COOKIE);
  response.cookies.delete(STAFF_TOKEN_COOKIE);
  response.cookies.set(STAFF_REFRESH_COOKIE, "", { path: STAFF_REFRESH_COOKIE_PATH, maxAge: 0 });
  return response;
}

// AA-1111 (S284): every path but the build chunks, the image optimiser and the BFF (/api/*), so pages, 404s,
// /robots.txt and /sitemap.xml get the nonce CSP; isGatedPath() keeps the staff gate on /admin/* and /login only.
// /api/* stays out on purpose: a proxy that runs clones the request body and cuts it at 10 MiB (uploads go through
// the BFF); next.config.ts gives those paths the same policy without a nonce.
export const config = {
  matcher: ["/((?!_next/static|_next/image|api/).*)"],
};
