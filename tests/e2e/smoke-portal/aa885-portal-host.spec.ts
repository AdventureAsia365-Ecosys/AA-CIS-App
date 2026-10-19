// tests/e2e/smoke-portal/aa885-portal-host.spec.ts
// AA-885 — what the PORTAL host (portal app, Vercel aa-cis-portal) must and must not serve.
// Unauthenticated checks run always (no tenant key needed); the cookie-scope check needs the
// tenant storage state from auth.setup.ts.
//   • /portal/* without a session → /tenant-login; / → the dashboard (then the gate);
//   • a forged tenant/staff cookie is refused by pages (→ /tenant-login) and by the BFF (401);
//   • no admin page, admin BFF or staff login exists here (404);
//   • every response carries the security headers;
//   • /privacy and /terms are draft pages (AA-886: 200, draft banner, noindex);
//   • after the tenant login, cis_tenant_token is host-only (no parent Domain), httpOnly and Secure.
import { test, expect, request as pwRequest, type APIRequestContext } from '@playwright/test';
import fs from 'fs';
import { baseUrl } from '../smoke/smoke-helpers';
import { NO_KEY_REASON, TENANT_STATE, hasTenantState } from './portal-helpers';

async function apiContext(cookie?: string): Promise<APIRequestContext> {
  const headers: Record<string, string> = {};
  if (cookie) headers.cookie = cookie;
  const secret = process.env.VERCEL_AUTOMATION_BYPASS_SECRET;
  // Every request goes to BASE_URL, so the bypass header never leaves that origin.
  if (secret) headers['x-vercel-protection-bypass'] = secret;
  return pwRequest.newContext({ baseURL: baseUrl(), extraHTTPHeaders: headers });
}

function pathOf(location: string): string {
  return new URL(location || '/', baseUrl()).pathname;
}

const FORGED: Record<string, string> = {
  'role cookie only (tenant)': 'cis_role=tenant',
  'garbage tenant token': 'cis_role=tenant; cis_tenant_token=not-a-jwt',
  'staff token on the portal host': 'cis_role=admin; cis_admin_token=not-a-jwt',
};

test.describe('AA-885 — portal host', () => {
  test('unauthenticated /portal/dashboard redirects to /tenant-login', async () => {
    const ctx = await apiContext();
    try {
      const res = await ctx.get('/portal/dashboard', { maxRedirects: 0, timeout: 15000 });
      expect(res.status(), 'expected a redirect').toBeGreaterThanOrEqual(300);
      expect(res.status()).toBeLessThan(400);
      expect(pathOf(res.headers()['location'] ?? '')).toBe('/tenant-login');
    } finally {
      await ctx.dispose();
    }
  });

  test('/ redirects into /portal/dashboard', async () => {
    const ctx = await apiContext();
    try {
      const res = await ctx.get('/', { maxRedirects: 0, timeout: 15000 });
      expect(res.status()).toBeGreaterThanOrEqual(300);
      expect(res.status()).toBeLessThan(400);
      expect(pathOf(res.headers()['location'] ?? '')).toBe('/portal/dashboard');
    } finally {
      await ctx.dispose();
    }
  });

  for (const [label, cookie] of Object.entries(FORGED)) {
    test(`forged session is refused: ${label}`, async () => {
      const ctx = await apiContext(cookie);
      try {
        const page = await ctx.get('/portal/dashboard', { maxRedirects: 0, timeout: 15000 });
        expect(page.status(), `${label}: /portal/dashboard must redirect`).toBeGreaterThanOrEqual(300);
        expect(page.status()).toBeLessThan(400);
        expect(pathOf(page.headers()['location'] ?? '')).toBe('/tenant-login');
        for (const p of ['/api/tenant/me', '/api/tenant/v1/billing']) {
          const res = await ctx.get(p, { maxRedirects: 0, timeout: 15000 });
          expect(res.status(), `${label}: GET ${p}`).toBe(401);
        }
        const play = await ctx.post('/api/playground/proxy', {
          maxRedirects: 0, timeout: 15000, data: { endpoint: '/v1/tours/my-versions' },
        });
        expect(play.status(), `${label}: POST /api/playground/proxy`).toBe(401);
      } finally {
        await ctx.dispose();
      }
    });
  }

  test('no admin page, admin BFF or staff login on the portal host', async () => {
    const ctx = await apiContext();
    try {
      for (const p of ['/admin/overview', '/api/admin/health', '/login', '/api/auth/login']) {
        const res = await ctx.get(p, { maxRedirects: 0, timeout: 15000 });
        expect(res.status(), `GET ${p} on the portal host`).toBe(404);
      }
    } finally {
      await ctx.dispose();
    }
  });

  test('security headers on the portal pages', async () => {
    const ctx = await apiContext();
    try {
      for (const p of ['/tenant-login', '/terms']) {
        const res = await ctx.get(p, { maxRedirects: 0, timeout: 15000 });
        expect(res.status(), `GET ${p}`).toBe(200);
        const h = res.headers();
        expect(h['strict-transport-security'], `${p}: HSTS`).toContain('max-age=63072000');
        expect(h['x-content-type-options'], `${p}: nosniff`).toBe('nosniff');
        expect(h['x-frame-options'], `${p}: X-Frame-Options`).toBe('DENY');
        expect(h['referrer-policy'], `${p}: Referrer-Policy`).toBe('strict-origin-when-cross-origin');
        expect(h['content-security-policy'], `${p}: CSP`).toContain("frame-ancestors 'none'");
        expect(h['permissions-policy'], `${p}: Permissions-Policy`).toContain('camera=()');
      }
    } finally {
      await ctx.dispose();
    }
  });

  // AA-886: both are portal pages now (no redirect), marked as drafts and kept out of search engines.
  test('/privacy and /terms are draft pages (200, banner, noindex)', async () => {
    const ctx = await apiContext();
    try {
      for (const p of ['/privacy', '/terms']) {
        const res = await ctx.get(p, { maxRedirects: 0, timeout: 15000 });
        expect(res.status(), `GET ${p}`).toBe(200);
        const html = await res.text();
        expect(html, `${p}: draft banner`).toContain('Draft — pending legal review');
        expect(html, `${p}: noindex`).toMatch(/<meta name="robots" content="noindex/);
      }
    } finally {
      await ctx.dispose();
    }
  });

  test('tenant cookie is host-only, httpOnly and Secure', async () => {
    test.skip(!hasTenantState(), NO_KEY_REASON);
    const state = JSON.parse(fs.readFileSync(TENANT_STATE, 'utf8')) as {
      cookies: { name: string; domain: string; httpOnly: boolean; secure: boolean; sameSite: string }[];
    };
    const host = new URL(baseUrl()).hostname;
    const token = state.cookies.find((c) => c.name === 'cis_tenant_token');
    expect(token, 'cis_tenant_token not in the storage state').toBeTruthy();
    // A host-only cookie is stored with the exact host (no leading dot / parent domain).
    expect(token!.domain, 'cis_tenant_token must be host-only on the portal host').toBe(host);
    expect(token!.httpOnly).toBe(true);
    if (new URL(baseUrl()).protocol === 'https:') expect(token!.secure).toBe(true);
    expect(token!.sameSite).toBe('Lax');
  });
});
