import { test, expect, Page } from '@playwright/test';
import fs from 'fs';

// AA-660 — Jev is tracked like the LLM models: the Decisions page shows real questions/verdicts/cost,
// Settings > LLM Models lists Jev per stage.
const SHOT_DIR = 'tests/e2e/results/aa660';
fs.mkdirSync(SHOT_DIR, { recursive: true });

async function login(page: Page) {
  await page.goto('/login');
  await page.fill('input[type="text"]', 'e2e-test-admin');
  await page.fill('input[type="password"]', 'e2eTest2026!');
  await page.click('button:has-text("Login")');
  await page.waitForURL(/\/admin\/dashboard/, { timeout: 15000 });
}

test('Jev Decisions page shows real questions, verdicts and cost', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  await login(page);
  const summary = page.waitForResponse(r => r.url().includes('/api/admin/decisions/summary'));
  await page.goto('/admin/decisions?days=30');
  expect((await summary).status()).toBe(200);
  await expect(page.getByText('Questions by stage')).toBeVisible({ timeout: 15000 });
  await expect(page.getByText('a3_keyword_belongs').first()).toBeVisible();
  await expect(page.getByText('a3_idea_traveller').first()).toBeVisible();
  await page.selectOption('select >> nth=0', '30');
  await expect(page.getByText('Recent verdicts')).toBeVisible();
  await page.screenshot({ path: `${SHOT_DIR}/decisions.png`, fullPage: true });
  expect(errors).toEqual([]);
});

test('Settings > LLM Models lists Jev per stage', async ({ page }) => {
  await login(page);
  await page.goto('/admin/settings?tab=models');
  await expect(page.getByText('Jev decisions per stage')).toBeVisible({ timeout: 20000 });
  await expect(page.getByText('a3_keyword_belongs').first()).toBeVisible({ timeout: 15000 });
  await page.getByText('Jev decisions per stage').scrollIntoViewIfNeeded();
  await page.screenshot({ path: `${SHOT_DIR}/settings-jev.png` });
});
