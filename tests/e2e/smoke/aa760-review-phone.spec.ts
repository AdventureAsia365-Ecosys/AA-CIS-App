// tests/e2e/smoke/aa760-review-phone.spec.ts
// AA-760 (S248) — /admin/review at a 400 px viewport: the filter row (status / country / score selects +
// Refresh) must fit. It used to run ~480 px wide and was cut off by the page's own scrolling <main>.
// The shell check (aa752-shell (d)) cannot see this: <main class="aa-admin-main"> has overflow-y:auto, which
// makes overflow-x auto too, so the page itself never scrolls sideways — the overflow hides INSIDE main.
// So this measures main.scrollWidth against main.clientWidth and lists what sticks out past main's right
// edge. Viewer-safe (the review queue is the viewer's landing page). Every wait is bounded.
import { test, expect } from '@playwright/test';
import { ADMIN_STATE, baseUrl, installBypassRoute } from './smoke-helpers';

test.describe('AA-760 — review queue on a phone', { tag: '@staff' }, () => {
  test('filter row fits a 400 px viewport (no overflow inside main)', async ({ browser }) => {
    // The row wraps (it is not sized to fit exactly), so headless Chromium's hidden scrollbar does not hide
    // a near-miss here; clientWidth is used as the bound either way.
    const context = await browser.newContext({
      baseURL: baseUrl(),
      storageState: ADMIN_STATE,
      viewport: { width: 400, height: 860 },
    });
    await installBypassRoute(context);
    const page = await context.newPage();
    try {
      await page.goto('/admin/review', { waitUntil: 'domcontentloaded' });
      await expect(page.locator('.aa-admin-main')).toBeVisible({ timeout: 15000 });
      // The filter selects render with the DataTable toolbar, before the data lands.
      await expect(page.locator('.aa-admin-main select').first()).toBeVisible({ timeout: 15000 });

      const m = await page.evaluate(() => {
        const main = document.querySelector<HTMLElement>('.aa-admin-main')!;
        const box = main.getBoundingClientRect();
        const right = box.left + main.clientWidth;
        const offenders: string[] = [];
        for (const el of Array.from(main.querySelectorAll<HTMLElement>('select, button, input'))) {
          const r = el.getBoundingClientRect();
          if (r.width === 0 || r.height === 0) continue;
          // Elements inside the table's own horizontal scroller may extend past main by design.
          if (el.closest('table')) continue;
          if (r.right > right + 1) {
            offenders.push(`<${el.tagName.toLowerCase()}> "${(el.textContent || (el as HTMLInputElement).placeholder || '').trim().slice(0, 30)}" right=${Math.round(r.right)}`);
          }
        }
        return { scrollWidth: main.scrollWidth, clientWidth: main.clientWidth, offenders };
      });

      expect(m.offenders, `controls past main's right edge at 400 px:\n  ${m.offenders.join('\n  ')}`).toEqual([]);
      expect(
        m.scrollWidth,
        `horizontal overflow inside main at 400 px (scrollWidth ${m.scrollWidth} > clientWidth ${m.clientWidth})`,
      ).toBeLessThanOrEqual(m.clientWidth + 1);
    } finally {
      await context.close();
    }
  });
});
