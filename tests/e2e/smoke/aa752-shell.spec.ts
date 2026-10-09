// tests/e2e/smoke/aa752-shell.spec.ts
// AA-752 — smoke for the shared admin shell. Runs in the "smoke" project (admin storageState from
// smoke-setup). Every wait is bounded. Checks:
//   (a) the sidebar logo links to /admin/overview;
//   (b) the collapse toggle narrows the rail and the collapsed state survives a reload;
//   (c) ⌘K opens the palette and typing 2+ chars of a REAL tour name (read from the Master Content
//       table on the page, not hardcoded) returns ≥ 1 tour row;
//   (d) at 390px the bottom nav is visible and the page has no horizontal overflow.
import { test, expect } from '@playwright/test';
import { ADMIN_STATE, baseUrl, installBypassRoute } from './smoke-helpers';

async function adminPage(browser: import('@playwright/test').Browser) {
  const context = await browser.newContext({
    baseURL: baseUrl(),
    storageState: ADMIN_STATE,
    viewport: { width: 1440, height: 900 },
  });
  await installBypassRoute(context);
  const page = await context.newPage();
  return { context, page };
}

test.describe('AA-752 — admin shell', () => {
  test('(a) sidebar logo links to /admin/overview', async ({ browser }) => {
    const { context, page } = await adminPage(browser);
    try {
      await page.goto('/admin/jobs', { waitUntil: 'domcontentloaded' });
      const logo = page.getByRole('button', { name: /Adventure Asia CIS Admin — go to Overview/i });
      await expect(logo).toBeVisible({ timeout: 10000 });
      await logo.click();
      await page.waitForURL(/\/admin\/overview\b/, { timeout: 15000 });
      expect(new URL(page.url()).pathname).toBe('/admin/overview');
    } finally {
      await context.close();
    }
  });

  test('(b) collapse toggles the rail and survives reload', async ({ browser }) => {
    const { context, page } = await adminPage(browser);
    try {
      await page.goto('/admin/jobs', { waitUntil: 'domcontentloaded' });
      const rail = page.locator('.aa-shell-sidebar');
      await expect(rail).toBeVisible({ timeout: 10000 });

      const expandedWidth = await rail.evaluate((el) => el.getBoundingClientRect().width);
      await page.getByRole('button', { name: /^Collapse sidebar$/i }).click();
      // Wait (bounded) for the width transition to land on the 68px rail.
      await expect
        .poll(async () => rail.evaluate((el) => Math.round(el.getBoundingClientRect().width)), { timeout: 5000 })
        .toBeLessThan(expandedWidth - 50);

      // Survives a reload (localStorage cis_sidebar + the no-flash restore script).
      await page.reload({ waitUntil: 'domcontentloaded' });
      await expect(rail).toBeVisible({ timeout: 10000 });
      const widthAfterReload = await rail.evaluate((el) => Math.round(el.getBoundingClientRect().width));
      expect(widthAfterReload).toBeLessThan(expandedWidth - 50);
    } finally {
      await context.close();
    }
  });

  test('(c) ⌘K palette returns a real tour', async ({ browser }) => {
    const { context, page } = await adminPage(browser);
    try {
      await page.goto('/admin/master-content', { waitUntil: 'domcontentloaded' });
      // Read a real tour name from the Master Content table (first non-empty cell with letters).
      const name = await page.waitForFunction(() => {
        const cells = Array.from(document.querySelectorAll('.aa-admin-main table tbody tr td'));
        for (const c of cells) {
          const t = (c.textContent ?? '').trim();
          if (t.length >= 4 && /[A-Za-z]/.test(t)) return t;
        }
        return null;
      }, undefined, { timeout: 15000 }).then((h) => h.jsonValue() as Promise<string>).catch(() => '');

      // A fragment of ≥ 2 chars from the real name. Prefer a word with letters.
      const frag = (name.match(/[A-Za-z]{2,}/)?.[0] ?? name).slice(0, 4);
      expect(frag.length, 'could not read a real tour name from the Master Content table').toBeGreaterThanOrEqual(2);

      // Open the palette with the keyboard shortcut.
      await page.keyboard.press('ControlOrMeta+KeyK');
      const input = page.getByRole('dialog', { name: /Global search/i }).getByRole('textbox');
      await expect(input).toBeVisible({ timeout: 5000 });
      await input.fill(frag);

      // A tour row (its deep link href carries ?tour=) appears for the real name. Bounded.
      const tourRow = page.getByRole('dialog', { name: /Global search/i }).locator('button', { hasText: new RegExp(frag, 'i') });
      await expect(tourRow.first()).toBeVisible({ timeout: 10000 });
    } finally {
      await context.close();
    }
  });

  test('(d) phone: bottom nav visible and no horizontal overflow at 390px', async ({ browser }) => {
    const { context, page } = await adminPage(browser);
    try {
      await page.goto('/admin/jobs', { waitUntil: 'domcontentloaded' });
      await page.setViewportSize({ width: 390, height: 844 });
      await page.waitForTimeout(300);

      await expect(page.locator('.aa-bottom-nav')).toBeVisible({ timeout: 5000 });

      const metrics = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        innerWidth: window.innerWidth,
      }));
      expect(
        metrics.scrollWidth,
        `page-level horizontal overflow at 390px (scrollWidth ${metrics.scrollWidth} > innerWidth ${metrics.innerWidth})`,
      ).toBeLessThanOrEqual(metrics.innerWidth + 1);
    } finally {
      await context.close();
    }
  });
});
