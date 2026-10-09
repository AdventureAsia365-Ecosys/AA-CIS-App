// tests/e2e/smoke/auth.setup.ts
// AA-732 — smoke-only setup project (Playwright `dependencies`). It logs the admin in ONCE and
// persists storageState to tests/e2e/.auth/admin-state.json, so every smoke page test runs
// independently from that state. It is matched only by the `smoke-setup` project
// (testMatch /smoke\/.*\.setup\.ts/) and listed as a dependency of the `smoke` project — the
// legacy `chromium` project never runs it, so legacy runs never log in.
//
// The tenant storageState is NOT created here: no tenant test account exists yet (AA-741) and the
// real tenant login takes an API key. The portal block logs in on demand when E2E_TENANT_API_KEY
// is present.
import { test as setup } from '@playwright/test';
import { loginAsAdmin, adminUsername, adminPassword } from '../helpers/auth';
import { ADMIN_STATE, baseUrl, ensureAuthDir, installBypassRoute } from './smoke-helpers';

setup('authenticate admin', async ({ browser }) => {
  ensureAuthDir();

  // Fail loudly and clearly if the admin credentials are not in the environment — a missing
  // secret should break CI here, not on a downstream content assertion. `adminUsername()` /
  // `adminPassword()` throw `Missing env var E2E_ADMIN_USERNAME|PASSWORD`.
  adminUsername();
  adminPassword();

  const context = await browser.newContext({ baseURL: baseUrl() });
  await installBypassRoute(context);
  const page = await context.newPage();
  try {
    await loginAsAdmin(page);
    await page.waitForURL(/\/admin\//, { timeout: 20000 });
    await context.storageState({ path: ADMIN_STATE });
  } finally {
    await context.close();
  }
});
