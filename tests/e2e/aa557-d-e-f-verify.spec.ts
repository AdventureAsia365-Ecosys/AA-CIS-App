import { test } from '@playwright/test';
import { adminUsername, adminPassword } from './helpers/auth';
import fs from 'fs';
const SHOT_DIR = 'tests/e2e/results/aa557';
fs.mkdirSync(SHOT_DIR, { recursive: true });

async function loginAsAdmin(page) {
  await page.goto('/login');
  await page.fill('input[type="text"]', adminUsername());
  await page.fill('input[type="password"]', adminPassword());
  await page.click('button:has-text("Login")');
  await page.waitForTimeout(2000);
}

test('D.3 pre-fix — Segment sticky filter overlap check', async ({ page }) => {
  await loginAsAdmin(page);
  await page.goto('/admin/atom-curation');
  await page.waitForSelector('text=Social Content', { timeout: 15000 });
  await page.getByRole('button', { name: /02 · Segment/ }).click();
  await page.waitForTimeout(1200);
  await page.screenshot({ path: `${SHOT_DIR}/D-00-segment-before-scroll.png` });
  await page.mouse.move(1050, 500);
  await page.mouse.wheel(0, 400);
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${SHOT_DIR}/D-00-segment-after-scroll-PREFIX.png` });
});
