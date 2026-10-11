// tests/e2e/smoke/aa656-forged-cookie.spec.ts
// AA-656 — a forged session is rejected on every protected page and every BFF proxy route.
// Runs in the "smoke" project against the Vercel preview, WITHOUT the admin storage state: each
// request carries only hand-made cookies. The page list is lib/rbac.ts PAGE_RULES itself, so a new
// page added to the matrix is covered automatically.
//   • pages  → a redirect to /login (or /tenant-login for the portal), never a 200;
//   • proxies → 401/403, never a forwarded backend answer.
import { test, expect, request as pwRequest, type APIRequestContext } from '@playwright/test';
import { PAGE_RULES } from '../../../frontend/lib/rbac';
import { baseUrl } from './smoke-helpers';

const FORGED: Record<string, string> = {
  'role cookie only (admin)': 'cis_role=admin',
  'role cookie only (content)': 'cis_role=content',
  'role cookie only (reviewer)': 'cis_role=reviewer',
  'garbage admin token': 'cis_role=admin; cis_admin_token=not-a-jwt',
  'garbage tenant token': 'cis_role=tenant; cis_tenant_token=not-a-jwt',
};

const PROXY_ROUTES: { method: 'GET' | 'POST'; path: string }[] = [
  { method: 'GET', path: '/api/admin/jobs' },
  { method: 'POST', path: '/api/admin/waves/check' },
  { method: 'GET', path: '/api/pipeline/status' },
  { method: 'GET', path: '/api/tour-full/00000000-0000-0000-0000-000000000000' },
  { method: 'GET', path: '/api/tenant/pipeline/brand-identity' },
  { method: 'GET', path: '/api/tenant/admin/tenants' },
  { method: 'POST', path: '/api/playground/proxy' },
];

async function contextWith(cookie: string): Promise<APIRequestContext> {
  const headers: Record<string, string> = { cookie };
  const secret = process.env.VERCEL_AUTOMATION_BYPASS_SECRET;
  // Every request goes to BASE_URL, so the bypass header never leaves that origin.
  if (secret) headers['x-vercel-protection-bypass'] = secret;
  return pwRequest.newContext({ baseURL: baseUrl(), extraHTTPHeaders: headers });
}

test.describe('AA-656 — forged staff/tenant sessions are rejected', () => {
  for (const [label, cookie] of Object.entries(FORGED)) {
    test(`pages: ${label}`, async () => {
      const ctx = await contextWith(cookie);
      const leaks: string[] = [];
      try {
        for (const rule of PAGE_RULES) {
          const res = await ctx.get(rule.prefix, { maxRedirects: 0, timeout: 15000 });
          const location = res.headers()['location'] ?? '';
          // A garbage admin token on /portal lands on /login (staff login), a tenant one on
          // /tenant-login — either is a rejection.
          const toLogin = /\/(tenant-)?login(\?|$)/.test(new URL(location || '/', baseUrl()).pathname + '?');
          const redirected = res.status() >= 300 && res.status() < 400 && toLogin;
          if (!redirected) leaks.push(`${rule.prefix} → ${res.status()} ${location}`);
        }
      } finally {
        await ctx.dispose();
      }
      expect(leaks, `pages reachable with a forged session (${label}):\n${leaks.join('\n')}`).toEqual([]);
    });

    test(`proxies: ${label}`, async () => {
      const ctx = await contextWith(cookie);
      const leaks: string[] = [];
      try {
        for (const r of PROXY_ROUTES) {
          const res =
            r.method === 'GET'
              ? await ctx.get(r.path, { maxRedirects: 0, timeout: 15000 })
              : await ctx.post(r.path, { maxRedirects: 0, timeout: 15000, data: { endpoint: '/v1/tours/my-versions' } });
          if (![401, 403].includes(res.status())) leaks.push(`${r.method} ${r.path} → ${res.status()}`);
        }
      } finally {
        await ctx.dispose();
      }
      expect(leaks, `proxy routes answered a forged session (${label}):\n${leaks.join('\n')}`).toEqual([]);
    });
  }
});
