import { test, expect, Page } from '@playwright/test';
import { adminUsername, adminPassword } from './helpers/auth';
import fs from 'fs';

// AA-660 — Jev is tracked like the LLM models. v2 page (S203 feedback): tabs Overview / Questions /
// Verdicts / Guide, filters + sort, explained terms. Waits for real data, not only a render.
const SHOT_DIR = 'tests/e2e/results/aa660';
fs.mkdirSync(SHOT_DIR, { recursive: true });

async function login(page: Page) {
  await page.goto('/login');
  await page.fill('input[type="text"]', adminUsername());
  await page.fill('input[type="password"]', adminPassword());
  await page.click('button:has-text("Login")');
  await page.waitForURL(/\/admin\/dashboard/, { timeout: 15000 });
}

test('Jev Decisions v2: overview stats, questions table, verdicts with filters, guide', async ({ page }) => {
  test.setTimeout(90000);
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.setViewportSize({ width: 1440, height: 900 });
  await login(page);
  const summary = page.waitForResponse(r => r.url().includes('/api/admin/decisions/summary'));
  await page.goto('/admin/decisions');
  expect((await summary).status()).toBe(200);

  // Overview
  await expect(page.getByText('How the answers fell')).toBeVisible({ timeout: 15000 });
  await expect(page.getByText('By stage', { exact: true })).toBeVisible();
  await page.selectOption('select >> nth=0', '30');
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${SHOT_DIR}/v2-overview.png`, fullPage: true });

  // Questions: table + expand a row
  await page.getByRole('button', { name: /^Questions \(/ }).click();
  await expect(page.getByText('a3_keyword_belongs').first()).toBeVisible();
  await page.getByText('a3_keyword_belongs').first().click();
  await expect(page.getByText('Question asked')).toBeVisible();
  await page.screenshot({ path: `${SHOT_DIR}/v2-questions.png`, fullPage: true });

  // Verdicts: filter by zone, paging text
  const log = page.waitForResponse(r => r.url().includes('/api/admin/decisions/log'));
  await page.getByRole('button', { name: 'Verdicts' }).click();
  expect((await log).status()).toBe(200);
  await expect(page.getByText(/of [\d,]+|0 answers/)).toBeVisible();
  await page.screenshot({ path: `${SHOT_DIR}/v2-verdicts.png`, fullPage: true });

  // Guide
  await page.getByRole('button', { name: 'Guide' }).click();
  await expect(page.getByText('How an answer becomes an action')).toBeVisible();
  await page.screenshot({ path: `${SHOT_DIR}/v2-guide.png`, fullPage: true });

  expect(errors).toEqual([]);
});

test('Settings > LLM Models lists Jev per stage', async ({ page }) => {
  await login(page);
  await page.goto('/admin/settings?tab=models');
  await expect(page.getByText('Jev decisions per stage')).toBeVisible({ timeout: 20000 });
  await expect(page.getByText('a3_keyword_belongs').first()).toBeVisible({ timeout: 15000 });
});
