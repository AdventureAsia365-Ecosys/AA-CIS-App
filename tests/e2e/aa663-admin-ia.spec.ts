import { test, expect, Page } from '@playwright/test';
import { adminUsername, adminPassword } from './helpers/auth';
import fs from 'fs';

// AA-663 — admin information architecture: every nav item opens with no JS error at 1440 px and
// 400 px, and every retired URL redirects to its new home.
const SHOT_DIR = 'tests/e2e/results/aa663';
fs.mkdirSync(SHOT_DIR, { recursive: true });

test.describe.configure({ mode: 'serial' });

const NAV: { label: string; path: string }[] = [
  { label: 'Dashboard', path: '/admin/dashboard' },
  { label: 'Upload (S0)', path: '/admin/upload' },
  { label: 'Rewrite (S1)', path: '/admin/s1-rewrite' },
  { label: 'Review Queue', path: '/admin/review' },
  { label: 'Master Content', path: '/admin/master-content' },
  { label: 'Social Content', path: '/admin/atom-curation' },
  { label: 'Tenants', path: '/admin/tenants' },
  { label: 'External Spend', path: '/admin/llm-usage' },
  { label: 'Jobs', path: '/admin/jobs' },
  { label: 'Jev Decisions', path: '/admin/decisions' },
  { label: 'Settings', path: '/admin/settings' },
];

const REDIRECTS: [string, RegExp][] = [
  ['/upload', /\/admin\/upload$/],
  ['/review', /\/admin\/review$/],
  ['/catalog', /\/admin\/master-content$/],
  ['/brand', /\/admin\/settings\?tab=brand$/],
  ['/admin/brand', /\/admin\/settings\?tab=brand$/],
];

async function login(page: Page) {
  await page.goto('/login');
  await page.fill('input[type="text"]', adminUsername());
  await page.fill('input[type="password"]', adminPassword());
  await page.click('button:has-text("Login")');
  await page.waitForURL(/\/admin\/dashboard/, { timeout: 15000 });
}

for (const vp of [{ width: 1440, height: 900 }, { width: 400, height: 860 }]) {
  test(`nav items open without JS errors at ${vp.width}px`, async ({ page }) => {
    test.setTimeout(180000);
    await page.setViewportSize(vp);
    const errors: string[] = [];
    page.on('pageerror', e => errors.push(`${page.url()} :: ${e.message}`));
    await login(page);

    const sidebar = page.locator('aside');
    await expect(sidebar.getByText('ADMIN', { exact: true })).toHaveCount(0);
    for (const group of ['Overview', 'Content', 'Intelligence', 'Tenants', 'Operations']) {
      await expect(sidebar.getByText(group, { exact: true }).first()).toBeVisible();
    }
    await expect(sidebar.getByRole('button', { name: 'Dashboard', exact: true })).toHaveCount(1);

    for (const item of NAV) {
      await sidebar.getByRole('button', { name: item.label, exact: true }).click();
      await page.waitForURL(u => u.pathname === item.path, { timeout: 15000 });
      await page.waitForLoadState('networkidle').catch(() => {});
      await expect(sidebar.getByRole('button', { name: item.label, exact: true }))
        .toHaveAttribute('aria-current', 'page');
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
