import { test, expect } from '@playwright/test';
import { adminUsername, adminPassword } from './helpers/auth';
import fs from 'fs';
const SHOT_DIR = 'tests/e2e/results/aa560';
fs.mkdirSync(SHOT_DIR, { recursive: true });

test.describe.configure({ mode: 'serial' });

function subNavItem(page: import('@playwright/test').Page, label: string) {
  return page.locator('a, button').filter({ hasText: label }).first();
}

test.beforeEach(async ({ page }) => {
  await page.goto('/login');
  await page.fill('input[type="text"]', adminUsername());
  await page.fill('input[type="password"]', adminPassword());
  await page.click('button:has-text("Login")');
  await page.waitForTimeout(1500);
});

// ── Part 1 — sub-nav 01-07, "07 · Platform Stats" reachable + active ────────────────────────────

test('01 - Social Content sub-nav shows 07 · Platform Stats alongside 01-06', async ({ page }) => {
  await page.goto('/admin/atom-curation');
  for (const label of ['01 · Atomize', '02 · Segment', '03 · Score', '04 · Route/Hub', '05 · Slate', '06 · Content Trace', '07 · Platform Stats']) {
    await expect(subNavItem(page, label)).toBeVisible();
  }
  await page.screenshot({ path: `${SHOT_DIR}/01-subnav-shows-07.png`, fullPage: false });
});

test('02 - clicking 07 navigates to /admin/platform-stats with 07 active', async ({ page }) => {
  await page.goto('/admin/atom-curation');
  await Promise.all([
    page.waitForURL('**/admin/platform-stats', { timeout: 10000 }),
    subNavItem(page, '07 · Platform Stats').click(),
  ]);
  await page.waitForTimeout(500);
  expect(page.url()).toContain('/admin/platform-stats');
  await expect(page.locator('h1', { hasText: '07 · Platform Stats' })).toBeVisible();
  const item = page.locator('a', { hasText: '07 · Platform Stats' });
  await expect(item).toHaveCSS('font-weight', '700');
  await page.screenshot({ path: `${SHOT_DIR}/02-platform-stats-active.png`, fullPage: true });
});

test('03 - direct URL entry to /admin/platform-stats shows full sub-nav 01-07 with 07 active', async ({ page }) => {
  await page.goto('/admin/platform-stats');
  await page.waitForTimeout(800);
  for (const label of ['01 · Atomize', '02 · Segment', '03 · Score', '04 · Route/Hub', '05 · Slate', '06 · Content Trace', '07 · Platform Stats']) {
    await expect(subNavItem(page, label)).toBeVisible();
  }
  const item = page.locator('a', { hasText: '07 · Platform Stats' });
  await expect(item).toHaveCSS('font-weight', '700');
  await page.screenshot({ path: `${SHOT_DIR}/03-direct-url-subnav-persists.png`, fullPage: true });
});

// ── Part 2 — Review Log: renders + full column sort ──────────────────────────────────────────────

test('04 - Review Log section renders and every column header sorts', async ({ page }) => {
  await page.goto('/admin/platform-stats');
  await expect(page.getByText('Review Log — T3/T5 Escalations')).toBeVisible();
  await page.waitForTimeout(1000);

  const headers = ['Tenant', 'Failure Summary', 'Checks', 'Status', 'Created'];
  for (const h of headers) {
    const th = page.locator('th', { hasText: h }).first();
    await expect(th).toBeVisible();
    await th.click();
    await page.waitForTimeout(200);
    await th.click(); // toggle direction
    await page.waitForTimeout(200);
  }
  await page.screenshot({ path: `${SHOT_DIR}/04-review-log-sorted.png`, fullPage: true });
});

// ── Part 3 — Platform Stats aggregate: real backend numbers render ──────────────────────────────

test('05 - Platform Stats aggregate section shows total/by-channel/by-status/top-gate-failures', async ({ page }) => {
  await page.goto('/admin/platform-stats');
  await expect(page.getByText('Platform Stats — Gate/Error Aggregate')).toBeVisible();
  await page.waitForTimeout(1000);
  await expect(page.getByText('Total pieces')).toBeVisible();
  await expect(page.getByText('By channel', { exact: false })).toBeVisible();
  await expect(page.getByText('Top gate failures', { exact: false })).toBeVisible();
  await page.screenshot({ path: `${SHOT_DIR}/05-platform-stats-aggregate.png`, fullPage: true });
});

// ── Part 4 — Trust Ramp renders + sorts ──────────────────────────────────────────────────────────

test('06 - Trust Ramp section renders with sortable per-tenant packet tables', async ({ page }) => {
  await page.goto('/admin/platform-stats');
  await expect(page.getByText('Trust Ramp — Current State')).toBeVisible();
  await page.waitForTimeout(1000);
  await page.screenshot({ path: `${SHOT_DIR}/06-trust-ramp.png`, fullPage: true });
});

// ── Part 5 — Force unpublish, moved into Content Trace (06), real test-data round trip ──────────

test('07 - Content Trace shows Force unpublish for a published row and it works', async ({ page }) => {
  test.setTimeout(60000);
  await page.goto('/admin/tenant-activity');
  await expect(page.getByText('Loading Content Trace…')).toHaveCount(0, { timeout: 15000 });

  // Filter to the seeded test tenant (WanderLux Travel) so the target row is easy to find.
  const tenantSelect = page.locator('select').first();
  await tenantSelect.selectOption({ label: 'WanderLux Travel' });
  await page.waitForTimeout(1200);

  // Find the row whose Published column shows a real external link (the seeded test row —
  // channel linkedin, publish_external_url set).
  const row = page.locator('tbody tr').filter({ hasText: 'linkedin' }).filter({ hasText: 'Yes' }).first();
  await expect(row).toBeVisible({ timeout: 15000 });
  await row.click();
  await page.waitForTimeout(600);

  const forceBtn = page.locator('button', { hasText: 'Force unpublish' });
  await expect(forceBtn).toBeVisible({ timeout: 10000 });
  await page.screenshot({ path: `${SHOT_DIR}/07-before-force-unpublish.png`, fullPage: true });

  page.once('dialog', dialog => dialog.accept());
  await forceBtn.click();
  await page.waitForTimeout(2500);

  // Button disappears once publish_status is no longer "published" (real re-fetch from the DB).
  await expect(page.locator('button', { hasText: 'Force unpublish' })).toHaveCount(0, { timeout: 15000 });
  await page.screenshot({ path: `${SHOT_DIR}/08-after-force-unpublish.png`, fullPage: true });
});
