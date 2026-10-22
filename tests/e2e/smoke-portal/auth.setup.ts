// tests/e2e/smoke-portal/auth.setup.ts
// AA-885 — `smoke-portal-setup` project: the tenant logs in ONCE on the portal host with the real
// form (API key, "Access Portal") and the storage state is written to tests/e2e/.auth/tenant-state.json
// (never committed, never uploaded; the workflow deletes it). Skips (not fails) without
// E2E_TENANT_API_KEY, so the unauthenticated host checks still run.
import { expect, test as setup } from '@playwright/test';
import { baseUrl, ensureAuthDir, installBypassRoute } from '../smoke/smoke-helpers';
import { NO_KEY_REASON, TENANT_STATE, hasTenantKey } from './portal-helpers';

setup('authenticate tenant', async ({ browser }) => {
  setup.skip(!hasTenantKey(), NO_KEY_REASON);
  ensureAuthDir();
  const context = await browser.newContext({ baseURL: baseUrl() });
  await installBypassRoute(context);
  const page = await context.newPage();
  try {
    await page.goto('/tenant-login', { waitUntil: 'load' });
    // AA-760 — a value typed before React hydrates the controlled input is reset to "" (the form then
    // says "Please enter your API key" and the login times out; seen on the PR #925 preview). Fill until
    // the input keeps the value, then submit.
    const input = page.locator('input[type="password"]');
    await expect(async () => {
      await input.fill(process.env.E2E_TENANT_API_KEY!);
      await page.waitForTimeout(250);
      await expect(input).not.toHaveValue('');
    }).toPass({ timeout: 15000 });
    await page.click('button:has-text("Access Portal")');
    await page.waitForURL(/\/portal\//, { timeout: 20000 });
    await context.storageState({ path: TENANT_STATE });
  } finally {
    await context.close();
  }
});
