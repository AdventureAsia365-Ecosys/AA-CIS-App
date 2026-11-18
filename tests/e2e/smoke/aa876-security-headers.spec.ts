// tests/e2e/smoke/aa876-security-headers.spec.ts
// AA-876 — the ADMIN host sends the same security headers as the portal (AA-885) on every response.
// AA-1111 (S284, ZAP baseline): a page, a static route (/robots.txt), a 404 and the BFF all carry the set, the CSP is
// the full policy (scripts by a per-response nonce on pages, no 'unsafe-inline' for scripts, no wildcard fetch
// directive) and COOP is same-origin.
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

function expectSecurityHeaders(h: Record<string, string>, label: string) {
  expect(h['strict-transport-security'], `${label}: HSTS`).toContain('max-age=63072000');
  expect(h['x-content-type-options'], `${label}: nosniff`).toBe('nosniff');
  expect(h['x-frame-options'], `${label}: X-Frame-Options`).toBe('DENY');
  expect(h['referrer-policy'], `${label}: Referrer-Policy`).toBe('strict-origin-when-cross-origin');
  expect(h['permissions-policy'], `${label}: Permissions-Policy`).toContain('camera=()');
  expect(h['cross-origin-opener-policy'], `${label}: COOP`).toBe('same-origin');
  expect(h['x-powered-by'], `${label}: x-powered-by`).toBeUndefined();
  const csp = h['content-security-policy'] ?? '';
  expect(csp, `${label}: CSP`).toContain("frame-ancestors 'none'");
  expect(csp, `${label}: CSP default-src`).toContain("default-src 'self'");
  // The S0 upload PUTs to a presigned S3 URL: connect-src must allow the S3 hosts.
  expect(csp, `${label}: CSP connect-src S3`).toContain('https://*.s3.us-west-1.amazonaws.com');
  const script = csp.split(';').map((d) => d.trim()).find((d) => d.startsWith('script-src ')) ?? '';
  expect(script, `${label}: script-src`).not.toBe('');
  expect(script, `${label}: script-src without unsafe-inline`).not.toContain("'unsafe-inline'");
  return script;
}

test.describe('AA-876 / AA-1111 — admin host security headers', () => {
  test('/login carries the security headers, a nonce CSP and no x-powered-by', async () => {
    const ctx = await anon();
    try {
      const res = await ctx.get('/login', { maxRedirects: 0, timeout: 15000 });
      expect(res.status(), 'GET /login').toBe(200);
      const script = expectSecurityHeaders(res.headers(), '/login');
      expect(script).toMatch(/'nonce-[A-Za-z0-9+/=]+'/);
      // The nonce is per response.
      const again = await ctx.get('/login', { maxRedirects: 0, timeout: 15000 });
      expect(again.headers()['content-security-policy']).not.toBe(res.headers()['content-security-policy']);
    } finally {
      await ctx.dispose();
    }
  });

  for (const path of ['/robots.txt', '/sitemap.xml', '/aa1111-no-such-page', '/api/healthz']) {
    test(`${path} carries the security headers too`, async () => {
      const ctx = await anon();
      try {
        const res = await ctx.get(path, { maxRedirects: 0, timeout: 15000 });
        expectSecurityHeaders(res.headers(), path);
      } finally {
        await ctx.dispose();
      }
    });
  }
});
