// tests/e2e/smoke/aa816-cognito-auth-routes.spec.ts
// AA-816 (C4) — the Cognito auth routes refuse what they must refuse, with only hand-made cookies (no
// admin storage state) and without ever reaching Cognito with a real credential. Safe on a Vercel
// preview: a preview host is not on the redirect-URI allowlist, so no route can start a real sign-in.
//   • GET  /api/auth/refresh?next=//evil.com, no cookie      → /login on this origin, never evil.com;
//   • POST /api/auth/refresh with a forged cis_staff_rt      → 401, no session cookie with a value;
//   • GET  /api/auth/signin/cognito on a non-allowlisted host → /login?error=cognito_host;
//   • GET  /api/auth/callback/cognito with no tx cookie       → /login with an error, no session cookie.
import { test, expect, request as pwRequest, type APIRequestContext, type APIResponse } from '@playwright/test';
import { baseUrl } from './smoke-helpers';

const PROD_ORIGIN = 'https://aa-cis.lumiguides.it.com';
const PREVIEW_SIGNIN_ORIGIN = 'https://aa-cis-preview.lumiguides.it.com';
const SESSION_COOKIES = ['cis_admin_token', 'cis_staff_rt', 'cis_role', 'cis_user'];

async function contextWith(cookie?: string): Promise<APIRequestContext> {
  const headers: Record<string, string> = {};
  if (cookie) headers.cookie = cookie;
  const secret = process.env.VERCEL_AUTOMATION_BYPASS_SECRET;
  // Every request goes to BASE_URL, so the bypass header never leaves that origin.
  if (secret) headers['x-vercel-protection-bypass'] = secret;
  return pwRequest.newContext({ baseURL: baseUrl(), extraHTTPHeaders: headers });
}

function location(res: APIResponse): URL {
  return new URL(res.headers()['location'] ?? '/', baseUrl());
}

/** Session cookies this response sets WITH a value (clearing ones, `name=; Max-Age=0`, are fine). */
function sessionCookiesSet(res: APIResponse): string[] {
  return res
    .headersArray()
    .filter((h) => h.name.toLowerCase() === 'set-cookie')
    .map((h) => h.value)
    .filter((v) => {
      const [pair] = v.split(';');
      const eq = pair.indexOf('=');
      const name = pair.slice(0, eq).trim();
      const value = pair.slice(eq + 1).trim();
      return SESSION_COOKIES.includes(name) && value !== '' && !/max-age=0/i.test(v);
    });
}

test.describe('AA-816 — Cognito auth routes refuse forged or missing state', () => {
  test('refresh (navigation) without a cookie never redirects off-site', async () => {
    const ctx = await contextWith();
    try {
      for (const next of ['//evil.com', '/\\evil.com', 'https://evil.com/admin']) {
        const res = await ctx.get(`/api/auth/refresh?next=${encodeURIComponent(next)}`, { maxRedirects: 0 });
        expect(res.status(), `next=${next}`).toBeGreaterThanOrEqual(300);
        expect(res.status(), `next=${next}`).toBeLessThan(400);
        const loc = location(res);
        // Not compared with BASE_URL's origin: `next start` answers with `localhost` for 127.0.0.1.
        expect(loc.hostname, `next=${next}`).not.toContain('evil.com');
        expect(loc.pathname, `next=${next}`).toBe('/login');
        expect(sessionCookiesSet(res), `next=${next}`).toEqual([]);
      }
    } finally {
      await ctx.dispose();
    }
  });

  test('refresh (keep-alive) with a forged refresh cookie is 401 and sets no session', async () => {
    // Well-formed ("ca~<future end>~<token>") so it reaches the token call; Cognito (or a missing
    // client secret on a preview) refuses it.
    const end = Math.floor(Date.now() / 1000) + 3600;
    const ctx = await contextWith(`cis_staff_rt=ca~${end}~forged-refresh-token`);
    try {
      const res = await ctx.post('/api/auth/refresh', { maxRedirects: 0 });
      expect(res.status()).toBe(401);
      expect(sessionCookiesSet(res)).toEqual([]);
    } finally {
      await ctx.dispose();
    }
  });

  test('sign-in on a host without a registered callback goes back to /login', async () => {
    // AA-816 P6a: the fixed Preview host is a registered callback too (the cis-admin-preview client).
    test.skip([PROD_ORIGIN, PREVIEW_SIGNIN_ORIGIN].includes(new URL(baseUrl()).origin),
      'a registered callback host starts a real sign-in');
    const ctx = await contextWith();
    try {
      const res = await ctx.get('/api/auth/signin/cognito', { maxRedirects: 0 });
      expect(res.status()).toBeGreaterThanOrEqual(300);
      expect(res.status()).toBeLessThan(400);
      const loc = location(res);
      expect(loc.pathname).toBe('/login');
      expect(loc.searchParams.get('error')).toBe('cognito_host');
    } finally {
      await ctx.dispose();
    }
  });

  test('callback without the transaction cookie sets no session', async () => {
    const ctx = await contextWith();
    try {
      const res = await ctx.get('/api/auth/callback/cognito?code=forged&state=forged', { maxRedirects: 0 });
      expect(res.status()).toBeGreaterThanOrEqual(300);
      expect(res.status()).toBeLessThan(400);
      const loc = location(res);
      expect(loc.pathname).toBe('/login');
      // A preview host is refused before the state check (cognito_host); production fails the state
      // check (signin_failed). Either way, no session.
      expect(['cognito_host', 'signin_failed']).toContain(loc.searchParams.get('error'));
      expect(sessionCookiesSet(res)).toEqual([]);
    } finally {
      await ctx.dispose();
    }
  });
});
