// tests/e2e/smoke/aa858-ops-overview.spec.ts
// AA-858 — the business view on /admin/overview (apps health, TripPlanner customers, Booking funnel, cost per
// app). ADMIN-ONLY: GET /admin/overview/ops is ops.read, so a viewer/reviewer run must skip this file.
// Runs in the "smoke" project (admin storageState from smoke-setup). Every wait is bounded. Checks:
//   (a) GET /api/admin/overview/ops answers 200 with the four sections, each "ok" or a plain "unavailable";
//   (b) the four cards and the "CIS content & operations" heading render; every KPI of an "ok" section
//       carries a source hint (title + aria-label "Source: …"); no raw JSON on the page;
//   (c) at 390 px, with real scrollbars (headless hides them by default, lesson S239), the page has no
//       horizontal overflow; a table wrapper of the new cards may scroll sideways only inside its own box and
//       then shows the kit ScrollX hint (S243: every column stays, e.g. Booking "Cancelled").
import { test, expect, chromium } from '@playwright/test';
import { ADMIN_STATE, baseUrl, installBypassRoute } from './smoke-helpers';

const CARDS = ['ops-health', 'ops-tripplanner', 'ops-booking', 'ops-cost'] as const;
const SECTIONS = ['health', 'tripplanner', 'booking', 'cost'] as const;

test.describe('AA-858 — ops overview', () => {
  test('(a)+(b) endpoint shape, cards, source hints', async ({ browser }) => {
    const context = await browser.newContext({
      baseURL: baseUrl(),
      storageState: ADMIN_STATE,
      viewport: { width: 1440, height: 900 },
    });
    await installBypassRoute(context);
    const page = await context.newPage();
    try {
      const opsResponse = page.waitForResponse((r) => r.url().includes('/api/admin/overview/ops'), { timeout: 30000 });
      await page.goto('/admin/overview', { waitUntil: 'domcontentloaded' });
      const resp = await opsResponse;
      expect(resp.status(), await resp.text().catch(() => '')).toBe(200);
      const body = await resp.json();
      for (const s of SECTIONS) {
        expect(['ok', 'unavailable'], `section ${s}`).toContain(body[s]?.status);
        if (body[s].status === 'unavailable') expect(typeof body[s].message).toBe('string');
      }
      if (body.health.status === 'ok') {
        expect(body.health.apps.map((a: { app: string }) => a.app)).toEqual(['cis', 'tripplanner', 'booking']);
      }

      for (const id of CARDS) {
        await expect(page.getByTestId(id)).toBeVisible({ timeout: 20000 });
        await expect(page.getByTestId(id).locator('h2')).toBeVisible();
      }
      await expect(page.getByTestId('overview-cis-heading')).toBeVisible();

      // Every KPI tile in an "ok" section names its source.
      if (body.tripplanner.status === 'ok') {
        for (const k of ['planner_session_started', 'trip_started', 'inquiry_sent', 'ai_search']) {
          const hint = page.getByTestId(`ops-tp-${k}`).getByTestId('source-hint');
          await expect(hint).toHaveAttribute('aria-label', /^Source: /);
        }
      }
      if (body.health.status === 'ok') {
        for (const app of ['cis', 'tripplanner', 'booking']) {
          const hints = page.getByTestId(`ops-health-${app}`).getByTestId('source-hint');
          expect(await hints.count()).toBeGreaterThanOrEqual(3);
        }
      }
      if (body.cost.status === 'ok') {
        await expect(page.getByTestId('ops-cost-total')).toBeVisible();
        await expect(page.getByTestId('ops-cost-table').getByTestId('source-hint').first()).toHaveAttribute(
          'title',
          /^Source: /,
        );
      }
      // No raw JSON or error codes leak into the business view.
      const text = await page.getByTestId('ops-overview').innerText();
      expect(text).not.toMatch(/[{}]|contract_view_missing|cache_offline|not_permitted|undefined|NaN/);
    } finally {
      await context.close();
    }
  });

  test('(c) 390 px: no horizontal overflow, tables fit their cards', async () => {
    const real = await chromium.launch({ ignoreDefaultArgs: ['--hide-scrollbars'] });
    const context = await real.newContext({
      baseURL: baseUrl(),
      storageState: ADMIN_STATE,
      viewport: { width: 390, height: 844 },
    });
    await installBypassRoute(context);
    const page = await context.newPage();
    try {
      await page.goto('/admin/overview', { waitUntil: 'domcontentloaded' });
      for (const id of CARDS) await expect(page.getByTestId(id)).toBeVisible({ timeout: 30000 });
      await page.waitForTimeout(300); // charts measure their container once
      const m = await page.evaluate(() => {
        const wraps = Array.from(document.querySelectorAll('[data-testid="ops-overview"] [data-testid$="-table"], [data-testid="ops-overview"] [data-testid$="-weeks"]'));
        const offenders = Array.from(document.querySelectorAll('[data-testid="ops-overview"] *'))
          .filter((el) => el.getBoundingClientRect().right > window.innerWidth + 1)
          .slice(0, 5)
          .map((el) => `${el.tagName.toLowerCase()}[${el.getAttribute('data-testid') ?? ''}] right=${Math.round(el.getBoundingClientRect().right)}`);
        return {
          scrollWidth: document.documentElement.scrollWidth,
          innerWidth: window.innerWidth,
          wraps: wraps.map((w) => ({
            id: w.getAttribute('data-testid'), sw: w.scrollWidth, cw: w.clientWidth,
            overflows: w.getAttribute('data-overflows') === 'true',
            hint: !!document.querySelector(`[data-testid="${w.getAttribute('data-testid')}-scroll-hint"]`),
          })),
          offenders,
        };
      });
      expect(m.scrollWidth, `page overflow at 390 px: ${m.offenders.join(', ')}`).toBeLessThanOrEqual(m.innerWidth);
      for (const w of m.wraps) {
        if (w.sw > w.cw + 1) {
          expect(w.overflows && w.hint, `${w.id} scrolls sideways (${w.sw} > ${w.cw}) without the scroll hint`).toBe(true);
        }
      }
    } finally {
      await context.close();
      await real.close();
    }
  });
});
