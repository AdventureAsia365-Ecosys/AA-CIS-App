// frontend/middleware.ts
// AA-232: ADMIN_PATHS/INTERNAL_PATHS verify a real JWT (cis_admin_token) via
// /auth/verify-admin, mirroring the tenant-portal verify below. The old check
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
import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { adminVerifyCache, tenantVerifyCache } from "./lib/auth-server";
import { isStaffRole, landingPage, pageRule } from "./lib/rbac";

const PUBLIC_PATHS = ["/login", "/tenant-login"];

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "https://api-cis.lumiguides.it.com";

export async function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  if (PUBLIC_PATHS.some(p => pathname.startsWith(p))) {
    return NextResponse.next();
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
    return NextResponse.redirect(new URL(landingPage(dashRole), request.url));
  }

  const route = pageRule(pathname);
  if (!route) {
    // AA-252 #4/#5: reached middleware (config.matcher matched) but isn't in
    // the allow-list — fail closed instead of silently falling through.
    return NextResponse.redirect(new URL("/login", request.url));
  }

  const loginPath = route.prefix === "/portal" ? "/tenant-login" : "/login";
  const allowed = route.roles as string[];

  // AA-656: identity comes from whichever session token is present, verified server-side.
  if (request.cookies.get("cis_admin_token")?.value) {
    const verified = await verifyAdminToken(request);
    if (verified instanceof NextResponse) return verified; // invalid/expired token
    if (!isStaffRole(verified.role)) {
      const response = NextResponse.redirect(new URL("/login", request.url));
      response.cookies.delete("cis_role");
      response.cookies.delete("cis_admin_token");
      return response;
    }
    if (!allowed.includes(verified.role)) {
      // Real staff session, page not allowed for its role → its own landing page, not /login.
      // Every role's landing page is allowed for that role, so this cannot loop.
      return NextResponse.redirect(new URL(landingPage(verified.role), request.url));
    }
    return NextResponse.next();
  }

  if (request.cookies.get("cis_tenant_token")?.value && allowed.includes("tenant")) {
    const verifyResult = await verifyTenantToken(request);
    if (verifyResult) return verifyResult;
    return NextResponse.next();
  }

  // No token (a bare or forged cis_role cookie proves nothing) → log in.
  const response = NextResponse.redirect(new URL(loginPath, request.url));
  response.cookies.delete("cis_role");
  return response;
}

// ── AA-232 helper — verify cis_admin_token ──────────────────────────────────
// Returns a NextResponse (redirect) if verification fails, or the verified
// role if it passed (caller checks it against the page rule). A missing token always fails closed (AA-252
// #1). Only the /auth/verify-admin network-failure case below keeps the
// fail-open-in-dev / fail-closed-in-prod policy, matching the tenant helper.
async function verifyAdminToken(request: NextRequest): Promise<NextResponse | { role: string }> {
  const token = request.cookies.get("cis_admin_token")?.value;
  if (!token) {
    // AA-252: no JWT present → fail closed. The legacy ADMIN_SECRET fallback
    // (login/route.ts) that used to leave sessions with a role cookie but no
    // JWT has been retired — cis_role alone is client-writable and proves
    // nothing. Every admin/reviewer session must carry a verified JWT now.
    const response = NextResponse.redirect(new URL("/login", request.url));
    response.cookies.delete("cis_role");
    response.cookies.delete("cis_admin_token");
    return response;
  }

  // AA-573: shares lib/auth-server.ts's cache (AA-551). Without this, the
  // very first protected-page navigation right after login re-ran this
  // same 3s-timeout /auth/verify-admin round trip for a token issued
  // milliseconds earlier — under any transient backend latency this could
  // lose the race and bounce the fresh login straight back to /login.
  const cached = adminVerifyCache.get(token);
  if (cached) return { role: cached.role };

  try {
    const res = await fetch(`${API_URL}/auth/verify-admin`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
      signal: AbortSignal.timeout(3000),
    });

    if (!res.ok) {
      const response = NextResponse.redirect(new URL("/login", request.url));
      response.cookies.delete("cis_role");
      response.cookies.delete("cis_admin_token");
      return response;
    }
    const data = await res.json();
    adminVerifyCache.set(token, { adminId: data.admin_id, role: data.role });
    return { role: data.role };
  } catch {
    const isDev = process.env.NODE_ENV === "development";
    if (!isDev) {
      return NextResponse.redirect(new URL("/login", request.url));
    }
    console.warn("[middleware] Admin JWT verify failed — dev mode, allowing through");
    return { role: request.cookies.get("cis_role")?.value ?? "" };
  }
}

// ── Tenant helper — verify cis_tenant_token ─────────────────────────────────
// Mirrors verifyAdminToken(). A missing token fails closed (was already the
// case pre-AA-252 for the tenant role — unchanged here).
async function verifyTenantToken(request: NextRequest): Promise<NextResponse | null> {
  const token = request.cookies.get("cis_tenant_token")?.value;
  if (!token) {
    return NextResponse.redirect(new URL("/tenant-login", request.url));
  }

  // AA-573: same shared-cache fix as verifyAdminToken() above.
  if (tenantVerifyCache.get(token)) return null;

  try {
    const res = await fetch(`${API_URL}/auth/verify-tenant`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
      // Short timeout — don't block page load
      signal: AbortSignal.timeout(3000),
    });

    if (!res.ok) {
      // JWT invalid/expired → clear cookies + redirect
      const response = NextResponse.redirect(new URL("/tenant-login", request.url));
      response.cookies.delete("cis_role");
      response.cookies.delete("cis_tenant_token");
      response.cookies.delete("cis_tenant_id");
      response.cookies.delete("cis_tenant_name");
      response.cookies.delete("cis_tenant_plan");
      return response;
    }
    const data = await res.json();
    tenantVerifyCache.set(token, { tenantId: data.tenant_id, name: data.name, planTier: data.plan_tier });
    return null;
  } catch {
    // Verification service unreachable — fail open in dev, fail closed in prod
    const isDev = process.env.NODE_ENV === "development";
    if (!isDev) {
      return NextResponse.redirect(new URL("/tenant-login", request.url));
    }
    console.warn("[middleware] Tenant JWT verify failed — dev mode, allowing through");
    return null;
  }
}

export const config = {
  matcher: [
    "/admin/:path*",
    "/portal/:path*",
    "/login",
    "/tenant-login",
  ],
};
