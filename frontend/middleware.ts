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
// its list) is denied by default. See PROTECTED_ROUTES below.
//
// Known limitation, kept as-is (not in AA-252 scope, no new exposure):
// the "content" role (login/route.ts CONTENT_PASSWORD check) has no JWT/
// token and nothing re-verifies it after the initial login — the cis_role
// cookie is trusted at face value, same as before this fix. Tracked for a
// real fix under AA-253 (see also: the BFF proxy routes under /api/admin,
// /api/pipeline, /api/tour-full, /api/tenant/pipeline are not covered by
// this middleware's matcher at all — a separate, deeper gap also tracked
// under AA-253, out of scope here).
import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { adminVerifyCache, tenantVerifyCache } from "./lib/auth-server";

const PUBLIC_PATHS = ["/login", "/tenant-login"];

// AA-252: allow-list. Every protected path is declared here with the exact
// roles permitted to reach it. First-match-wins by prefix; prefixes are
// disjoint so order doesn't matter in practice. A path that matches
// config.matcher but has no entry here is denied by default (see the
// `!route` branch in middleware()) — this is what closes #4/#5.
const PROTECTED_ROUTES: { prefix: string; roles: string[] }[] = [
  // Admin-only (was ADMIN_PATHS)
  { prefix: "/admin/tenants", roles: ["admin"] },
  // AA-505 — LLM cost/quality monitoring (Tenant->Model->Stage). Admin-only, same tier as
  // tenants above — this shows real per-call spend + Việc C's model choice is
  // reached via /admin/settings (already admin+reviewer+content, unchanged).
  { prefix: "/admin/llm-usage", roles: ["admin"] },
  // AA-527 — Atom Curation, replacing the removed tenant-facing T6 (AA-526): admin decides which
  // atoms are good to use, same admin-only tier as llm-usage above (Nghiệp's
  // explicit choice among 2 options presented, 05/09/2026), not the broader content-team tier.
  { prefix: "/admin/atom-curation", roles: ["admin"] },
  // AA-551 — split out of Atom Curation (06-08, per-Tour tenant activity, moved to its own page/
  // URL so the Admin/Tenant boundary AA-550 flagged is visible in the nav, not just a comment).
  // Same admin-only tier.
  { prefix: "/admin/tenant-activity", roles: ["admin"] },
  // AA-560 — "07 · Platform Stats": Review Log + Trust Ramp (moved from a4-oversight) + a new
  // real backend gate/error aggregate. Same admin-only tier, same Social Content sub-nav grouping
  // as tenant-activity above.
  { prefix: "/admin/platform-stats", roles: ["admin"] },
  // Internal staff pages (was INTERNAL_PATHS) — admin/reviewer get real JWT
  // verification; content is the known-limitation carve-out described above.
  { prefix: "/admin/dashboard", roles: ["admin", "reviewer", "content"] },
  { prefix: "/admin/upload", roles: ["admin", "reviewer", "content"] },
  { prefix: "/admin/pipeline", roles: ["admin", "reviewer", "content"] },
  { prefix: "/admin/master-content", roles: ["admin", "reviewer", "content"] },
  { prefix: "/admin/review", roles: ["admin", "reviewer", "content"] },
  { prefix: "/admin/brand", roles: ["admin", "reviewer", "content"] },
  { prefix: "/admin/s1-rewrite", roles: ["admin", "reviewer", "content"] }, // #4
  { prefix: "/admin/settings", roles: ["admin", "reviewer", "content"] },   // #4
  { prefix: "/upload", roles: ["admin", "reviewer", "content"] },
  { prefix: "/review", roles: ["admin", "reviewer", "content"] },
  { prefix: "/catalog", roles: ["admin", "reviewer", "content"] },
  { prefix: "/brand", roles: ["admin", "reviewer", "content"] }, // #5
  // Tenant portal (was TENANT_PATHS) — admin now goes through the same real
  // verifyAdminToken() as every other admin route, no bypass (#3).
  { prefix: "/portal", roles: ["admin", "tenant"] },
];

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "https://api-cis.lumiguides.it.com";

export async function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  if (PUBLIC_PATHS.some(p => pathname.startsWith(p))) {
    return NextResponse.next();
  }

  const route = PROTECTED_ROUTES.find(r => pathname.startsWith(r.prefix));
  if (!route) {
    // AA-252 #4/#5: reached middleware (config.matcher matched) but isn't in
    // the allow-list — fail closed instead of silently falling through.
    return NextResponse.redirect(new URL("/login", request.url));
  }

  const loginPath = route.prefix === "/portal" ? "/tenant-login" : "/login";
  const role = request.cookies.get("cis_role")?.value;

  if (!role || !route.roles.includes(role)) {
    return NextResponse.redirect(new URL(loginPath, request.url));
  }

  if (role === "admin" || role === "reviewer") {
    const verifyResult = await verifyAdminToken(request);
    if (verifyResult) return verifyResult; // redirect — invalid/expired/missing token
    return NextResponse.next();
  }

  if (role === "tenant") {
    const verifyResult = await verifyTenantToken(request);
    if (verifyResult) return verifyResult;
    return NextResponse.next();
  }

  // role === "content" — known limitation, see file header. Not hardened in
  // this PR; behavior is unchanged from before AA-252.
  return NextResponse.next();
}

// ── AA-232 helper — verify cis_admin_token ──────────────────────────────────
// Returns a NextResponse (redirect) if verification fails, or null if it
// passed (caller continues). A missing token always fails closed (AA-252
// #1). Only the /auth/verify-admin network-failure case below keeps the
// fail-open-in-dev / fail-closed-in-prod policy, matching the tenant helper.
async function verifyAdminToken(request: NextRequest): Promise<NextResponse | null> {
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
  if (adminVerifyCache.get(token)) return null;

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
    return null;
  } catch {
    const isDev = process.env.NODE_ENV === "development";
    if (!isDev) {
      return NextResponse.redirect(new URL("/login", request.url));
    }
    console.warn("[middleware] Admin JWT verify failed — dev mode, allowing through");
    return null;
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
    "/upload/:path*",
    "/review/:path*",
    "/catalog/:path*",
    "/brand/:path*", // AA-252 #5 — was missing, middleware never ran for /brand
    "/portal/:path*",
    "/login",
    "/tenant-login",
  ],
};
