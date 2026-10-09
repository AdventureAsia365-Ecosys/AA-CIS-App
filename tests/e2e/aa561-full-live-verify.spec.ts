// tests/e2e/aa561-full-live-verify.spec.ts — AA-561 post-deploy live verify, real production.
// Captures the real, current-build screenshots for every changed section, to attach to the
// Linear issue as required (not just described in text).
import { test, expect } from '@playwright/test';
import { adminUsername, adminPassword } from './helpers/auth';
import fs from 'fs';

const SHOT_DIR = 'tests/e2e/results/aa561-final';
fs.mkdirSync(SHOT_DIR, { recursive: true });

async function loginAsAdmin(page) {
  await page.goto('/login');
  await page.fill('input[type="text"]', adminUsername());
  await page.fill('input[type="password"]', adminPassword());
  await page.click('button:has-text("Login")');
  await page.waitForTimeout(2500);
}

test('01 — A/B re-confirm on the deployed build (post AA-561 merge)', async ({ page }) => {
  await loginAsAdmin(page);
  await page.goto('/admin/atom-curation');
  await page.waitForSelector('text=Social Content', { timeout: 15000 });
  await page.waitForTimeout(1000);
  await page.screenshot({ path: `${SHOT_DIR}/01-A-before-scroll.png`, fullPage: false });

  const scrollInfo = await page.evaluate(() => {
    const divs = Array.from(document.querySelectorAll('div'));
    const scrollable = divs.find(d => {
      const cs = getComputedStyle(d);
      return cs.overflowY === 'auto' && d.scrollHeight > d.clientHeight + 50;
    });
    if (!scrollable) return { found: false };
    scrollable.scrollTop = 2000;
    return { found: true, scrollTop: scrollable.scrollTop };
  });
  console.log('SCROLL:', JSON.stringify(scrollInfo));
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${SHOT_DIR}/01-A-after-scroll.png`, fullPage: false });

  const checkboxes = page.locator('input[type="checkbox"][title="Select for bulk star"]');
  await checkboxes.nth(0).check();
  await checkboxes.nth(1).check();
  await checkboxes.nth(2).check();
  await page.waitForTimeout(300);
  await page.screenshot({ path: `${SHOT_DIR}/01-B-star-selected.png`, fullPage: false });
});

test('02 — Segment section: single merged table', async ({ page }) => {
  await loginAsAdmin(page);
  await page.goto('/admin/atom-curation');
  await page.waitForSelector('text=Social Content', { timeout: 15000 });
  await page.getByRole('button', { name: /Segment/ }).first().click();
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${SHOT_DIR}/02-segment-table.png`, fullPage: true });
});

test('03 — Slate section: plain-language intro', async ({ page }) => {
  await loginAsAdmin(page);
  await page.goto('/admin/atom-curation');
  await page.waitForSelector('text=Social Content', { timeout: 15000 });
  // pick first tour so Slate has a tour_id (still per-tenant/required)
  const tourSelect = page.locator('select').first();
  const options = await tourSelect.locator('option').allTextContents();
  if (options.length > 1) await tourSelect.selectOption({ index: 1 });
  await page.waitForTimeout(800);
  await page.getByRole('button', { name: /Slate/ }).first().click();
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${SHOT_DIR}/03-slate-explainer.png`, fullPage: true });
});

test('04 — Tenant Activity: Write/Gate lineage card expanded + Review Source/Goal columns', async ({ page }) => {
  await loginAsAdmin(page);
  await page.goto('/admin/tenant-activity');
  await page.waitForTimeout(1500);
  const tourSelect = page.locator('select').first();
  const options = await tourSelect.locator('option').allTextContents();
  console.log('tour options:', options.length);
  if (options.length > 1) await tourSelect.selectOption({ index: 1 });
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${SHOT_DIR}/04-write-gate-list.png`, fullPage: true });

  // expand the first piece card if present
  const expandBtn = page.getByRole('button', { name: /Show \d+ angle/ }).first();
  if (await expandBtn.isVisible().catch(() => false)) {
    await expandBtn.click();
    await page.waitForTimeout(500);
    await page.screenshot({ path: `${SHOT_DIR}/05-write-gate-expanded.png`, fullPage: true });
  } else {
    console.log('No expandable piece card found for this tour — trying other tours');
    for (let i = 2; i < Math.min(options.length, 8); i++) {
      await tourSelect.selectOption({ index: i });
      await page.waitForTimeout(1200);
      const btn = page.getByRole('button', { name: /Show \d+ angle/ }).first();
      if (await btn.isVisible().catch(() => false)) {
        await btn.click();
        await page.waitForTimeout(500);
        await page.screenshot({ path: `${SHOT_DIR}/05-write-gate-expanded.png`, fullPage: true });
        break;
      }
    }
  }

  // Review tab
  const reviewTab = page.getByRole('button', { name: /Review/ }).first();
  if (await reviewTab.isVisible().catch(() => false)) {
    await reviewTab.click();
    await page.waitForTimeout(1000);
    await page.screenshot({ path: `${SHOT_DIR}/06-review-source-goal.png`, fullPage: true });
  }
});
