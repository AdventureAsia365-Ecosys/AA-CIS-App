// tests/e2e/aa562-content-trace-rename.spec.ts — AA-562 live-verify: "06-08 · Tenant Activity" ->
// "Content Trace" rename, sidebar + page title + all cross-link text, route unchanged.
import { test, expect } from '@playwright/test';
import { adminUsername, adminPassword } from './helpers/auth';
import fs from 'fs';

const SHOT_DIR = 'tests/e2e/results/aa562';
fs.mkdirSync(SHOT_DIR, { recursive: true });

async function loginAsAdmin(page) {
  await page.goto('/login');
  await page.fill('input[type="text"]', adminUsername());
  await page.fill('input[type="password"]', adminPassword());
  await page.click('button:has-text("Login")');
  await page.waitForTimeout(2500);
}

test('01 — Social Content sidebar shows "06-08 · Content Trace", not "Tenant Activity"', async ({ page }) => {
  await loginAsAdmin(page);
  await page.goto('/admin/atom-curation');
  await page.waitForSelector('text=Social Content', { timeout: 15000 });
  await page.waitForTimeout(800);

  const sidebarText = await page.locator('a[href="/admin/tenant-activity"]').first().innerText();
  console.log('SIDEBAR LINK TEXT:', sidebarText);
  expect(sidebarText).toContain('Content Trace');
  expect(sidebarText).not.toContain('Tenant Activity');

  await page.screenshot({ path: `${SHOT_DIR}/01-sidebar-content-trace.png`, fullPage: false });

  // Header description link at top of Social Content page also renamed.
  const headerLinkText = await page.locator('a[href="/admin/tenant-activity"]').nth(1).innerText().catch(() => null);
  console.log('HEADER DESC LINK TEXT (2nd occurrence, if any):', headerLinkText);
});

test('02 — Social Content header description line renamed', async ({ page }) => {
  await loginAsAdmin(page);
  await page.goto('/admin/atom-curation');
  await page.waitForSelector('text=Social Content', { timeout: 15000 });
  await page.waitForTimeout(800);
  await page.screenshot({ path: `${SHOT_DIR}/02-atom-curation-header.png`, fullPage: false, clip: { x: 0, y: 0, width: 1280, height: 220 } });

  const bodyText = await page.locator('body').innerText();
  expect(bodyText).toContain('Content Trace');
});

test('03 — /admin/tenant-activity page h1 renamed, route unchanged', async ({ page }) => {
  await loginAsAdmin(page);
  await page.goto('/admin/tenant-activity');
  await page.waitForSelector('h1', { timeout: 15000 });
  await page.waitForTimeout(800);

  expect(page.url()).toContain('/admin/tenant-activity');
  const h1Text = await page.locator('h1').first().innerText();
  console.log('H1 TEXT:', h1Text);
  expect(h1Text).toContain('Content Trace');
  expect(h1Text).not.toContain('Tenant Activity');

  await page.screenshot({ path: `${SHOT_DIR}/03-tenant-activity-h1.png`, fullPage: false });
});

test('04 — No leftover "Tenant Activity" text anywhere on either page', async ({ page }) => {
  await loginAsAdmin(page);

  await page.goto('/admin/atom-curation');
  await page.waitForSelector('text=Social Content', { timeout: 15000 });
  await page.waitForTimeout(800);
  const atomCurationBody = await page.locator('body').innerText();
  expect(atomCurationBody).not.toContain('Tenant Activity');

  await page.goto('/admin/tenant-activity');
  await page.waitForSelector('h1', { timeout: 15000 });
  await page.waitForTimeout(800);
  const tenantActivityBody = await page.locator('body').innerText();
  expect(tenantActivityBody).not.toContain('Tenant Activity');

  await page.screenshot({ path: `${SHOT_DIR}/04-tenant-activity-fullpage.png`, fullPage: true });
});

test('05 — Cross-links from a4-oversight and tenants pages also renamed', async ({ page }) => {
  await loginAsAdmin(page);

  await page.goto('/admin/a4-oversight');
  await page.waitForTimeout(1500);
  const oversightBody = await page.locator('body').innerText();
  expect(oversightBody).not.toContain('Tenant Activity');
  expect(oversightBody).toContain('Content Trace');
  await page.screenshot({ path: `${SHOT_DIR}/05-a4-oversight.png`, fullPage: true });

  await page.goto('/admin/tenants');
  await page.waitForTimeout(1500);
  const tenantsBody = await page.locator('body').innerText();
  expect(tenantsBody).not.toContain('Tenant Activity');
  await page.screenshot({ path: `${SHOT_DIR}/06-tenants-page.png`, fullPage: false });
});
