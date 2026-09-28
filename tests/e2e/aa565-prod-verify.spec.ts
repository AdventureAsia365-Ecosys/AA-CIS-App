import { test, expect } from '@playwright/test';
const SHOT_DIR = 'tests/e2e/results/aa565-prod';
const WANDERLUX_API_KEY = process.env.WANDERLUX_API_KEY || '';

test('PROD — My Catalog rebuild live on aa-cis.lumiguides.it.com', async ({ page }) => {
  test.skip(!WANDERLUX_API_KEY, 'WANDERLUX_API_KEY env var not set');
  await page.goto('https://aa-cis.lumiguides.it.com/tenant-login');
  await page.fill('input[type="password"]', WANDERLUX_API_KEY);
  await page.click('button:has-text("Access Portal")');
  await page.waitForURL('**/portal**', { timeout: 15000 });

  await page.goto('https://aa-cis.lumiguides.it.com/portal/t4-pool');
  await page.waitForSelector('table', { timeout: 20000 });
  await page.waitForTimeout(1000);
  await page.screenshot({ path: `${SHOT_DIR}/1-prod-table.png`, fullPage: true });

  const headers = await page.locator('table thead th').allTextContents();
  expect(headers.map(h => h.trim())).toEqual(['', 'Tour Name', 'Country', 'Actions']);

  const bodyText = await page.locator('body').innerText();
  for (const forbidden of ['Queued', 'Ready to Review', 'New Version Requested', 'AI Generated', 'AI Writing', 'Extra QA pass']) {
    expect(bodyText).not.toContain(forbidden);
  }

  await page.locator('table tbody tr button:has-text("Open")').first().click();
  await page.waitForTimeout(1200);
  await page.screenshot({ path: `${SHOT_DIR}/2-prod-drawer.png`, fullPage: true });

  await expect(page.locator('button:has-text("Request Rewrite")')).toBeVisible();
  await expect(page.locator('button:has-text("Add to Catalog")')).toHaveCount(0);
  await expect(page.locator('button:has-text("Save as New Version")')).toHaveCount(0);
  await expect(page.locator('text=Version History')).toHaveCount(0);
});
