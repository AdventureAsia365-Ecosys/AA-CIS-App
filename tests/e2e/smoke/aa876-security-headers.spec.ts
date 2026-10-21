// tests/e2e/smoke/aa876-security-headers.spec.ts
// AA-876 — the ADMIN host sends the same security headers as the portal (AA-885) on every response.
// Runs in the admin `smoke` project without a session (a fresh request context, no storage state).
import { test, expect, request as pwRequest, type APIRequestContext } from '@playwright/test';
import { baseUrl } from './smoke-helpers';

async function anon(): Promise<APIRequestContext> {
  const headers: Record<string, string> = {};
  const secret = process.env.VERCEL_AUTOMATION_BYPASS_SECRET;
  // Every request goes to BASE_URL, so the bypass header never leaves that origin.
  if (secret) headers['x-vercel-protection-bypass'] = secret;
  return pwRequest.newContext({ baseURL: baseUrl(), extraHTTPHeaders: headers });
}

test.describe('AA-876 — admin host security headers', () => {
  test('/login carries the security headers and no x-powered-by', async () => {
    const ctx = await anon();
    try {
      const res = await ctx.get('/login', { maxRedirects: 0, timeout: 15000 });
      expect(res.status(), 'GET /login').toBe(200);
      const h = res.headers();
      expect(h['strict-transport-security'], 'HSTS').toContain('max-age=63072000');
      expect(h['x-content-type-options'], 'nosniff').toBe('nosniff');
      expect(h['x-frame-options'], 'X-Frame-Options').toBe('DENY');
      expect(h['referrer-policy'], 'Referrer-Policy').toBe('strict-origin-when-cross-origin');
      expect(h['content-security-policy'], 'CSP').toContain("frame-ancestors 'none'");
      expect(h['permissions-policy'], 'Permissions-Policy').toContain('camera=()');
      expect(h['x-powered-by'], 'x-powered-by').toBeUndefined();
    } finally {
      await ctx.dispose();
    }
  });
});
