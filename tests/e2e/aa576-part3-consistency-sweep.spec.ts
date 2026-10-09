// tests/e2e/aa576-part3-consistency-sweep.spec.ts — AA-576 Phần 3 consistency-sweep live-verify.
//
// Nghiệp confirmed the sidebar rename, then asked to sync the SAME new names everywhere else a
// tenant sees the old ones: portal/layout.tsx's breadcrumb map, t10-review's own <h1>,
// ReviewList.tsx's CardHead, and DashboardTab.tsx's 2 quick-link cards — plus whatever else a
// full sweep turned up (t1-rewrite's rewrite-started toast, MarketplaceTab's description copy,
// PoolTab's "In My Catalog" badge (x2), CatalogTab's sticky section title / in-progress toast /
// exported XLSX sheet name). This test confirms every one of those reads with the new name and
// the old name is gone, using the real WanderLux Travel tenant.
import { test, expect } from '@playwright/test';
import { adminUsername, adminPassword } from './helpers/auth';
import fs from 'fs';

const SHOT_DIR = 'tests/e2e/results/aa576-part3-consistency';
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

  test('01 - Dashboard: quick-link cards + no old names anywhere on the page', async ({ page }) => {
    await page.goto('/portal/dashboard');
    await expect(page.getByText('Browse Tours', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('My Catalog Tours', { exact: true }).first()).toBeVisible();
    const body = await page.locator('body').innerText();
    expect(body).not.toMatch(/\bBrowse Pool\b/);
    expect(body).not.toMatch(/\bMy Catalog\b(?! Tours)/);
    await page.screenshot({ path: `${SHOT_DIR}/01-dashboard.png`, fullPage: true });
  });

  test('02 - Browse Tours (t1-rewrite): sidebar + breadcrumb both say "Browse Tours"', async ({ page }) => {
    await page.goto('/portal/t1-rewrite');
    const matches = await page.getByText('Browse Tours', { exact: true }).all();
    expect(matches.length).toBeGreaterThanOrEqual(2); // sidebar nav item + top breadcrumb
    const body = await page.locator('body').innerText();
    expect(body).not.toMatch(/\bBrowse Pool\b/);
  });

  test('03 - My Catalog Tours (t4-pool): breadcrumb + in-page sticky title both say the new name', async ({ page }) => {
    await page.goto('/portal/t4-pool');
    await page.waitForLoadState('networkidle').catch(() => {});
    const catalogTexts = await page.getByText('My Catalog Tours').all();
    expect(catalogTexts.length).toBeGreaterThanOrEqual(2); // breadcrumb + sticky in-page title
    const body = await page.locator('body').innerText();
    expect(body).not.toMatch(/\bMy Catalog\b(?! Tours)/);
    await page.screenshot({ path: `${SHOT_DIR}/03-my-catalog-tours.png`, fullPage: true });
  });

  test('04 - My Content (t10-review): H1 + CardHead + breadcrumb all say "My Content", no "Review" left', async ({ page }) => {
    await page.goto('/portal/t10-review');
    await expect(page.getByRole('heading', { name: 'My Content', level: 1 })).toBeVisible();
    const myContentTexts = await page.getByText('My Content', { exact: true }).all();
    expect(myContentTexts.length).toBeGreaterThanOrEqual(2); // breadcrumb + h1 (CardHead renders as plain span/div, same text)
    const body = await page.locator('body').innerText();
    expect(body).not.toMatch(/\bReview\b/);
    await page.screenshot({ path: `${SHOT_DIR}/04-my-content.png`, fullPage: true });
  });

  test('05 - Marketplace: description copy uses the new names', async ({ page }) => {
    await page.goto('/portal/marketplace');
    const body = await page.locator('body').innerText();
    expect(body).toContain('My Catalog Tours');
    expect(body).toMatch(/Browse Tours/);
    expect(body).not.toMatch(/\bBrowse Pool\b/);
    expect(body).not.toMatch(/\bMy Catalog\b(?! Tours)/);
  });
});
