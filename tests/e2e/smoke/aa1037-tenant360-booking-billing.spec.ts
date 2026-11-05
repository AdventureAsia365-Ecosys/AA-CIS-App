// tests/e2e/smoke/aa1037-tenant360-booking-billing.spec.ts
// AA-1037 (root ADR 0019 d9 "Screens now") — Tenant 360 › Billing › Booking invoices and the Trade nav group.
// Runs in the "smoke" project (staff storageState) for a role that reads Tenant 360 › Billing (billing.read or
// tenant.book.account.read: admin, finance, customer_success); a viewer or another role skips it. Every wait is bounded.
//   (a) GET /api/admin/tenants/<id>/booking-billing answers 200 with available / booking / account / subscriptions;
//   (b) /admin/tenants/<id>?tab=billing shows the "Booking invoices" section above the meters, the account chip, and
//       "Open in Booking" pointing to Booking admin's /agency-billing?tab=invoices&tenant=<id> (new tab);
//   (c) the sidebar's Trade group lists the four Booking admin pages as external entries (same tab, never the current
//       page); no console error, no failing same-origin /api/ call.
import { test, expect } from '@playwright/test';
import { ADMIN_STATE, baseUrl, installBypassRoute, smokeRole } from './smoke-helpers';
import { can } from '../../../frontend/lib/rbac';

const AA_INTERNAL = '00000000-0000-0000-0000-000000000001';
const TRADE = ['/', '/agency-billing', '/pricing/agency-tiers', '/b2b-desk'];

test.describe('AA-1037 — Tenant 360 Booking invoices + Trade nav', { tag: '@staff' }, () => {
  test.beforeEach(() => {
    const role = smokeRole();
    test.skip(!(can(role, 'billing.read') || can(role, 'tenant.book.account.read')),
      `role ${role} does not read Tenant 360 › Billing`);
  });

  test('(a)-(c) the Billing tab section, the deep link, the Trade group', async ({ browser }) => {
    test.setTimeout(90000);
    const context = await browser.newContext({ baseURL: baseUrl(), storageState: ADMIN_STATE,
      viewport: { width: 1440, height: 900 } });
    await installBypassRoute(context);
    const page = await context.newPage();
    const consoleErrors: string[] = [];
    const apiFailures: string[] = [];
    page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
    page.on('response', (r) => {
      if (r.url().startsWith(baseUrl()) && r.url().includes('/api/') && r.status() >= 400) {
        apiFailures.push(`${r.status()} ${new URL(r.url()).pathname}`);
      }
    });
    try {
      // The directory first (an in-page fetch keeps the Preview bypass route and the session cookie).
      await page.goto('/admin/tenants', { waitUntil: 'domcontentloaded', timeout: 30000 });
      const list = await page.evaluate(async () => {
        const r = await fetch('/api/admin/tenants?include_inactive=true');
        return { status: r.status, body: r.ok ? await r.json() : null };
      });
      expect(list.status).toBe(200);
      const tenants = (list.body?.tenants ?? []) as { tenant_id: string }[];
      const tenant = tenants.find((x) => x.tenant_id !== AA_INTERNAL);
      test.skip(!tenant, 'no agency tenant in the caller scope');
      const id = tenant!.tenant_id;

      const read = page.waitForResponse((r) => r.url().includes(`/api/admin/tenants/${id}/booking-billing`),
        { timeout: 30000 });
      await page.goto(`/admin/tenants/${id}?tab=billing`, { waitUntil: 'domcontentloaded', timeout: 30000 });
      const resp = await read;
      expect(resp.status(), await resp.text().catch(() => '')).toBe(200);
      const body = await resp.json();
      expect(Object.keys(body).sort()).toEqual(['account', 'available', 'booking', 'subscriptions', 'tenant_id']);
      expect(typeof body.available).toBe('boolean');

      const section = page.getByTestId('t360-booking-billing');
      await expect(section).toBeVisible({ timeout: 20000 });
      await expect(section.getByTestId('t360-booking-cis-side')).toBeVisible();
      if (body.booking) await expect(section.getByTestId('t360-booking-status')).toBeVisible();
      const link = section.getByTestId('t360-open-in-booking');
      const href = await link.getAttribute('href');
      expect(href).toMatch(new RegExp(`^https://[^/]+/agency-billing\\?tab=invoices&tenant=${id}$`));
      expect(await link.getAttribute('target')).toBe('_blank');

      const trade = page.locator('[data-testid="admin-nav"] button[data-external="true"]');
      await expect(trade).toHaveCount(TRADE.length, { timeout: 10000 });
      const hrefs = await trade.evaluateAll((els) => els.map((e) => e.getAttribute('data-href') ?? ''));
      const origin = new URL(hrefs[0]).origin;
      expect(hrefs.map((h) => h.slice(origin.length))).toEqual(TRADE);
      for (const el of await trade.all()) expect(await el.getAttribute('aria-current')).toBeNull();

      expect(apiFailures, 'failing same-origin /api/ calls').toEqual([]);
      expect(consoleErrors, 'console errors').toEqual([]);
    } finally {
      await context.close();
    }
  });
});
