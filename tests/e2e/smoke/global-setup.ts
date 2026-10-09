// tests/e2e/smoke/global-setup.ts
// AA-732 — log in the admin ONCE and persist storageState, so every page test in the smoke suite
// is fully independent (no serial mode, no shared beforeAll ordering). A red page then cannot skip
// the others.
//
// The tenant storageState is NOT created here: no tenant test account exists yet (AA-741) and the
// real tenant login takes an API key, not email/password. The portal block skips when
// E2E_TENANT_API_KEY is absent.
import { chromium, type FullConfig } from '@playwright/test';
import fs from 'fs';
import { loginAsAdmin } from '../helpers/auth';
import { ADMIN_STATE, SMOKE_RESULTS_DIR, baseUrl, installBypassRoute } from './smoke-helpers';

export default async function globalSetup(_config: FullConfig): Promise<void> {
  fs.mkdirSync(SMOKE_RESULTS_DIR, { recursive: true });

  // Only admin credentials gate the admin block. If they are absent, write an empty storageState
  // so per-test context creation does not crash with ENOENT; the admin tests then fail fast with
  // the helper's clear "Missing env var E2E_ADMIN_*" message when they actually navigate.
  if (!process.env.E2E_ADMIN_USERNAME || !process.env.E2E_ADMIN_PASSWORD) {
    fs.writeFileSync(ADMIN_STATE, JSON.stringify({ cookies: [], origins: [] }));
    return;
  }

  const browser = await chromium.launch();
  const context = await browser.newContext({ baseURL: baseUrl() });
  await installBypassRoute(context);
  const page = await context.newPage();
  try {
    await loginAsAdmin(page);
    await page.waitForURL(/\/admin\//, { timeout: 20000 });
    await context.storageState({ path: ADMIN_STATE });
  } finally {
    await context.close();
    await browser.close();
  }
}
