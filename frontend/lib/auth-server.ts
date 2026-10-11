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

const VERIFY_CACHE_TTL_MS = 20_000;

interface CachedVerify<T> { result: T; expiresAt: number; }

// Only successful verifications are cached — a failure is never cached, so
// a real revoke/expiry is never masked past its own TTL by this layer; the
// worst case of a cache hit is serving an already-correct "yes" a few
// seconds sooner from a round trip that would have said the same thing.
class VerifyCache<T> {
  private entries = new Map<string, CachedVerify<T>>();

  get(token: string): T | undefined {
    const hit = this.entries.get(token);
    if (!hit) return undefined;
    if (Date.now() > hit.expiresAt) {
      this.entries.delete(token);
      return undefined;
    }
    return hit.result;
  }

  set(token: string, result: T) {
    this.entries.set(token, { result, expiresAt: Date.now() + VERIFY_CACHE_TTL_MS });
    // Cheap, bounded cleanup — avoids an unbounded map across many distinct
    // tokens/sessions over a long-lived warm serverless instance.
    if (this.entries.size > 500) {
      const now = Date.now();
      for (const [key, entry] of this.entries) {
        if (now > entry.expiresAt) this.entries.delete(key);
      }
    }
  }
}

// AA-573: exported so middleware.ts's own verifyAdminToken()/verifyTenantToken()
// can share the same cache — those run the identical uncached-round-trip
// pattern this file already fixed for /api/admin/* proxy calls (same
// /auth/verify-admin|verify-tenant endpoint, same token, milliseconds apart)
// but as a separate module they never benefited from it. A fresh login
// followed immediately by a client-side navigation to a protected route is
// exactly this shape — see docs/implementation-notes/AA-573.md.
export const adminVerifyCache = new VerifyCache<{ adminId: string; role: string }>();
export const tenantVerifyCache = new VerifyCache<{ tenantId: string; name: string; planTier: string }>();

const API_URL = process.env.API_URL ?? "https://api-cis.lumiguides.it.com";

function unauthorized(): NextResponse {
  return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
}

function forbidden(): NextResponse {
  return NextResponse.json({ error: "Forbidden" }, { status: 403 });
}

export type RequireAdminResult =
  | { ok: true; adminId: string; role: string }
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

async function verifyStaff(request: NextRequest): Promise<RequireAdminResult> {
  const token = request.cookies.get("cis_admin_token")?.value;
  if (!token) {
    return { ok: false, response: unauthorized() };
  }

  const cached = adminVerifyCache.get(token);
  if (cached) {
    return { ok: true, adminId: cached.adminId, role: cached.role };
  }

  try {
    const res = await fetch(`${API_URL}/auth/verify-admin`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
      signal: AbortSignal.timeout(3000),
    });

    if (!res.ok) {
      return { ok: false, response: unauthorized() };
    }

    const data = await res.json();
    const result = { adminId: data.admin_id, role: data.role };
    adminVerifyCache.set(token, result);
    return { ok: true, ...result };
  } catch {
    // Network error / timeout — fail closed, no dev carve-out (see file header).
    return { ok: false, response: unauthorized() };
  }
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
