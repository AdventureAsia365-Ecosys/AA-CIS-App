import { test } from '@playwright/test';
import { adminUsername, adminPassword } from './helpers/auth';
import fs from 'fs';
const SHOT_DIR = 'tests/e2e/results/aa561-final';
fs.mkdirSync(SHOT_DIR, { recursive: true });

test('06 review tab screenshot', async ({ page }) => {
  await page.goto('/login');
  await page.fill('input[type="text"]', adminUsername());
  await page.fill('input[type="password"]', adminPassword());
  await page.click('button:has-text("Login")');
  await page.waitForTimeout(2000);
  await page.goto('/admin/tenant-activity');
  await page.waitForTimeout(1200);
  const tourSelect = page.locator('select').first();
  await tourSelect.selectOption({ label: 'Route A: Tokyo Bay' });
  await page.waitForTimeout(1200);
  await page.getByText('07 · Review', { exact: true }).click();
  await page.waitForTimeout(1200);
  await page.screenshot({ path: `${SHOT_DIR}/06-review-source-goal.png`, fullPage: true });
});
