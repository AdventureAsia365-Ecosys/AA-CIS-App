// tests/e2e/smoke/aa885-portal-redirect.spec.ts
// AA-885 — the ADMIN host no longer serves the tenant portal (it is its own app on
// https://portal.lumiguides.it.com). Old portal URLs answer 308 to the same path on the portal host
// (query string kept), and the tenant BFF routes that moved are gone (404). Runs in the admin
// `smoke` project, without any session.
import { test, expect, request as pwRequest, type APIRequestContext } from '@playwright/test';
import { baseUrl } from './smoke-helpers';

const PORTAL = 'https://portal.lumiguides.it.com';

async function anon(): Promise<APIRequestContext> {
  const headers: Record<string, string> = {};
  const secret = process.env.VERCEL_AUTOMATION_BYPASS_SECRET;
  // Every request goes to BASE_URL, so the bypass header never leaves that origin.
  if (secret) headers['x-vercel-protection-bypass'] = secret;
  return pwRequest.newContext({ baseURL: baseUrl(), extraHTTPHeaders: headers });
}

const REDIRECTS: [string, string][] = [
  ['/portal', `${PORTAL}/portal`],
  ['/portal/t4-pool?x=1', `${PORTAL}/portal/t4-pool?x=1`],
  ['/portal/help/facebook', `${PORTAL}/portal/help/facebook`],
  ['/tenant-login', `${PORTAL}/tenant-login`],
];

test.describe('AA-885 — admin host hands the portal over', () => {
  for (const [from, to] of REDIRECTS) {
    test(`${from} → 308 ${to}`, async () => {
      const ctx = await anon();
      try {
        const res = await ctx.get(from, { maxRedirects: 0, timeout: 15000 });
        expect(res.status(), `GET ${from}`).toBe(308);
        expect(res.headers()['location']).toBe(to);
      } finally {
        await ctx.dispose();
      }
    });
  }

  test('the moved tenant BFF routes are gone from the admin host', async () => {
    const ctx = await anon();
    try {
      const login = await ctx.post('/api/auth/tenant-login', { maxRedirects: 0, timeout: 15000, data: {} });
      expect(login.status(), 'POST /api/auth/tenant-login').toBe(404);
      const logout = await ctx.post('/api/auth/tenant-logout', { maxRedirects: 0, timeout: 15000 });
      expect(logout.status(), 'POST /api/auth/tenant-logout').toBe(404);
      const play = await ctx.post('/api/playground/proxy', { maxRedirects: 0, timeout: 15000, data: {} });
      expect(play.status(), 'POST /api/playground/proxy').toBe(404);
    } finally {
      await ctx.dispose();
    }
  });

  test('a tenant cookie gets nothing from the admin tenant proxy (staff-only)', async () => {
    const headers: Record<string, string> = { cookie: 'cis_role=tenant; cis_tenant_token=not-a-jwt' };
    const secret = process.env.VERCEL_AUTOMATION_BYPASS_SECRET;
    if (secret) headers['x-vercel-protection-bypass'] = secret;
    const ctx = await pwRequest.newContext({ baseURL: baseUrl(), extraHTTPHeaders: headers });
    try {
      // /api/tenant/me no longer exists here, so it falls into the staff-only catch-all → 401.
      for (const p of ['/api/tenant/me', '/api/tenant/v1/billing']) {
        const res = await ctx.get(p, { maxRedirects: 0, timeout: 15000 });
        expect(res.status(), `GET ${p} with a tenant cookie`).toBe(401);
      }
    } finally {
      await ctx.dispose();
    }
  });
});
