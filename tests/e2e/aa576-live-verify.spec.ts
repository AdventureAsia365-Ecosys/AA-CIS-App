// tests/e2e/aa576-live-verify.spec.ts — AA-576 live-verify.
//
// Phần 1: every /portal/* route renders with zero leftover Vietnamese UI text (broad
// diacritic sweep) + the specific SlateTab.tsx strings fixed this task read correctly in
// English (weekly/on-demand tab tooltip, eligible-count line, empty-state copy, bar-reason
// text, confirm dialog, Pick/Cut button labels) — using WanderLux Travel's REAL Slate data
// (blog: decided-only/no-new-eligible; linkedin: real proposed subjects with needs_said>0;
// email: on-demand proposed subjects), no synthetic setup needed.
//
// Phần 2: /portal/t8-angle-gate is confirmed NOT dead code (Sidebar.tsx NAV1 "Write Content"
// still links to it) — this test clicks that real sidebar link and confirms the route still
// resolves (not 404), proving nothing was deleted, per the STEP0 finding.
//
// Auth: real generate-key + real /tenant-login form (see memory
// reference_tenant_test_login_via_generate_key.md) — never a minted JWT.
import { test, expect } from '@playwright/test';
import { adminUsername, adminPassword } from './helpers/auth';
import fs from 'fs';

const SHOT_DIR = 'tests/e2e/results/aa576';
fs.mkdirSync(SHOT_DIR, { recursive: true });

const VIETNAMESE_DIACRITICS = /[àáâãèéêìíòóôõùúýăđĩũơưẠ-ỹ]/;

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

  // Persist the authenticated context's storage state for every later test in this file.
  await context.storageState({ path: `${SHOT_DIR}/tenant-auth.json` });
  await context.close();
});

test.describe('with tenant session', () => {
  test.use({ storageState: `${SHOT_DIR}/tenant-auth.json` });

  const ROUTES = [
    '/portal/dashboard', '/portal/t1-rewrite', '/portal/t4-pool', '/portal/t0-brand',
    '/portal/t7-planning', '/portal/t8-angle-gate', '/portal/t10-review',
    '/portal/t11-publish', '/portal/marketplace', '/portal/api',
    '/portal/activity', '/portal/billing', '/portal/settings',
  ];

  for (const route of ROUTES) {
    test(`01 - ${route} renders with zero Vietnamese diacritics in visible text`, async ({ page }) => {
      await page.goto(route);
      await page.waitForLoadState('networkidle').catch(() => {});
      await page.waitForTimeout(600); // let client fetches paint (Slate/Catalog/etc.)
      const bodyText = await page.locator('body').innerText();
      const match = bodyText.match(VIETNAMESE_DIACRITICS);
      if (match) {
        const idx = bodyText.indexOf(match[0]);
        console.log(`[AA-576] Vietnamese char found on ${route}: "${bodyText.slice(Math.max(0, idx - 60), idx + 60)}"`);
      }
      expect(match, `${route} should have no leftover Vietnamese UI text`).toBeNull();
    });
  }

  test('02 - Social Content (Slate) tab tooltips are English', async ({ page }) => {
    await page.goto('/portal/t7-planning');
    await expect(page.locator('.aa511-slate-tabstrip')).toBeVisible({ timeout: 10000 });
    await expect(page.getByRole('button', { name: /Blog/ })).toHaveAttribute('title', 'Weekly rhythm');
    await expect(page.getByRole('button', { name: /Email/ })).toHaveAttribute('title', 'On-demand');
  });

  test('03 - Blog tab (decided-only, no new eligible) shows English empty-state copy', async ({ page }) => {
    await page.goto('/portal/t7-planning');
    await page.getByRole('button', { name: /Blog/ }).click();
    await expect(page.getByText('No new eligible Subjects right now')).toBeVisible({ timeout: 10000 });
    await expect(page.getByText(/Review the Subjects already decided below/)).toBeVisible();
    await page.screenshot({ path: `${SHOT_DIR}/03-blog-empty-state.png`, fullPage: true });
  });

  test('04 - LinkedIn tab: eligible-count line + real proposed subject bar-reason text', async ({ page }) => {
    await page.goto('/portal/t7-planning');
    await page.getByRole('button', { name: /LinkedIn/ }).click();
    await expect(page.getByText(/eligible subjects? · target rhythm/)).toBeVisible({ timeout: 10000 });
    await expect(page.getByText(/chars ≥/).first()).toBeVisible();
    await expect(page.getByRole('button', { name: /Pick to write/ }).first()).toBeVisible();
    await page.screenshot({ path: `${SHOT_DIR}/04-linkedin-proposed.png`, fullPage: true });
  });

  test('05 - Cut confirm dialog is English (dismissed, no real mutation)', async ({ page }) => {
    await page.goto('/portal/t7-planning');
    await page.getByRole('button', { name: /LinkedIn/ }).click();
    await expect(page.getByRole('button', { name: /Pick to write/ }).first()).toBeVisible({ timeout: 10000 });

    let dialogText = '';
    page.once('dialog', async dialog => {
      dialogText = dialog.message();
      await dialog.dismiss(); // never confirm — this must NOT actually cut the real subject
    });
    await page.getByRole('button', { name: 'Cut' }).first().click();
    await page.waitForTimeout(300);
    expect(dialogText).toBe('Cut this proposal? This cannot be undone.');
  });

  test('06 - Email tab (on-demand): "written on demand" line + "no threshold applies" reason', async ({ page }) => {
    await page.goto('/portal/t7-planning');
    await page.getByRole('button', { name: /Email/ }).click();
    await expect(page.getByText(/written on demand/)).toBeVisible({ timeout: 10000 });
    await expect(page.getByText('On demand — no threshold applies').first()).toBeVisible();
    await page.screenshot({ path: `${SHOT_DIR}/06-email-on-demand.png`, fullPage: true });
  });

  // Test 07 ("Write Content" sidebar link still resolves /portal/t8-angle-gate) removed here —
  // Phần 3 (Nghiệp's confirmed decision, same Linear issue) later removed that nav item from the
  // sidebar entirely (redundant since AA-564 embeds the wizard inline in Social Content). The
  // underlying claim this test protected — the route itself still works as a deep-link even
  // without a menu entry — is now covered by tests/e2e/aa576-part3-sidebar-rename.spec.ts
  // (test 02 confirms the link is gone, test 03 confirms the bare URL still renders).
});
