// portal/lib/auth-server.ts
// AA-885 — the tenant-only part of frontend/lib/auth-server.ts (AA-253 / AA-551 / AA-656), for the
// portal app. Not a byte copy: the portal has no staff session at all, so it carries only the tenant
// verification and its cache. Route handlers and proxy.ts both verify the tenant JWT against the
// backend's POST /auth/verify-tenant before a tenant Bearer is forwarded anywhere.
//
// Route handlers always fail closed (no dev carve-out): there is no page to redirect to and the
// Bearer must not be forwarded on an unverified cookie.
import { NextRequest, NextResponse } from "next/server";

// Same TTL as the admin app had for tenants (S241: the tenant cache keeps 20 s). Every entry is also
// capped at the token's own `exp`, so an expired JWT is never served from cache.
const TENANT_VERIFY_CACHE_TTL_MS = 20_000;

export const API_URL = process.env.API_URL ?? "https://api-cis.lumiguides.it.com";

/** The httpOnly tenant session cookies set by /api/auth/tenant-login (AA-427). */
export const TENANT_COOKIES = [
  "cis_role",
  "cis_tenant_token",
  "cis_tenant_id",
  "cis_tenant_name",
  "cis_tenant_plan",
] as const;

export const TENANT_TOKEN_COOKIE = "cis_tenant_token";

/** The JWT's `exp` in epoch ms, or null. Decoded WITHOUT checking the signature — only ever used to
 * SHORTEN a cache entry for a token the backend has just verified. */
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

export interface VerifiedTenant {
  tenantId: string;
  name: string;
  planTier: string;
}

interface CachedVerify { result: VerifiedTenant; expiresAt: number; }

// Only successful verifications are cached (AA-551): a failure is never cached.
class TenantVerifyCache {
  private entries = new Map<string, CachedVerify>();

  get(token: string): VerifiedTenant | undefined {
    const hit = this.entries.get(token);
    if (!hit) return undefined;
    if (Date.now() >= hit.expiresAt) {
      this.entries.delete(token);
      return undefined;
    }
    return hit.result;
  }

  set(token: string, result: VerifiedTenant) {
    const now = Date.now();
    const exp = tokenExpiryMs(token);
    const expiresAt = exp === null ? now + TENANT_VERIFY_CACHE_TTL_MS : Math.min(now + TENANT_VERIFY_CACHE_TTL_MS, exp);
    if (expiresAt <= now) return;
    this.entries.set(token, { result, expiresAt });
    if (this.entries.size > 500) {
      for (const [key, entry] of this.entries) {
        if (now > entry.expiresAt) this.entries.delete(key);
      }
    }
  }

  delete(token: string) {
    this.entries.delete(token);
  }
}

/** Shared by the route handlers below and by proxy.ts (AA-573: one cache per instance). */
export const tenantVerifyCache = new TenantVerifyCache();

export type VerifyTenantResult =
  | ({ ok: true } & VerifiedTenant)
  // status: the backend's answer (401 invalid/expired), or 0 when it could not be reached.
  | { ok: false; status: number };

/** POST /auth/verify-tenant for one token, through the shared cache. Never fails open. */
export async function verifyTenantToken(token: string): Promise<VerifyTenantResult> {
  const cached = tenantVerifyCache.get(token);
  if (cached) return { ok: true, ...cached };
  try {
    const res = await fetch(`${API_URL}/auth/verify-tenant`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
      signal: AbortSignal.timeout(3000),
    });
    if (!res.ok) return { ok: false, status: res.status };
    const data = await res.json();
    const result: VerifiedTenant = { tenantId: data.tenant_id, name: data.name, planTier: data.plan_tier };
    tenantVerifyCache.set(token, result);
    return { ok: true, ...result };
  } catch {
    return { ok: false, status: 0 };
  }
}

export function unauthorized(): NextResponse {
  return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
}

export type RequireTenantResult =
  | { ok: true; tenantId: string; name: string; planTier: string; token: string }
  | { ok: false; response: NextResponse };

/** Verify the request's tenant session. On success the verified token is returned so the caller
 * forwards exactly the token that was checked. */
export async function requireTenant(request: NextRequest): Promise<RequireTenantResult> {
  const token = request.cookies.get(TENANT_TOKEN_COOKIE)?.value;
  if (!token) return { ok: false, response: unauthorized() };
  const verified = await verifyTenantToken(token);
  if (!verified.ok) return { ok: false, response: unauthorized() };
  return { ok: true, tenantId: verified.tenantId, name: verified.name, planTier: verified.planTier, token };
}
