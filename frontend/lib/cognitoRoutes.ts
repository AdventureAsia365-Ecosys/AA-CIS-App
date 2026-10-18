// frontend/lib/cognitoRoutes.ts
// AA-816 (C4) — the handlers behind the Cognito auth routes. The route files under app/api/auth/
// are one-liners that pick a client; the paths of the two callbacks are fixed by Terraform
// (identity_variables.tf: /api/auth/callback/cognito and /api/auth/callback/cognito-breakglass).
import "server-only";
import { NextRequest, NextResponse } from "next/server";
import {
  CognitoAuthError,
  allowedOriginForRequest,
  clearOAuthTx,
  clearStaffSessionCookies,
  completeSignIn,
  readRefreshCookie,
  refreshStaffTokens,
  setStaffSessionCookies,
  staffClient,
  startSignIn,
  type StaffClientKind,
} from "./cognito";
import { adminVerifyCache, verifyStaffToken } from "./auth-server";
import { isStaffRole } from "./rbac";
import {
  POST_LOGIN_PATH,
  REFRESH_SKEW_S,
  ROLE_COOKIE,
  STAFF_TOKEN_COOKIE,
  USER_COOKIE,
  isCognitoToken,
  safeNextPath,
  tokenExp,
  tokenExpiresSoon,
} from "./staffSession";

function toLogin(request: NextRequest, error?: string): NextResponse {
  const url = new URL("/login", request.url);
  if (error) url.searchParams.set("error", error);
  return NextResponse.redirect(url, { status: 303 });
}

function noStore(response: NextResponse): NextResponse {
  response.headers.set("Cache-Control", "no-store");
  return response;
}

/** GET /api/auth/signin/<client> — starts the authorization code + PKCE flow. */
export async function handleSignIn(request: NextRequest, kind: StaffClientKind): Promise<NextResponse> {
  const origin = allowedOriginForRequest(request, kind);
  // A host that is not on the allowlist (e.g. a Vercel preview) has no registered callback URL.
  if (!origin) return toLogin(request, "cognito_host");
  if (!staffClient(kind).secret()) return toLogin(request, "cognito_config");
  const { authorizeUrl, setTxCookie } = await startSignIn(kind, origin);
  const response = NextResponse.redirect(authorizeUrl, { status: 303 });
  setTxCookie(response);
  return noStore(response);
}

/** GET /api/auth/callback/<client> — exchanges the code, verifies the staff user, sets the session. */
export async function handleCallback(request: NextRequest, kind: StaffClientKind): Promise<NextResponse> {
  const origin = allowedOriginForRequest(request, kind);
  if (!origin) return toLogin(request, "cognito_host");

  let tokens;
  try {
    tokens = await completeSignIn(request, kind, origin);
  } catch (e) {
    const code = e instanceof CognitoAuthError ? e.code : "token_error";
    console.warn(`[auth] cognito callback refused: client=${kind} reason=${code}`);
    const response = toLogin(request, code === "denied" ? "denied" : "signin_failed");
    clearOAuthTx(response);
    return noStore(response);
  }

  // The backend decides who this is (admin_users row via iss+sub, first-link by email) and the role
  // (highest cis-* group). A user without a cis-* group, or with an inactive row, is refused here.
  const verified = await verifyStaffToken(tokens.accessToken);
  if (!verified.ok || !isStaffRole(verified.role)) {
    console.warn(`[auth] cognito callback: verify-admin refused client=${kind} status=${verified.ok ? "role" : verified.status}`);
    // A backend that did not answer (timeout, 5xx) is not a refusal: say so instead of "no access".
    const response = toLogin(request, !verified.ok && isTransientStatus(verified.status) ? "backend" : "no_access");
    clearOAuthTx(response);
    clearStaffSessionCookies(response);
    return noStore(response);
  }

  const response = NextResponse.redirect(new URL(POST_LOGIN_PATH, origin), { status: 303 });
  clearOAuthTx(response);
  setStaffSessionCookies(response, kind, tokens, { role: verified.role, name: verified.name });
  return noStore(response);
}

/**
 * A verify-admin answer that says nothing about the user: no answer at all (timeout / network, status 0)
 * or a backend error (5xx, e.g. the C3 JWKS or directory answering 503, or ECS mid-deploy). Only a real
 * refusal (401/403, or any other 4xx) ends a session.
 */
function isTransientStatus(status: number): boolean {
  return status === 0 || status >= 500;
}

type ApplyCookies = (response: NextResponse) => void;

type RefreshOutcome =
  // apply() writes the rotated session cookies onto whatever response the caller builds (a no-op when
  // the current access token was still fresh and no refresh happened).
  | { ok: true; expiresAt: number | null; apply: ApplyCookies }
  // transient: Cognito or the backend did not answer. The session is NOT over and its cookies must not be
  // cleared. When Cognito already rotated the refresh token, `apply` writes the new tokens anyway: they
  // come straight from Cognito, the old refresh token is about to stop working, and every later use of
  // the access token is verified again (middleware, BFF). `rotated` says whether that happened.
  | { ok: false; transient: true; reason: string; rotated: boolean; apply: ApplyCookies }
  // terminal: invalid_grant (revoked / expired refresh token), a refusal from verify-admin (401/403),
  // no CIS role, or a token that fails our own checks. The caller clears the session.
  | { ok: false; transient: false; reason: string };

/**
 * Shared refresh core. Skips the Cognito call when the current access token still has more than
 * `skipIfFreshS` seconds left (the keep-alive pings often; middleware only comes here when needed).
 */
async function refreshSession(request: NextRequest, skipIfFreshS: number): Promise<RefreshOutcome> {
  const current = request.cookies.get(STAFF_TOKEN_COOKIE)?.value;
  const rt = readRefreshCookie(request);
  if (!rt) return { ok: false, transient: false, reason: "no_refresh_token" };
  if (current && !tokenExpiresSoon(current, skipIfFreshS)) {
    return { ok: true, expiresAt: tokenExp(current), apply: () => {} };
  }

  let tokens;
  try {
    tokens = await refreshStaffTokens(rt.kind, rt.refreshToken);
  } catch (e) {
    const code = e instanceof CognitoAuthError ? e.code : "token_error";
    // Cognito not reachable (network / timeout / 5xx): the old refresh token is still valid, keep it.
    if (code === "unreachable") {
      return { ok: false, transient: true, reason: code, rotated: false, apply: () => {} };
    }
    return { ok: false, transient: false, reason: code };
  }

  const verified = await verifyStaffToken(tokens.accessToken);
  if (current) adminVerifyCache.delete(current);
  if (verified.ok && isStaffRole(verified.role)) {
    return {
      ok: true,
      expiresAt: tokenExp(tokens.accessToken),
      apply: (response) =>
        setStaffSessionCookies(
          response, rt.kind, tokens, { role: verified.role, name: verified.name }, rt.sessionEnd, rt.refreshToken,
        ),
    };
  }
  if (!verified.ok && isTransientStatus(verified.status)) {
    // The display cookies (cis_role / cis_user) are left as they are: only the backend can say who
    // this is, and it did not answer. setStaffSessionCookies needs a display value, so the current
    // cookies are written back unchanged.
    const display = {
      role: request.cookies.get(ROLE_COOKIE)?.value ?? "",
      name: request.cookies.get(USER_COOKIE)?.value ?? "",
    };
    return {
      ok: false,
      transient: true,
      reason: `verify_${verified.status}`,
      rotated: true,
      apply: (response) =>
        setStaffSessionCookies(response, rt.kind, tokens, display, rt.sessionEnd, rt.refreshToken),
    };
  }
  return { ok: false, transient: false, reason: verified.ok ? "role" : `verify_${verified.status}` };
}

/** GET /api/auth/refresh?next=/admin/... — used by proxy.ts on a page navigation. */
export async function handleRefreshNavigation(request: NextRequest): Promise<NextResponse> {
  const next = safeNextPath(request.nextUrl.searchParams.get("next"));
  const outcome = await refreshSession(request, REFRESH_SKEW_S);
  if (outcome.ok) {
    const response = NextResponse.redirect(new URL(next, request.url), { status: 303 });
    outcome.apply(response);
    return noStore(response);
  }
  console.warn(`[auth] refresh refused (navigation): reason=${outcome.reason} transient=${outcome.transient}`);
  if (outcome.transient) {
    // Keep the session. With rotated tokens, go back to `next`: middleware verifies the fresh token
    // again and, if the backend is still down, fails closed to /login?error=backend without clearing
    // anything. Without a rotation the cookie still holds the expired token, so going back to `next`
    // would only bounce here again: show the backend error instead.
    const response = outcome.rotated
      ? NextResponse.redirect(new URL(next, request.url), { status: 303 })
      : toLogin(request, "backend");
    outcome.apply(response);
    return noStore(response);
  }
  const response = toLogin(request, outcome.reason === "no_refresh_token" ? undefined : "session_expired");
  clearStaffSessionCookies(response);
  return noStore(response);
}

/**
 * POST /api/auth/refresh — the staff shells' keep-alive (SessionKeepAlive). Answers:
 *   - { mode: "cognito", expiresAt } after refreshing when the access token has under 3 min left;
 *   - { mode: "legacy" } for a legacy (HS256) session: nothing to keep alive;
 *   - { mode: "none" } when there is no staff session cookie at all (a tenant on /portal, or a Cognito
 *     session whose 12 h / 1 h window has ended: both cookies expire at the session end). The client
 *     tells the two apart by whether it saw a Cognito session before;
 *   - 503 { mode: "cognito", error: "unavailable" } when Cognito or the backend did not answer: the
 *     session is kept (rotated tokens are still written) and the client retries in a minute;
 *   - 401 when the session is over.
 */
export async function handleRefreshKeepAlive(request: NextRequest): Promise<NextResponse> {
  const current = request.cookies.get(STAFF_TOKEN_COOKIE)?.value;
  if (!isCognitoToken(current) && !readRefreshCookie(request)) {
    return noStore(NextResponse.json({ mode: current ? "legacy" : "none" }));
  }
  const outcome = await refreshSession(request, 180);
  if (outcome.ok) {
    const response = NextResponse.json({ mode: "cognito", expiresAt: outcome.expiresAt });
    outcome.apply(response);
    return noStore(response);
  }
  console.warn(`[auth] refresh refused (keep-alive): reason=${outcome.reason} transient=${outcome.transient}`);
  if (outcome.transient) {
    const response = NextResponse.json({ mode: "cognito", error: "unavailable" }, { status: 503 });
    outcome.apply(response);
    return noStore(response);
  }
  const response = NextResponse.json({ mode: "cognito", error: "session_expired" }, { status: 401 });
  clearStaffSessionCookies(response);
  return noStore(response);
}
