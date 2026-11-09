// tests/e2e/smoke/aa1039-admin-ia-redirects.spec.ts
// AA-1039 R6 slice 1 (S266, root ADR 0019 d13) — the admin pages that moved into the Tenants and Tenant content QA
// groups. Every old URL answers 307 to its new home with the query string kept (frontend/lib/adminIa.ts
// ADMIN_ROUTE_MOVES, served by next.config.ts before proxy.ts, so no session is needed), and every page URL the S265
// navigation linked lands on a page of the new layout (followed without a session it ends on the sign-in page, never
// a 404). Runs in the per-PR `smoke-public` project.
import { test, expect, request as pwRequest, type APIRequestContext } from '@playwright/test';
import { ADMIN_ROUTE_MOVES, PRE_R6_ADMIN_ROUTES, resolveAdminRoute } from '../../../frontend/lib/adminIa';
import { baseUrl } from './smoke-helpers';

async function anon(): Promise<APIRequestContext> {
  const headers: Record<string, string> = {};
  const secret = process.env.VERCEL_AUTOMATION_BYPASS_SECRET;
  // Every request goes to BASE_URL, so the bypass header never leaves that origin.
  if (secret) headers['x-vercel-protection-bypass'] = secret;
  return pwRequest.newContext({ baseURL: baseUrl(), extraHTTPHeaders: headers });
}

/** Path + query of a Location header (Next.js may send it relative or absolute). */
function target(location: string | undefined): string {
  const u = new URL(location ?? '', baseUrl());
  return `${u.pathname}${u.search}`;
}

test.describe('AA-1039 — old admin URLs redirect to the new layout', () => {
  for (const m of ADMIN_ROUTE_MOVES) {
    const from = m.query ? `${m.from}?${m.query.key}=${m.query.value}` : `${m.from}?probe=1`;
    const to = resolveAdminRoute(from);
    test(`${from} → 307 ${to}`, async () => {
      const ctx = await anon();
      try {
        const res = await ctx.get(from, { maxRedirects: 0, timeout: 15000 });
        expect(res.status(), `GET ${from}`).toBe(307);
        expect(target(res.headers()['location']), `Location of ${from}`).toBe(to);
      } finally {
        await ctx.dispose();
      }
    });
  }

  test('every page URL of the S265 nav ends on a page, never a 404', async () => {
    const ctx = await anon();
    try {
      for (const old of PRE_R6_ADMIN_ROUTES) {
        const res = await ctx.get(old, { timeout: 15000 });
        expect(res.status(), `GET ${old} (followed to ${res.url()})`).toBeLessThan(400);
      }
    } finally {
      await ctx.dispose();
    }
  });
});
