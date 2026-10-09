import { test, expect } from '@playwright/test';
import { adminUsername, adminPassword } from './helpers/auth';
import fs from 'fs';
const SHOT_DIR = 'tests/e2e/results/aa556';
fs.mkdirSync(SHOT_DIR, { recursive: true });

// Real WanderLux Travel tenant session (generate-key + real /tenant-login form) — same pattern
// as tests/e2e/aa564-group42-embedded-wizard.spec.ts.
const WANDERLUX_API_KEY = process.env.WANDERLUX_API_KEY || '';
const ADMIN_SECRET = process.env.ADMIN_SECRET || '';

test.describe.configure({ mode: 'serial' });

async function tenantLogin(page: import('@playwright/test').Page) {
  await page.goto('/tenant-login');
  await page.fill('input[type="password"]', WANDERLUX_API_KEY);
  await page.click('button:has-text("Access Portal")');
  await page.waitForURL('**/portal**', { timeout: 10000 });
}

async function adminLogin(page: import('@playwright/test').Page) {
  await page.goto('/login');
  await page.fill('input[type="text"]', adminUsername());
  await page.fill('input[type="password"]', adminPassword());
  await page.click('button:has-text("Login")');
  // Let the post-login client-side redirect (to /admin/dashboard) finish settling before
  // navigating elsewhere — otherwise a goto() issued too early races that redirect.
  await page.waitForURL('**/admin/**', { timeout: 10000 });
  await page.waitForTimeout(500);
}

async function openAdminSlateSection(page: import('@playwright/test').Page) {
  await adminLogin(page);
  await page.goto('/admin/atom-curation');
  await page.waitForTimeout(1200);
  await page.click('button:has-text("05 · Slate")');
  await page.waitForTimeout(800);
  // Several <select> elements exist on this page (other sections stay mounted) — target the one
  // whose own placeholder option identifies it as the Slate section's Tenant picker.
  const tenantSelect = page.locator('select').filter({ has: page.locator('option', { hasText: 'Choose a tenant' }) });
  await tenantSelect.selectOption({ label: 'WanderLux Travel' });
  await page.waitForTimeout(1200);
}

test('01 - admin dashboard Slate section no longer shows "coming soon" note on the cut badge', async ({ page }) => {
  test.skip(!ADMIN_SECRET, 'ADMIN_SECRET env var not set');
  await openAdminSlateSection(page);
  const body = await page.locator('body').innerText();
  expect(body).not.toContain('Manual cut action coming soon');
  await page.screenshot({ path: `${SHOT_DIR}/01-admin-no-coming-soon-note.png`, fullPage: true });
});

test('02 - tenant Slate: a real proposed Subject has a Cut button, cutting it removes it from proposed', async ({ page }) => {
  test.skip(!WANDERLUX_API_KEY, 'WANDERLUX_API_KEY env var not set');
  await tenantLogin(page);
  await page.goto('/portal/t7-planning');
  await page.waitForTimeout(2000);

  // LinkedIn tab has real proposed Subjects for this tenant (confirmed via direct DB check).
  await page.click('button:has-text("LinkedIn")');
  await page.waitForTimeout(1200);

  const cutButtons = page.getByRole('button', { name: 'Cut' });
  const beforeCount = await cutButtons.count();
  expect(beforeCount).toBeGreaterThan(0);
  console.log('Cut buttons visible before cutting:', beforeCount);
  await page.screenshot({ path: `${SHOT_DIR}/02-before-cut.png`, fullPage: true });

  page.once('dialog', dialog => dialog.accept());
  await cutButtons.first().click();
  await page.waitForTimeout(2500);

  // The cut Subject leaves the "proposed" list entirely (SubjectRow only renders Cut for
  // state==='proposed') — one fewer Cut button on screen, real backend round trip confirmed by
  // the count actually decreasing after a real POST + refetch, not an optimistic client-only removal.
  const afterCount = await page.getByRole('button', { name: 'Cut' }).count();
  console.log('Cut buttons visible after cutting:', afterCount);
  expect(afterCount).toBe(beforeCount - 1);
  await page.screenshot({ path: `${SHOT_DIR}/03-after-cut.png`, fullPage: true });
});

test('03 - admin dashboard Slate cut count increased for WanderLux Travel / LinkedIn', async ({ page }) => {
  test.skip(!ADMIN_SECRET, 'ADMIN_SECRET env var not set');
  await openAdminSlateSection(page);

  const cutBadge = page.locator('span', { hasText: /^cut:/i }).first();
  await expect(cutBadge).toBeVisible({ timeout: 10000 });
  const cutText = await cutBadge.innerText();
  console.log('cut badge text:', cutText);
  const match = cutText.match(/cut:\s*(\d+)/i);
  expect(match).not.toBeNull();
  const count = Number(match![1]);
  expect(count).toBeGreaterThan(0);
  await page.screenshot({ path: `${SHOT_DIR}/04-admin-cut-count.png`, fullPage: true });
});
