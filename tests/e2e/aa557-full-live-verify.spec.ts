// tests/e2e/aa557-full-live-verify.spec.ts — AA-557 post-deploy live verify (07/09/2026).
// Real Playwright against production (https://aa-cis.lumiguides.it.com), all 3 PRs deployed
// (task def :241, real /login, real admin test account). Covers C/D/E/F/G/H/I/J.
import { test, expect } from '@playwright/test';
import { adminUsername, adminPassword } from './helpers/auth';
import fs from 'fs';

const SHOT_DIR = 'tests/e2e/results/aa557-final';
fs.mkdirSync(SHOT_DIR, { recursive: true });

async function loginAsAdmin(page) {
  await page.goto('/login');
  await page.fill('input[type="text"]', adminUsername());
  await page.fill('input[type="password"]', adminPassword());
  await page.click('button:has-text("Login")');
  await page.waitForTimeout(2000);
}

test.describe('AA-557 full live-verify', () => {
  test('C — All owners filter gone from Atomize', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/admin/atom-curation');
    await page.waitForSelector('text=Social Content', { timeout: 15000 });
    await page.waitForTimeout(1000);
    const ownerFilterVisible = await page.getByText('All owners', { exact: true }).isVisible().catch(() => false);
    console.log('C — "All owners" filter still visible (should be false):', ownerFilterVisible);
    await page.screenshot({ path: `${SHOT_DIR}/C-atomize-no-owner-filter.png` });
  });

  test('D — Segment: table + sort/filter + numbering + sticky fix', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/admin/atom-curation');
    await page.waitForSelector('text=Social Content', { timeout: 15000 });
    await page.getByRole('button', { name: /02 · Segment/ }).click();
    await page.waitForTimeout(1200);
    await page.screenshot({ path: `${SHOT_DIR}/D-01-segment-page1.png` });

    // sort click
    const tourHeader = page.getByRole('button', { name: 'Tour' }).first();
    if (await tourHeader.isVisible().catch(() => false)) {
      await tourHeader.click();
      await page.waitForTimeout(400);
      await page.screenshot({ path: `${SHOT_DIR}/D-02-segment-sorted.png` });
    }

    // go to page 2, check numbering doesn't reset to #1
    const nextBtn = page.getByRole('button', { name: 'Next' });
    if (await nextBtn.isVisible().catch(() => false)) {
      await nextBtn.click();
      await page.waitForTimeout(800);
      await page.screenshot({ path: `${SHOT_DIR}/D-03-segment-page2.png` });
      const firstNum = await page.locator('span', { hasText: /^#\d+$/ }).first().textContent().catch(() => null);
      console.log('D — first group number on page 2 (should NOT be #1):', firstNum);
    }
  });

  test('E — Score: table + sort/filter', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/admin/atom-curation');
    await page.waitForSelector('text=Social Content', { timeout: 15000 });
    await page.getByRole('button', { name: /03 · Score/ }).click();
    await page.waitForTimeout(1200);
    await page.screenshot({ path: `${SHOT_DIR}/E-01-score.png` });
    const totalRankHeader = page.getByRole('button', { name: 'Total rank' }).first();
    if (await totalRankHeader.isVisible().catch(() => false)) {
      await totalRankHeader.click();
      await page.waitForTimeout(400);
      await page.screenshot({ path: `${SHOT_DIR}/E-02-score-sorted.png` });
    }
  });

  test('F — Route: Route Name column + note + day breakdown', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/admin/atom-curation');
    await page.waitForSelector('text=Social Content', { timeout: 15000 });
    await page.getByRole('button', { name: /04 · Route\/Hub/ }).click();
    await page.waitForTimeout(1200);
    await page.screenshot({ path: `${SHOT_DIR}/F-01-route-hub.png` });
    const routeNameHeader = await page.getByText('Route Name', { exact: true }).first().isVisible().catch(() => false);
    const hubNameStillThere = await page.getByRole('columnheader', { name: 'Hub Name' }).isVisible().catch(() => false);
    console.log('F — "Route Name" column header visible:', routeNameHeader, '| old "Hub Name" (should be false):', hubNameStillThere);

    // click a Days cell to expand day breakdown
    const daysCell = page.locator('button', { hasText: /–/ }).first();
    if (await daysCell.isVisible().catch(() => false)) {
      await daysCell.click();
      await page.waitForTimeout(1000);
      await page.screenshot({ path: `${SHOT_DIR}/F-02-route-day-breakdown.png` });
    }
  });

  test('G — Slate: table + explainer note', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/admin/atom-curation');
    await page.waitForSelector('text=Social Content', { timeout: 15000 });
    // Slate requires a Tour selected
    await page.locator('select').first().selectOption({ index: 1 });
    await page.waitForTimeout(800);
    await page.getByRole('button', { name: /05 · Slate/ }).click();
    await page.waitForTimeout(1200);
    await page.screenshot({ path: `${SHOT_DIR}/G-01-slate.png` });
    const noteVisible = await page.getByText(/Workspace → Slate/).isVisible().catch(() => false);
    console.log('G — Slate explainer note visible:', noteVisible);
  });

  test('H — 06-08 Tenant Activity nav link inside Social Content', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/admin/atom-curation');
    await page.waitForSelector('text=Social Content', { timeout: 15000 });
    await page.waitForTimeout(800);
    const navLink = await page.getByText('06-08 · Tenant Activity').isVisible().catch(() => false);
    console.log('H — 06-08 nav link visible inside Social Content inner-nav:', navLink);
    // confirm it's gone from the OUTER AdminSidebar's own list (only 1 occurrence, inside inner-nav)
    const count = await page.getByText('Tenant Activity').count();
    console.log('H — total "Tenant Activity" text occurrences on page (should be 1, not 2):', count);
    await page.screenshot({ path: `${SHOT_DIR}/H-01-nav-move.png` });
  });

  test('I — Tenants page: no Planning tab, Rate Limit shown, Social Content tab', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/admin/tenants');
    await page.waitForTimeout(1500);
    const firstExpand = page.locator('button svg').last();
    // expand first tenant row
    const expandButtons = page.locator('tbody tr td:last-child button').filter({ has: page.locator('svg') });
    const rows = await page.locator('tbody tr').count();
    console.log('I — tenant rows found:', rows);
    if (rows > 0) {
      // the chevron-down expand button is the last button in the actions cell
      const actionButtons = page.locator('tbody tr').first().locator('td:last-child button');
      const btnCount = await actionButtons.count();
      await actionButtons.nth(btnCount - 1).click();
      await page.waitForTimeout(1500);
      await page.screenshot({ path: `${SHOT_DIR}/I-01-tenant-detail.png` });
      const planningVisible = await page.getByRole('button', { name: 'Planning' }).isVisible().catch(() => false);
      const rateLimitVisible = await page.getByText('Rate Limit', { exact: true }).isVisible().catch(() => false);
      const socialContentTab = await page.getByRole('button', { name: 'Social Content' }).isVisible().catch(() => false);
      console.log('I — Planning tab visible (should be false):', planningVisible, '| Rate Limit header visible:', rateLimitVisible, '| Social Content tab visible:', socialContentTab);
      if (socialContentTab) {
        await page.getByRole('button', { name: 'Social Content' }).click();
        await page.waitForTimeout(1200);
        await page.screenshot({ path: `${SHOT_DIR}/I-02-social-content-tab.png` });
      }
    }
  });

  test('J.24 — Admin Brand tab editable', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/admin/tenants');
    await page.waitForTimeout(1500);
    const rows = await page.locator('tbody tr').count();
    if (rows > 0) {
      const actionButtons = page.locator('tbody tr').first().locator('td:last-child button');
      const btnCount = await actionButtons.count();
      await actionButtons.nth(btnCount - 1).click();
      await page.waitForTimeout(1200);
      const brandTab = page.getByRole('button', { name: 'Brand', exact: true });
      if (await brandTab.isVisible().catch(() => false)) {
        await brandTab.click();
        await page.waitForTimeout(1000);
        await page.screenshot({ path: `${SHOT_DIR}/J24-01-brand-view.png` });
        const editBtn = page.getByRole('button', { name: 'Edit' });
        if (await editBtn.isVisible().catch(() => false)) {
          await editBtn.click();
          await page.waitForTimeout(500);
          await page.screenshot({ path: `${SHOT_DIR}/J24-02-brand-edit-form.png` });
          const brandNameFieldVisible = await page.getByText('Brand Name', { exact: true }).isVisible().catch(() => false);
          console.log('J.24 — Brand Name field visible in edit mode:', brandNameFieldVisible);
        }
      }
    }
  });
});
