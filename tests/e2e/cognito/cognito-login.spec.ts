// tests/e2e/cognito/cognito-login.spec.ts
// AA-816 (E1) — real Cognito email-code sign-in on the production admin host, end to end:
//   /login → "Sign in with company email" → staff-auth.lumiguides.it.com managed login → email address →
//   code read from the SES inbound mailbox (helpers/mailbox.ts) → back on aa-cis → admin shell under /admin;
//   then Sign out → Cognito /logout → /login, and no silent re-login (admin pages bounce to /login, the
//   company-email button shows the managed-login form again, and after the address it asks for a NEW email
//   code — a second code mail arrives; it is never typed).
// AA-680 (staff half, evidence): while still signed in, the same WORKFORCE user — who has NO booking-* group — tries
//   the Booking staff admin (booking.lumiguides.it.com → "Sign in with company email"). Booking must refuse:
//   /login?reason=no_booking_role, the "no Booking role" notice, no Booking session cookie, and a Booking page
//   still bounces to /login. Screenshots 04b-04d are the evidence.
//
// Runs ONLY in the `cognito` project with E2ECOGNITO=1 (set by .github/workflows/e2e-cognito.yml, which also
// assumes the read-only mailbox role). The E2E user is "E2E Cognito" (e2e-cognito@adventure.asia — an ordinary
// company address whose mail the adventure.asia mail host forwards to the SES test mailbox; CIS role reviewer,
// Booking none). Never the break-glass client.
// Only the production host is a registered callback, so this never runs against a preview.
// The code is a live credential: it is never logged, and no screenshot is taken while it may be on screen.
import { test, expect, type Page, type TestInfo } from '@playwright/test';
import fs from 'fs';
import path from 'path';
import { E2E_COGNITO_EMAIL, mailboxBaseline, waitForSignInCode } from '../helpers/mailbox';

const CIS_PROD_ORIGIN = 'https://aa-cis.lumiguides.it.com';
// AA-680: the Booking staff admin's production host (a registered callback of the booking-staff client).
const BOOKING_PROD_ORIGIN = 'https://booking.lumiguides.it.com';
// Booking session cookies (AA-Booking frontend/lib/session.ts): none may hold a value after a refusal.
const BOOKING_SESSION_COOKIES = ['bk_access', 'bk_staff_rt', 'bk_cis_session'];
const STAFF_AUTH_HOST = 'staff-auth.lumiguides.it.com';
// Root ADR 0006a A10: the E2E account's only CIS role. Anything wider fails the run (detective guard).
const E2E_COGNITO_ROLE = 'reviewer';
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

/**
 * On the managed-login email form: type the E2E address, ask for a code, read it from the mailbox, submit it.
 * Screenshots only before the code is typed (`emailShot` with the address, `codeShot` with the code field empty).
 */
async function signInByEmailCode(page: Page, emailShot: string, codeShot: string) {
  const emailField = page.getByLabel(/email/i).first();
  await expect(emailField).toBeVisible({ timeout: 15000 });
  await emailField.fill(E2E_COGNITO_EMAIL);
  await shot(page, emailShot);

  // Snapshot the mailbox BEFORE asking for a code, then ask.
  const baseline = await mailboxBaseline();
  const since = new Date();
  await page.getByRole('button', { name: /^next$/i }).click({ timeout: 10000 });
  const codeField = await reachCodeStep(page);
  await shot(page, codeShot);

  // Code from S3 (hard 90 s cap inside the helper).
  const code = await waitForSignInCode({ since, baseline, timeoutMs: 90000 });
  await codeField.fill(code);
  await page.getByRole('button', { name: /^(continue|sign in|next)$/i }).first().click({ timeout: 10000 });
}

/** Booking session cookies that still hold a value (names only — values are never printed). */
async function bookingSessionCookies(page: Page): Promise<string[]> {
  const host = new URL(BOOKING_PROD_ORIGIN).host;
  const all = await page.context().cookies();
  return all
    .filter((c) => (c.domain.replace(/^\./, '') === host) && BOOKING_SESSION_COOKIES.includes(c.name) && c.value !== '')
    .map((c) => c.name);
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
    // Two code mails (up to 90 s + 60 s of mailbox polling) plus the page steps, plus the AA-680 Booking step
    // (normally no code: the managed-login session is shared; up to one more 90 s code wait if it is not).
    test.setTimeout(360000);
    // ── 1. CIS login page → managed login ──
    await page.goto('/login', { waitUntil: 'domcontentloaded' });
    const companyEmail = page.getByRole('link', { name: /sign in with company email/i });
    await expect(companyEmail).toBeVisible({ timeout: 15000 });
    await shot(page, '01-cis-login');
    await companyEmail.click();
    await page.waitForURL((u) => u.host === STAFF_AUTH_HOST, { timeout: 20000 });

    // ── 2-3. Address → code mail → code (signInByEmailCode) ──
    await signInByEmailCode(page, '02-managed-login-email', '03-managed-login-code-empty');

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
    const cisLanding = page.url();

    // ── 4a. Detective guard (root ADR 0006a A10): the E2E account is an ordinary reviewer and must stay one ──
    // Its codes sit in a CI-readable mailbox, so any widening of this account must fail the next run.
    // cis_role is set by the server from the VERIFIED /auth/verify-admin answer at the callback (lib/cognito.ts
    // setDisplayCookies); the admin-only Overview page is checked by proxy.ts against the verified token role.
    const display = await page.context().cookies(CIS_PROD_ORIGIN);
    expect(display.find((c) => c.name === 'cis_role')?.value, 'the E2E account is a CIS reviewer').toBe(
      E2E_COGNITO_ROLE,
    );
    await page.goto('/admin/overview', { waitUntil: 'domcontentloaded' });
    await page.waitForURL((u) => u.origin === CIS_PROD_ORIGIN && u.pathname.startsWith('/admin/review'), {
      timeout: 30000,
    });

    // ── 4b. AA-680: the same WORKFORCE user (CIS reviewer, NO booking-* group) is refused by Booking ──
    // Booking's callback asks the Booking API GET /booking/staff/me with the new token; the API answers 403
    // no_booking_role, the callback revokes the refresh token, sets no session and redirects to
    // /login?reason=no_booking_role (AA-Booking frontend/app/api/auth/callback/cognito/route.ts).
    await page.goto(`${BOOKING_PROD_ORIGIN}/login`, { waitUntil: 'domcontentloaded' });
    const bookingCompany = page.getByRole('link', { name: /sign in with company email/i });
    await expect(bookingCompany).toBeVisible({ timeout: 15000 });
    expect(await bookingSessionCookies(page), 'no Booking session before the attempt').toEqual([]);
    await shot(page, '04b-booking-login');
    await bookingCompany.click();
    // Usually silent: the managed-login session from step 3 is shared by every client of the WORKFORCE pool, so
    // Cognito redirects straight back to Booking. If it asks for the address instead, sign in by code once more.
    const refusedUrl = (u: URL) => u.origin === BOOKING_PROD_ORIGIN && u.pathname === '/login' && u.searchParams.has('reason');
    await expect
      .poll(
        async () => {
          const u = new URL(page.url());
          if (refusedUrl(u)) return 'back-on-booking';
          if (u.host === STAFF_AUTH_HOST && (await page.getByLabel(/email/i).first().isVisible().catch(() => false))) {
            return 'asks-email';
          }
          return 'waiting';
        },
        { timeout: 30000, intervals: [500, 1000, 2000] },
      )
      .not.toBe('waiting');
    if (onStaffAuth(page)) {
      await signInByEmailCode(page, '04b2-booking-managed-login-email', '04b3-booking-managed-login-code-empty');
      await page.waitForURL(refusedUrl, { timeout: 30000 });
    }
    const refused = new URL(page.url());
    expect(refused.origin).toBe(BOOKING_PROD_ORIGIN);
    expect(refused.pathname).toBe('/login');
    expect(refused.searchParams.get('reason')).toBe('no_booking_role');
    // Filtered: Next.js renders its own (empty) route announcer with role="alert" too.
    await expect(page.getByRole('alert').filter({ hasText: /no Booking role/i })).toBeVisible({ timeout: 15000 });
    expect(await bookingSessionCookies(page), 'no Booking session after the refusal').toEqual([]);
    await shot(page, '04c-booking-refused-no-role');
    // …and no way in: a Booking staff page bounces back to /login (proxy.ts: no session → /login?next=…).
    await page.goto(`${BOOKING_PROD_ORIGIN}/`, { waitUntil: 'domcontentloaded' });
    await page.waitForURL((u) => u.origin === BOOKING_PROD_ORIGIN && u.pathname === '/login', { timeout: 15000 });
    expect(await bookingSessionCookies(page), 'still no Booking session').toEqual([]);
    await shot(page, '04d-booking-still-signed-out');

    // Back to CIS: the CIS session is untouched by the Booking refusal.
    await page.goto(cisLanding, { waitUntil: 'domcontentloaded' });
    await page.waitForURL((u) => u.origin === CIS_PROD_ORIGIN && u.pathname.startsWith('/admin'), { timeout: 30000 });
    await expect(account).toBeVisible({ timeout: 15000 });

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
    const emailAgain = page.getByLabel(/email/i).first();
    await expect(emailAgain).toBeVisible({ timeout: 15000 });
    expect(new URL(page.url()).host).toBe(STAFF_AUTH_HOST);
    await shot(page, '06-relogin-asks-email');

    // ── 7. …and a NEW email code (S244): the next sign-in is a full email-code sign-in, not a silent one ──
    // The code is read only to prove Cognito sent a fresh one; it is never typed, logged or compared by value.
    await emailAgain.fill(E2E_COGNITO_EMAIL);
    const baseline2 = await mailboxBaseline();
    const since2 = new Date();
    await page.getByRole('button', { name: /^next$/i }).click({ timeout: 10000 });
    await reachCodeStep(page);
    expect(new URL(page.url()).host).toBe(STAFF_AUTH_HOST);
    await shot(page, '07-relogin-asks-code-empty');
    const secondCode = await waitForSignInCode({ since: since2, baseline: baseline2, timeoutMs: 60000 });
    // Boolean assert: a failing toMatch would print the received value.
    expect(/^\d{6,8}$/.test(secondCode), 'a second code mail arrived').toBe(true);
    // Still on the managed-login page: nothing signed the browser back in while the code was pending.
    expect(new URL(page.url()).host).toBe(STAFF_AUTH_HOST);
    const stillOut = await page.context().cookies(CIS_PROD_ORIGIN);
    expect(stillOut.some((c) => c.name === 'cis_admin_token' && c.value !== '')).toBe(false);
  });
});
