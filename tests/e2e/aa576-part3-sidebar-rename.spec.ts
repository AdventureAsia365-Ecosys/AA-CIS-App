// tests/e2e/aa576-part3-sidebar-rename.spec.ts — AA-576 Phần 3 live-verify.
//
// Confirms the sidebar rename list Nghiệp + Claude Chat confirmed on the Linear issue landed
// exactly as agreed:
//   My Catalog -> My Catalog Tours, Review -> My Content, Browse Pool -> Browse Tours,
//   Social Content unchanged, "Write Content" removed from the menu entirely (but the
//   /portal/t8-angle-gate route itself must still work as a deep-link).
//
// Auth: real generate-key + real /tenant-login form (see memory
// reference_tenant_test_login_via_generate_key.md) — never a minted JWT.
import { test, expect } from '@playwright/test';
import { adminUsername, adminPassword } from './helpers/auth';
import fs from 'fs';

const SHOT_DIR = 'tests/e2e/results/aa576-part3';
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

  test('01 - sidebar shows exactly the 3 new labels, "Social Content" unchanged', async ({ page }) => {
    await page.goto('/portal/dashboard');
    const nav = page.locator('aside').first();
    await expect(nav.getByRole('link', { name: 'Browse Tours' })).toBeVisible();
    await expect(nav.getByRole('link', { name: 'Browse Tours' })).toHaveAttribute('href', '/portal/t1-rewrite');

    await expect(nav.getByRole('link', { name: 'My Catalog Tours' })).toBeVisible();
    await expect(nav.getByRole('link', { name: 'My Catalog Tours' })).toHaveAttribute('href', '/portal/t4-pool');

    await expect(nav.getByRole('link', { name: 'My Content' })).toBeVisible();
    await expect(nav.getByRole('link', { name: 'My Content' })).toHaveAttribute('href', '/portal/t10-review');

    await expect(nav.getByRole('link', { name: 'Social Content' })).toBeVisible();
    await expect(nav.getByRole('link', { name: 'Social Content' })).toHaveAttribute('href', '/portal/t7-planning');

    // Old labels must be gone.
    await expect(nav.getByRole('link', { name: 'Browse Pool', exact: true })).toHaveCount(0);
    await expect(nav.getByRole('link', { name: 'My Catalog', exact: true })).toHaveCount(0);
    await expect(nav.getByRole('link', { name: 'Review', exact: true })).toHaveCount(0);

    await page.screenshot({ path: `${SHOT_DIR}/01-sidebar-renamed.png`, fullPage: true });
  });

  test('02 - "Write Content" nav item is gone from the sidebar entirely', async ({ page }) => {
    await page.goto('/portal/dashboard');
    const nav = page.locator('aside').first();
    await expect(nav.getByRole('link', { name: 'Write Content' })).toHaveCount(0);
    // Sanity: nav still has exactly 9 Workspace items now (was 10 before removing one).
    const workspaceLinks = nav.locator('a');
    await expect(workspaceLinks).toHaveCount(12); // 9 Workspace + 3 Account (Activity/Billing/Settings)
  });

  test('03 - direct URL to /portal/t8-angle-gate still works (deep-link survives menu removal)', async ({ page }) => {
    await page.goto('/portal/t8-angle-gate');
    await expect(page.locator('body')).not.toContainText('404');
    // AngleGateWizard's own goal-selection step renders (requestId=null, standalone, fresh flow).
    await expect(page.getByText(/Choose a Goal|Pick a Subject to start/).first()).toBeVisible({ timeout: 10000 });
    await page.screenshot({ path: `${SHOT_DIR}/03-t8-angle-gate-deep-link-still-alive.png`, fullPage: true });
  });

  test('04 - My Content page (t10-review) still reachable via its new sidebar label', async ({ page }) => {
    await page.goto('/portal/dashboard');
    await page.locator('aside').first().getByRole('link', { name: 'My Content' }).click();
    await page.waitForURL('**/portal/t10-review');
    await expect(page.locator('body')).not.toContainText('404');
  });
});
