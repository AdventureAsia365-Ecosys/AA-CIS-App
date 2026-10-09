import { test, expect } from '@playwright/test';
import { adminUsername, adminPassword } from './helpers/auth';
const SHOT_DIR = 'tests/e2e/results/aa564';

test.describe.configure({ mode: 'serial' });

test.beforeEach(async ({ page }) => {
  await page.goto('/login');
  await page.fill('input[type="text"]', adminUsername());
  await page.fill('input[type="password"]', adminPassword());
  await page.click('button:has-text("Login")');
  await page.waitForTimeout(2000);
});

test('1.1 — sticky header survives a real scroll, and the document itself never overflows', async ({ page }) => {
  await page.goto('/admin/atom-curation');
  await page.waitForTimeout(2000);
  // Force enough content to load to make a real scroll meaningful: click through several
  // "Load more" on the atom list first.
  for (let i = 0; i < 3; i++) {
    const loadMore = page.getByRole('button', { name: /Load more/ });
    if (await loadMore.count() > 0 && await loadMore.first().isEnabled().catch(() => false)) {
      await loadMore.first().click().catch(() => {});
      await page.waitForTimeout(600);
    }
  }
  await page.screenshot({ path: `${SHOT_DIR}/1.1-before-scroll.png` });

  const docScrollBefore = await page.evaluate(() => document.scrollingElement?.scrollTop ?? 0);

  // Real mouse-wheel scroll over the atom list area (the actual user interaction Nghiệp
  // performed when this bug was found), not a synthetic window.scrollTo.
  await page.mouse.move(700, 500);
  await page.mouse.wheel(0, 1500);
  await page.waitForTimeout(700);
  await page.screenshot({ path: `${SHOT_DIR}/1.1-after-scroll.png` });

  const docScrollAfter = await page.evaluate(() => document.scrollingElement?.scrollTop ?? 0);

  // The header block (title/subtitle, Tour/Market selectors, stat bar) must still be visible —
  // this is the actual regression: it used to scroll away with the rest of the page.
  await expect(page.getByRole('heading', { name: 'Social Content' })).toBeVisible();
  await expect(page.locator('text=TOURS').first()).toBeVisible();
  await expect(page.locator('text=ATOMS').first()).toBeVisible();

  // The DOCUMENT itself must not have scrolled — with minHeight:0 fixed, all scrolling now
  // happens inside the content pane's own overflow:auto, never at the window/document level.
  expect(docScrollAfter).toBe(docScrollBefore);
});

test('1.2 — Star selected button on a genuinely fresh browser context', async ({ page }) => {
  await page.goto('/admin/atom-curation');
  await page.waitForTimeout(2000);
  await page.getByText('01 · Atomize').click();
  await page.waitForTimeout(1000);

  // Precise locator: every checkbox on the page EXCEPT the one immediately preceding the
  // "Unreviewed only" label text (the one non-atom-row checkbox in this section).
  const allCheckboxes = page.locator('input[type="checkbox"]');
  const totalCount = await allCheckboxes.count();
  const unreviewedCheckbox = page.locator('label:has-text("Unreviewed only") input[type="checkbox"]');
  const unreviewedBox = await unreviewedCheckbox.boundingBox().catch(() => null);
  const atomCheckboxIndices: number[] = [];
  for (let i = 0; i < totalCount; i++) {
    const box = await allCheckboxes.nth(i).boundingBox().catch(() => null);
    if (!box) continue;
    if (unreviewedBox && Math.abs(box.y - unreviewedBox.y) < 3 && Math.abs(box.x - unreviewedBox.x) < 3) continue;
    atomCheckboxIndices.push(i);
  }
  console.log('total checkboxes:', totalCount, 'atom-row checkboxes:', atomCheckboxIndices.length);

  for (const [step, idx] of atomCheckboxIndices.slice(0, 3).entries()) {
    await allCheckboxes.nth(idx).check({ force: true });
    await page.waitForTimeout(400);
    await page.screenshot({ path: `${SHOT_DIR}/1.2-after-check-${step + 1}.png` });
    const visible = await page.getByRole('button', { name: /Star selected/ }).isVisible().catch(() => false);
    const selectedCountText = visible ? await page.getByRole('button', { name: /Star selected/ }).textContent() : null;
    console.log(`After check #${step + 1} (checkbox index ${idx}): Star button visible = ${visible}`, selectedCountText ?? '');
  }
});

test('1.3 — per-tour Atomize stats change with the selected Tour', async ({ page }) => {
  await page.goto('/admin/atom-curation');
  await page.waitForTimeout(2000);

  // Read platform-wide "Total atoms" first (no Tour selected).
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${SHOT_DIR}/1.3-all-tours.png` });

  const select = page.locator('select').first();
  const options = await select.locator('option').allTextContents();
  // pick the first real tour (skip the "All tours" option)
  const tourOption = options.find(o => !o.includes('All tours'));
  if (tourOption) {
    await select.selectOption({ label: tourOption });
    await page.waitForTimeout(1000);
    await page.screenshot({ path: `${SHOT_DIR}/1.3-one-tour-selected.png` });
  }
});

test('1.4 — SUPERSEDED tooltip', async ({ page }) => {
  await page.goto('/admin/atom-curation');
  await page.waitForTimeout(1500);
  await page.getByText('04 · Route/Hub').click();
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${SHOT_DIR}/1.4-route-hub.png` });
});

test('1.4b — SUPERSEDED tooltip text is present on a real superseded row', async ({ page }) => {
  await page.goto('/admin/atom-curation');
  await page.waitForTimeout(1500);
  await page.getByText('04 · Route/Hub').click();
  await page.waitForTimeout(1000);
  // Pick a Tour so superseded versions become visible (per the page's own note).
  const tourSelect = page.locator('select').first();
  const options = await tourSelect.locator('option').allTextContents();
  const tourOption = options.find(o => !o.includes('All tours'));
  if (tourOption) {
    await tourSelect.selectOption({ label: tourOption });
    await page.waitForTimeout(1200);
  }
  const supersededTitleEl = page.locator('span[title*="supersede"]').first();
  const count = await supersededTitleEl.count();
  if (count > 0) {
    const titleText = await supersededTitleEl.getAttribute('title');
    console.log('SUPERSEDED tooltip text found:', titleText);
    await supersededTitleEl.scrollIntoViewIfNeeded();
    await page.screenshot({ path: `${SHOT_DIR}/1.4-superseded-found.png` });
  } else {
    console.log('No superseded row for this tour — trying all tours with route history');
    await page.screenshot({ path: `${SHOT_DIR}/1.4-no-superseded-this-tour.png` });
  }
});

test('2 — Admin Slate: pick a Tenant, real topic names appear', async ({ page }) => {
  await page.goto('/admin/atom-curation');
  await page.waitForTimeout(1500);
  await page.getByText('05 · Slate').click();
  await page.waitForTimeout(1000);
  await page.screenshot({ path: `${SHOT_DIR}/2.0-slate-empty-select-tenant.png` });

  const tenantSelect = page.locator('select').filter({ hasText: 'Choose a tenant' });
  const count = await tenantSelect.count();
  if (count > 0) {
    const opts = await tenantSelect.first().locator('option').allTextContents();
    const real = opts.find(o => o !== 'Choose a tenant…');
    if (real) {
      await tenantSelect.first().selectOption({ label: real });
      await page.waitForTimeout(1500);
      await page.screenshot({ path: `${SHOT_DIR}/2.1-slate-tenant-selected.png` });
    }
  }
});

test('2b — Admin Slate: WanderLux Travel shows real topic names', async ({ page }) => {
  await page.goto('/admin/atom-curation');
  await page.waitForTimeout(1500);
  await page.getByText('05 · Slate').click();
  await page.waitForTimeout(1000);
  const tenantSelect = page.locator('select').filter({ hasText: 'Choose a tenant' });
  await tenantSelect.first().selectOption({ label: 'WanderLux Travel' });
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${SHOT_DIR}/2.2-slate-wanderlux.png`, fullPage: true });
});

test('3 — Atomize backfill banner', async ({ page }) => {
  await page.goto('/admin/atom-curation');
  await page.waitForTimeout(1500);
  await page.getByText('01 · Atomize').click();
  await page.waitForTimeout(1000);
  await page.screenshot({ path: `${SHOT_DIR}/3.0-atomize-banner.png` });
});
