import { test, expect } from '@playwright/test';
import { adminUsername, adminPassword } from './helpers/auth';
import fs from 'fs';
const SHOT_DIR = 'tests/e2e/results/aa572-prod';
fs.mkdirSync(SHOT_DIR, { recursive: true });

test.describe.configure({ mode: 'serial' });

async function waitLoaded(page: import('@playwright/test').Page) {
  await expect(page.getByText('Loading Content Trace…')).toHaveCount(0, { timeout: 15000 });
}

test.beforeEach(async ({ page }) => {
  await page.goto('/login');
  await page.fill('input[type="text"]', adminUsername());
  await page.fill('input[type="password"]', adminPassword());
  await page.click('button:has-text("Login")');
  await page.waitForTimeout(1500);
  await page.goto('/admin/tenant-activity');
  await waitLoaded(page);
});

// ── 1. English labels ────────────────────────────────────────────────────────────────────────

test('01 - Retry reason label is English, old Vietnamese label is gone', async ({ page }) => {
  const filteredResponse = page.waitForResponse(
    r => r.url().includes('/api/admin/a4/content-log') && r.url().includes('status=held') && r.status() === 200,
  );
  await page.locator('select').nth(2).selectOption({ label: 'Held' }); // Tenant, Channel, Status
  await filteredResponse;
  await waitLoaded(page);

  await page.locator('tbody tr').first().click();
  await page.waitForTimeout(500);

  const bodyText = await page.locator('body').innerText();
  expect(bodyText).toMatch(/Round \d+ — Retry reason/);
  expect(bodyText).not.toContain('Lý do yêu cầu viết lại');

  await page.screenshot({ path: `${SHOT_DIR}/01-english-retry-label.png`, fullPage: true });
});

// ── 2. Tenant stat header ────────────────────────────────────────────────────────────────────

test('02 - tenant stat header shows total pieces + real channel breakdown, updates with filters', async ({ page }) => {
  await expect(page.getByText('Total pieces', { exact: true })).toBeVisible();
  const rowCountAll = await page.locator('tbody tr').count();

  await page.screenshot({ path: `${SHOT_DIR}/02a-stat-header-all-tenants.png`, fullPage: false });

  // Narrow to a real tenant and confirm the stat header actually changes (not a static number).
  const tenantSelect = page.locator('select').first();
  await expect(tenantSelect.locator('option')).toHaveCount(5, { timeout: 10000 });
  const filteredResponse = page.waitForResponse(
    r => r.url().includes('/api/admin/a4/content-log') && r.url().includes('tenant_id=') && r.status() === 200,
  );
  await tenantSelect.selectOption({ label: 'WanderLux Travel' });
  await filteredResponse;
  await waitLoaded(page);

  const rowCountWanderlux = await page.locator('tbody tr').count();
  const totalTileValue = await page.locator('text=Total pieces').locator('..').locator('div').nth(1).innerText();
  expect(Number(totalTileValue)).toBe(rowCountWanderlux);
  expect(rowCountWanderlux).toBeLessThanOrEqual(rowCountAll);

  await page.screenshot({ path: `${SHOT_DIR}/02b-stat-header-wanderlux.png`, fullPage: false });
});

// ── 3. Sortable columns ──────────────────────────────────────────────────────────────────────

test('03 - Created at column sorts ascending/descending on click', async ({ page }) => {
  const dates = async () => page.locator('tbody tr td:nth-child(11)').allTextContents();

  const before = await dates();
  expect(before.length).toBeGreaterThan(1);

  await page.getByRole('columnheader', { name: /Created at/ }).click();
  await page.waitForTimeout(200);
  const firstClick = await dates();
  const firstClickTimes = firstClick.map(d => new Date(d).getTime());
  const isDescending = firstClickTimes.every((t, i) => i === 0 || t <= firstClickTimes[i - 1]);
  expect(isDescending).toBe(true); // first click defaults to desc (newest first)

  await page.getByRole('columnheader', { name: /Created at/ }).click();
  await page.waitForTimeout(200);
  const secondClick = await dates();
  const secondClickTimes = secondClick.map(d => new Date(d).getTime());
  const isAscending = secondClickTimes.every((t, i) => i === 0 || t >= secondClickTimes[i - 1]);
  expect(isAscending).toBe(true); // second click on the same header toggles to asc
  // The two directions must be genuine, different orderings — not a no-op click handler
  // (the unfiltered dataset spans multiple real days, so asc != desc is a meaningful check).
  expect(secondClick).not.toEqual(firstClick);
  expect(secondClick).not.toEqual(before);

  await page.screenshot({ path: `${SHOT_DIR}/03-sort-created-at.png`, fullPage: true });
});

test('04 - Gate count and Retry count columns are also sortable', async ({ page }) => {
  const gateCounts = async () => page.locator('tbody tr td:nth-child(8)').allTextContents();
  await page.getByRole('columnheader', { name: /Gate count/ }).click();
  await page.waitForTimeout(200);
  const sortedGate = (await gateCounts()).map(t => Number(t.split('/')[0]));
  const gateDescOrAsc =
    sortedGate.every((v, i) => i === 0 || v <= sortedGate[i - 1]) ||
    sortedGate.every((v, i) => i === 0 || v >= sortedGate[i - 1]);
  expect(gateDescOrAsc).toBe(true);

  const retryCounts = async () => page.locator('tbody tr td:nth-child(9)').allTextContents();
  await page.getByRole('columnheader', { name: /Retry count/ }).click();
  await page.waitForTimeout(200);
  const sortedRetry = (await retryCounts()).map(t => Number(t.trim().split(' ')[0]));
  const retryDescOrAsc =
    sortedRetry.every((v, i) => i === 0 || v <= sortedRetry[i - 1]) ||
    sortedRetry.every((v, i) => i === 0 || v >= sortedRetry[i - 1]);
  expect(retryDescOrAsc).toBe(true);

  await page.screenshot({ path: `${SHOT_DIR}/04-sort-gate-retry.png`, fullPage: true });
});

// ── 4. Content section no longer height-capped ──────────────────────────────────────────────

test('05 - Content block in accordion has no fixed max-height / inner scrollbar', async ({ page }) => {
  // Held pieces tend to have long content_text (multiple write attempts) — good stress case.
  const filteredResponse = page.waitForResponse(
    r => r.url().includes('/api/admin/a4/content-log') && r.url().includes('status=approved') && r.status() === 200,
  );
  await page.locator('select').nth(2).selectOption({ label: 'Approved' });
  await filteredResponse;
  await waitLoaded(page);

  await page.locator('tbody tr').first().click();
  await page.waitForTimeout(500);

  const contentLabel = page.getByText('Content', { exact: true });
  await expect(contentLabel).toBeVisible();
  // The content box is the sibling <div> right after the "Content" SectionLabel.
  const contentBox = contentLabel.locator('xpath=following-sibling::div[1]');
  await expect(contentBox).toBeVisible();

  const box = await contentBox.evaluate(el => {
    const s = getComputedStyle(el);
    return { maxHeight: s.maxHeight, overflowY: s.overflowY, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight };
  });
  expect(box.maxHeight).toBe('none');
  expect(box.overflowY).not.toBe('auto');
  // No internal clipping — the box's rendered height matches its full content.
  expect(box.scrollHeight).toBe(box.clientHeight);

  await page.screenshot({ path: `${SHOT_DIR}/05-content-no-maxheight.png`, fullPage: true });
});

// ── 5. Lineage split into separate lines ────────────────────────────────────────────────────

test('06 - Lineage renders Tour/Atom/(Segment or Route+Hub)/Slate each on its own line', async ({ page }) => {
  await page.locator('tbody tr').first().click();
  await page.waitForTimeout(500);

  const lineageLabel = page.getByText('Lineage', { exact: true });
  await expect(lineageLabel).toBeVisible();
  const lineageBlock = lineageLabel.locator('xpath=following-sibling::div[1]');
  const lineHtml = await lineageBlock.innerHTML();
  // Each entity is its own <div> child (a real separate line), not concatenated with " → ".
  expect(lineHtml).not.toContain('→');
  const lineTexts = await lineageBlock.locator('> div').allTextContents();
  expect(lineTexts.some(t => t.startsWith('Tour:'))).toBe(true);
  expect(lineTexts.some(t => t.startsWith('Slate:'))).toBe(true);
  expect(lineTexts.length).toBeGreaterThanOrEqual(2); // at least Tour + Slate lines

  await page.screenshot({ path: `${SHOT_DIR}/06-lineage-lines.png`, fullPage: true });
});

// ── 6. Sticky header ─────────────────────────────────────────────────────────────────────────

test('07 - table header stays visible (sticky) when the page is scrolled', async ({ page }) => {
  // Real, unfiltered dataset — enough rows to actually overflow the viewport.
  await page.setViewportSize({ width: 1400, height: 700 });
  await waitLoaded(page);

  const headerCell = page.getByRole('columnheader', { name: 'Topic' });
  const boxBefore = await headerCell.boundingBox();
  expect(boxBefore).not.toBeNull();

  // Scroll the page's real scroll container (not the window — this page scrolls an inner div).
  await page.evaluate(() => {
    const scroller = Array.from(document.querySelectorAll('div')).find(
      el => el.scrollHeight > el.clientHeight + 50 && getComputedStyle(el).overflowY === 'auto',
    );
    scroller?.scrollBy(0, 400);
  });
  await page.waitForTimeout(300);

  const boxAfter = await headerCell.boundingBox();
  expect(boxAfter).not.toBeNull();
  // Sticky: the header's Y position on screen barely moves (stays pinned near the scroll
  // container's top) even though the page scrolled — a non-sticky header would move up by
  // roughly the scroll delta (400px) or scroll off-screen entirely.
  expect(Math.abs((boxAfter!.y) - (boxBefore!.y))).toBeLessThan(20);
  await expect(headerCell).toBeVisible();

  await page.screenshot({ path: `${SHOT_DIR}/07-sticky-header-after-scroll.png`, fullPage: false });
});
