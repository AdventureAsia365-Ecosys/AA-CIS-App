// tests/e2e/smoke/aa752-shell.spec.ts
// AA-752 — smoke for the shared admin shell. Runs in the "smoke" project (admin storageState from
// smoke-setup). Every wait is bounded. Checks:
//   (a) the sidebar logo links to /admin/overview;
//   (b) the collapse toggle narrows the rail and the collapsed state survives a reload;
//   (c) ⌘K opens the palette and typing 2+ chars of a REAL tour name (read from the Master Content
//       table on the page, not hardcoded) returns ≥ 1 tour row;
//   (d) at 390px the bottom nav is visible and the page has no horizontal overflow (prints the
//       offending elements on failure).
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
      const logo = page.getByTestId('sidebar-logo');
      await expect(logo).toBeVisible({ timeout: 10000 });
      // The logo is a client-component button whose onClick (router.push) only works AFTER React
      // hydrates. Clicking right after domcontentloaded can land before hydration, so the click is
      // a no-op and waitForURL times out (the flake this test had). Wait for a bounded hydration
      // signal — the page's own <main className="aa-admin-main"> mounts only once the client page
      // component has rendered — then retry the click within a bound in case it still raced.
      await expect(page.locator('.aa-admin-main')).toBeVisible({ timeout: 15000 });
      await expect
        .poll(
          async () => {
            if (new URL(page.url()).pathname === '/admin/overview') return '/admin/overview';
            await logo.click();
            // Give the client navigation a short, bounded moment to update the URL before re-reading.
            await page.waitForURL(/\/admin\/overview\b/, { timeout: 3000 }).catch(() => {});
            return new URL(page.url()).pathname;
          },
          { timeout: 15000, intervals: [500, 1000, 1500] },
        )
        .toBe('/admin/overview');
      expect(new URL(page.url()).pathname).toBe('/admin/overview');
    } finally {
      await context.close();
    }
  });

  test('(b) collapse toggles the rail and survives reload', async ({ browser }) => {
    const { context, page } = await adminPage(browser);
    try {
      await page.goto('/admin/jobs', { waitUntil: 'domcontentloaded' });
      const rail = page.getByTestId('admin-sidebar');
      await expect(rail).toBeVisible({ timeout: 10000 });

      const expandedWidth = await rail.evaluate((el) => el.getBoundingClientRect().width);
      await page.getByTestId('sidebar-collapse').click();
      // Bounded wait for the width transition to land on the collapsed rail.
      await expect
        .poll(async () => rail.evaluate((el) => Math.round(el.getBoundingClientRect().width)), { timeout: 5000 })
        .toBeLessThan(expandedWidth - 50);
      // data-collapsed flips too.
      await expect(rail).toHaveAttribute('data-collapsed', 'true', { timeout: 2000 });

      // Survives a reload (localStorage cis_sidebar + the no-flash restore script).
      await page.reload({ waitUntil: 'domcontentloaded' });
      await expect(rail).toBeVisible({ timeout: 10000 });
      await expect(rail).toHaveAttribute('data-collapsed', 'true', { timeout: 5000 });
      // Poll the width (the 0.18s transition can be mid-flight on a single read).
      await expect
        .poll(async () => rail.evaluate((el) => Math.round(el.getBoundingClientRect().width)), { timeout: 5000 })
        .toBeLessThan(expandedWidth - 50);
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

      const frag = (name.match(/[A-Za-z]{2,}/)?.[0] ?? name).slice(0, 4);
      expect(frag.length, 'could not read a real tour name from the Master Content table').toBeGreaterThanOrEqual(2);

      await page.keyboard.press('ControlOrMeta+KeyK');
      const dialog = page.getByRole('dialog', { name: /Global search/i });
      const input = dialog.getByRole('textbox');
      await expect(input).toBeVisible({ timeout: 5000 });
      await input.fill(frag);

      // A tour row (its deep link href carries ?tour=) appears for the real name. Bounded.
      const tourRow = dialog.locator('button', { hasText: new RegExp(frag, 'i') });
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

      const metrics = await page.evaluate(() => {
        const innerWidth = window.innerWidth;
        const scrollable = (el: HTMLElement): boolean => {
          let node: HTMLElement | null = el.parentElement;
          while (node && node !== document.documentElement) {
            const ox = getComputedStyle(node).overflowX;
            if (ox === 'auto' || ox === 'hidden' || ox === 'scroll') return true;
            node = node.parentElement;
          }
          return false;
        };
        const offenders: { tag: string; cls: string; width: number; right: number }[] = [];
        for (const el of Array.from(document.body.querySelectorAll<HTMLElement>('*'))) {
          const r = el.getBoundingClientRect();
          if (r.right <= innerWidth + 1 || r.width === 0 || r.height === 0) continue;
          if (scrollable(el)) continue;
          offenders.push({
            tag: el.tagName.toLowerCase(),
            cls: (typeof el.className === 'string' ? el.className : '').slice(0, 48),
            width: Math.round(r.width),
            right: Math.round(r.right),
          });
        }
        offenders.sort((a, b) => b.right - a.right);
        return { scrollWidth: document.documentElement.scrollWidth, innerWidth, offenders: offenders.slice(0, 12) };
      });

      const offenderText = metrics.offenders
        .map((o) => `    <${o.tag} class="${o.cls}"> width=${o.width} right=${o.right}`)
        .join('\n');
      expect(
        metrics.scrollWidth,
        `page-level horizontal overflow at 390px (scrollWidth ${metrics.scrollWidth} > innerWidth ${metrics.innerWidth}). Offenders:\n${offenderText || '    (none found)'}`,
      ).toBeLessThanOrEqual(metrics.innerWidth + 1);
    } finally {
      await context.close();
    }
  });
});
