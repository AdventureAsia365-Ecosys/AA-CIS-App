// tests/e2e/cognito/cognito-login.spec.ts
// AA-816 (E1) — real Cognito email-code sign-in on the production admin host, end to end:
//   /login → "Sign in with company email" → staff-auth.lumiguides.it.com managed login → email address →
//   code read from the SES inbound mailbox (helpers/mailbox.ts) → back on aa-cis → admin shell under /admin;
//   then Sign out → Cognito /logout → /login, and no silent re-login (admin pages bounce to /login, the
//   company-email button shows the managed-login form again).
//
// Runs ONLY in the `cognito` project with E2ECOGNITO=1 (set by .github/workflows/e2e-cognito.yml, which also
// assumes the read-only mailbox role). The E2E user is "E2E Cognito" (e2e@e2e-mail.lumiguides.it.com — the SES
// test mailbox itself, a TEMPORARY exception to the @adventure.asia rule, S243 option B; CIS role reviewer,
// Booking none). Never the break-glass client.
// Only the production host is a registered callback, so this never runs against a preview.
// The code is a live credential: it is never logged, and no screenshot is taken while it may be on screen.
import { test, expect, type Page, type TestInfo } from '@playwright/test';
import fs from 'fs';
import path from 'path';
import { E2E_COGNITO_EMAIL, mailboxBaseline, waitForSignInCode } from '../helpers/mailbox';

const CIS_PROD_ORIGIN = 'https://aa-cis.lumiguides.it.com';
const STAFF_AUTH_HOST = 'staff-auth.lumiguides.it.com';
const SHOTS_DIR = 'tests/e2e/results/cognito';

const onStaffAuth = (page: Page) => {
  try {
    return new URL(page.url()).host === STAFF_AUTH_HOST;
  } catch {
    return false;
  }
};

async function shot(page: Page, name: string) {
  // Never while the managed-login code field could hold a code (see the failure hook below too).
  fs.mkdirSync(SHOTS_DIR, { recursive: true });
  await page.screenshot({ path: path.join(SHOTS_DIR, `${name}.png`), fullPage: false });
}

/** After "Next": either the code page directly (passwordless user), or a sign-in method choice first. */
async function reachCodeStep(page: Page) {
  const codeField = page.getByLabel(/verification code|sign-in code|^code$/i);
  await expect
    .poll(
      async () => {
        if (await codeField.first().isVisible().catch(() => false)) return 'code';
        // A user with a password gets a method choice; pick the email code option.
        const emailChoice = page
          .getByRole('button', { name: /email|one-time code/i })
          .or(page.getByRole('radio', { name: /email/i }))
          .first();
        if (await emailChoice.isVisible().catch(() => false)) {
          await emailChoice.click({ timeout: 5000 });
          const next = page.getByRole('button', { name: /^(next|continue)$/i }).first();
          if (await next.isVisible().catch(() => false)) await next.click({ timeout: 5000 }).catch(() => {});
        }
        return 'waiting';
      },
      { timeout: 30000, intervals: [500, 1000, 2000] },
    )
    .toBe('code');
  return codeField.first();
}

test.describe('AA-816 — Cognito email-code sign-in (production host)', () => {
  test.skip(process.env.E2ECOGNITO !== '1', 'opt-in: run via e2e-cognito.yml (E2ECOGNITO=1 + mailbox role)');

  test.afterEach(async ({ page }, testInfo: TestInfo) => {
    if (testInfo.status === testInfo.expectedStatus) return;
    // Failure evidence without leaking a code: no screenshot on the managed-login host.
    const url = page.url();
    if (onStaffAuth(page)) {
      const heading = await page.locator('h1, h2').first().textContent({ timeout: 2000 }).catch(() => '');
      await testInfo.attach('managed-login-state', {
        body: `url=${url.split('?')[0]}\nheading=${(heading ?? '').trim()}`,
        contentType: 'text/plain',
      });
    } else {
      await shot(page, '99-failure').catch(() => {});
    }
  });

  test('email code signs in, lands under /admin, sign-out does not silently re-login', async ({ page }) => {
    // ── 1. CIS login page → managed login ──
    await page.goto('/login', { waitUntil: 'domcontentloaded' });
    const companyEmail = page.getByRole('link', { name: /sign in with company email/i });
    await expect(companyEmail).toBeVisible({ timeout: 15000 });
    await shot(page, '01-cis-login');
    await companyEmail.click();
    await page.waitForURL((u) => u.host === STAFF_AUTH_HOST, { timeout: 20000 });

    const emailField = page.getByLabel(/email/i).first();
    await expect(emailField).toBeVisible({ timeout: 15000 });
    await emailField.fill(E2E_COGNITO_EMAIL);
    await shot(page, '02-managed-login-email');

    // ── 2. Snapshot the mailbox BEFORE asking for a code, then ask ──
    const baseline = await mailboxBaseline();
    const since = new Date();
    await page.getByRole('button', { name: /^next$/i }).click({ timeout: 10000 });
    const codeField = await reachCodeStep(page);
    await shot(page, '03-managed-login-code-empty');

    // ── 3. Code from S3 (hard 90 s cap inside the helper) ──
    const code = await waitForSignInCode({ since, baseline, timeoutMs: 90000 });
    await codeField.fill(code);
    await page.getByRole('button', { name: /^(continue|sign in|next)$/i }).first().click({ timeout: 10000 });

    // ── 4. Back on CIS, inside the admin shell ──
    await page.waitForURL((u) => u.origin === CIS_PROD_ORIGIN && u.pathname.startsWith('/admin'), {
      timeout: 30000,
    });
    // The shell (AdminShell: sidebar + topbar) wraps every /admin page; the account menu is in its topbar.
    // The landing page is role-aware (reviewer → /admin/review), so only the shell is asserted.
    await expect(page.locator('main').first()).toBeVisible({ timeout: 20000 });
    const account = page.getByRole('button', { name: 'Account menu' });
    await expect(account).toBeVisible({ timeout: 15000 });
    expect(new URL(page.url()).pathname).toMatch(/^\/admin\//);
    const cookies = await page.context().cookies(CIS_PROD_ORIGIN);
    expect(cookies.some((c) => c.name === 'cis_admin_token' && c.value !== '')).toBe(true);
    await shot(page, '04-admin-landing');

    // ── 5. Sign out → Cognito /logout → /login ──
    await account.click();
    await page.getByRole('button', { name: 'Sign out' }).click({ timeout: 5000 });
    await page.waitForURL((u) => u.origin === CIS_PROD_ORIGIN && u.pathname === '/login', { timeout: 30000 });
    await shot(page, '05-after-sign-out');
    const after = await page.context().cookies(CIS_PROD_ORIGIN);
    expect(after.some((c) => c.name === 'cis_admin_token' && c.value !== '')).toBe(false);

    // ── 6. No silent re-login ──
    await page.goto('/admin/dashboard', { waitUntil: 'domcontentloaded' });
    await page.waitForURL((u) => u.pathname === '/login', { timeout: 15000 });
    await page.getByRole('link', { name: /sign in with company email/i }).click({ timeout: 10000 });
    await page.waitForURL((u) => u.host === STAFF_AUTH_HOST, { timeout: 20000 });
    // The managed-login session is gone: Cognito asks for the email again instead of redirecting back.
    await expect(page.getByLabel(/email/i).first()).toBeVisible({ timeout: 15000 });
    expect(new URL(page.url()).host).toBe(STAFF_AUTH_HOST);
    await shot(page, '06-relogin-asks-email');
  });
});
