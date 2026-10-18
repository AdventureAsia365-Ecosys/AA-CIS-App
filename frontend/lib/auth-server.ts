// frontend/lib/auth-server.ts
// AA-253 — shared server-side verification for BFF proxy route handlers.
//
// Route handlers under app/api/**/route.ts run outside middleware.ts's
// matcher (see middleware.ts header comment) — a cookie present on the
// request is NOT proof it was already checked. These helpers call the
// backend's real verify endpoints (same ones middleware.ts uses for page
// gating) so a route handler can independently confirm the caller's
// identity before forwarding X-Admin-Secret or a tenant Bearer token.
//
// Deliberately no dev/prod fail-open branch here (unlike middleware.ts):
// this is the last line of defense before the backend admin secret is
// attached to an outbound request, and there's no page to redirect to on
// failure — always fail closed.
//
// AA-551 — in-memory, short-TTL verification cache (see _VerifyCache below).
// Root cause (docs/implementation-notes/AA-551.md, AA-551 Linear comment,
// 07/09/2026): `/admin/atom-curation`'s dashboard fires up to ~8 concurrent
// `/api/admin/*` proxy calls on one Tour selection (useSectionCounts's
// Promise.all + the active panel's own fetch) — a concurrency level no
// other admin page in this repo reaches. Each call independently re-ran
// this ENTIRE function, including its own 3000ms-timeout network round
// trip to `/auth/verify-admin`, for the SAME token, in the SAME page load.
// Under any transient latency (ECS/ALB/API-Gateway jitter), exactly one of
// N near-simultaneous round trips can exceed 3s and 401 while its siblings
// (same token, same URL, fired milliseconds apart) succeed — this is the
// exact anomaly AA-550's audit observed once on the Publish panel and could
// not reproduce on demand (a latency race, not a deterministic bug).
// verify_admin_secret()/verify_jwt() on the backend were read and ruled out
// as the source (see the Linear comment) — this cache addresses the actual
// mechanism (redundant round trips) rather than only widening the timeout,
// which would lower the odds without removing the cause.
import { NextRequest, NextResponse } from "next/server";
import { STAFF_ROLES, type StaffRole } from "./rbac";
import { STAFF_TOKEN_COOKIE, isCognitoToken } from "./staffSession";

//
// S241 — TTLs (Vercel Fluid CPU: POST /auth/verify-admin was the top proxied
// backend call, 42.2k in 13 days at a 20 s TTL — S240 audit). The admin cache
// keeps a "yes" for up to 5 min; the tenant cache keeps the old 20 s (not part
// of that audit). What a longer admin TTL can and cannot change:
//   - Both backend verify endpoints are STATELESS (api/main.py verify_admin /
//     verify_tenant: JWT signature + exp + role claim only — no DB read, no
//     denylist). So a role change or a deactivated user already only takes
//     effect at the next login / at token exp; this cache never made that
//     faster and does not make it slower.
//   - Logout (/api/auth/admin-logout) deletes the cookie. AA-816: for a Cognito
//     session it also revokes the refresh token and drops this cache entry on the
//     instance that served the logout; other warm instances may still hold a "yes"
//     until the entry's cap (at most the 15-min access-token exp, ADR 0006 §7).
//   - Expiry: every entry is capped at the token's own `exp` claim (see
//     tokenExpiryMs), so an expired JWT is never accepted from cache.
//   - Worst-case staleness: a JWT_SECRET rotation, or a backend change that
//     makes verify stricter, still lets an already-cached token through for up
//     to ADMIN_VERIFY_CACHE_TTL_MS on a warm instance (was 20 s). Each serverless
//     instance has its own Map, so a redeploy clears it.
const ADMIN_VERIFY_CACHE_TTL_MS = 300_000;
const TENANT_VERIFY_CACHE_TTL_MS = 20_000;
// Used when a token carries no readable `exp` (never the case for tokens the
// backend issues today) — the pre-S241 TTL, the conservative choice.
const NO_EXP_TTL_MS = 20_000;

/** The JWT's `exp` in epoch ms, or null. The payload is decoded WITHOUT
 * checking the signature — only ever used to SHORTEN a cache entry for a token
 * the backend has just verified (whose signature covers this same payload). */
function tokenExpiryMs(token: string): number | null {
  try {
    const part = token.split(".")[1];
    if (!part) return null;
    const b64 = part.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(part.length / 4) * 4, "=");
    const exp = (JSON.parse(atob(b64)) as { exp?: unknown }).exp;
    return typeof exp === "number" && Number.isFinite(exp) ? exp * 1000 : null;
  } catch {
    return null;
  }
}

interface CachedVerify<T> { result: T; expiresAt: number; }

// Only successful verifications are cached — a failure is never cached, so
// a real revoke/expiry is never masked past its own TTL by this layer; the
// worst case of a cache hit is serving an already-correct "yes" from a round
// trip that would have said the same thing (see the S241 note above).
class VerifyCache<T> {
  private entries = new Map<string, CachedVerify<T>>();

  constructor(private readonly ttlMs: number) {}

  get(token: string): T | undefined {
    const hit = this.entries.get(token);
    if (!hit) return undefined;
    if (Date.now() >= hit.expiresAt) {
      this.entries.delete(token);
      return undefined;
    }
    return hit.result;
  }

  set(token: string, result: T) {
    const now = Date.now();
    const exp = tokenExpiryMs(token);
    const expiresAt = exp === null ? now + Math.min(this.ttlMs, NO_EXP_TTL_MS) : Math.min(now + this.ttlMs, exp);
    if (expiresAt <= now) return; // already expired by its own claim — never cache it
    this.entries.set(token, { result, expiresAt });
    // Cheap, bounded cleanup — avoids an unbounded map across many distinct
    // tokens/sessions over a long-lived warm serverless instance.
    if (this.entries.size > 500) {
      for (const [key, entry] of this.entries) {
        if (now > entry.expiresAt) this.entries.delete(key);
      }
    }
  }

  /** AA-816: logout drops the token's entry so a revoked session is not served from this cache. */
  delete(token: string) {
    this.entries.delete(token);
  }
}

// AA-573: exported so middleware.ts's own verifyAdminToken()/verifyTenantToken()
// can share the same cache — those run the identical uncached-round-trip
// pattern this file already fixed for /api/admin/* proxy calls (same
// /auth/verify-admin|verify-tenant endpoint, same token, milliseconds apart)
// but as a separate module they never benefited from it. A fresh login
// followed immediately by a client-side navigation to a protected route is
// exactly this shape — see docs/implementation-notes/AA-573.md.
export interface VerifiedStaff {
  adminId: string;
  role: string;
  /** Display name for the UI: display_name, else email, else username (AA-816). */
  name: string;
}
export const adminVerifyCache = new VerifyCache<VerifiedStaff>(ADMIN_VERIFY_CACHE_TTL_MS);
export const tenantVerifyCache = new VerifyCache<{ tenantId: string; name: string; planTier: string }>(
  TENANT_VERIFY_CACHE_TTL_MS,
);

const API_URL = process.env.API_URL ?? "https://api-cis.lumiguides.it.com";

function unauthorized(): NextResponse {
  return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
}

function forbidden(): NextResponse {
  return NextResponse.json({ error: "Forbidden" }, { status: 403 });
}

export type RequireAdminResult =
  // AA-816 (C5): `bearer` is the staff access token when the cookie holds a Cognito token (it has an
  // `iss`), else null. Proxies forward it as Authorization: Bearer (staffBearerHeader) so the API's
  // staff_bearer middleware takes the role and actor from the verified token instead of the headers.
  | { ok: true; adminId: string; role: string; bearer: string | null }
  | { ok: false; response: NextResponse };

// AA-656 — `roles` is the route's entry in lib/rbac.ts API_RULES. The role checked is the one inside
// the verified JWT; the `cis_role` cookie is never consulted. A valid staff token with a role not in
// `roles` gets 403 (not 401) so the UI can tell "log in again" from "not allowed".
export async function requireAdmin(
  request: NextRequest,
  roles: readonly StaffRole[] = STAFF_ROLES,
): Promise<RequireAdminResult> {
  const result = await verifyStaff(request);
  if (result.ok && !(roles as readonly string[]).includes(result.role)) {
    return { ok: false, response: forbidden() };
  }
  return result;
}

/**
 * AA-816 (C5) — the extra header a BFF proxy adds for a Cognito session: `Authorization: Bearer
 * <cis_admin_token>`. A legacy HS256 session gets nothing (unchanged behaviour). X-Admin-Secret is
 * still sent alongside until the cutover (plan step C9).
 */
export function staffBearerHeader(auth: { bearer: string | null }): Record<string, string> {
  return auth.bearer ? { Authorization: `Bearer ${auth.bearer}` } : {};
}

export type VerifyStaffTokenResult =
  | ({ ok: true } & VerifiedStaff)
  // status: the backend's answer (401 unknown/invalid, 403 e.g. a booking-only Cognito user), or 0
  // when the backend could not be reached.
  | { ok: false; status: number };

/**
 * POST /auth/verify-admin for one token, through the shared cache. Accepts both a legacy HS256 token
 * and (once the API has plan step C3) a Cognito access token; the backend picks the path by `iss`.
 * Never fails open.
 */
export async function verifyStaffToken(token: string): Promise<VerifyStaffTokenResult> {
  const cached = adminVerifyCache.get(token);
  if (cached) return { ok: true, ...cached };
  try {
    const res = await fetch(`${API_URL}/auth/verify-admin`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
      signal: AbortSignal.timeout(3000),
    });
    if (!res.ok) return { ok: false, status: res.status };
    const data = await res.json();
    const result: VerifiedStaff = {
      adminId: data.admin_id,
      role: data.role,
      name: data.display_name || data.email || data.username || "",
    };
    adminVerifyCache.set(token, result);
    return { ok: true, ...result };
  } catch {
    return { ok: false, status: 0 };
  }
}

async function verifyStaff(request: NextRequest): Promise<RequireAdminResult> {
  const token = request.cookies.get(STAFF_TOKEN_COOKIE)?.value;
  if (!token) {
    return { ok: false, response: unauthorized() };
  }
  // Network error / timeout fails closed too — no dev carve-out (see file header).
  const verified = await verifyStaffToken(token);
  if (!verified.ok) {
    return { ok: false, response: unauthorized() };
  }
  return {
    ok: true,
    adminId: verified.adminId,
    role: verified.role,
    bearer: isCognitoToken(token) ? token : null,
  };
}

export type RequireTenantResult =
  // AA-427: name/planTier added (backend /auth/verify-tenant already returned
  // them; just wasn't surfaced) so /api/tenant/me can hand the FE tenant
  // display info without a second round trip or ever exposing the token.
  | { ok: true; tenantId: string; name: string; planTier: string }
  | { ok: false; response: NextResponse };

export async function requireTenant(request: NextRequest): Promise<RequireTenantResult> {
  const token = request.cookies.get("cis_tenant_token")?.value;
  if (!token) {
    return { ok: false, response: unauthorized() };
  }

  // AA-551 — same cache/reasoning as requireAdmin() above; the tenant portal
  // (/portal/t7-planning etc.) can fire multiple concurrent /api/tenant/*
  // calls on one page too, same class of redundant-round-trip risk.
  const cached = tenantVerifyCache.get(token);
  if (cached) {
    return { ok: true, tenantId: cached.tenantId, name: cached.name, planTier: cached.planTier };
  }

  try {
    const res = await fetch(`${API_URL}/auth/verify-tenant`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
      signal: AbortSignal.timeout(3000),
    });

    if (!res.ok) {
      return { ok: false, response: unauthorized() };
    }

    const data = await res.json();
    const result = { tenantId: data.tenant_id, name: data.name, planTier: data.plan_tier };
    tenantVerifyCache.set(token, result);
    return { ok: true, ...result };
  } catch {
    return { ok: false, response: unauthorized() };
  }
}
