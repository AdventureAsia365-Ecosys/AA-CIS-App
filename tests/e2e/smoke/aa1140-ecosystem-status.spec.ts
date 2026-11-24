// tests/e2e/smoke/aa1140-ecosystem-status.spec.ts (was aa858-ops-overview.spec.ts)
// AA-1140 (S289) — /admin/overview is the Ecosystem status page (one live and period status page for CIS,
// TripPlanner, Booking and the partner CRM). ADMIN-ONLY: GET /admin/overview/status is ops.read, so a viewer or
// reviewer run skips this file. Runs in the "smoke" project (admin storageState from smoke-setup). Every wait is
// bounded. Checks:
//   (a) GET /api/admin/overview/status?range=24h answers 200 with the verdict, the thresholds and the nine sections,
//       each "ok", "unavailable" or "not_measured" (never a 5xx for one bad source), and a sources row per section;
//   (b) the verdict and every card render; a not-measured signal shows the grey chip, no raw JSON on the page;
//   (c) the view lives in the URL: the 6 h preset writes range=6h, a reload keeps it, Reset view clears it;
//   (d) at 390 px, with real scrollbars (headless hides them by default, lesson S239), no horizontal page overflow;
//       a table wrapper may scroll sideways only inside its own box and then shows the kit scroll hint;
//   (e) the content that moved off the page: Data coverage shows the content pipeline and the content counts;
//   (f) /admin/spend?tab=apps (cost per app moved there earlier): the four KPI values on screen add up,
//       AWS bill + AI estimate − AI already on the AWS bill = Total, to the cent (S243 review).
import { test, expect, chromium } from '@playwright/test';
import { ADMIN_STATE, baseUrl, installBypassRoute, isAdminRun } from './smoke-helpers';

// AA-1141 (P1): + changes (What changed), forecast (capacity), anomaly (AI cost anomaly)
const SECTIONS = ['signals', 'apps', 'runtimes', 'resources', 'jobs', 'ai', 'cost', 'events', 'delivery', 'changes',
  'forecast', 'anomaly'] as const;
const CARDS = ['status-verdict', 'status-apps-card', 'status-signals', 'status-services', 'status-jobs',
  'status-resources', 'status-cost', 'status-delivery', 'status-ai', 'status-events', 'status-sources',
  'status-drill', 'status-changes', 'status-forecast'] as const;
const STATES = ['ok', 'unavailable', 'not_measured'];

test.describe('AA-1140 — Ecosystem status', { tag: '@staff' }, () => {
  test.beforeEach(() => {
    test.skip(!isAdminRun(), 'admin-only (ops.read): skipped in a viewer or reviewer run');
  });

  test('(a)+(b) endpoint shape, verdict and cards', async ({ browser }) => {
    const context = await browser.newContext({ baseURL: baseUrl(), storageState: ADMIN_STATE,
      viewport: { width: 1440, height: 900 } });
    await installBypassRoute(context);
    const page = await context.newPage();
    try {
      const statusResp = page.waitForResponse((r) => r.url().includes('/api/admin/overview/status'), { timeout: 45000 });
      await page.goto('/admin/overview', { waitUntil: 'domcontentloaded' });
      const resp = await statusResp;
      expect(resp.status(), await resp.text().catch(() => '')).toBe(200);
      const body = await resp.json();
      expect(['ok', 'attention', 'partial', 'unknown']).toContain(body.verdict.status);
      expect(typeof body.thresholds.error_rate_pct.warn).toBe('number');
      for (const s of SECTIONS) expect(STATES, `section ${s}`).toContain(body.sections[s]?.status);
      expect(body.sources.map((r: { section: string }) => r.section).sort()).toEqual([...SECTIONS].sort());
      expect(body.buckets.length).toBeGreaterThan(1);

      await expect(page.getByTestId('status-verdict-title')).toBeVisible({ timeout: 30000 });
      for (const id of CARDS) await expect(page.getByTestId(id), id).toBeVisible({ timeout: 20000 });
      const text = await page.getByTestId('status-body').innerText();
      expect(text).not.toMatch(/\{"status"|\[object Object\]|undefined|NaN/);
    } finally {
      await context.close();
    }
  });

  test('(c) the view lives in the URL', async ({ browser }) => {
    const context = await browser.newContext({ baseURL: baseUrl(), storageState: ADMIN_STATE,
      viewport: { width: 1440, height: 900 } });
    await installBypassRoute(context);
    const page = await context.newPage();
    try {
      await page.goto('/admin/overview', { waitUntil: 'domcontentloaded' });
      await expect(page.getByTestId('status-verdict-title')).toBeVisible({ timeout: 45000 });
      const next = page.waitForResponse((r) => r.url().includes('/api/admin/overview/status?range=6h'), { timeout: 45000 });
      await page.getByTestId('status-range').getByRole('radio', { name: '6 h' }).click();
      expect((await next).status()).toBe(200);
      await expect(page).toHaveURL(/range=6h/);
      await page.reload({ waitUntil: 'domcontentloaded' });
      await expect(page).toHaveURL(/range=6h/);
      await expect(page.getByTestId('status-verdict-title')).toBeVisible({ timeout: 45000 });
      await page.getByRole('button', { name: 'Reset view' }).click();
      await expect(page).not.toHaveURL(/range=/);
    } finally {
      await context.close();
    }
  });

  test('(d) 390 px: no horizontal overflow, tables fit their cards', async () => {
    const real = await chromium.launch({ ignoreDefaultArgs: ['--hide-scrollbars'] });
    const context = await real.newContext({ baseURL: baseUrl(), storageState: ADMIN_STATE,
      viewport: { width: 390, height: 844 } });
    await installBypassRoute(context);
    const page = await context.newPage();
    try {
      await page.goto('/admin/overview', { waitUntil: 'domcontentloaded' });
      await expect(page.getByTestId('status-body')).toBeVisible({ timeout: 45000 });
      await page.waitForTimeout(300); // charts measure their container once
      const m = await page.evaluate(() => {
        const root = document.querySelector('[data-testid="status-body"]');
        const wraps = Array.from(root?.querySelectorAll('[data-overflows]') ?? []);
        const offenders = Array.from(root?.querySelectorAll('*') ?? [])
          .filter((el) => el.getBoundingClientRect().right > window.innerWidth + 1)
          .filter((el) => !el.closest('[data-overflows]'))
          .slice(0, 5)
          .map((el) => `${el.tagName.toLowerCase()}[${el.getAttribute('data-testid') ?? ''}]`);
        return {
          scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth, offenders,
          wraps: wraps.map((w) => ({ sw: w.scrollWidth, cw: w.clientWidth,
            overflows: w.getAttribute('data-overflows') === 'true' })),
        };
      });
      expect(m.scrollWidth, `page overflow at 390 px: ${m.offenders.join(', ')}`).toBeLessThanOrEqual(m.innerWidth);
      for (const w of m.wraps) if (w.sw > w.cw + 1) expect(w.overflows).toBe(true);
    } finally {
      await context.close();
      await real.close();
    }
  });

  test('(e) the content pipeline and the content counts are on Data coverage', async ({ browser }) => {
    const context = await browser.newContext({ baseURL: baseUrl(), storageState: ADMIN_STATE,
      viewport: { width: 1440, height: 900 } });
    await installBypassRoute(context);
    const page = await context.newPage();
    try {
      await page.goto('/admin/data-coverage', { waitUntil: 'domcontentloaded' });
      await expect(page.getByTestId('coverage-pipeline-section')).toBeVisible({ timeout: 45000 });
      await expect(page.getByTestId('coverage-intelligence-section').locator('[data-kit-kpi]').first())
        .toBeVisible({ timeout: 30000 });
    } finally {
      await context.close();
    }
  });

  test('(f) the Cost per app KPI strip adds up on screen', async ({ browser }) => {
    const context = await browser.newContext({ baseURL: baseUrl(), storageState: ADMIN_STATE,
      viewport: { width: 1440, height: 900 } });
    await installBypassRoute(context);
    const page = await context.newPage();
    try {
      await page.goto('/admin/spend?tab=apps', { waitUntil: 'domcontentloaded' });
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

  test('(g) AA-1141 P1: annotations, what changed, ack rights, freshness footer, saved views, refresh pause', async ({
    browser }) => {
    const context = await browser.newContext({ baseURL: baseUrl(), storageState: ADMIN_STATE,
      viewport: { width: 1440, height: 900 } });
    await installBypassRoute(context);
    const page = await context.newPage();
    try {
      const statusResp = page.waitForResponse((r) => r.url().includes('/api/admin/overview/status'), { timeout: 45000 });
      await page.goto('/admin/overview', { waitUntil: 'domcontentloaded' });
      const body = await (await statusResp).json();
      expect(typeof body.viewer?.can_ack).toBe('boolean');
      expect(typeof body.acks?.rules?.reason_min_chars).toBe('number');
      expect(Array.isArray(body.sections.signals.annotations)).toBe(true);
      expect(body.pipelines.map((r: { key: string }) => r.key).sort()).toEqual(
        ['delivery', 'digest', 'guards', 'sampler', 'schema_dump']);
      await expect(page.getByTestId('status-ann-legend')).toBeVisible({ timeout: 30000 });
      await expect(page.getByTestId('status-changes')).toBeVisible();
      await expect(page.getByTestId('status-forecast')).toBeVisible();
      await expect(page.getByTestId('status-pipelines')).toBeVisible();
      await expect(page.getByTestId('status-refresh-chip')).toBeVisible();
      await expect(page.getByTestId('status-verdict-title')).toHaveAttribute('aria-live', 'polite');
      if (!body.viewer.can_ack) {
        // a read-only identity (the E2E admin) or the business owner: no ack buttons anywhere
        await expect(page.locator('[data-testid$="-ack-btn"]')).toHaveCount(0);
      }
      // the What changed type chip writes chg= into the URL
      await page.getByTestId('status-chg-filter').getByRole('button').first().click();
      await expect(page).toHaveURL(/chg=deploy/);
      // the pause chip pauses auto refresh
      await page.getByTestId('status-refresh-chip').getByRole('button').click();
      await expect(page.getByTestId('status-refresh-chip')).toContainText(/Paused/);
    } finally {
      await context.close();
    }
  });
});
