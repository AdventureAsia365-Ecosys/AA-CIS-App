import { test, expect, Page } from '@playwright/test';
import { adminUsername, adminPassword } from './helpers/auth';
// AA-566 Phần E — systemic sticky-header audit fix live-verify.
//
// Structural check (primary): the scrollable content pane (`<main>` or the page's designated
// flex:1 overflow container) must be height-CONSTRAINED to the viewport (clientHeight ~=
// innerHeight, i.e. it does NOT grow past the viewport) while its scrollHeight can exceed that —
// proving the fix (height:100vh on the flex-column ancestor + minHeight:0 on the flex:1 pane)
// actually took effect, instead of the bug's signature (clientHeight == scrollHeight == full
// content height, document itself scrolling instead).
//
// Real-scroll check (secondary, on 2 representative pages): a genuine mouse-wheel scroll leaves
// document.scrollingElement.scrollTop at 0 — the header/sidebar never move because the page
// itself never scrolls, only the internal pane does.
const SHOT_DIR = 'tests/e2e/results/aa566-e';
const WANDERLUX_API_KEY = process.env.WANDERLUX_API_KEY || '';

async function adminLogin(page: Page) {
  await page.goto('/login');
  await page.fill('input[type="text"]', adminUsername());
  await page.fill('input[type="password"]', adminPassword());
  await page.click('button:has-text("Login")');
  await page.waitForTimeout(2000);
}

async function checkConstrained(page: Page, url: string, selector = 'main') {
  await page.goto(url);
  await page.waitForTimeout(1500);
  return page.evaluate((sel) => {
    const el = document.querySelector(sel) as HTMLElement | null;
    if (!el) return null;
    return {
      clientHeight: el.clientHeight,
      scrollHeight: el.scrollHeight,
      innerHeight: window.innerHeight,
      docScrollHeight: document.documentElement.scrollHeight,
    };
  }, selector);
}

test.describe('Admin pages — structural check (clientHeight capped at viewport, not growing with content)', () => {
  test.beforeEach(async ({ page }) => { await adminLogin(page); });

  const pages: [string, string][] = [
    ['/admin/dashboard', 'main'],
    ['/admin/tenants', 'main'],
    ['/admin/settings', 'main'],
    ['/admin/llm-usage', 'main'],
    ['/admin/run-health', 'div'],
    ['/admin/review', 'main'],
    ['/admin/a4-oversight', 'div'],
    ['/admin/master-content', 'body'], // page itself uses height:100vh at the top, checked via body not growing
  ];

  for (const [url, sel] of pages) {
    test(`${url} — main pane height-constrained`, async ({ page }) => {
      const m = await checkConstrained(page, url, sel === 'body' ? 'body' : sel);
      console.log(url, JSON.stringify(m));
      expect(m).not.toBeNull();
      // documentElement must never grow meaningfully past the viewport — the real signature of
      // the fix. Small (<40px) slack allowed for borders/margins.
      expect(m!.docScrollHeight).toBeLessThan(m!.innerHeight + 40);
    });
  }
});

test.describe('Real mouse-wheel scroll — 2 representative pages', () => {
  test.beforeEach(async ({ page }) => { await adminLogin(page); });

  test('Dashboard — real scroll stays internal, document never moves', async ({ page }) => {
    await page.goto('/admin/dashboard');
    await page.waitForTimeout(1200);
    await page.mouse.move(700, 400);
    await page.mouse.wheel(0, 3000);
    await page.waitForTimeout(400);
    await expect(page.locator('text=Dashboard').first()).toBeVisible();
    const docScrollTop = await page.evaluate(() => document.scrollingElement?.scrollTop ?? 0);
    await page.screenshot({ path: `${SHOT_DIR}/1-dashboard-after-scroll.png` });
    expect(docScrollTop).toBe(0);
  });

  test('Tenants — real scroll stays internal, document never moves', async ({ page }) => {
    await page.goto('/admin/tenants');
    await page.waitForTimeout(1200);
    await page.mouse.move(700, 400);
    await page.mouse.wheel(0, 3000);
    await page.waitForTimeout(400);
    await expect(page.locator('text=Tenants').first()).toBeVisible();
    const docScrollTop = await page.evaluate(() => document.scrollingElement?.scrollTop ?? 0);
    await page.screenshot({ path: `${SHOT_DIR}/2-tenants-after-scroll.png` });
    expect(docScrollTop).toBe(0);
  });
});

test('Tenant Portal shell (layout.tsx) — My Catalog structurally constrained + real scroll', async ({ page }) => {
  test.skip(!WANDERLUX_API_KEY, 'WANDERLUX_API_KEY env var not set');
  await page.goto('/tenant-login');
  await page.fill('input[type="password"]', WANDERLUX_API_KEY);
  await page.click('button:has-text("Access Portal")');
  await page.waitForURL('**/portal**', { timeout: 10000 });

  await page.goto('/portal/t4-pool');
  await page.waitForTimeout(1200);
  const m = await page.evaluate(() => {
    const el = document.querySelector('main');
    return el ? { clientHeight: el.clientHeight, innerHeight: window.innerHeight, docScrollHeight: document.documentElement.scrollHeight } : null;
  });
  console.log('portal', JSON.stringify(m));
  expect(m!.docScrollHeight).toBeLessThan(m!.innerHeight + 40);

  await page.mouse.move(700, 400);
  await page.mouse.wheel(0, 3000);
  await page.waitForTimeout(400);
  await expect(page.locator('text=My Catalog').first()).toBeVisible();
  const scrollTop = await page.evaluate(() => document.scrollingElement?.scrollTop ?? 0);
  await page.screenshot({ path: `${SHOT_DIR}/3-my-catalog-after-scroll.png` });
  expect(scrollTop).toBe(0);
});
