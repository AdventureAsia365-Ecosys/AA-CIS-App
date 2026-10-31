import { test, expect, Page } from '@playwright/test';
import { adminUsername, adminPassword } from './helpers/auth';
import fs from 'fs';

// AA-663 — admin information architecture: every nav item opens with no JS error at 1440 px and
// 400 px, and every retired URL redirects to its new home.
const SHOT_DIR = 'tests/e2e/results/aa663';
fs.mkdirSync(SHOT_DIR, { recursive: true });

test.describe.configure({ mode: 'serial' });

// AA-883 (S241) — the admin nav is two sections, Platform and CIS Content (frontend/app/_kit/adminNav.tsx).
const NAV: { label: string; path: string }[] = [
  // Platform
  { label: 'Overview', path: '/admin/overview' },
  { label: 'Staff', path: '/admin/platform/staff' },
  { label: 'Tenants', path: '/admin/tenants' },
  { label: 'Jobs', path: '/admin/jobs' },
  { label: 'AI providers', path: '/admin/ai-gateway' },   // AA-804
  { label: 'Cost Operations', path: '/admin/cost-ops' },
  { label: 'Jev Decisions', path: '/admin/decisions' },
  { label: 'API Docs', path: '/admin/api-docs' },
  { label: 'Data model', path: '/admin/data-model' },
  // CIS Content
  { label: 'Upload (S0)', path: '/admin/upload' },
  { label: 'Rewrite (S1)', path: '/admin/s1-rewrite' },
  { label: 'Review Queue', path: '/admin/review' },
  { label: 'Master Content', path: '/admin/master-content' },
  { label: 'Photos', path: '/admin/photos' },
  { label: 'Social Content', path: '/admin/atom-curation' },
  { label: 'SEO Intelligence', path: '/admin/seo-intelligence' },
  { label: 'Content Trace', path: '/admin/tenant-activity' },
  { label: 'A4 Quality', path: '/admin/platform-stats' },
  { label: 'Content Quality', path: '/admin/content-quality' },
  { label: 'Writing memory', path: '/admin/writing-memory' },   // AA-917
  { label: 'Settings', path: '/admin/settings' },
];

// Section headers and the group titles shown over more than one item (a one-item group named like its
// item — Overview, Tenants, Jobs — has no title).
const SECTIONS_AND_GROUPS = [
  'Platform', 'CIS Content',
  'People & access', 'AI & spend', 'Developer', 'Pipeline', 'Assets', 'Intelligence', 'Tenant output',
];

/** Below 768 px the sidebar is a drawer: open it before reading or clicking the nav. */
async function openNav(page: Page, phone: boolean) {
  if (phone) await page.getByRole('button', { name: 'Open navigation menu' }).click();
}

const REDIRECTS: [string, RegExp][] = [
  ['/upload', /\/admin\/upload$/],
  ['/review', /\/admin\/review$/],
  ['/catalog', /\/admin\/master-content$/],
  ['/brand', /\/admin\/settings\?tab=brand$/],
  ['/admin/brand', /\/admin\/settings\?tab=brand$/],
  // AA-804 UI part 2 — tabs that moved to AI providers: each old link lands on its new tab with exactly one `tab`.
  ['/admin/settings?tab=models', /\/admin\/ai-gateway\?tab=routing$/],
  ['/admin/settings?tab=jev-credit', /\/admin\/ai-gateway\?tab=providers&view=jev-credit$/],
  ['/admin/cost-ops?tab=budgets', /\/admin\/ai-gateway\?tab=budgets$/],
  ['/admin/llm-usage', /\/admin\/cost-ops\?tab=spend$/],
  ['/admin/llm-usage?tab=budgets', /\/admin\/ai-gateway\?tab=budgets$/],
  // AA-804 UI part 3 — the LLM, DataForSEO and Jev tabs became views of Cost Operations › AI & data spend.
  ['/admin/cost-ops?tab=llm', /\/admin\/cost-ops\?tab=spend$/],
  ['/admin/cost-ops?tab=dfs', /\/admin\/cost-ops\?tab=spend&view=dfs$/],
  ['/admin/cost-ops?tab=jev', /\/admin\/cost-ops\?tab=spend&view=jev$/],
  ['/admin/llm-usage?tab=jev', /\/admin\/cost-ops\?tab=spend&view=jev$/],
];

async function login(page: Page) {
  await page.goto('/login');
  await page.fill('input[type="text"]', adminUsername());
  await page.fill('input[type="password"]', adminPassword());
  await page.click('button:has-text("Login")');
  // /admin/dashboard redirects to the role's landing page (admin: Overview).
  await page.waitForURL(/\/admin\/(dashboard|overview)/, { timeout: 15000 });
}

for (const vp of [{ width: 1440, height: 900 }, { width: 400, height: 860 }]) {
  test(`nav items open without JS errors at ${vp.width}px`, async ({ page }) => {
    test.setTimeout(180000);
    await page.setViewportSize(vp);
    const errors: string[] = [];
    page.on('pageerror', e => errors.push(`${page.url()} :: ${e.message}`));
    await login(page);

    const phone = vp.width < 768;
    const sidebar = page.locator('aside');
    await openNav(page, phone);
    await expect(sidebar.getByText('ADMIN', { exact: true })).toHaveCount(0);
    for (const group of SECTIONS_AND_GROUPS) {
      await expect(sidebar.getByText(group, { exact: true }).first()).toBeVisible();
    }
    await expect(sidebar.getByRole('button', { name: 'Dashboard', exact: true })).toHaveCount(0);
    if (phone) await page.keyboard.press('Escape');

    for (const item of NAV) {
      await openNav(page, phone);
      await sidebar.getByRole('button', { name: item.label, exact: true }).click();
      await page.waitForURL(u => u.pathname === item.path, { timeout: 15000 });
      await page.waitForLoadState('networkidle').catch(() => {});
      await openNav(page, phone);
      await expect(sidebar.getByRole('button', { name: item.label, exact: true }))
        .toHaveAttribute('aria-current', 'page');
      if (phone) await page.keyboard.press('Escape');
      await page.screenshot({ path: `${SHOT_DIR}/${vp.width}-${item.path.split('/').pop()}.png` });
    }
    expect(errors).toEqual([]);
  });
}

test('retired URLs redirect to their new home', async ({ page }) => {
  await login(page);
  for (const [from, to] of REDIRECTS) {
    await page.goto(from);
    await expect(page).toHaveURL(to);
  }
  // Brand Identity now lives in Settings: the ?tab=brand link opens the editor directly.
  await expect(page.getByText('Brands', { exact: true })).toBeVisible({ timeout: 15000 });
  await page.screenshot({ path: `${SHOT_DIR}/settings-brand-tab.png` });
});

// S262 C4 (AA-804 live finding F7): at phone width the tab strip scrolls sideways; the active tab is scrolled into
// view (Guide is the last tab) and the strip shows a fade on the side that has more tabs.
test('AI providers tab strip keeps the active tab in view at 400px', async ({ page }) => {
  await page.setViewportSize({ width: 400, height: 860 });
  await login(page);
  await page.goto('/admin/ai-gateway?tab=guide');
  const strip = page.getByTestId('ai-gateway-tabs').locator('div').first();
  const active = strip.locator('[data-kit-tab-active="true"]');
  await expect(active).toHaveText('Guide', { timeout: 15000 });
  await expect.poll(async () => {
    const [s, a] = await Promise.all([strip.boundingBox(), active.boundingBox()]);
    return !!s && !!a && a.x >= s.x - 1 && a.x + a.width <= s.x + s.width + 1;
  }, { timeout: 5000 }).toBe(true);
  const overflow = await strip.evaluate((el) => el.scrollWidth > el.clientWidth);
  if (overflow) {
    await expect.poll(() => strip.evaluate((el) => getComputedStyle(el).maskImage || getComputedStyle(el).webkitMaskImage))
      .toContain('linear-gradient');
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: `${SHOT_DIR}/400-ai-gateway-guide-tab.png` });
});

// S262 C4 (root ADR 0009 UI): a Rewrite (S1) run never picks its own model; the page shows the stage route's model.
test('Rewrite (S1) shows the routed model and has no per-run model select', async ({ page }) => {
  await login(page);
  await page.goto('/admin/s1-rewrite');
  await expect(page.getByTestId('s1-route-model')).toBeVisible({ timeout: 15000 });
  await expect(page.getByTestId('s1-route-model').getByRole('link')).toHaveAttribute('href', '/admin/ai-gateway?tab=routing');
  await expect(page.locator('option', { hasText: 'this run only' })).toHaveCount(0);
});
