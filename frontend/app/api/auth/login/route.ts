// frontend/app/api/auth/login/route.ts
// AA-232: admin login goes through JWT (/auth/admin-login) only.
//
// AA-252 (security fix): the legacy ADMIN_SECRET shared-secret fallback is
// retired. It used to trigger on JWT infra failure (network error/timeout/
// 500) and returned { token: password, role: "admin" } with NO cis_admin_token
// cookie — a session shape proxy.ts had to specially allow through
// without JWT verification. That carve-out was exploitable: cis_role is a
// plain client-writable cookie, so anyone could set it by hand and skip auth
// entirely, no secret required. Both real admins (nghiep, admin) already have
// JWT accounts in shared.admin_users — the fallback served no one. On infra
// failure we now return a real error instead of silently granting access.
//
// AA-435 (security fix, same family as AA-427's tenant-login fix): this used
// to also return the raw JWT in the JSON response body ("stored client-side
// too") so login/page.tsx could duplicate it into a plain, non-httpOnly
// `cis_api_token` cookie for a few legacy pages that read it via
// document.cookie and attach it as an Authorization header themselves. That
// duplicate cookie was pure exposure — the httpOnly `cis_admin_token` cookie
// set below already IS the real session (proxy.ts, requireAdmin() in
// lib/auth-server.ts never read cis_api_token at all), so any XSS on an
// admin page could read the token wholesale via document.cookie or the
// fetch() response. Fixed by dropping `token` from this response entirely —
// see docs/implementation-notes/AA-435.md for the call-site audit that
// confirmed no live admin flow actually needs the JSON-body token anymore.
//
// AA-656: the separate content login (username "content" checked against the
// CONTENT_PASSWORD env var, no JWT) is removed. Content staff are rows in
// shared.admin_users with role 'content' (migration 218) and log in here like
// admin/reviewer, so every staff session carries a verifiable JWT.
//
// AA-816 (C4, ADR 0006 D3): this password login is the legacy path. LEGACY_ADMIN_LOGIN_ENABLED=false
// makes it 404 (default ON when unset; the code stays 30 days as the rollback path). The display
// cookies cis_role / cis_user are now set here by the server (they used to be written by
// login/page.tsx with document.cookie), which also overwrites a tenant session's httpOnly cis_role.
import { NextRequest, NextResponse } from "next/server";
import { STAFF_REFRESH_COOKIE, STAFF_REFRESH_COOKIE_PATH, legacyAdminLoginEnabled } from "@/lib/staffSession";
import { isPreviewSignInHost, setDisplayCookies } from "@/lib/cognito";

const API_URL = process.env.API_URL ?? "https://api-cis.lumiguides.it.com";
const LEGACY_SESSION_S = 60 * 60 * 24; // 24h — matches backend JWT_EXPIRY_H

export async function POST(req: NextRequest) {
  // AA-911: no password login on the fixed Preview host (company sign-in only there).
  if (!legacyAdminLoginEnabled() || isPreviewSignInHost(req.headers.get("host") ?? req.nextUrl.host)) {
    return NextResponse.json({ detail: "Not found" }, { status: 404 });
  }
  const { username, password } = await req.json().catch(() => ({}));

  if (!username || !password) {
    return NextResponse.json({ detail: "Missing credentials" }, { status: 400 });
  }

  // ── AA-232: real per-user JWT login (only path — AA-252 retired the
  // shared-secret fallback) ────────────────────────────────────────────────
  try {
    const res = await fetch(`${API_URL}/auth/admin-login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
      signal: AbortSignal.timeout(5000),
    });

    if (res.status === 401) {
      return NextResponse.json({ detail: "Invalid credentials" }, { status: 401 });
    }

    if (!res.ok) {
      // Backend responded but not OK/401 (e.g. 500) — surface as a real
      // error. No more silent fallback into an unauthenticated session.
      return NextResponse.json({ detail: "Login failed — backend error" }, { status: 502 });
    }

    const data = await res.json();
    // AA-435: no `token` field here anymore — see file header. role/name/
    // admin_id are not secrets (used only for UI display + redirect), the
    // JWT itself now lives ONLY in the httpOnly cookie below.
    const response = NextResponse.json({
      role: data.role,   // 'admin' | 'reviewer' | 'content'
      name: data.username,
      admin_id: data.admin_id,
    });
    // Server-readable cookie for middleware verification (see proxy.ts).
    // httpOnly so client JS can't read/tamper with it — the sole session
    // credential now, nothing duplicates it into the JSON body or another
    // cookie.
    response.cookies.set("cis_admin_token", data.token, {
      httpOnly: true,
      secure: process.env.NODE_ENV === "production",
      sameSite: "lax",
      path: "/",
      maxAge: LEGACY_SESSION_S,
    });
    setDisplayCookies(response, { role: data.role, name: data.username ?? "" }, LEGACY_SESSION_S);
    // AA-816: a password login replaces any earlier Cognito session on this browser. Drop its refresh
    // cookie, or the keep-alive would later refresh that old session over this legacy token (and logout
    // would take the Cognito path).
    response.cookies.set(STAFF_REFRESH_COOKIE, "", { path: STAFF_REFRESH_COOKIE_PATH, maxAge: 0 });
    return response;
  } catch {
    // Network error / timeout / route unreachable — a real infra failure,
    // not a reason to grant access. Surface it instead of falling back.
    return NextResponse.json({ detail: "Backend connection error" }, { status: 502 });
  }
}
