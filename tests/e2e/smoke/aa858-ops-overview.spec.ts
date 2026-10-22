// tests/e2e/smoke/aa858-ops-overview.spec.ts
// AA-858 — the business view on /admin/overview (apps health, TripPlanner customers, Booking funnel, cost per
// app). ADMIN-ONLY: GET /admin/overview/ops is ops.read, so a viewer/reviewer run must skip this file.
// Runs in the "smoke" project (admin storageState from smoke-setup). Every wait is bounded. Checks:
//   (a) GET /api/admin/overview/ops answers 200 with the four sections, each "ok" or a plain "unavailable";
//   (b) the four cards and the "CIS content & operations" heading render; every KPI of an "ok" section
//       carries a source hint (kit InfoTip, its text "Source: …"); no raw JSON on the page;
//   (c) at 390 px, with real scrollbars (headless hides them by default, lesson S239), the page has no
//       horizontal overflow; a table wrapper of the new cards may scroll sideways only inside its own box and
//       then shows the kit ScrollX hint (S243: every column stays, e.g. Booking "Cancelled");
//   (d) /admin/platform/cost (also admin-only): the four KPI values on screen add up,
//       AWS bill + AI estimate − AI already on the AWS bill = Total, to the cent (S243 review);
//   (e) every number tile ([data-kpi]) and every number column ([data-num-col]) on the page, business view and CIS
//       sections alike, names its source; at least one per card; every source text is plain words (no table or
//       metric names); a hint opens with the keyboard (Enter) and closes with Escape, and is tied to its button
//       with aria-describedby (AA-858 Done-when, S243); the AI note under the cost table has a hint too; a hint
//       opened by hover stays open while the pointer moves onto it (WCAG 1.4.13), a press inside a pinned hint keeps
//       it open, and the button's hit area is at least 24 px (WCAG 2.5.8);
//   (f) 390 px with touch: a tap opens a hint inside the screen, a tap elsewhere closes it.
import { test, expect, chromium } from '@playwright/test';
import { ADMIN_STATE, baseUrl, installBypassRoute, isAdminRun } from './smoke-helpers';

const CARDS = ['ops-health', 'ops-tripplanner', 'ops-booking', 'ops-cost'] as const;
const SECTIONS = ['health', 'tripplanner', 'booking', 'cost'] as const;

// AA-816 S2 path: @staff = needs the smoke session (project `smoke`, never the per-PR `smoke-public` run).
test.describe('AA-858 — ops overview', { tag: '@staff' }, () => {
  // AA-816 C9 / AA-878: ops.read is admin-only; a viewer run (the Preview credential is capped at viewer) skips it.
  // Read at run time (the role file is written by the setup project, after collection).
  test.beforeEach(() => {
    test.skip(!isAdminRun(), 'admin-only (ops.read): skipped in a viewer or reviewer run');
  });
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
          const hint = page.getByTestId(`ops-tp-${k}`).getByTestId('source-hint-text');
          await expect(hint).toHaveText(/^Source: /);
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
        await expect(page.getByTestId('ops-cost-table').getByTestId('source-hint-text').first()).toHaveText(
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

  test('(d) the Cost per app KPI strip adds up on screen', async ({ browser }) => {
    const context = await browser.newContext({
      baseURL: baseUrl(),
      storageState: ADMIN_STATE,
      viewport: { width: 1440, height: 900 },
    });
    await installBypassRoute(context);
    const page = await context.newPage();
    try {
      await page.goto('/admin/platform/cost', { waitUntil: 'domcontentloaded' });
      await expect(page.getByTestId('platform-cost-kpis')).toBeVisible({ timeout: 30000 });
      const cents = async (id: string) => {
        const t = (await page.getByTestId(`${id}-value`).innerText()).trim();
        return t === '—' ? null : Math.round(Number(t.replace(/[$,]/g, '')) * 100);
      };
      const [aws, ai, onBill, total] = await Promise.all(
        ['platform-cost-aws', 'platform-cost-ai', 'platform-cost-ai-on-bill', 'platform-cost-total'].map(cents),
      );
      expect(ai, 'AI estimate').not.toBeNull();
      expect(onBill, 'AI already on the AWS bill').not.toBeNull();
      expect(total, 'Total').toBe((aws ?? 0) + (ai as number) - (onBill as number));
    } finally {
      await context.close();
    }
  });
  test('(e) every number names its source; keyboard opens and Escape closes a hint', async ({ browser }) => {
    const context = await browser.newContext({
      baseURL: baseUrl(),
      storageState: ADMIN_STATE,
      viewport: { width: 1440, height: 900 },
    });
    await installBypassRoute(context);
    const page = await context.newPage();
    try {
      await page.goto('/admin/overview', { waitUntil: 'domcontentloaded' });
      for (const id of CARDS) await expect(page.getByTestId(id)).toBeVisible({ timeout: 30000 });
      await expect(page.getByTestId('overview-cis-heading')).toBeVisible();
      await expect(page.locator('[data-testid="overview-job-counts"]')).toBeVisible({ timeout: 30000 });

      const m = await page.evaluate(() => {
        const numbered = Array.from(document.querySelectorAll('main [data-kpi], main [data-num-col]'));
        const missing = numbered
          .filter((el) => !el.querySelector('[data-testid="source-hint"]'))
          .map((el) => (el as HTMLElement).innerText.replace(/\s+/g, ' ').slice(0, 40));
        const perCard: Record<string, number> = {};
        for (const id of ['ops-health', 'ops-tripplanner', 'ops-booking', 'ops-cost']) {
          perCard[id] = document.querySelectorAll(`[data-testid="${id}"] [data-testid="source-hint"]`).length;
        }
        const texts = Array.from(document.querySelectorAll('main [data-testid="source-hint-text"]')).map(
          (el) => el.textContent ?? '',
        );
        return { numbered: numbered.length, missing, perCard, texts };
      });
      expect(m.missing, `numbers without a source: ${m.missing.join(' | ')}`).toEqual([]);
      // CIS sections alone: 6 intelligence + 4 tenant tiles, 3 job numbers, 5 pipeline + 3 provider-cost columns.
      expect(m.numbered).toBeGreaterThanOrEqual(21);
      for (const [id, n] of Object.entries(m.perCard)) expect(n, `${id} has no source hint`).toBeGreaterThanOrEqual(1);
      for (const t of m.texts) {
        expect(t).toMatch(/^Source: /);
        expect(t, `source text names a table or code: ${t}`).not.toMatch(/\b[a-z]+_[a-z0-9_]+\b|\b[a-z_]+\.[a-z_]+\b/);
      }

      // Keyboard: focus the first hint button, Enter keeps it open, Escape closes it.
      const btn = page.getByTestId('source-hint-button').first();
      const describedBy = await btn.getAttribute('aria-describedby');
      expect(describedBy).toBeTruthy();
      const tip = page.locator(`[id="${describedBy}"]`);
      await expect(tip).toHaveAttribute('role', 'tooltip');
      await btn.focus();
      await page.keyboard.press('Enter');
      await expect(tip).toBeVisible();
      await expect(tip).toHaveText(/^Source: /);
      await page.keyboard.press('Escape');
      await expect(tip).toBeHidden();

      // The AI note under the cost table (a number in a sentence) names its source too.
      const aiNote = page.getByTestId('ops-cost-ai-note');
      if (await aiNote.count()) await expect(aiNote.getByTestId('source-hint')).toHaveCount(1);

      // Hit area >= 24 px; hover, then move onto the bubble: it stays open; a press inside a pinned bubble too.
      const box = await btn.boundingBox();
      expect(box).not.toBeNull();
      expect(Math.min(box!.width, box!.height)).toBeGreaterThanOrEqual(24);
      await btn.scrollIntoViewIfNeeded();
      await btn.hover();
      await expect(tip).toBeVisible();
      const tb = (await btn.boundingBox())!;
      const tt = (await tip.boundingBox())!;
      await page.mouse.move(tb.x + tb.width / 2, tb.y + tb.height / 2);
      await page.mouse.move(tt.x + tt.width / 2, tt.y + tt.height / 2, { steps: 4 });
      await page.waitForTimeout(400);
      await expect(tip, 'a hover tip must stay open while the pointer is on it').toBeVisible();
      await btn.click();
      await page.mouse.move(tt.x + tt.width / 2, tt.y + tt.height / 2);
      await page.mouse.down();
      await page.mouse.up();
      await expect(tip, 'a press inside a pinned tip must keep it open').toBeVisible();
      await page.keyboard.press('Escape');
      await expect(tip).toBeHidden();
    } finally {
      await context.close();
    }
  });

  test('(f) 390 px touch: a tap opens a hint inside the screen, a tap elsewhere closes it', async ({ browser }) => {
    const context = await browser.newContext({
      baseURL: baseUrl(),
      storageState: ADMIN_STATE,
      viewport: { width: 390, height: 844 },
      hasTouch: true,
      isMobile: true,
    });
    await installBypassRoute(context);
    const page = await context.newPage();
    try {
      await page.goto('/admin/overview', { waitUntil: 'domcontentloaded' });
      await expect(page.getByTestId('ops-tripplanner')).toBeVisible({ timeout: 30000 });
      const btn = page.getByTestId('ops-tripplanner').getByTestId('source-hint-button').last();
      await btn.scrollIntoViewIfNeeded();
      await btn.tap();
      const tip = page.getByTestId('ops-tripplanner').getByTestId('source-hint-text').last();
      await expect(tip).toBeVisible();
      const box = await tip.boundingBox();
      expect(box).not.toBeNull();
      expect(box!.x).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width).toBeLessThanOrEqual(390);
      const sw = await page.evaluate(() => document.documentElement.scrollWidth);
      expect(sw, 'an open hint must not widen the page').toBeLessThanOrEqual(390);
      await page.getByTestId('ops-tripplanner').locator('h2').tap();
      await expect(tip).toBeHidden();
    } finally {
      await context.close();
    }
  });
});
