// tests/e2e/smoke/aa816-admin-logout.spec.ts
// AA-816 (cutover prep, S244) — staff sign-out, with hand-made cookies only (no admin storage state, no real
// credential). Safe on a Vercel preview and on the production host.
//   • unit: logoutNavigationTarget follows only "/login" or an https Cognito /logout URL (client_id + logout_uri);
//   • unit: isSameOrigin (same rule as AA-Booking lib/sameOrigin.ts);
//   • POST /api/auth/admin-logout from another origin → 403, nothing cleared;
//   • same-origin POST without a session → 200 { redirect: "/login" }, every session cookie cleared;
//   • same-origin POST with a Cognito-shaped refresh cookie → on the production host the answer is the managed-login
//     /logout URL for the cis-admin client returning to <prod>/login (a registered logout URL); on a preview host
//     (no registered logout URL) it is "/login". The forged refresh token is only sent to Cognito's revoke
//     endpoint, which refuses it.
// The real browser round trip (sign-in → sign-out → the next sign-in asks for a new email code) is
// tests/e2e/cognito/cognito-login.spec.ts (e2e-cognito.yml, production host only).
import { test, expect, request as pwRequest, type APIRequestContext, type APIResponse } from '@playwright/test';
import { baseUrl } from './smoke-helpers';
import { LOGIN_PATH, logoutNavigationTarget } from '../../../frontend/lib/staffLogout';
import { isSameOrigin } from '../../../frontend/lib/sameOrigin';

const PROD_ORIGIN = 'https://aa-cis.lumiguides.it.com';
const STAFF_AUTH_ORIGIN = 'https://staff-auth.lumiguides.it.com';
const SESSION_COOKIES = ['cis_admin_token', 'cis_staff_rt', 'cis_role', 'cis_user'];

function requestWith(headers: Record<string, string>, origin = 'https://aa-cis.lumiguides.it.com') {
  const lower = Object.fromEntries(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]));
  return { headers: { get: (name: string) => lower[name.toLowerCase()] ?? null }, nextUrl: { origin } };
}

async function contextWith(extra: Record<string, string>): Promise<APIRequestContext> {
  const headers: Record<string, string> = { ...extra };
  const secret = process.env.VERCEL_AUTOMATION_BYPASS_SECRET;
  // Every request goes to BASE_URL, so the bypass header never leaves that origin.
  if (secret) headers['x-vercel-protection-bypass'] = secret;
  return pwRequest.newContext({ baseURL: baseUrl(), extraHTTPHeaders: headers });
}

/** Session cookies this response clears (`name=; Max-Age=0` or an expiry in the past). */
function sessionCookiesCleared(res: APIResponse): string[] {
  return res
    .headersArray()
    .filter((h) => h.name.toLowerCase() === 'set-cookie')
    .map((h) => h.value)
    .filter((v) => /max-age=0|expires=thu, 01 jan 1970/i.test(v))
    .map((v) => v.split(';')[0].split('=')[0].trim())
    .filter((name) => SESSION_COOKIES.includes(name));
}

test.describe('AA-816 — staff sign-out', () => {
  test('logoutNavigationTarget follows only /login or a Cognito /logout URL', () => {
    const cognito = `${STAFF_AUTH_ORIGIN}/logout?client_id=abc&logout_uri=${encodeURIComponent(`${PROD_ORIGIN}/login`)}`;
    expect(logoutNavigationTarget(cognito)).toBe(cognito);
    expect(logoutNavigationTarget('/login')).toBe(LOGIN_PATH);
    for (const bad of [
      undefined,
      null,
      42,
      '',
      'https://evil.example/',
      'https://evil.example/login',
      'http://staff-auth.lumiguides.it.com/logout?client_id=a&logout_uri=b',
      'javascript:alert(1)',
      '//evil.example/logout?client_id=a&logout_uri=b',
      `${STAFF_AUTH_ORIGIN}/logout`,
      `${STAFF_AUTH_ORIGIN}/logout?client_id=a`,
      'https://user:pw@evil.example/logout?client_id=a&logout_uri=b',
    ]) {
      expect(logoutNavigationTarget(bad), String(bad)).toBe(LOGIN_PATH);
    }
  });

  test('isSameOrigin: Origin first, then Sec-Fetch-Site, strict refuses neither', () => {
    expect(isSameOrigin(requestWith({ Origin: PROD_ORIGIN }))).toBe(true);
    expect(isSameOrigin(requestWith({ Origin: 'https://evil.example' }))).toBe(false);
    // Origin wins over Sec-Fetch-Site.
    expect(isSameOrigin(requestWith({ Origin: 'https://evil.example', 'Sec-Fetch-Site': 'same-origin' }))).toBe(false);
    expect(isSameOrigin(requestWith({ 'Sec-Fetch-Site': 'same-origin' }))).toBe(true);
    expect(isSameOrigin(requestWith({ 'Sec-Fetch-Site': 'cross-site' }))).toBe(false);
    expect(isSameOrigin(requestWith({ 'Sec-Fetch-Site': 'same-site' }))).toBe(false);
    expect(isSameOrigin(requestWith({}))).toBe(true);
    expect(isSameOrigin(requestWith({}), { strict: true })).toBe(false);
  });

  test('cross-site POST /api/auth/admin-logout is refused and clears nothing', async () => {
    const end = Math.floor(Date.now() / 1000) + 3600;
    const ctx = await contextWith({ origin: 'https://evil.example', cookie: `cis_staff_rt=ca~${end}~forged-refresh-token` });
    try {
      const res = await ctx.post('/api/auth/admin-logout', { maxRedirects: 0 });
      expect(res.status()).toBe(403);
      expect(sessionCookiesCleared(res)).toEqual([]);
    } finally {
      await ctx.dispose();
    }
  });

  test('same-origin POST without a session answers /login and clears the session cookies', async () => {
    const ctx = await contextWith({ origin: new URL(baseUrl()).origin });
    try {
      const res = await ctx.post('/api/auth/admin-logout', { maxRedirects: 0 });
      expect(res.status()).toBe(200);
      const body = (await res.json()) as { ok?: unknown; redirect?: unknown };
      expect(body).toEqual({ ok: true, redirect: LOGIN_PATH });
      expect(sessionCookiesCleared(res).sort()).toEqual([...SESSION_COOKIES].sort());
    } finally {
      await ctx.dispose();
    }
  });

  test('same-origin POST with a Cognito refresh cookie answers the managed-login /logout URL (prod host)', async () => {
    const end = Math.floor(Date.now() / 1000) + 3600;
    const origin = new URL(baseUrl()).origin;
    const ctx = await contextWith({ origin, cookie: `cis_staff_rt=ca~${end}~forged-refresh-token` });
    try {
      const res = await ctx.post('/api/auth/admin-logout', { maxRedirects: 0 });
      expect(res.status()).toBe(200);
      const body = (await res.json()) as { redirect?: unknown };
      if (origin !== PROD_ORIGIN) {
        // A preview (or localhost in production mode) has no registered logout URL.
        expect(body.redirect).toBe(LOGIN_PATH);
      } else {
        const url = new URL(String(body.redirect));
        expect(url.origin).toBe(STAFF_AUTH_ORIGIN);
        expect(url.pathname).toBe('/logout');
        expect(url.searchParams.get('client_id')).toBeTruthy();
        expect(url.searchParams.get('logout_uri')).toBe(`${PROD_ORIGIN}/login`);
        expect(logoutNavigationTarget(body.redirect)).toBe(url.toString());
      }
      expect(sessionCookiesCleared(res)).toContain('cis_staff_rt');
    } finally {
      await ctx.dispose();
    }
  });
});
