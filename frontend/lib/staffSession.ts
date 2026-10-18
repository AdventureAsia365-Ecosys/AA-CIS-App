// frontend/lib/staffSession.ts
// AA-816 (C4) — staff session cookie names and token helpers shared by middleware.ts, the BFF
// (lib/auth-server.ts) and the auth routes. Edge-safe: no secrets, no Node APIs, no React.
//
// Two kinds of token can sit in the `cis_admin_token` cookie during the Phase 1 window:
//   - legacy: the CIS HS256 JWT from /auth/admin-login (no `iss` claim, 24 h);
//   - cognito: the WORKFORCE access token from the cis-admin / breakglass client (has `iss`, 15 min).
// The backend picks its verify path by the unverified `iss` the same way (/auth/verify-admin), so the
// BFF uses the same test. Nothing here VERIFIES a token: the payload is read only to route the
// request (which refresh path, whether to forward a Bearer); the backend still checks the signature.

export const STAFF_TOKEN_COOKIE = "cis_admin_token";
/** Cognito refresh token, scoped to /api/auth so it only ever reaches the auth routes. */
export const STAFF_REFRESH_COOKIE = "cis_staff_rt";
export const STAFF_REFRESH_COOKIE_PATH = "/api/auth";
/** Display hints for the UI (never an access decision): set by the server after a verified sign-in. */
export const ROLE_COOKIE = "cis_role";
export const USER_COOKIE = "cis_user";

/** Where the browser lands after sign-in; middleware.ts redirects it to the role's landing page. */
export const POST_LOGIN_PATH = "/admin/dashboard";
export const REFRESH_PATH = "/api/auth/refresh";

/** Refresh this many seconds before `exp`, so a token never expires between middleware and the API. */
export const REFRESH_SKEW_S = 60;

export interface StaffTokenClaims {
  iss?: unknown;
  exp?: unknown;
  client_id?: unknown;
  token_use?: unknown;
  nonce?: unknown;
  aud?: unknown;
}

/** The JWT payload, decoded WITHOUT checking the signature, or null when it is not a JWT. */
export function decodeJwtPayload(token: string): StaffTokenClaims | null {
  try {
    const part = token.split(".")[1];
    if (!part) return null;
    const b64 = part.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(part.length / 4) * 4, "=");
    const payload = JSON.parse(atob(b64));
    return payload && typeof payload === "object" ? (payload as StaffTokenClaims) : null;
  } catch {
    return null;
  }
}

/** True when the token carries an `iss` claim: a Cognito token. Legacy CIS HS256 tokens have none. */
export function isCognitoToken(token: string | undefined | null): boolean {
  if (!token) return false;
  const iss = decodeJwtPayload(token)?.iss;
  return typeof iss === "string" && iss.length > 0;
}

/** The token's `exp` in epoch seconds, or null. */
export function tokenExp(token: string): number | null {
  const exp = decodeJwtPayload(token)?.exp;
  return typeof exp === "number" && Number.isFinite(exp) ? exp : null;
}

/** True when the token is expired or will be within `skewS` seconds (or has no readable `exp`). */
export function tokenExpiresSoon(token: string, skewS = REFRESH_SKEW_S, nowS = Date.now() / 1000): boolean {
  const exp = tokenExp(token);
  return exp === null || exp - skewS <= nowS;
}

/**
 * A same-site relative path to return to after a refresh, or the post-login default. Only a path
 * that starts with exactly one "/" is accepted: no scheme, no "//host", no backslash tricks, no
 * control characters. Never redirect to an absolute URL taken from the query string.
 */
export function safeNextPath(raw: string | null | undefined): string {
  if (!raw || raw.length > 2048) return POST_LOGIN_PATH;
  if (!raw.startsWith("/") || raw.startsWith("//") || raw.includes("\\")) return POST_LOGIN_PATH;
  if (/[\u0000-\u001f\u007f]/.test(raw)) return POST_LOGIN_PATH;
  if (raw.startsWith("/api/")) return POST_LOGIN_PATH; // never bounce into another API route
  return raw;
}

/**
 * LEGACY_ADMIN_LOGIN_ENABLED (ADR 0006 D3): gates only the CIS UI password login. Default ON when
 * unset; only an explicit false / 0 / off / no turns it off. Read per call so a Vercel env change
 * plus redeploy is the whole rollback.
 */
export function legacyAdminLoginEnabled(): boolean {
  const raw = (process.env.LEGACY_ADMIN_LOGIN_ENABLED ?? "").trim().toLowerCase();
  return !["false", "0", "off", "no"].includes(raw);
}
