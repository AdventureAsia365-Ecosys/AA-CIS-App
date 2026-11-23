// frontend/lib/auth-server.ts
// AA-253 — shared server-side verification for BFF proxy route handlers.
//
// Route handlers under app/api/**/route.ts run outside proxy.ts's
// matcher (see proxy.ts header comment) — a cookie present on the
// request is NOT proof it was already checked. These helpers call the
// backend's real verify endpoint (the same one proxy.ts uses for page
// gating) so a route handler can independently confirm the caller's
// identity before forwarding X-Admin-Secret. (AA-885: staff only; the
// tenant helpers live in the portal app, portal/lib/auth-server.ts.)
//
// Deliberately no dev/prod fail-open branch here (unlike proxy.ts):
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
import { STAFF_ROLES, isReadMethod, isReadOnlyRole, passesApiRule, type ApiRule, type StaffRole } from "./rbac";
import { STAFF_TOKEN_COOKIE, isCognitoToken } from "./staffSession";
import {
  STAFF_VIEW_AS_COOKIE, STAFF_VIEW_AS_HEADER, VIEW_AS_READ_ONLY, ownStaffView, refusedWriteLog, staffViewStep,
} from "./staffViewAs";

//
// S241 — TTLs (Vercel Fluid CPU: POST /auth/verify-admin was the top proxied
// backend call, 42.2k in 13 days at a 20 s TTL — S240 audit). The admin cache
// keeps a "yes" for up to 5 min. (AA-885: the tenant cache and requireTenant()
// moved to the portal app, portal/lib/auth-server.ts.) What a longer admin TTL
// can and cannot change:
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

// AA-573: exported so proxy.ts's own verifyAdminToken() can share the same
// cache — it ran the identical uncached-round-trip pattern this file already
// fixed for /api/admin/* proxy calls (same /auth/verify-admin endpoint, same
// token, milliseconds apart) but as a separate module never benefited from it. A fresh login
// followed immediately by a client-side navigation to a protected route is
// exactly this shape — see docs/implementation-notes/AA-573.md.
export interface VerifiedStaff {
  adminId: string;
  role: string;
  /** Display name for the UI: display_name, else email, else username (AA-816). */
  name: string;
  /** AA-1155 (R5.2): the capabilities verify-admin returned for this session (a custom role's own list). */
  capabilities: string[];
  /** AA-1155: the custom role's name (a cis-custom holder), else null. */
  customLabel: string | null;
}
export const adminVerifyCache = new VerifyCache<VerifiedStaff>(ADMIN_VERIFY_CACHE_TTL_MS);

const API_URL = process.env.API_URL ?? "https://api-cis.lumiguides.it.com";

function unauthorized(): NextResponse {
  return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
}

function forbidden(): NextResponse {
  return NextResponse.json({ error: "Forbidden" }, { status: 403 });
}

/** AA-1157: the owner's own open "view as a lower role" session of this request (lib/staffViewAs.ts). `forward`: the
 *  proxies send the token as STAFF_VIEW_AS_HEADER (false on the routes that act as the owner: end, sign-out). */
export interface StaffViewAsAuth {
  token: string;
  sessionId: string;
  role: string;
  forward: boolean;
}

export type RequireAdminResult =
  // AA-816 (C5): `bearer` is the staff access token when the cookie holds a Cognito token (it has an
  // `iss`), else null. Proxies forward it as Authorization: Bearer (staffCredentialHeaders) so the API's
  // staff_bearer middleware takes the role and actor from the verified token instead of the headers.
  // AA-1157: inside an own view, role / capabilities are the view's (never wider than the verified ones).
  | { ok: true; adminId: string; role: string; capabilities: string[]; bearer: string | null; viewAs?: StaffViewAsAuth }
  | { ok: false; response: NextResponse };

/** AA-1157: requireAdmin() options. `allowDuringViewAs`: the route acts as the owner during a view (the end and the
 *  sign-out): no write refusal, no role swap, the token is not forwarded. */
export interface RequireAdminOptions {
  allowDuringViewAs?: boolean;
}

// AA-656 — `rule` is the route's entry in lib/rbac.ts API_RULES. The role checked is the one inside
// the verified JWT; the `cis_role` cookie is never consulted. A valid staff token the rule does not admit
// gets 403 (not 401) so the UI can tell "log in again" from "not allowed". AA-1155: an API_RULES entry decides by the
// verified capability list (a custom role passes by its own capabilities); a plain role list (the view-as routes)
// decides by role, so it never admits a custom role.
// AA-878: a read-only role (the viewer) gets 403 on every method but GET/HEAD/OPTIONS here, in the one place
// every secret-attaching proxy goes through, so no BFF route can forward a write for it by default.
// AA-1157: inside the owner's own "view as a lower role" session (httpOnly cookie, lib/staffViewAs.ts) every write is
// refused 403 view_as_read_only here, before any secret or token is attached (one structured log line
// "staff_view_as_write_refused" per refusal: it never reaches the API, so the API writes no staff.request row), and the rule is checked with the view
// role (its claimed capabilities intersected with the verified ones), unless `allowDuringViewAs`.
export async function requireAdmin(
  request: NextRequest,
  rule: ApiRule | readonly StaffRole[] = STAFF_ROLES,
  options: RequireAdminOptions = {},
): Promise<RequireAdminResult> {
  const verified = await verifyStaff(request);
  const result = verified.ok ? withStaffView(request, verified, options) : verified;
  if (!result.ok) return result;
  if (result.ok && !passesApiRule({ role: result.role, capabilities: result.capabilities }, rule)) {
    return { ok: false, response: forbidden() };
  }
  if (result.ok && isReadOnlyRole(result.role) && !isReadMethod(request.method)) {
    return { ok: false, response: forbidden() };
  }
  return result;
}

type VerifiedResult = Extract<RequireAdminResult, { ok: true }>;

/** AA-1157: the verified result as the request runs: the owner's own view swapped in, or a refused write. Fails
 *  closed (lib/staffViewAs.ts staffViewStep): an own, unexpired view cookie refuses every write even when the view is
 *  not honoured here; a read then keeps the verified rights and still forwards the token (the API ends the session). */
function withStaffView(request: NextRequest, verified: VerifiedResult, options: RequireAdminOptions): RequireAdminResult {
  const token = request.cookies.get(STAFF_VIEW_AS_COOKIE)?.value;
  const view = token ? ownStaffView(token, verified.adminId) : null;
  const step = staffViewStep(isReadMethod(request.method), { ...verified, cognito: verified.bearer !== null }, view,
    !!options.allowDuringViewAs);
  if (step.kind === "none" || !token || !view) return verified;
  if (step.kind === "refuse") {
    // The refusal never reaches the API (no staff.request row): this structured line is its trace.
    console.warn(refusedWriteLog("cis", { ...view, staffId: verified.adminId }, request.method,
      request.nextUrl.pathname));
    return { ok: false, response: NextResponse.json({ detail: VIEW_AS_READ_ONLY }, { status: 403 }) };
  }
  const viewAs = { token, sessionId: view.sessionId, role: view.role, forward: step.kind !== "owner" };
  if (step.kind === "owner") return { ...verified, viewAs };
  return { ...verified, role: step.role, capabilities: step.capabilities, viewAs };
}

// AA-816 (C5/C9): the backend paths where the API's staff_bearer middleware reads a staff Bearer
// (api/core/staff_authz.py STAFF_PATH_PREFIXES; a unit test keeps the two lists equal). Elsewhere
// (e.g. /v1/tours/{id}/full) a Bearer is read as a TENANT token, so a staff call there stays secret-only.
export const STAFF_API_PREFIXES = ["admin", "v1/pipeline", "v1/rules", "v1/social"] as const;

/** True when `backendPath` (no leading slash needed, e.g. "admin/overview") is a staff API path. */
export function isStaffApiPath(backendPath: string): boolean {
  const p = backendPath.replace(/^\/+/, "");
  return STAFF_API_PREFIXES.some((prefix) => p === prefix || p.startsWith(prefix + "/"));
}

/**
 * AA-816 (C9) — the credential a staff BFF proxy sends to `backendPath`, or null when it needs the admin
 * secret and none is configured (the proxy answers 503).
 *
 * - Cognito session on a staff API path: `Authorization: Bearer <cis_admin_token>` ONLY. The API accepts
 *   the verified principal without X-Admin-Secret, and takes role + actor from the token. AA-1157: inside the
 *   owner's own view, plus `x-staff-view-as: <view token>`.
 * - Legacy HS256 session (the BFF can only verify one while the legacy flag is on), or a path outside the
 *   staff prefixes: `X-Admin-Secret` (on a Vercel Preview this is the Preview credential, capped at viewer
 *   by the API). No Bearer is sent off the staff paths.
 */
export function staffCredentialHeaders(
  auth: { bearer: string | null; viewAs?: StaffViewAsAuth },
  adminSecret: string,
  backendPath: string,
): Record<string, string> | null {
  if (auth.bearer && isStaffApiPath(backendPath)) {
    return { Authorization: `Bearer ${auth.bearer}`, ...staffViewAsHeader(auth) };
  }
  if (!adminSecret) return null;
  return { "X-Admin-Secret": adminSecret };
}

/** AA-1157: the owner's view-as token rides along on every staff path (the API runs the call as the view role). */
function staffViewAsHeader(auth: { viewAs?: StaffViewAsAuth }): Record<string, string> {
  return auth.viewAs?.forward ? { [STAFF_VIEW_AS_HEADER]: auth.viewAs.token } : {};
}

export function secretNotConfigured(): NextResponse {
  return NextResponse.json({ detail: "Admin secret not configured" }, { status: 503 });
}

export type VerifyStaffTokenResult =
  | ({ ok: true } & VerifiedStaff)
  // status: the backend's answer (401 unknown/invalid, 403 e.g. a booking-only Cognito user), or 0
  // when the backend could not be reached. detail: the backend's `detail` text when it sent one (AA-1111:
  // "mfa_required" = an administrator signed in without an authenticator code while the switch is on).
  | { ok: false; status: number; detail?: string };

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
    if (!res.ok) return { ok: false, status: res.status, detail: await refusalDetail(res) };
    const data = await res.json();
    const result = verifiedStaffOf(data);
    adminVerifyCache.set(token, result);
    return { ok: true, ...result };
  } catch {
    return { ok: false, status: 0 };
  }
}

/** The cache entry of a /auth/verify-admin answer (AA-1155: with its capability list and custom role name). */
export function verifiedStaffOf(data: Record<string, unknown>): VerifiedStaff {
  const caps = Array.isArray(data.capabilities) ? data.capabilities : [];
  return {
    adminId: String(data.admin_id ?? ""),
    role: String(data.role ?? ""),
    name: String(data.display_name || data.email || data.username || ""),
    capabilities: caps.filter((c): c is string => typeof c === "string"),
    customLabel: typeof data.custom_role_label === "string" ? data.custom_role_label : null,
  };
}

/** The `detail` string of a refused verify-admin answer, or undefined (never throws). */
async function refusalDetail(res: Response): Promise<string | undefined> {
  try {
    const body = await res.json();
    return typeof body?.detail === "string" ? body.detail : undefined;
  } catch {
    return undefined;
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
    capabilities: verified.capabilities,
    bearer: isCognitoToken(token) ? token : null,
  };
}
