// AA-573 live-verify: after the fix, both staff (/login) and B2B tenant
// (/tenant-login) logins should auto-redirect on their own, no manual
// refresh/URL-typing needed. Screenshots + timing recorded as evidence.
import { test, expect } from '@playwright/test';
import { adminUsername, adminPassword } from './helpers/auth';
import fs from 'fs';

const SHOT_DIR = 'tests/e2e/results/aa573';
fs.mkdirSync(SHOT_DIR, { recursive: true });

test.describe.configure({ mode: 'serial' });

test('01 - staff login auto-redirects to /admin/dashboard, no manual action', async ({ page }) => {
  await page.goto('/login');
  await page.fill('input[type="text"]', adminUsername());
  await page.fill('input[type="password"]', adminPassword());

  const t0 = Date.now();
  await page.click('button:has-text("Login")');

  // Real assertion: URL must change on its own within 3s. No goto/reload here.
  await page.waitForURL('**/admin/dashboard', { timeout: 3000 });
  const elapsed = Date.now() - t0;
  console.log(`[AA-573] staff login -> auto-redirect in ${elapsed}ms`);
  expect(elapsed).toBeLessThan(3000);

  await page.waitForTimeout(500);
  await page.screenshot({ path: `${SHOT_DIR}/01-staff-auto-dashboard.png`, fullPage: true });
  expect(page.url()).toContain('/admin/dashboard');
});

test('02 - staff login: double-Enter no longer fires two POSTs / hangs', async ({ page }) => {
  const loginPosts: number[] = [];
  page.on('request', req => {
    if (req.method() === 'POST' && req.url().endsWith('/api/auth/login')) loginPosts.push(Date.now());
  });

  await page.goto('/login');
  await page.fill('input[type="text"]', adminUsername());
  const pw = page.locator('input[type="password"]');
  await pw.fill(adminPassword());

  const t0 = Date.now();
  await pw.press('Enter');
  await pw.press('Enter'); // second Enter while first is in flight — should be a no-op now

  await page.waitForURL('**/admin/dashboard', { timeout: 3000 });
  const elapsed = Date.now() - t0;
  console.log(`[AA-573] double-Enter -> auto-redirect in ${elapsed}ms, POST /api/auth/login count=${loginPosts.length}`);
  expect(elapsed).toBeLessThan(3000);
  expect(loginPosts.length).toBe(1); // was 2 before the loading-guard fix
});

test('03 - B2B tenant login auto-redirects to /portal, no manual action', async ({ page, request }) => {
  // Get a real tenant browser session first (admin login), then use the
  // real generate-key admin action to mint a fresh key for the existing
  // WanderLux Travel test tenant (reused across many prior sessions —
  // see memory reference_tenant_test_login_via_generate_key.md).
  await page.goto('/login');
  await page.fill('input[type="text"]', adminUsername());
  await page.fill('input[type="password"]', adminPassword());
  await page.click('button:has-text("Login")');
  await page.waitForURL('**/admin/dashboard', { timeout: 3000 });

  const tenantsRes = await page.request.get('/api/admin/tenants');
  expect(tenantsRes.ok()).toBeTruthy();
  const tenants = await tenantsRes.json();
  const list = Array.isArray(tenants) ? tenants : tenants.tenants;
  const wanderlux = list.find((t: any) => /wanderlux/i.test(t.name) || /wanderlux/i.test(t.slug));
  expect(wanderlux, 'WanderLux Travel test tenant must exist').toBeTruthy();

  const keyRes = await page.request.post(`/api/admin/tenants/${wanderlux.tenant_id}/generate-key`);
  expect(keyRes.ok()).toBeTruthy();
  const keyData = await keyRes.json();
  const apiKey = keyData.api_key;
  expect(apiKey).toBeTruthy();

  // Fresh, unauthenticated context — real tenant-login flow only.
  const tenantPage = await page.context().browser()!.newPage();
  await tenantPage.goto('/tenant-login');
  await tenantPage.fill('input[type="password"]', apiKey);

  const t0 = Date.now();
  await tenantPage.click('button:has-text("Access Portal")');
  await tenantPage.waitForURL('**/portal**', { timeout: 3000 });
  const elapsed = Date.now() - t0;
  console.log(`[AA-573] tenant login -> auto-redirect in ${elapsed}ms`);
  expect(elapsed).toBeLessThan(3000);

  await tenantPage.waitForTimeout(500);
  await tenantPage.screenshot({ path: `${SHOT_DIR}/03-tenant-auto-portal.png`, fullPage: true });
  expect(tenantPage.url()).toContain('/portal');
  await tenantPage.close();
});
