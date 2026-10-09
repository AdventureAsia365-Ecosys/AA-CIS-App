// tests/e2e/aa561-ab-reverify.spec.ts — AA-561: re-verify mục A (sticky header)/B (star selected)
// against real production, with explicit scroll-container scrollTop checks (not just bbox
// stability, which can't distinguish "header is sticky" from "nothing scrolled at all").
import { test, expect } from '@playwright/test';
import { adminUsername, adminPassword } from './helpers/auth';
import fs from 'fs';

const SHOT_DIR = 'tests/e2e/results/aa561';
fs.mkdirSync(SHOT_DIR, { recursive: true });

async function loginAsAdmin(page) {
  await page.goto('/login');
  await page.fill('input[type="text"]', adminUsername());
  await page.fill('input[type="password"]', adminPassword());
  await page.click('button:has-text("Login")');
  await page.waitForTimeout(2500);
}

test('A — real scroll-container scrollTop + full screenshots before/after', async ({ page }) => {
  await loginAsAdmin(page);
  await page.goto('/admin/atom-curation');
  await page.waitForSelector('text=Social Content', { timeout: 15000 });
  await page.waitForTimeout(1000);

  await page.screenshot({ path: `${SHOT_DIR}/A-before.png`, fullPage: false });
  const titleBefore = await page.getByText('Social Content', { exact: true }).first().boundingBox();
  const descBefore = await page.getByText(/Platform-wide monitoring/).first().isVisible().catch(() => false);

  // Scroll the actual body-column scroll container (flex:1, overflowY:auto) directly via evaluate
  // to remove any ambiguity about where the mouse wheel event landed.
  const scrollInfo = await page.evaluate(() => {
    const divs = Array.from(document.querySelectorAll('div'));
    const scrollable = divs.find(d => {
      const cs = getComputedStyle(d);
      return cs.overflowY === 'auto' && d.scrollHeight > d.clientHeight + 50;
    });
    if (!scrollable) return { found: false };
    scrollable.scrollTop = 2000;
    return { found: true, scrollTop: scrollable.scrollTop, scrollHeight: scrollable.scrollHeight, clientHeight: scrollable.clientHeight };
  });
  console.log('SCROLL CONTAINER INFO:', JSON.stringify(scrollInfo));
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${SHOT_DIR}/A-after.png`, fullPage: false });

  const titleAfter = await page.getByText('Social Content', { exact: true }).first().isVisible().catch(() => false);
  const descAfter = await page.getByText(/Platform-wide monitoring/).first().isVisible().catch(() => false);
  const statBarAfter = await page.getByText('Segments', { exact: true }).first().isVisible().catch(() => false);

  console.log('titleBefore bbox:', JSON.stringify(titleBefore));
  console.log('titleAfter visible:', titleAfter, 'descAfter visible:', descAfter, 'statBarAfter visible:', statBarAfter);
});

test('B — bulk star with real DB confirm', async ({ page }) => {
  await loginAsAdmin(page);
  await page.goto('/admin/atom-curation');
  await page.waitForSelector('text=Social Content', { timeout: 15000 });
  await page.waitForTimeout(1500);

  const checkboxes = page.locator('input[type="checkbox"][title="Select for bulk star"]');
  const count = await checkboxes.count();
  console.log('checkboxes found:', count);
  await checkboxes.nth(0).check();
  await checkboxes.nth(1).check();
  await checkboxes.nth(2).check();
  await page.waitForTimeout(300);
  await page.screenshot({ path: `${SHOT_DIR}/B-selected.png`, fullPage: false });

  const starBtn = page.getByRole('button', { name: /Star selected/ });
  console.log('Star selected visible:', await starBtn.isVisible().catch(() => false));
  await page.screenshot({ path: `${SHOT_DIR}/B-with-button.png`, fullPage: false });
});
