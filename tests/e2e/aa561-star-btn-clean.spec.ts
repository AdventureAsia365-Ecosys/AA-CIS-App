import { test } from '@playwright/test';
import fs from 'fs';
const SHOT_DIR = 'tests/e2e/results/aa561-final';
fs.mkdirSync(SHOT_DIR, { recursive: true });

test('clean star-selected button screenshot', async ({ page }) => {
  await page.goto('/login');
  await page.fill('input[type="text"]', 'e2e-test-admin');
  await page.fill('input[type="password"]', 'e2eTest2026!');
  await page.click('button:has-text("Login")');
  await page.waitForTimeout(2000);
  await page.goto('/admin/atom-curation');
  await page.waitForTimeout(1500);

  const checkboxes = page.locator('input[type="checkbox"][title="Select for bulk star"]');
  await checkboxes.nth(0).check({ force: true });
  await checkboxes.nth(1).check({ force: true });
  await checkboxes.nth(2).check({ force: true });
  await page.evaluate(() => window.scrollTo(0, 0));
  const scrollable = await page.evaluateHandle(() => {
    const divs = Array.from(document.querySelectorAll('div'));
    return divs.find(d => getComputedStyle(d).overflowY === 'auto' && d.scrollHeight > d.clientHeight + 50);
  });
  await page.evaluate((el) => { if (el) (el as HTMLElement).scrollTop = 0; }, scrollable);
  await page.waitForTimeout(400);
  await page.screenshot({ path: `${SHOT_DIR}/01-B-star-selected-clean.png`, fullPage: false });
});
