// tests/e2e/smoke/aa911-bff-cross-site.spec.ts
// AA-911 — every admin BFF write route refuses a cross-site request before it reads a cookie or a body.
// `it.com` is a public suffix, so every *.lumiguides.it.com host (including the fixed Preview hosts that serve
// unmerged PR code) is the same site as the admin host and its requests carry the SameSite=Lax session cookies.
// Runs in the "smoke" project WITHOUT the admin storage state: the requests carry no session at all, so a refused
// request is the guard (403 "Cross-site request refused") and an allowed one stops at the auth check (401/403).
//   • unit: isCrossSiteWrite (only non-GET/HEAD/OPTIONS from another origin);
//   • a cross-site text/plain POST (no CORS preflight in a browser) to each proxy → 403 from the guard;
//   • Sec-Fetch-Site: same-site (a sibling *.lumiguides.it.com host) without Origin → 403;
//   • the same request from this origin → the auth answer, not the guard; a cross-site GET is not refused.
// Static twin: tests/unit/test_aa911_bff_cross_site.py (every write route calls the guard first).
import { test, expect, request as pwRequest, type APIRequestContext } from '@playwright/test';
import { baseUrl } from './smoke-helpers';
import { isCrossSiteWrite } from '../../../frontend/lib/sameOrigin';

const REFUSED = 'Cross-site request refused';
const SIBLING = 'https://booking-preview.lumiguides.it.com';

const WRITE_ROUTES: { method: 'POST' | 'PUT' | 'PATCH' | 'DELETE'; path: string }[] = [
  { method: 'POST', path: '/api/admin/waves/check' },
  { method: 'PATCH', path: '/api/admin/content-quality/place-cards/00000000-0000-0000-0000-000000000000' },
  { method: 'DELETE', path: '/api/admin/tenants/00000000-0000-0000-0000-000000000000' },
  { method: 'POST', path: '/api/pipeline/status' },
  { method: 'POST', path: '/api/tenant/admin/tenants' },
  { method: 'POST', path: '/api/tenant/pipeline/brand-identity' },
];

function requestWith(method: string, headers: Record<string, string>, origin = 'https://aa-cis.lumiguides.it.com') {
  const lower = Object.fromEntries(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]));
  return { method, headers: { get: (name: string) => lower[name.toLowerCase()] ?? null }, nextUrl: { origin } };
}

async function contextWith(extra: Record<string, string>): Promise<APIRequestContext> {
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

test.describe('AA-911 — BFF write routes refuse cross-site requests', () => {
  test('isCrossSiteWrite: only a state-changing request from another origin', () => {
    const own = 'https://aa-cis.lumiguides.it.com';
    for (const m of ['POST', 'PUT', 'PATCH', 'DELETE', 'post']) {
      expect(isCrossSiteWrite(requestWith(m, { Origin: SIBLING })), m).toBe(true);
      expect(isCrossSiteWrite(requestWith(m, { Origin: 'https://evil.example' })), m).toBe(true);
      expect(isCrossSiteWrite(requestWith(m, { 'Sec-Fetch-Site': 'same-site' })), m).toBe(true);
      expect(isCrossSiteWrite(requestWith(m, { Origin: own })), m).toBe(false);
      expect(isCrossSiteWrite(requestWith(m, { 'Sec-Fetch-Site': 'same-origin' })), m).toBe(false);
      // Neither header: a non-browser client, which carries no victim's cookies.
      expect(isCrossSiteWrite(requestWith(m, {})), m).toBe(false);
    }
    for (const m of ['GET', 'HEAD', 'OPTIONS']) {
      expect(isCrossSiteWrite(requestWith(m, { Origin: SIBLING })), m).toBe(false);
    }
  });

  for (const { method, path } of WRITE_ROUTES) {
    test(`cross-site ${method} ${path} → 403 from the guard`, async () => {
      for (const headers of [
        { origin: SIBLING, 'content-type': 'text/plain' },
        { origin: 'https://evil.example', 'content-type': 'text/plain' },
        { 'sec-fetch-site': 'same-site', 'content-type': 'text/plain' },
      ]) {
        const ctx = await contextWith(headers);
        try {
          const res = await ctx.fetch(path, { method, data: '{"x":1}', maxRedirects: 0, timeout: 15000 });
          expect(res.status(), `${method} ${path} ${JSON.stringify(headers)}`).toBe(403);
          expect(await detailOf(res)).toBe(REFUSED);
        } finally {
          await ctx.dispose();
        }
      }
    });

    test(`same-origin ${method} ${path} reaches the auth check`, async () => {
      const ctx = await contextWith({ origin: new URL(baseUrl()).origin, 'content-type': 'application/json' });
      try {
        const res = await ctx.fetch(path, { method, data: { x: 1 }, maxRedirects: 0, timeout: 15000 });
        expect([401, 403], `${method} ${path}`).toContain(res.status());
        expect(await detailOf(res)).not.toBe(REFUSED);
      } finally {
        await ctx.dispose();
      }
    });
  }

  test('a cross-site GET is not refused by the guard (reads stay as they were)', async () => {
    const ctx = await contextWith({ origin: SIBLING });
    try {
      const res = await ctx.get('/api/admin/jobs', { maxRedirects: 0, timeout: 15000 });
      expect([401, 403]).toContain(res.status());
      expect(await detailOf(res)).not.toBe(REFUSED);
    } finally {
      await ctx.dispose();
    }
  });
});
