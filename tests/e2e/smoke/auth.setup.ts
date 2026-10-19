// tests/e2e/smoke/auth.setup.ts
// AA-732 — smoke-only setup project (Playwright `dependencies`). It logs the admin in ONCE and
// persists storageState to tests/e2e/.auth/admin-state.json, so every smoke page test runs
// independently from that state. AA-878: it also records the role the server answered
// (tests/e2e/.auth/admin-role.json); a `viewer` account runs every smoke spec in viewer mode. It is matched only by the `smoke-setup` project
// (testMatch /smoke\/.*\.setup\.ts/) and listed as a dependency of the `smoke` project — the
// legacy `chromium` project never runs it, so legacy runs never log in.
//
// The tenant storageState is NOT created here: no tenant test account exists yet (AA-741) and the
// real tenant login takes an API key. The portal block logs in on demand when E2E_TENANT_API_KEY
// is present.
import { test as setup } from '@playwright/test';
import { loginAsAdmin, adminUsername, adminPassword } from '../helpers/auth';
import fs from 'fs';
import { ADMIN_STATE, ROLE_FILE, baseUrl, ensureAuthDir, installBypassRoute } from './smoke-helpers';

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
    // AA-878 — the role comes from the server's login answer (the DB role, POST /api/auth/login → { role }),
    // never from the display cookie: a viewer account switches the specs to viewer mode.
    const loginResponse = page.waitForResponse(
      (r) => new URL(r.url()).pathname === '/api/auth/login' && r.request().method() === 'POST',
      { timeout: 20000 },
    );
    await loginAsAdmin(page);
    const res = await loginResponse;
    const body = (await res.json().catch(() => ({}))) as { role?: unknown };
    if (!res.ok() || typeof body.role !== 'string' || !body.role) {
      throw new Error(`smoke login failed: POST /api/auth/login → HTTP ${res.status()} (no role in the answer)`);
    }
    await page.waitForURL(/\/admin\//, { timeout: 20000 });
    await context.storageState({ path: ADMIN_STATE });
    fs.writeFileSync(ROLE_FILE, JSON.stringify({ role: body.role }));
    console.log(`[smoke] logged in with staff role: ${body.role}`);
  } finally {
    await context.close();
  }
});
