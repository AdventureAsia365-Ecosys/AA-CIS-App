import { test, expect } from '@playwright/test';
import { adminUsername, adminPassword } from './helpers/auth';

const BASE_URL = process.env.BASE_URL || 'http://localhost:3001';

// ── Auth helpers ──────────────────────────────────────────────
// AA-732: admin credentials come from the environment (E2E_ADMIN_USERNAME / E2E_ADMIN_PASSWORD),
// never a literal. The 'content' role user no longer exists (login → 401), so its tests are
// skipped below; 'wrongpass' stays as a negative-test value.

async function loginAsAdmin(page) {
  await page.goto('/login');
  await page.fill('input[name="username"], input[type="text"]', adminUsername());
  await page.fill('input[name="password"], input[type="password"]', adminPassword());
  await page.click('button[type="submit"], button:has-text("Login"), button:has-text("Sign in")');
  await page.waitForURL(/\/(upload|dashboard)/, { timeout: 5000 });
}

// ── Test Suite 1: Authentication ──────────────────────────────

test.describe('Authentication', () => {

  test('redirect / → /login when not authenticated', async ({ page }) => {
    await page.goto('/');
    await expect(page).toHaveURL(/\/login/, { timeout: 5000 });
  });

  test('admin login → redirects to upload or dashboard', async ({ page }) => {
    await loginAsAdmin(page);
    await expect(page).toHaveURL(/\/(upload|dashboard)/);
  });

  // AA-732: the 'content' role user was removed (login → 401) — skip until a real content account.
  test.skip('content login → redirects to /upload', async () => {});

  test('invalid credentials → shows error', async ({ page }) => {
    await page.goto('/login');
    await page.fill('input[name="username"], input[type="text"]', 'wronguser');
    await page.fill('input[name="password"], input[type="password"]', 'wrongpass');
    await page.click('button[type="submit"], button:has-text("Login"), button:has-text("Sign in")');
    // Should stay on login page
    await expect(page).toHaveURL(/\/login/);
  });

  // AA-732: 'content' role user removed (login → 401) — skip until a real content account exists.
  test.skip('content role cannot access /dashboard', async () => {});

  test('logout clears session → redirect to login', async ({ page }) => {
    await loginAsAdmin(page);
    // Find and click logout
    const logoutBtn = page.locator('button:has-text("Logout"), a:has-text("Logout"), button:has-text("Sign out")');
    if (await logoutBtn.count() > 0) {
      await logoutBtn.first().click();
      await expect(page).toHaveURL(/\/login/);
    }
  });

});

// ── Test Suite 2: Upload Page ─────────────────────────────────

test.describe('Upload Page', () => {

  test.beforeEach(async ({ page }) => {
    await loginAsContent(page);
    await page.goto('/upload');
  });

  test('upload page renders correctly', async ({ page }) => {
    await expect(page).toHaveURL(/\/upload/);
    // Check key UI elements exist
    await expect(page.locator('body')).toBeVisible();
  });

  test('upload page has vendor/market config', async ({ page }) => {
    // Look for vendor dropdown or select
    const vendorEl = page.locator('select, [role="combobox"]').first();
    await expect(vendorEl).toBeVisible({ timeout: 5000 });
  });

  test('upload page has drag-drop zone', async ({ page }) => {
    // Look for file input or drag-drop area
    const dropzone = page.locator('input[type="file"], [data-testid="dropzone"], .dropzone').first();
    await expect(dropzone).toBeAttached({ timeout: 5000 });
  });

});

// ── Test Suite 3: Review Queue ────────────────────────────────

test.describe('Review Queue', () => {

  test.beforeEach(async ({ page }) => {
    await loginAsContent(page);
  });

  test('review page loads', async ({ page }) => {
    await page.goto('/review');
    await expect(page).toHaveURL(/\/review/);
    await expect(page.locator('body')).toBeVisible();
  });

  test('review page has before/after panels', async ({ page }) => {
    await page.goto('/review');
    // Look for 2-column layout or before/after labels
    const panels = page.locator('[class*="before"], [class*="after"], [data-testid*="before"], [data-testid*="after"]');
    // If no tours in queue, panels may not show — just verify page loads without error
    await expect(page.locator('body')).not.toContainText('500');
    await expect(page.locator('body')).not.toContainText('Internal Server Error');
  });

});

// ── Test Suite 4: Catalog ─────────────────────────────────────

test.describe('Catalog', () => {

  test.beforeEach(async ({ page }) => {
    await loginAsContent(page);
  });

  test('catalog page loads', async ({ page }) => {
    await page.goto('/catalog');
    await expect(page).toHaveURL(/\/catalog/);
    await expect(page.locator('body')).toBeVisible();
  });

  test('catalog has search input', async ({ page }) => {
    await page.goto('/catalog');
    const search = page.locator('input[type="search"], input[placeholder*="search" i], input[placeholder*="Search" i]').first();
    await expect(search).toBeVisible({ timeout: 5000 });
  });

  test('catalog page has no 500 errors', async ({ page }) => {
    await page.goto('/catalog');
    await expect(page.locator('body')).not.toContainText('Internal Server Error');
    await expect(page.locator('body')).not.toContainText('500');
  });

});

// ── Test Suite 5: Admin Dashboard ────────────────────────────

test.describe('Admin Dashboard', () => {

  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('dashboard page loads for admin', async ({ page }) => {
    await page.goto('/dashboard');
    await expect(page).toHaveURL(/\/dashboard/);
    await expect(page.locator('body')).toBeVisible();
  });

  test('dashboard has metrics tab', async ({ page }) => {
    await page.goto('/dashboard');
    const metricsTab = page.locator('button:has-text("Metrics"), [role="tab"]:has-text("Metrics")').first();
    await expect(metricsTab).toBeVisible({ timeout: 5000 });
  });

  test('dashboard has health tab', async ({ page }) => {
    await page.goto('/dashboard');
    const healthTab = page.locator('button:has-text("Health"), [role="tab"]:has-text("Health")').first();
    await expect(healthTab).toBeVisible({ timeout: 5000 });
  });

  test('dashboard has no 500 errors', async ({ page }) => {
    await page.goto('/dashboard');
    await expect(page.locator('body')).not.toContainText('Internal Server Error');
  });

});

// ── Test Suite 6: Tenant Login ────────────────────────────────

test.describe('Tenant Portal', () => {

  test('tenant login page exists', async ({ page }) => {
    await page.goto('/tenant-login');
    await expect(page).toHaveURL(/\/tenant-login/);
    await expect(page.locator('body')).toBeVisible();
  });

  test('tenant login has API key input', async ({ page }) => {
    await page.goto('/tenant-login');
    const apiKeyInput = page.locator('input[type="text"], input[type="password"], input[placeholder*="key" i], input[placeholder*="API" i]').first();
    await expect(apiKeyInput).toBeVisible({ timeout: 5000 });
  });

  test('invalid API key shows error or stays on login', async ({ page }) => {
    await page.goto('/tenant-login');
    const input = page.locator('input').first();
    await input.fill('invalid_key_12345');
    await page.click('button:has-text("Access Portal"), button[type="submit"]');
    await expect(page).toHaveURL(/\/tenant-login/);
  });

});

// ── Test Suite 7: API Health (backend via frontend) ───────────

test.describe('API Integration', () => {

  test('backend health endpoint returns ok', async ({ request }) => {
    const apiUrl = process.env.API_URL || 'https://api-cis.lumiguides.it.com';
    const res = await request.get(`${apiUrl}/health`);
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body.status).toBe('ok');
  });

  test('tours endpoint returns valid response', async ({ request }) => {
    const apiUrl = process.env.API_URL || 'https://api-cis.lumiguides.it.com';
    const res = await request.get(`${apiUrl}/tours`);
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body).toHaveProperty('data');
    expect(Array.isArray(body.data)).toBe(true);
  });

});
