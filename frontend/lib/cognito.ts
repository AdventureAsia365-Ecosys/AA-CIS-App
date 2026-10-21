// frontend/lib/cognito.ts
// AA-816 (C4) — staff sign-in through the Cognito WORKFORCE pool (ADR 0006 Phase 1):
// authorization code + PKCE (S256) against the confidential `cis-admin` client, plus the separate
// `breakglass` client. Server-only: it reads the client secrets. Route handlers import it; the edge
// middleware never does (it only redirects to /api/auth/refresh, see proxy.ts).
//
// Security rules this file enforces:
//   - The redirect URI is always taken from a FIXED allowlist, never built from the Host header. cis-admin:
//     a Vercel production build accepts the production host, a Vercel preview build only the fixed Preview
//     host aa-cis-preview.lumiguides.it.com (AA-911, root ADR 0006a A11; CI aliases it to the preview under
//     test on opt-in), `next dev` also localhost. Break-glass: production only. The request host is only
//     COMPARED with the allowlist; any other host (a per-deployment *.vercel.app preview URL) gets no
//     Cognito sign-in at all, because those hosts are not registered callback URLs (identity_variables.tf).
//   - The fixed Preview host never offers the legacy password form or POST /api/auth/login (AA-911).
//   - AA-911 decision D-1 (root ADR 0006a A11): the "cis-admin" sign-in of a Vercel PREVIEW build uses the
//     Preview-only `cis-admin-preview` app client (its only callback is the Preview host) and its own secret env
//     var; it never reads the production client secret. The CIS API caps every token of that client at the
//     viewer role (api/core/staff_identity.py), so preview code from an unmerged PR never gets a real role.
//   - state, nonce and the PKCE verifier live in one short-lived httpOnly cookie scoped to the
//     callback path; the callback checks state against it and nonce against the ID token.
//   - Token exchange, refresh and revoke authenticate with HTTP Basic (client id + secret).
//   - Refresh is single-flight per refresh token on a warm instance, and a result is reused for a
//     few seconds, so ~8 parallel calls never rotate the same refresh token 8 times (risk 8).
import "server-only";
import type { NextRequest, NextResponse } from "next/server";
import {
  ROLE_COOKIE,
  STAFF_REFRESH_COOKIE,
  STAFF_REFRESH_COOKIE_PATH,
  STAFF_TOKEN_COOKIE,
  USER_COOKIE,
  decodeJwtPayload,
} from "./staffSession";

// ── Configuration (Dev pool; code constants with an optional env override, like the backend) ──
// The values are public identifiers, not secrets. Only the two client SECRETS must be set in Vercel.
// AA-816 (S242): managed login is served on our own custom domain, not the Cognito prefix domain
// (aa365-staff-auth-dev.auth.us-west-1.amazoncognito.com). Chrome Safe Browsing flagged the redirect
// from our site to an unbranded amazoncognito.com page as deceptive. The prefix domain still answers
// (a pool keeps both), so COGNITO_WORKFORCE_DOMAIN can point back to it if the custom domain breaks.
// Optional: unset or empty means the default below. Trailing slashes are dropped.
const DEFAULT_WORKFORCE_DOMAIN = "https://staff-auth.lumiguides.it.com";
const COGNITO_DOMAIN =
  (process.env.COGNITO_WORKFORCE_DOMAIN ?? "").trim().replace(/\/+$/, "") || DEFAULT_WORKFORCE_DOMAIN;

/** Host of the managed-login page (shown on /login so staff know where the sign-in opens). */
export function workforceSignInHost(): string {
  try {
    return new URL(COGNITO_DOMAIN).host;
  } catch {
    return new URL(DEFAULT_WORKFORCE_DOMAIN).host;
  }
}
export const WORKFORCE_ISSUER =
  process.env.COGNITO_WORKFORCE_ISSUER ?? "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_bCcFvbSOS";

const PROD_ORIGIN = "https://aa-cis.lumiguides.it.com";
// AA-911: the one fixed Preview host of aa-cis-frontend (cis-admin callback; aliased to the preview under test).
export const PREVIEW_ORIGIN = "https://aa-cis-preview.lumiguides.it.com";
const DEV_ORIGIN = "http://localhost:3000";

/** A Vercel preview build (VERCEL_ENV is fixed per deployment): its cis-admin sign-in is the preview client. */
const PREVIEW_BUILD = (process.env.VERCEL_ENV ?? "").trim() === "preview";

/** cis-admin origins for THIS build: a preview build never runs the production callback and vice versa. */
function cisAdminOrigins(): string[] {
  const vercelEnv = (process.env.VERCEL_ENV ?? "").trim();
  if (vercelEnv === "production") return [PROD_ORIGIN];
  if (vercelEnv === "preview") return [PREVIEW_ORIGIN];
  // Outside Vercel the production client is used, and its callbacks never include the Preview host.
  return process.env.NODE_ENV === "development" ? [PROD_ORIGIN, DEV_ORIGIN] : [PROD_ORIGIN];
}

// AA-911 D-1: the `cis-admin-preview` client (AA-CIS-Infra identity.tf). Empty until the Infra apply creates it,
// then filled in here; while empty a preview build offers no Cognito sign-in (never the production client).
const DEFAULT_CIS_ADMIN_PREVIEW_CLIENT_ID = "3d6u7r00ljf4m80gjil20fp2kg";
const CIS_ADMIN_CLIENT_ID = PREVIEW_BUILD
  ? (process.env.COGNITO_CIS_ADMIN_PREVIEW_CLIENT_ID ?? "").trim() || DEFAULT_CIS_ADMIN_PREVIEW_CLIENT_ID
  : process.env.COGNITO_CIS_ADMIN_CLIENT_ID ?? "3oqkumijh04cp81k3nlnub6qtv";
const cisAdminSecret = (): string => {
  if (CIS_ADMIN_CLIENT_ID === "") return "";
  return (
    (PREVIEW_BUILD
      ? process.env.COGNITO_CIS_ADMIN_PREVIEW_CLIENT_SECRET
      : process.env.COGNITO_CIS_ADMIN_CLIENT_SECRET) ?? ""
  );
};

/** True when the request is served on the fixed Preview sign-in host (no legacy password login there). */
export function isPreviewSignInHost(host: string | null | undefined): boolean {
  return !!host && host.trim().toLowerCase() === new URL(PREVIEW_ORIGIN).host;
}

export type StaffClientKind = "cis-admin" | "breakglass";

interface StaffClient {
  kind: StaffClientKind;
  /** Short tag stored in the refresh cookie so refresh/logout know which client issued it. */
  tag: string;
  clientId: string;
  secret: () => string;
  callbackPath: string;
  /** Refresh-token lifetime set on the Cognito client (identity.tf): the session cap. */
  refreshTtlS: number;
  /** Origins whose callback/logout URLs are registered on the client. */
  origins: () => string[];
}

const CLIENTS: Record<StaffClientKind, StaffClient> = {
  "cis-admin": {
    kind: "cis-admin",
    tag: "ca",
    clientId: CIS_ADMIN_CLIENT_ID,
    secret: cisAdminSecret,
    callbackPath: "/api/auth/callback/cognito",
    refreshTtlS: 12 * 60 * 60,
    origins: cisAdminOrigins,
  },
  breakglass: {
    kind: "breakglass",
    tag: "bg",
    clientId: process.env.COGNITO_BREAKGLASS_CLIENT_ID ?? "2g3f3ncfuqdiuu1o5b70o4h0lq",
    secret: () => process.env.COGNITO_BREAKGLASS_CLIENT_SECRET ?? "",
    callbackPath: "/api/auth/callback/cognito-breakglass",
    refreshTtlS: 60 * 60,
    origins: () => [PROD_ORIGIN], // break-glass callback is registered on production only
  },
};

export function staffClient(kind: StaffClientKind): StaffClient {
  return CLIENTS[kind];
}

function clientByTag(tag: string): StaffClient | null {
  return Object.values(CLIENTS).find((c) => c.tag === tag) ?? null;
}

function clientById(clientId: unknown): StaffClient | null {
  if (typeof clientId !== "string" || clientId === "") return null; // an unset preview client id matches nothing
  return Object.values(CLIENTS).find((c) => c.clientId === clientId) ?? null;
}

/**
 * The allowlisted origin that matches this request's host, or null. The returned value is always a
 * constant from the allowlist; the host header only selects among them.
 */
export function allowedOriginForHost(host: string | null | undefined, kind: StaffClientKind): string | null {
  if (!host) return null;
  const h = host.trim().toLowerCase();
  return CLIENTS[kind].origins().find((o) => new URL(o).host === h) ?? null;
}

export function allowedOriginForRequest(request: NextRequest, kind: StaffClientKind): string | null {
  return allowedOriginForHost(request.headers.get("host") ?? request.nextUrl.host, kind);
}

/** Cognito sign-in is offered only when the host is allowlisted and the client secret is set. */
export function cognitoSignInAvailable(host: string | null | undefined, kind: StaffClientKind): boolean {
  return allowedOriginForHost(host, kind) !== null && CLIENTS[kind].secret() !== "";
}

// ── Small crypto helpers (Web Crypto: works in Node and edge runtimes) ─────────────────────────
function base64url(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomToken(byteLength = 32): string {
  const bytes = new Uint8Array(byteLength);
  crypto.getRandomValues(bytes);
  return base64url(bytes);
}

async function pkceChallenge(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64url(new Uint8Array(digest));
}

/** Length-safe string compare that does not short-circuit on the first differing character. */
function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function basicAuth(client: StaffClient): string {
  // RFC 6749 §2.3.1: form-urlencode id and secret before base64 (a no-op for Cognito's alphabet).
  return `Basic ${btoa(`${encodeURIComponent(client.clientId)}:${encodeURIComponent(client.secret())}`)}`;
}

// ── Authorization request ──────────────────────────────────────────────────────────────────────
export const OAUTH_TX_COOKIE = "cis_oauth_tx";
const OAUTH_TX_PATH = "/api/auth/callback";
// Cognito's auth session is 3 min (auth_session_validity); allow a little more for the round trip.
const OAUTH_TX_TTL_S = 5 * 60;

interface OAuthTx {
  k: StaffClientKind; // which client started the flow
  s: string;          // state
  n: string;          // nonce
  v: string;          // PKCE code verifier
}

function secureCookies(): boolean {
  return process.env.NODE_ENV === "production";
}

/**
 * Builds the Cognito /oauth2/authorize URL and a setter that stores state / nonce / PKCE verifier in
 * the transaction cookie of the redirect response.
 */
export async function startSignIn(
  kind: StaffClientKind,
  origin: string,
): Promise<{ authorizeUrl: string; setTxCookie: (response: NextResponse) => void }> {
  const client = CLIENTS[kind];
  const tx: OAuthTx = { k: kind, s: randomToken(24), n: randomToken(24), v: randomToken(48) };
  const params = new URLSearchParams({
    response_type: "code",
    client_id: client.clientId,
    redirect_uri: origin + client.callbackPath,
    scope: "openid email profile",
    state: tx.s,
    nonce: tx.n,
    code_challenge: await pkceChallenge(tx.v),
    code_challenge_method: "S256",
  });
  const value = base64url(new TextEncoder().encode(JSON.stringify(tx)));
  return {
    authorizeUrl: `${COGNITO_DOMAIN}/oauth2/authorize?${params.toString()}`,
    setTxCookie: (response) =>
      response.cookies.set(OAUTH_TX_COOKIE, value, {
        httpOnly: true,
        secure: secureCookies(),
        sameSite: "lax", // the return from Cognito is a top-level GET, which carries lax cookies
        path: OAUTH_TX_PATH,
        maxAge: OAUTH_TX_TTL_S,
      }),
  };
}

export function clearOAuthTx(response: NextResponse): void {
  response.cookies.set(OAUTH_TX_COOKIE, "", { path: OAUTH_TX_PATH, maxAge: 0 });
}

function readOAuthTx(request: NextRequest): OAuthTx | null {
  const raw = request.cookies.get(OAUTH_TX_COOKIE)?.value;
  if (!raw) return null;
  try {
    const json = atob(raw.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(raw.length / 4) * 4, "="));
    const tx = JSON.parse(new TextDecoder().decode(Uint8Array.from(json, (c) => c.charCodeAt(0))));
    if (
      tx && (tx.k === "cis-admin" || tx.k === "breakglass")
      && typeof tx.s === "string" && typeof tx.n === "string" && typeof tx.v === "string"
    ) {
      return tx as OAuthTx;
    }
  } catch {
    /* fall through */
  }
  return null;
}

// ── Token endpoint ─────────────────────────────────────────────────────────────────────────────
export interface StaffTokens {
  accessToken: string;
  /** Present on the code exchange; on refresh only when rotation returns a new one. */
  refreshToken?: string;
  idToken?: string;
}

export class CognitoAuthError extends Error {
  constructor(public readonly code: string, message?: string) {
    super(message ?? code);
  }
}

async function tokenRequest(client: StaffClient, body: URLSearchParams): Promise<StaffTokens> {
  if (!client.secret()) throw new CognitoAuthError("not_configured");
  let res: Response;
  try {
    res = await fetch(`${COGNITO_DOMAIN}/oauth2/token`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded", Authorization: basicAuth(client) },
      body: body.toString(),
      cache: "no-store",
      signal: AbortSignal.timeout(8000),
    });
  } catch {
    throw new CognitoAuthError("unreachable");
  }
  // A Cognito-side error says nothing about the grant: treat it like no answer (callers keep the session).
  if (res.status >= 500) throw new CognitoAuthError("unreachable");
  if (!res.ok) {
    // Cognito answers {"error":"invalid_grant"} for a used/expired code or a revoked refresh token.
    const err = (await res.json().catch(() => ({}))) as { error?: string };
    throw new CognitoAuthError(err.error === "invalid_grant" ? "invalid_grant" : "token_error");
  }
  const data = (await res.json()) as { access_token?: string; refresh_token?: string; id_token?: string };
  if (!data.access_token) throw new CognitoAuthError("token_error");
  return { accessToken: data.access_token, refreshToken: data.refresh_token, idToken: data.id_token };
}

/**
 * Callback half of the flow: checks the transaction cookie (state, client), exchanges the code with
 * the PKCE verifier, and checks the ID token's nonce / aud / iss. The ID token came straight from the
 * token endpoint over TLS with client authentication, so its claims are trusted without a signature
 * check (OIDC Core §3.1.3.7); the access token itself is verified by the backend (/auth/verify-admin).
 */
export async function completeSignIn(
  request: NextRequest,
  kind: StaffClientKind,
  origin: string,
): Promise<StaffTokens> {
  const params = request.nextUrl.searchParams;
  if (params.get("error")) throw new CognitoAuthError("denied");
  const code = params.get("code");
  const state = params.get("state");
  const tx = readOAuthTx(request);
  if (!code || !state || !tx) throw new CognitoAuthError("state");
  if (tx.k !== kind || !timingSafeEqual(tx.s, state)) throw new CognitoAuthError("state");

  const client = CLIENTS[kind];
  const tokens = await tokenRequest(
    client,
    new URLSearchParams({
      grant_type: "authorization_code",
      client_id: client.clientId,
      code,
      redirect_uri: origin + client.callbackPath,
      code_verifier: tx.v,
    }),
  );

  const id = tokens.idToken ? decodeJwtPayload(tokens.idToken) : null;
  if (!id || typeof id.nonce !== "string" || !timingSafeEqual(id.nonce, tx.n)) throw new CognitoAuthError("nonce");
  if (id.aud !== client.clientId || id.iss !== WORKFORCE_ISSUER) throw new CognitoAuthError("token_error");
  const access = decodeJwtPayload(tokens.accessToken);
  if (!access || access.token_use !== "access" || access.client_id !== client.clientId) {
    throw new CognitoAuthError("token_error");
  }
  if (!tokens.refreshToken) throw new CognitoAuthError("token_error");
  return tokens;
}

// ── Refresh (single-flight) ────────────────────────────────────────────────────────────────────
const inflight = new Map<string, Promise<StaffTokens>>();
const recent = new Map<string, { tokens: StaffTokens; until: number }>();
// Reuse one refresh result for this long: covers requests that arrive just after the first one
// resolved (they still carry the old refresh token). Well inside Cognito's 60 s rotation grace.
const RECENT_TTL_MS = 20_000;

export function refreshStaffTokens(kind: StaffClientKind, refreshToken: string): Promise<StaffTokens> {
  const now = Date.now();
  for (const [k, v] of recent) if (v.until <= now) recent.delete(k);
  const hit = recent.get(refreshToken);
  if (hit) return Promise.resolve(hit.tokens);
  const running = inflight.get(refreshToken);
  if (running) return running;

  const client = CLIENTS[kind];
  const p = tokenRequest(
    client,
    new URLSearchParams({ grant_type: "refresh_token", client_id: client.clientId, refresh_token: refreshToken }),
  )
    .then((tokens) => {
      const access = decodeJwtPayload(tokens.accessToken);
      if (!access || access.client_id !== client.clientId) throw new CognitoAuthError("token_error");
      recent.set(refreshToken, { tokens, until: Date.now() + RECENT_TTL_MS });
      return tokens;
    })
    .finally(() => inflight.delete(refreshToken));
  inflight.set(refreshToken, p);
  return p;
}

// ── Revoke + logout ────────────────────────────────────────────────────────────────────────────
/** Revokes a refresh token (and the access tokens issued from it). Best effort: never throws. */
export async function revokeRefreshToken(kind: StaffClientKind, refreshToken: string): Promise<boolean> {
  const client = CLIENTS[kind];
  if (!client.secret()) return false;
  try {
    const res = await fetch(`${COGNITO_DOMAIN}/oauth2/revoke`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded", Authorization: basicAuth(client) },
      body: new URLSearchParams({ token: refreshToken, client_id: client.clientId }).toString(),
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
    });
    return res.ok;
  } catch {
    return false;
  }
}

/** Cognito /logout URL that clears the managed-login session cookie and returns to /login. */
export function cognitoLogoutUrl(kind: StaffClientKind, origin: string): string {
  const params = new URLSearchParams({ client_id: CLIENTS[kind].clientId, logout_uri: `${origin}/login` });
  return `${COGNITO_DOMAIN}/logout?${params.toString()}`;
}

// ── Session cookies ────────────────────────────────────────────────────────────────────────────
// cis_staff_rt value: "<tag>~<sessionEndEpochS>~<refreshToken>". The session end is fixed at first
// sign-in (rotation does not extend the refresh window, ADR 0006 §7), so a rotated cookie keeps the
// original end instead of restarting the 12 h / 1 h clock.
export interface StaffRefreshCookie {
  kind: StaffClientKind;
  sessionEnd: number;
  refreshToken: string;
}

export function readRefreshCookie(request: NextRequest): StaffRefreshCookie | null {
  const raw = request.cookies.get(STAFF_REFRESH_COOKIE)?.value;
  if (!raw) return null;
  const first = raw.indexOf("~");
  const second = raw.indexOf("~", first + 1);
  if (first < 0 || second < 0) return null;
  const client = clientByTag(raw.slice(0, first));
  const sessionEnd = Number(raw.slice(first + 1, second));
  const refreshToken = raw.slice(second + 1);
  if (!client || !Number.isFinite(sessionEnd) || !refreshToken) return null;
  if (sessionEnd <= Date.now() / 1000) return null;
  return { kind: client.kind, sessionEnd, refreshToken };
}

/** Which client a session belongs to: from the refresh cookie, else from the access token's client_id. */
export function sessionClientKind(request: NextRequest): StaffClientKind | null {
  const rt = readRefreshCookie(request);
  if (rt) return rt.kind;
  const at = request.cookies.get(STAFF_TOKEN_COOKIE)?.value;
  return at ? (clientById(decodeJwtPayload(at)?.client_id)?.kind ?? null) : null;
}

export interface StaffDisplay {
  role: string;
  name: string;
}

/**
 * Writes the Cognito session. cis_admin_token holds the 15-min access token, but the COOKIE lives as
 * long as the refresh window: proxy.ts must still see an expired access token to know it should
 * refresh (the refresh cookie is path-scoped to /api/auth and never reaches page requests). An expired
 * token in the cookie is useless to anyone: /auth/verify-admin rejects it.
 */
export function setStaffSessionCookies(
  response: NextResponse,
  kind: StaffClientKind,
  tokens: StaffTokens,
  display: StaffDisplay,
  sessionEnd?: number,
  previousRefreshToken?: string,
): void {
  const nowS = Math.floor(Date.now() / 1000);
  const end = sessionEnd ?? nowS + CLIENTS[kind].refreshTtlS;
  const maxAge = Math.max(0, end - nowS);
  const refreshToken = tokens.refreshToken ?? previousRefreshToken;
  const secure = secureCookies();
  response.cookies.set(STAFF_TOKEN_COOKIE, tokens.accessToken, {
    httpOnly: true, secure, sameSite: "lax", path: "/", maxAge,
  });
  if (refreshToken) {
    response.cookies.set(STAFF_REFRESH_COOKIE, `${CLIENTS[kind].tag}~${end}~${refreshToken}`, {
      httpOnly: true, secure, sameSite: "lax", path: STAFF_REFRESH_COOKIE_PATH, maxAge,
    });
  }
  setDisplayCookies(response, display, maxAge);
}

/**
 * cis_role / cis_user: display hints the admin UI reads with document.cookie (useAdminIdentity), so
 * NOT httpOnly. Set by the server from the verified /auth/verify-admin answer; this also overwrites the
 * httpOnly `cis_role=tenant` a tenant-portal login leaves on the same host and path.
 */
export function setDisplayCookies(response: NextResponse, display: StaffDisplay, maxAge: number): void {
  const opts = { httpOnly: false, secure: secureCookies(), sameSite: "lax" as const, path: "/", maxAge };
  response.cookies.set(ROLE_COOKIE, display.role, opts);
  // NextResponse.cookies.set() URI-encodes the value itself (see tenant-login/route.ts).
  response.cookies.set(USER_COOKIE, display.name, opts);
}

export function clearStaffSessionCookies(response: NextResponse): void {
  for (const name of [STAFF_TOKEN_COOKIE, ROLE_COOKIE, USER_COOKIE, "cis_api_token"]) {
    response.cookies.set(name, "", { path: "/", maxAge: 0 });
  }
  response.cookies.set(STAFF_REFRESH_COOKIE, "", { path: STAFF_REFRESH_COOKIE_PATH, maxAge: 0 });
}
