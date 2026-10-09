import { test, expect } from '@playwright/test';
import { adminUsername, adminPassword } from './helpers/auth';
import fs from 'fs';
const SHOT_DIR = 'tests/e2e/results/aa575';
fs.mkdirSync(SHOT_DIR, { recursive: true });

test.describe.configure({ mode: 'serial' });

async function waitLoaded(page: import('@playwright/test').Page) {
  await expect(page.getByText('Loading Content Trace…')).toHaveCount(0, { timeout: 15000 });
}

// The sub-nav item text ("06 · Content Trace") also appears verbatim in Content Trace's own <h1>
// heading — scope to the actual nav link/button (a/button element) to avoid a strict-mode
// ambiguity, not because the heading match is wrong.
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

// ── Part 1 — sub-nav 01-06 persists across every page in the group ────────────────────────────

test('01 - Social Content shows sub-nav 01-06 with 01 active by default', async ({ page }) => {
  await page.goto('/admin/atom-curation');
  await expect(subNavItem(page, '01 · Atomize')).toBeVisible();
  for (const label of ['02 · Segment', '03 · Score', '04 · Route/Hub', '05 · Slate', '06 · Content Trace']) {
    await expect(subNavItem(page, label)).toBeVisible();
  }
  await page.screenshot({ path: `${SHOT_DIR}/01-atomcuration-subnav.png`, fullPage: false });
});

test('02 - clicking 02-05 switches tabs in place (no navigation) with correct item highlighted', async ({ page }) => {
  await page.goto('/admin/atom-curation');
  const urlBefore = page.url();

  await subNavItem(page, '03 · Score').click();
  await page.waitForTimeout(300);
  expect(page.url()).toBe(urlBefore); // same-page tab switch, not a navigation
  await expect(page.getByText('Score ranks Segments', { exact: false })).toBeVisible();
  const scoreBtn = page.locator('button', { hasText: '03 · Score' });
  await expect(scoreBtn).toHaveCSS('font-weight', '700');

  await subNavItem(page, '02 · Segment').click();
  await page.waitForTimeout(300);
  expect(page.url()).toBe(urlBefore);
  const segmentBtn = page.locator('button', { hasText: '02 · Segment' });
  await expect(segmentBtn).toHaveCSS('font-weight', '700');
  await expect(scoreBtn).not.toHaveCSS('font-weight', '700');

  await page.screenshot({ path: `${SHOT_DIR}/02-tab-switch-no-nav.png`, fullPage: false });
});

test('03 - clicking 06 · Content Trace navigates there AND the sub-nav is still visible with 06 active', async ({ page }) => {
  await page.goto('/admin/atom-curation');
  await subNavItem(page, '06 · Content Trace').click();
  await page.waitForURL('**/admin/tenant-activity');
  await waitLoaded(page);

  // The bug: sub-nav used to vanish entirely on this page.
  for (const label of ['01 · Atomize', '02 · Segment', '03 · Score', '04 · Route/Hub', '05 · Slate', '06 · Content Trace']) {
    await expect(subNavItem(page, label)).toBeVisible();
  }
  const contentTraceLink = page.locator('a', { hasText: '06 · Content Trace' });
  await expect(contentTraceLink).toHaveCSS('font-weight', '700');
  const atomizeLink = page.locator('a', { hasText: '01 · Atomize' });
  await expect(atomizeLink).not.toHaveCSS('font-weight', '700');

  await page.screenshot({ path: `${SHOT_DIR}/03-content-trace-subnav-visible.png`, fullPage: false });
});

test('04 - direct URL to /admin/tenant-activity also shows the sub-nav (not just via click)', async ({ page }) => {
  await page.goto('/admin/tenant-activity');
  await waitLoaded(page);
  for (const label of ['01 · Atomize', '02 · Segment', '03 · Score', '04 · Route/Hub', '05 · Slate', '06 · Content Trace']) {
    await expect(subNavItem(page, label)).toBeVisible();
  }
  await page.screenshot({ path: `${SHOT_DIR}/04-direct-url-subnav-visible.png`, fullPage: false });
});

test('05 - from Content Trace, clicking a non-default section (03 · Score) navigates back and opens that real tab', async ({ page }) => {
  await page.goto('/admin/tenant-activity');
  await waitLoaded(page);
  await subNavItem(page, '03 · Score').click();
  await page.waitForURL('**/admin/atom-curation?section=score');
  // Proves the ?section= query param actually drives the initial tab, not just a default landing.
  await expect(page.getByText('Score ranks Segments', { exact: false })).toBeVisible();
  const scoreBtn = page.locator('button', { hasText: '03 · Score' });
  await expect(scoreBtn).toHaveCSS('font-weight', '700');
  await page.screenshot({ path: `${SHOT_DIR}/05-deep-link-section-score.png`, fullPage: false });
});

// ── Part 2 — sort now works on every column, not just Gate/Retry/Created at ───────────────────

function isMonotonic(values: number[]): boolean {
  const asc = values.every((v, i) => i === 0 || v >= values[i - 1]);
  const desc = values.every((v, i) => i === 0 || v <= values[i - 1]);
  return asc || desc;
}

async function checkStringColumnSorts(page: import('@playwright/test').Page, headerName: string, cellSelector: string) {
  const before = await page.locator(cellSelector).allTextContents();
  expect(before.length).toBeGreaterThan(1);

  await page.getByRole('columnheader', { name: new RegExp(headerName) }).click();
  await page.waitForTimeout(200);
  const first = await page.locator(cellSelector).allTextContents();
  const firstMonotonicAsc = first.every((v, i) => i === 0 || v.localeCompare(first[i - 1]) >= 0);
  const firstMonotonicDesc = first.every((v, i) => i === 0 || v.localeCompare(first[i - 1]) <= 0);
  expect(firstMonotonicAsc || firstMonotonicDesc).toBe(true);

  await page.getByRole('columnheader', { name: new RegExp(headerName) }).click();
  await page.waitForTimeout(200);
  const second = await page.locator(cellSelector).allTextContents();
  const secondMonotonicAsc = second.every((v, i) => i === 0 || v.localeCompare(second[i - 1]) >= 0);
  const secondMonotonicDesc = second.every((v, i) => i === 0 || v.localeCompare(second[i - 1]) <= 0);
  expect(secondMonotonicAsc || secondMonotonicDesc).toBe(true);

  // Two clicks toggle direction — must be genuinely different orderings, but only when the
  // column actually has more than one distinct value in the current real data (e.g. Tenant can
  // legitimately be all "WanderLux Travel" if that's the only tenant with rows right now — asc
  // and desc are both correct and identical on a single-value column, not a no-op handler).
  if (new Set(before).size > 1) {
    expect(second).not.toEqual(first);
  }
}

test('06 - Topic column sorts on click (previously no reaction at all)', async ({ page }) => {
  await page.goto('/admin/tenant-activity');
  await waitLoaded(page);
  await checkStringColumnSorts(page, 'Topic', 'tbody tr td:nth-child(2)');
  await page.screenshot({ path: `${SHOT_DIR}/06-sort-topic.png`, fullPage: true });
});

test('07 - Tenant column sorts on click', async ({ page }) => {
  await page.goto('/admin/tenant-activity');
  await waitLoaded(page);
  await checkStringColumnSorts(page, 'Tenant', 'tbody tr td:nth-child(3)');
  await page.screenshot({ path: `${SHOT_DIR}/07-sort-tenant.png`, fullPage: true });
});

test('08 - Tour column sorts on click', async ({ page }) => {
  await page.goto('/admin/tenant-activity');
  await waitLoaded(page);
  await checkStringColumnSorts(page, 'Tour', 'tbody tr td:nth-child(4)');
  await page.screenshot({ path: `${SHOT_DIR}/08-sort-tour.png`, fullPage: true });
});

test('09 - Channel column sorts on click', async ({ page }) => {
  await page.goto('/admin/tenant-activity');
  await waitLoaded(page);
  await checkStringColumnSorts(page, 'Channel', 'tbody tr td:nth-child(5)');
  await page.screenshot({ path: `${SHOT_DIR}/09-sort-channel.png`, fullPage: true });
});

test('10 - Angle chosen column sorts on click', async ({ page }) => {
  await page.goto('/admin/tenant-activity');
  await waitLoaded(page);
  await checkStringColumnSorts(page, 'Angle chosen', 'tbody tr td:nth-child(6)');
  await page.screenshot({ path: `${SHOT_DIR}/10-sort-angle.png`, fullPage: true });
});

test('11 - Status column sorts on click', async ({ page }) => {
  await page.goto('/admin/tenant-activity');
  await waitLoaded(page);
  await checkStringColumnSorts(page, 'Status', 'tbody tr td:nth-child(7)');
  await page.screenshot({ path: `${SHOT_DIR}/11-sort-status.png`, fullPage: true });
});

test('12 - Published column sorts on click (categorical: No < Not yet < Yes)', async ({ page }) => {
  await page.goto('/admin/tenant-activity');
  await waitLoaded(page);

  const rank = (t: string) => (t.startsWith('Not yet') ? 1 : t.startsWith('Yes') ? 2 : 0);
  const cellSelector = 'tbody tr td:nth-child(10)';

  const before = await page.locator(cellSelector).allTextContents();
  expect(before.length).toBeGreaterThan(1);

  await page.getByRole('columnheader', { name: /Published/ }).click();
  await page.waitForTimeout(200);
  const first = (await page.locator(cellSelector).allTextContents()).map(rank);
  expect(isMonotonic(first)).toBe(true);

  await page.getByRole('columnheader', { name: /Published/ }).click();
  await page.waitForTimeout(200);
  const second = (await page.locator(cellSelector).allTextContents()).map(rank);
  expect(isMonotonic(second)).toBe(true);
  expect(second).not.toEqual(first);

  await page.screenshot({ path: `${SHOT_DIR}/12-sort-published.png`, fullPage: true });
});

test('13 - regression: Gate count/Retry count/Created at (AA-572) still sort correctly', async ({ page }) => {
  await page.goto('/admin/tenant-activity');
  await waitLoaded(page);

  const gateCounts = async () => page.locator('tbody tr td:nth-child(8)').allTextContents();
  await page.getByRole('columnheader', { name: /Gate count/ }).click();
  await page.waitForTimeout(200);
  expect(isMonotonic((await gateCounts()).map(t => Number(t.split('/')[0])))).toBe(true);

  const retryCounts = async () => page.locator('tbody tr td:nth-child(9)').allTextContents();
  await page.getByRole('columnheader', { name: /Retry count/ }).click();
  await page.waitForTimeout(200);
  expect(isMonotonic((await retryCounts()).map(t => Number(t.trim().split(' ')[0])))).toBe(true);

  const dates = async () => page.locator('tbody tr td:nth-child(11)').allTextContents();
  await page.getByRole('columnheader', { name: /Created at/ }).click();
  await page.waitForTimeout(200);
  expect(isMonotonic((await dates()).map(d => new Date(d).getTime()))).toBe(true);

  await page.screenshot({ path: `${SHOT_DIR}/13-regression-old-3-sorts.png`, fullPage: true });
});
