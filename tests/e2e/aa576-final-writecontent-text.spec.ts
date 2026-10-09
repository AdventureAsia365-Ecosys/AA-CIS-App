// tests/e2e/aa576-final-writecontent-text.spec.ts — AA-576 final live-verify.
//
// Nghiệp caught a real gap during his own Chrome verify on prod: the "Write Content" sidebar
// item was removed (AA-576 Phần 3), but 2 pieces of guidance copy still told tenants to go find
// something called "Write Content" — a name that no longer exists anywhere in the menu.
//   1. ReviewList.tsx's "Nothing written yet" empty state ("...written content in Write
//      Content..." — this test intercepts /api/tenant/v1/content-writing/reviews to force an
//      empty array so the state actually renders, since WanderLux Travel's real account already
//      has real content pieces and would never show this state organically).
//   2. AngleGateWizard.tsx's "Nothing to write yet" card (shown when /portal/t8-angle-gate is
//      opened directly, no ?resume_request_id= — this one needs no mocking, it's WanderLux's
//      real, organic state for that deep-link).
import { test, expect } from '@playwright/test';
import { adminUsername, adminPassword } from './helpers/auth';
import fs from 'fs';

const SHOT_DIR = 'tests/e2e/results/aa576-final';
fs.mkdirSync(SHOT_DIR, { recursive: true });

test.describe.configure({ mode: 'serial' });

test('00 - tenant login via real generate-key + tenant-login form', async ({ page, browser }) => {
  await page.goto('/login');
  await page.fill('input[type="text"]', adminUsername());
  await page.fill('input[type="password"]', adminPassword());
  await page.click('button:has-text("Login")');
  await page.waitForURL('**/admin/dashboard', { timeout: 5000 });

  const tenantsRes = await page.request.get('/api/admin/tenants');
  expect(tenantsRes.ok()).toBeTruthy();
  const tenants = await tenantsRes.json();
  const list = Array.isArray(tenants) ? tenants : tenants.tenants;
  const wanderlux = list.find((t: any) => /wanderlux/i.test(t.name) || /wanderlux/i.test(t.slug));
  expect(wanderlux, 'WanderLux Travel test tenant must exist').toBeTruthy();

  const keyRes = await page.request.post(`/api/admin/tenants/${wanderlux.tenant_id}/generate-key`);
  expect(keyRes.ok()).toBeTruthy();
  const { api_key: apiKey } = await keyRes.json();
  expect(apiKey).toBeTruthy();

  const context = await browser.newContext();
  const tenantPage = await context.newPage();
  await tenantPage.goto('/tenant-login');
  await tenantPage.fill('input[type="password"]', apiKey);
  await tenantPage.click('button:has-text("Access Portal")');
  await tenantPage.waitForURL('**/portal**', { timeout: 5000 });

  await context.storageState({ path: `${SHOT_DIR}/tenant-auth.json` });
  await context.close();
});

test.describe('with tenant session', () => {
  test.use({ storageState: `${SHOT_DIR}/tenant-auth.json` });

  test('01 - My Content empty state no longer references "Write Content"', async ({ page }) => {
    // Real backend, real auth — only the one list response is intercepted so the ALREADY-real
    // "Nothing written yet" empty-state branch actually renders for this WanderLux account
    // (which has real content pieces from prior sessions, so it wouldn't hit empty organically).
    await page.route('**/api/tenant/v1/content-writing/reviews*', route =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) })
    );
    await page.goto('/portal/t10-review');
    await expect(page.getByText('Nothing written yet')).toBeVisible({ timeout: 10000 });
    await expect(page.getByText(/Once you've written content from Social Content/)).toBeVisible();
    const body = await page.locator('body').innerText();
    expect(body).not.toMatch(/Write Content/);
    await page.screenshot({ path: `${SHOT_DIR}/01-my-content-empty-state.png`, fullPage: true });
  });

  test('02 - t8-angle-gate "Nothing to write yet" card no longer references "Write Content"', async ({ page }) => {
    await page.goto('/portal/t8-angle-gate');
    await expect(page.getByText('Nothing to write yet')).toBeVisible({ timeout: 10000 });
    await expect(page.getByText(/Every new piece starts from a Subject you pick in Social Content now/)).toBeVisible();
    // Scoped to the card's own text, not the whole page: this route's breadcrumb ("Workspace /
    // Write Content") is deliberately left alone — it just labels the page you're on, not an
    // instruction to go find a menu item — see the AA-576 report-back comment. Only the 2
    // INSTRUCTIONAL copies (this card + ReviewList's empty state, test 01) told tenants to go
    // find something called "Write Content", which is what was actually broken.
    const emptyStateBlock = page.getByText('Nothing to write yet').locator('..');
    await expect(emptyStateBlock).not.toContainText('Write Content');
    await page.screenshot({ path: `${SHOT_DIR}/02-t8-angle-gate-empty-state.png`, fullPage: true });
  });

  test('03 - full portal sweep: "Write Content" text is gone from every route/state except the intentionally-kept t8-angle-gate breadcrumb', async ({ page }) => {
    const ROUTES = [
      '/portal/dashboard', '/portal/t1-rewrite', '/portal/t4-pool', '/portal/t0-brand',
      '/portal/t7-planning', '/portal/t10-review', '/portal/t11-publish', '/portal/marketplace',
      '/portal/api', '/portal/activity', '/portal/billing', '/portal/settings',
    ];
    for (const route of ROUTES) {
      await page.goto(route);
      await page.waitForTimeout(400);
      const body = await page.locator('body').innerText();
      expect(body, `${route} should not mention "Write Content"`).not.toMatch(/Write Content/);
    }
  });
});
