// tests/e2e/smoke-portal/aa911-portal-cross-site.spec.ts
// AA-911 — the tenant portal's BFF write routes refuse a cross-site request before they read the tenant cookie.
// portal.lumiguides.it.com is the same site as every other *.lumiguides.it.com host (`it.com` is a public suffix),
// including the fixed Preview hosts that serve unmerged PR code. No tenant session is used: a refused request is
// the guard (403 "Cross-site request refused"), an allowed one stops at requireTenant (401).
import { test, expect, request as pwRequest, type APIRequestContext } from '@playwright/test';
import { baseUrl } from '../smoke/smoke-helpers';

const REFUSED = 'Cross-site request refused';
const SIBLING = 'https://booking-preview.lumiguides.it.com';

const WRITE_ROUTES: { method: 'POST' | 'PATCH' | 'DELETE'; path: string }[] = [
  { method: 'POST', path: '/api/playground/proxy' },
  { method: 'POST', path: '/api/tenant/v1/webhooks' },
  { method: 'PATCH', path: '/api/tenant/v1/tenant/settings' },
  { method: 'DELETE', path: '/api/tenant/v1/webhooks/00000000-0000-0000-0000-000000000000' },
];

async function apiContext(extra: Record<string, string>): Promise<APIRequestContext> {
  const headers: Record<string, string> = { ...extra };
  const secret = process.env.VERCEL_AUTOMATION_BYPASS_SECRET;
  // Every request goes to BASE_URL, so the bypass header never leaves that origin.
  if (secret) headers['x-vercel-protection-bypass'] = secret;
  return pwRequest.newContext({ baseURL: baseUrl(), extraHTTPHeaders: headers });
}

async function detailOf(res: { json(): Promise<unknown> }): Promise<unknown> {
  try {
    return ((await res.json()) as { detail?: unknown }).detail;
  } catch {
    return undefined;
  }
}

test.describe('AA-911 — portal write routes refuse cross-site requests', () => {
  for (const { method, path } of WRITE_ROUTES) {
    test(`cross-site ${method} ${path} → 403; same-origin → 401`, async () => {
      const cross = await apiContext({ origin: SIBLING, 'content-type': 'text/plain' });
      try {
        const res = await cross.fetch(path, { method, data: '{"endpoint":"/v1/webhooks"}', maxRedirects: 0, timeout: 15000 });
        expect(res.status(), `cross-site ${method} ${path}`).toBe(403);
        expect(await detailOf(res)).toBe(REFUSED);
      } finally {
        await cross.dispose();
      }
      const same = await apiContext({ origin: new URL(baseUrl()).origin, 'content-type': 'application/json' });
      try {
        const res = await same.fetch(path, { method, data: { endpoint: '/v1/webhooks' }, maxRedirects: 0, timeout: 15000 });
        expect(res.status(), `same-origin ${method} ${path}`).toBe(401);
      } finally {
        await same.dispose();
      }
    });
  }

  test('cross-site sign-out is refused; same-origin sign-out answers ok', async () => {
    const cross = await apiContext({ origin: SIBLING });
    try {
      const res = await cross.post('/api/auth/tenant-logout', { maxRedirects: 0, timeout: 15000 });
      expect(res.status()).toBe(403);
    } finally {
      await cross.dispose();
    }
    const same = await apiContext({ origin: new URL(baseUrl()).origin });
    try {
      const res = await same.post('/api/auth/tenant-logout', { maxRedirects: 0, timeout: 15000 });
      expect(res.status()).toBe(200);
    } finally {
      await same.dispose();
    }
  });
});
