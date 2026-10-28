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
