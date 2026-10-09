import { test, expect } from '@playwright/test';
import { adminUsername, adminPassword } from './helpers/auth';
import fs from 'fs';
const SHOT_DIR = 'tests/e2e/results/aa568';
fs.mkdirSync(SHOT_DIR, { recursive: true });

test.describe.configure({ mode: 'serial' });

// Waits for the "Loading Content Trace…" spinner to clear rather than a fixed timeout — real
// prod network latency (real API Gateway + ECS round trip) is slower and more variable than the
// local dev stack this suite was first written against, so a fixed timeout that passed locally
// flaked here.
async function waitLoaded(page: import('@playwright/test').Page) {
  await expect(page.getByText('Loading Content Trace…')).toHaveCount(0, { timeout: 15000 });
}

test.beforeEach(async ({ page }) => {
  await page.goto('/login');
  await page.fill('input[type="text"]', adminUsername());
  await page.fill('input[type="password"]', adminPassword());
  await page.click('button:has-text("Login")');
  await page.waitForTimeout(1500);
});

test('01 - sidebar: Content Trace sits as an equal peer of 01-05, not a demoted link', async ({ page }) => {
  await page.goto('/admin/atom-curation');
  await page.waitForTimeout(1200);
  const innerNav = page.locator('.a527-inner-sidebar, div:has(> button:has-text("01 · Atomize"))').first();
  await expect(page.getByText('06 · Content Trace', { exact: true })).toBeVisible();
  // The old "06-08" label must be gone.
  await expect(page.getByText('06-08 · Content Trace')).toHaveCount(0);
  await innerNav.screenshot({ path: `${SHOT_DIR}/01-sidebar-peer.png` });
});

test('02 - main table renders real rows with the required columns', async ({ page }) => {
  await page.goto('/admin/tenant-activity');
  await expect(page.getByRole('heading', { name: '06 · Content Trace' })).toBeVisible();
  await waitLoaded(page);
  const thead = page.locator('thead');
  for (const col of ['Topic', 'Tenant', 'Tour', 'Channel', 'Angle chosen', 'Status', 'Gate count', 'Retry count', 'Published', 'Created at']) {
    await expect(thead.getByText(col, { exact: true })).toBeVisible();
  }
  await expect(page.locator('tbody tr').first()).toBeVisible();
  await page.screenshot({ path: `${SHOT_DIR}/02-main-table.png`, fullPage: true });
});

test('03 - tenant filter narrows the table', async ({ page }) => {
  await page.goto('/admin/tenant-activity');
  await waitLoaded(page);
  const rowsBefore = await page.locator('tbody tr').count();

  const tenantSelect = page.locator('select').first();
  // Wait for the real tenant list to load (not just the default "All tenants" option) before
  // selecting, then wait for the filtered content-log response specifically (not the initial
  // unfiltered one, which is still in flight when this test starts).
  await expect(tenantSelect.locator('option')).toHaveCount(5, { timeout: 10000 });
  const filteredResponse = page.waitForResponse(
    r => r.url().includes('/api/admin/a4/content-log') && r.url().includes('tenant_id=') && r.status() === 200,
  );
  await tenantSelect.selectOption({ label: 'WanderLux Travel' });
  await filteredResponse;
  await waitLoaded(page);

  const rowsAfterTenant = await page.locator('tbody tr').count();
  expect(rowsAfterTenant).toBeGreaterThan(0);
  // Every visible row's Tenant cell must now read WanderLux Travel (no other tenant leaking through).
  const tenantCells = await page.locator('tbody tr td:nth-child(3)').allTextContents();
  expect(tenantCells.length).toBeGreaterThan(0);
  for (const t of tenantCells) expect(t.trim()).toBe('WanderLux Travel');

  await page.screenshot({ path: `${SHOT_DIR}/03-tenant-filter.png`, fullPage: true });
  console.log(`rows before filter: ${rowsBefore}, after WanderLux filter: ${rowsAfterTenant}`);
});

test('04 - status filter narrows to Held only', async ({ page }) => {
  await page.goto('/admin/tenant-activity');
  await waitLoaded(page);

  const statusSelect = page.locator('select').nth(2); // Tenant, Channel, Status
  const filteredResponse = page.waitForResponse(
    r => r.url().includes('/api/admin/a4/content-log') && r.url().includes('status=held') && r.status() === 200,
  );
  await statusSelect.selectOption({ label: 'Held' });
  await filteredResponse;
  await waitLoaded(page);

  const statusCells = await page.locator('tbody tr td:nth-child(7)').allTextContents();
  expect(statusCells.length).toBeGreaterThan(0);
  for (const s of statusCells) expect(s.trim().toLowerCase()).toBe('held');

  await page.screenshot({ path: `${SHOT_DIR}/04-status-filter-held.png`, fullPage: true });
});

test('05 - click a row: accordion shows lineage, all 3 angles (chosen marked), content, gate ledger, and honest retry labeling', async ({ page }) => {
  await page.goto('/admin/tenant-activity');
  await waitLoaded(page);

  // Narrow to Held — those rows are the ones with real repair_log entries (retries).
  const filteredResponse = page.waitForResponse(
    r => r.url().includes('/api/admin/a4/content-log') && r.url().includes('status=held') && r.status() === 200,
  );
  await page.locator('select').nth(2).selectOption({ label: 'Held' });
  await filteredResponse;
  await waitLoaded(page);

  const firstRow = page.locator('tbody tr').first();
  await firstRow.click();
  await page.waitForTimeout(600);

  await expect(page.getByText('Lineage', { exact: true })).toBeVisible();
  await expect(page.getByText(/Angles generated \(\d+\)/)).toBeVisible();
  await expect(page.getByText('chosen', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('Content', { exact: true })).toBeVisible();
  await expect(page.getByText(/Gate ledger \(\d+\/\d+ passed\)/)).toBeVisible();
  await expect(page.getByText(/Retry history \(\d+\)/)).toBeVisible();

  // The retry section must use the honest "Retry reason" label (AA-572 translated this from the
  // original Vietnamese "Lý do yêu cầu viết lại"), never a fabricated "content changed" / diff
  // framing (STEP0's confirmed constraint).
  const retryText = await page.locator('body').innerText();
  expect(retryText).toContain('Retry reason');
  expect(retryText).not.toContain('Lý do yêu cầu viết lại');
  expect(retryText.toLowerCase()).not.toContain('content diff');
  expect(retryText.toLowerCase()).not.toContain('nội dung đã đổi');

  await expect(page.getByText('Publish', { exact: true })).toBeVisible();

  await page.screenshot({ path: `${SHOT_DIR}/05-accordion-expanded.png`, fullPage: true });
});

test('06 - published filter + old sub-tab labels (Write/Gate, Review, Publish) are gone', async ({ page }) => {
  await page.goto('/admin/tenant-activity');
  await waitLoaded(page);

  await expect(page.getByText('Write/Gate', { exact: false })).toHaveCount(0);
  await expect(page.getByText('07 · Review', { exact: true })).toHaveCount(0);
  await expect(page.getByText('08 · Publish', { exact: true })).toHaveCount(0);

  const publishedSelect = page.locator('select').nth(3); // Tenant, Channel, Status, Published
  const publishedResponse = page.waitForResponse(
    r => r.url().includes('/api/admin/a4/content-log') && r.url().includes('published=yes') && r.status() === 200,
  );
  await publishedSelect.selectOption({ label: 'Published' });
  await publishedResponse;
  await waitLoaded(page);
  // Real data today has 0 genuinely published pieces (T11's own known gap — see AA-458 LIVE
  // STATE) — an honest empty state here, not stale unfiltered rows, is the correct proof.
  await expect(page.getByText('No content pieces match these filters')).toBeVisible();
  await page.screenshot({ path: `${SHOT_DIR}/06-published-filter-and-old-tabs-gone.png`, fullPage: true });
});
