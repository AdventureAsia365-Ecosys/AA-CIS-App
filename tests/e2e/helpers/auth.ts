// tests/e2e/helpers/auth.ts
// AA-732 — the single login helper for every e2e spec. Credentials come only from the
// environment (E2E_ADMIN_USERNAME / E2E_ADMIN_PASSWORD), never from a literal in a spec file.
// The old hardcoded admin user is being deactivated; no credential literal lives in the repo now.
//
// Why a shared helper: 23 spec files each inlined the same username/password pair. A credential
// in version control is a standing leak and a rotation blocker — one helper, one env source.
import type { Page } from '@playwright/test';

/** Read a required env var or throw a clear message naming it (never echo the value). */
function requireEnv(name: string): string {
  const v = process.env[name];
  if (!v) {
    throw new Error(
      `Missing env var ${name}. Admin e2e specs need E2E_ADMIN_USERNAME + E2E_ADMIN_PASSWORD ` +
        `(set as GitHub secrets in CI; export locally to run).`,
    );
  }
  return v;
}

export function adminUsername(): string {
  return requireEnv('E2E_ADMIN_USERNAME');
}

export function adminPassword(): string {
  return requireEnv('E2E_ADMIN_PASSWORD');
}

/**
 * Log in as the admin test user through the real /login form. Fills the username + password
 * fields from the environment and submits. Does NOT wait for a specific post-login URL — callers
 * that need that keep their own `waitForURL` (post-login landing differs per spec/legacy page).
 */
export async function loginAsAdmin(page: Page): Promise<void> {
  await page.goto('/login');
  await page.fill('input[type="text"]', adminUsername());
  await page.fill('input[type="password"]', adminPassword());
  await page.click('button:has-text("Login")');
}
