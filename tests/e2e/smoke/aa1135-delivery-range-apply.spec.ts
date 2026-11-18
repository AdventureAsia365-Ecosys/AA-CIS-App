// tests/e2e/smoke/aa1135-delivery-range-apply.spec.ts
// AA-1135 follow-up (S284 live check of Platform › Delivery v2): at 1440 px the custom range's "To" date box was
// 350 px wide inside its 150 px wrapper and covered the Apply button (elementFromPoint at Apply returned the "To"
// input), so the range could not be applied with a pointer. The kit core DateField now lets its text box shrink to the
// wrapper (lib/kitDateField.test.mjs is the unit guard); this spec checks the real page.
// ADMIN-ONLY: /admin/platform is admin (frontend/lib/rbac.ts), so a viewer or reviewer run skips it. Runs in the
// "smoke" project (admin storageState from smoke-setup). Read-only up to the click: Apply only changes the URL and
// reads the range from the API. Every wait is bounded. Checks, at 1440 x 900:
//   (a) after "Custom" is picked, each date box lies inside its own wrapper and ends before the next control;
//   (b) the point at the centre of Apply belongs to the Apply button (nothing drawn over it);
//   (c) a click on Apply puts range=custom with both days in the URL;
//   (d) on the Merge queue tab, every Y tick label of the wait histogram and the throughput chart lies inside its
//       chart (S284: a -16 px left margin cut "180" to "80"; lib/deliveryRange.test.mjs is the unit guard).
import { test, expect } from '@playwright/test';
import { ADMIN_STATE, baseUrl, installBypassRoute, isAdminRun } from './smoke-helpers';

test.describe('AA-1135 — delivery custom range Apply', { tag: '@staff' }, () => {
  test.beforeEach(() => {
    test.skip(!isAdminRun(), 'admin-only (/admin/platform): skipped in a viewer or reviewer run');
  });

  test('(a)+(b)+(c) the date boxes stay in their wrappers and Apply is clickable at 1440', async ({ browser }) => {
    const context = await browser.newContext({
      baseURL: baseUrl(), storageState: ADMIN_STATE, viewport: { width: 1440, height: 900 },
    });
    await installBypassRoute(context);
    const page = await context.newPage();
    try {
      await page.goto('/admin/platform/delivery', { waitUntil: 'domcontentloaded' });
      const picker = page.getByTestId('delivery-range-picker');
      await expect(picker).toBeVisible({ timeout: 30000 });
      await picker.getByRole('radio', { name: 'Custom' }).click({ timeout: 10000 });
      const apply = page.getByTestId('delivery-range-apply').getByRole('button');
      await expect(apply).toBeVisible({ timeout: 10000 });

      // (a) each box inside its wrapper, and the "To" box ends left of Apply
      const boxes = await page.locator('[data-testid="delivery-range"] [data-kit-field="date"]').evaluateAll((els) =>
        els.map((w) => {
          const input = w.querySelector('input[type="text"]') as HTMLInputElement;
          const wr = w.getBoundingClientRect();
          const ir = input.getBoundingClientRect();
          return { label: input.getAttribute('aria-label'), wrapLeft: wr.left, wrapRight: wr.right, left: ir.left,
            right: ir.right };
        }));
      expect(boxes.map((b) => b.label)).toEqual(['From', 'To']);
      for (const b of boxes) {
        expect(b.left, `${b.label} box starts in its wrapper`).toBeGreaterThanOrEqual(b.wrapLeft - 0.5);
        expect(b.right, `${b.label} box ends in its wrapper`).toBeLessThanOrEqual(b.wrapRight + 0.5);
      }
      const applyBox = await apply.boundingBox();
      expect(applyBox).not.toBeNull();
      expect(boxes[1].right, 'the To box ends before Apply').toBeLessThanOrEqual(applyBox!.x);

      // (b) nothing is drawn over Apply
      const hit = await page.evaluate(({ x, y }) => {
        const el = document.elementFromPoint(x, y);
        const host = document.querySelector('[data-testid="delivery-range-apply"]');
        return { inside: !!el && !!host && host.contains(el), tag: el?.tagName ?? null,
          label: el?.getAttribute('aria-label') ?? null };
      }, { x: applyBox!.x + applyBox!.width / 2, y: applyBox!.y + applyBox!.height / 2 });
      expect(hit.inside, `elementFromPoint at Apply is ${hit.tag} ${hit.label ?? ''}`).toBe(true);

      // (c) Apply is actionable (Playwright refuses a click another element would receive) and sets the URL
      if (await apply.isEnabled()) {
        await apply.click({ timeout: 10000 });
        await expect(page).toHaveURL(/[?&]range=custom&from=\d{4}-\d{2}-\d{2}&to=\d{4}-\d{2}-\d{2}/, { timeout: 10000 });
      } else {
        // the default days are refused (custom ranges paused, or a store shorter than the default 14 days): (a) and
        // (b) above already prove nothing covers the button
        test.info().annotations.push({ type: 'note', description: 'Apply disabled by the range checks; URL not set' });
      }
    } finally {
      await context.close();
    }
  });

  test('(d) the Y tick labels of the merge-queue charts are not cut at the chart edge', async ({ browser }) => {
    const context = await browser.newContext({
      baseURL: baseUrl(), storageState: ADMIN_STATE, viewport: { width: 1440, height: 900 },
    });
    await installBypassRoute(context);
    const page = await context.newPage();
    try {
      await page.goto('/admin/platform/delivery?app=merge-queue&range=90d', { waitUntil: 'domcontentloaded' });
      const hist = page.getByTestId('queue-histogram').first();
      await expect(hist).toBeVisible({ timeout: 30000 });
      const charts = page.locator('[data-testid="queue-histogram"] svg.recharts-surface, '
        + '[data-testid="queue-throughput"] svg.recharts-surface');
      if (await charts.count() === 0) {
        test.info().annotations.push({ type: 'note', description: 'no merge-queue chart drawn (no entries in range)' });
        return;
      }
      await expect(charts.first().locator('.recharts-yAxis text').first()).toBeVisible({ timeout: 10000 });
      const ticks = await charts.evaluateAll((svgs) => svgs.flatMap((svg) => {
        const box = svg.getBoundingClientRect();
        return [...svg.querySelectorAll('.recharts-yAxis text')].map((t) => {
          const r = t.getBoundingClientRect();
          return { text: t.textContent ?? '', left: r.left, svgLeft: box.left };
        });
      }));
      expect(ticks.length, 'the charts draw Y ticks').toBeGreaterThan(0);
      for (const tk of ticks) {
        expect(tk.left, `Y tick "${tk.text}" starts inside its chart`).toBeGreaterThanOrEqual(tk.svgLeft - 0.5);
      }
    } finally {
      await context.close();
    }
  });
});
