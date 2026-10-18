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
  STAFF_TOKEN_COOKIE,
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
    const response = toLogin(request, !verified.ok && verified.status === 0 ? "backend" : "no_access");
    clearOAuthTx(response);
    clearStaffSessionCookies(response);
    return noStore(response);
  }

  const response = NextResponse.redirect(new URL(POST_LOGIN_PATH, origin), { status: 303 });
  clearOAuthTx(response);
  setStaffSessionCookies(response, kind, tokens, { role: verified.role, name: verified.name });
  return noStore(response);
}

type RefreshOutcome =
  // apply() writes the rotated session cookies onto whatever response the caller builds (a no-op when
  // the current access token was still fresh and no refresh happened).
  | { ok: true; expiresAt: number | null; apply: (response: NextResponse) => void }
  | { ok: false; reason: string };

/**
 * Shared refresh core. Skips the Cognito call when the current access token still has more than
 * `skipIfFreshS` seconds left (the keep-alive pings often; middleware only comes here when needed).
 */
async function refreshSession(request: NextRequest, skipIfFreshS: number): Promise<RefreshOutcome> {
  const current = request.cookies.get(STAFF_TOKEN_COOKIE)?.value;
  const rt = readRefreshCookie(request);
  if (!rt) return { ok: false, reason: "no_refresh_token" };
  if (current && !tokenExpiresSoon(current, skipIfFreshS)) {
    return { ok: true, expiresAt: tokenExp(current), apply: () => {} };
  }

  let tokens;
  try {
    tokens = await refreshStaffTokens(rt.kind, rt.refreshToken);
  } catch (e) {
    return { ok: false, reason: e instanceof CognitoAuthError ? e.code : "token_error" };
  }
  const verified = await verifyStaffToken(tokens.accessToken);
  if (!verified.ok || !isStaffRole(verified.role)) {
    return { ok: false, reason: verified.ok ? "role" : `verify_${verified.status}` };
  }
  if (current) adminVerifyCache.delete(current);
  return {
    ok: true,
    expiresAt: tokenExp(tokens.accessToken),
    apply: (response) =>
      setStaffSessionCookies(
        response, rt.kind, tokens, { role: verified.role, name: verified.name }, rt.sessionEnd, rt.refreshToken,
      ),
  };
}

/** GET /api/auth/refresh?next=/admin/... — used by middleware.ts on a page navigation. */
export async function handleRefreshNavigation(request: NextRequest): Promise<NextResponse> {
  const next = safeNextPath(request.nextUrl.searchParams.get("next"));
  const outcome = await refreshSession(request, REFRESH_SKEW_S);
  if (outcome.ok) {
    const response = NextResponse.redirect(new URL(next, request.url), { status: 303 });
    outcome.apply(response);
    return noStore(response);
  }
  console.warn(`[auth] refresh refused (navigation): reason=${outcome.reason}`);
  const response = toLogin(request, outcome.reason === "no_refresh_token" ? undefined : "session_expired");
  clearStaffSessionCookies(response);
  return noStore(response);
}

/**
 * POST /api/auth/refresh — the admin shell's keep-alive (SessionKeepAlive). Answers
 * { mode: "cognito", expiresAt } after refreshing when the access token has under 3 min left,
 * { mode: "legacy" } when there is no Cognito session to refresh, or 401 when the session is over.
 */
export async function handleRefreshKeepAlive(request: NextRequest): Promise<NextResponse> {
  if (!isCognitoToken(request.cookies.get(STAFF_TOKEN_COOKIE)?.value) && !readRefreshCookie(request)) {
    return noStore(NextResponse.json({ mode: "legacy" }));
  }
  const outcome = await refreshSession(request, 180);
  if (!outcome.ok) {
    console.warn(`[auth] refresh refused (keep-alive): reason=${outcome.reason}`);
    const response = NextResponse.json({ mode: "cognito", error: "session_expired" }, { status: 401 });
    clearStaffSessionCookies(response);
    return noStore(response);
  }
  const response = NextResponse.json({ mode: "cognito", expiresAt: outcome.expiresAt });
  outcome.apply(response);
  return noStore(response);
}
