import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './helpers/auth';

// AA-384 bug: /admin/marketplace was missing from middleware.ts's PROTECTED_ROUTES allow-list,
// so a logged-in admin got redirected to /login (fail-closed #4/#5 pattern) while every other
// /admin/* page worked. Real form login via the shared helper (E2E_ADMIN_USERNAME /
// E2E_ADMIN_PASSWORD), no bypass. AA-732 removed the stale hardcoded admin credential pair.

test('logged-in admin reaches /admin/marketplace without redirect', async ({ page }) => {
  await loginAsAdmin(page);
  await page.waitForTimeout(1500);
  await page.goto('/admin/marketplace');
  await page.waitForTimeout(1000);
  expect(page.url()).toContain('/admin/marketplace');
  expect(page.url()).not.toContain('/login');
});

test('regression: /admin/tenants still reachable after the fix', async ({ page }) => {
  await loginAsAdmin(page);
  await page.waitForTimeout(1500);
  await page.goto('/admin/tenants');
  await page.waitForTimeout(1000);
  expect(page.url()).toContain('/admin/tenants');
  expect(page.url()).not.toContain('/login');
});

test('regression: /admin/dashboard still reachable after the fix', async ({ page }) => {
  await loginAsAdmin(page);
  await page.waitForTimeout(1500);
  await page.goto('/admin/dashboard');
  await page.waitForTimeout(1000);
  expect(page.url()).toContain('/admin/dashboard');
  expect(page.url()).not.toContain('/login');
});
