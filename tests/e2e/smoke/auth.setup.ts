// tests/e2e/smoke/auth.setup.ts
// AA-732 — smoke-only setup project (Playwright `dependencies`). It signs the staff smoke account in ONCE and
// persists storageState to tests/e2e/.auth/admin-state.json, so every authenticated smoke test (tag @staff) runs
// independently from that state. AA-878: it also records the session's role (tests/e2e/.auth/admin-role.json); a
// non-admin role runs the specs in that role's mode (viewer mode for the read-only viewer). It is matched only by
// the `smoke-setup` project and listed as a dependency of the `smoke` project — the `smoke-public` project (the
// per-PR preview run) and the legacy `chromium` project never run it.
//
// AA-816 S2 path — Cognito only (E2ECOGNITO=1, set only by the staff job of .github/workflows/ui-smoke.yml, which
// also assumes the read-only mailbox role): the E2E staff user "E2E Cognito" signs in with an email code
// (helpers/cognitoSignIn.ts) on one of the two hosts that are registered callbacks: the production host (cis-admin
// client; the user is a reviewer) or the fixed Preview host (cis-admin-preview client; the API caps every session
// there at viewer, so the setup FAILS unless the role is viewer — that is the cap's live evidence).
// The legacy password mode is gone (AA-816 S2: the API refuses every HS256 session on /auth/verify-admin). Without
// E2ECOGNITO=1 (a local run has no mailbox access) the setup fails with a clear message: run `smoke-public` locally.
//
// The tenant storageState is NOT created here (the portal smoke has its own setup, tests/e2e/smoke-portal/).
import { test as setup, type Browser } from '@playwright/test';
import { STAFF_AUTH_HOST, signInByEmailCode } from '../helpers/cognitoSignIn';
import fs from 'fs';
import {
  ADMIN_STATE, PREVIEW_SIGNIN_ORIGIN, PROD_ORIGIN, ROLE_FILE, baseOrigin, baseUrl, ensureAuthDir, installBypassRoute,
} from './smoke-helpers';

/** The display cookie the BFF sets from the /auth/verify-admin answer at the end of a sign-in (frontend/lib/cognito.ts). */
const ROLE_COOKIE = 'cis_role';

async function cognitoSignIn(browser: Browser): Promise<string> {
  const origin = baseOrigin();
  // Only the two registered callback hosts; anything else fails before any navigation.
  if (origin !== PROD_ORIGIN && origin !== PREVIEW_SIGNIN_ORIGIN) {
    throw new Error(`E2ECOGNITO=1 needs BASE_URL ${PROD_ORIGIN} or ${PREVIEW_SIGNIN_ORIGIN} (got ${origin})`);
  }
  const context = await browser.newContext({ baseURL: baseUrl() });
  await installBypassRoute(context);
  const page = await context.newPage();
  try {
    await page.goto('/login', { waitUntil: 'domcontentloaded' });
    const companyEmail = page.getByRole('link', { name: /sign in with company email/i });
    const offered = await companyEmail.waitFor({ state: 'visible', timeout: 15000 }).then(() => true, () => false);
    if (!offered) {
      // On the Preview host this means the preview build has no client secret (Vercel Preview
      // COGNITO_CIS_ADMIN_PREVIEW_CLIENT_SECRET) or the host points at a build made before it was set.
      throw new Error(`${origin}/login offers no "Sign in with company email" (Cognito sign-in unavailable on this host)`);
    }
    await companyEmail.click();
    await page.waitForURL((u) => u.host === STAFF_AUTH_HOST, { timeout: 20000 });
    // No screenshots in this setup at all: the code page must never be captured.
    await signInByEmailCode(page);
    await page.waitForURL((u) => u.origin === origin && u.pathname.startsWith('/admin'), { timeout: 30000 });
    const cookies = await context.cookies(origin);
    if (!cookies.some((c) => c.name === 'cis_admin_token' && c.value !== '')) {
      throw new Error('Cognito sign-in landed under /admin but set no cis_admin_token');
    }
    // The role the server verified (the BFF writes cis_role from /auth/verify-admin, never from the browser).
    const role = cookies.find((c) => c.name === ROLE_COOKIE)?.value ?? '';
    if (!role) throw new Error('Cognito sign-in set no cis_role display cookie');
    if (origin === PREVIEW_SIGNIN_ORIGIN && role !== 'viewer') {
      // AA-911 D-1: every cis-admin-preview session must be the viewer. Anything else is a broken cap: stop here.
      throw new Error(`Preview host session has role "${role}", expected "viewer" (the Preview client cap)`);
    }
    await context.storageState({ path: ADMIN_STATE });
    return role;
  } finally {
    await context.close();
  }
}

setup('authenticate staff', async ({ browser }) => {
  ensureAuthDir();
  if (process.env.E2ECOGNITO !== '1') {
    throw new Error(
      'The signed-in smoke needs E2ECOGNITO=1 and the e2e mailbox role (ui-smoke.yml job smoke-staff, from main). ' +
        'The legacy password login is off since AA-816 S2; run --project=smoke-public locally.',
    );
  }
  setup.setTimeout(180000); // one code mail (up to 90 s) plus the page steps
  const role = await cognitoSignIn(browser);
  fs.writeFileSync(ROLE_FILE, JSON.stringify({ role }));
  console.log(`[smoke] signed in (cognito) with staff role: ${role}`);
});
