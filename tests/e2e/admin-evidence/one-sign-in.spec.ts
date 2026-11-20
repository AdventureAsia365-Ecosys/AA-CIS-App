// AA-1150 (lean staff roles step 1): ONE company sign-in opens CIS /admin and then the Booking staff app, with no second
// sign-in prompt (the WORKFORCE managed-login session is reused; root ADR 0006 "one identity, every staff app").
// Agent-only, NEVER CI (codes land in inbound-admin/, which only the MFA admin role reads; no workflow sets E2ECS):
//
//   cd apps/AA-CIS-App && AWS_PROFILE=aa365-admin E2ECS=1 npx playwright test --project=admin-evidence one-sign-in
//
// Identity: the Dev-only non-admin E2E account e2e-cis-cs@ (CIS customer_success; root ADR 0006a A10d). Read-only in
// CIS, its Booking reads are plain staff reads. Its Booking role is NOT permanent: it holds Booking contracting only
// while an owner has given it the "Pricing team" preset (ADR 0006a A10e-2), and the ADR has the owner remove the preset
// after each run (S280 did so on 14/11/2026). So, per the account's state on the Staff page:
//
//   - preset held (Booking role Contracting): the default mode, EXPECT_BOOKING=role, ends on a Booking staff page with
//     a `contracting` session. Before the run an owner applies the preset (Platform > Staff > e2e-cis-cs, "Show all
//     roles", the "Pricing team preset"); after it, the owner sets its Booking role back to None.
//   - no preset (Booking role None, the account's resting state): run with EXPECT_BOOKING=refused. Booking's E2E scope
//     rule (AA-Booking app/auth/e2e_staff.py: this address only with exactly cis-customer_success +
//     booking-contracting) refuses it, so the success state is the callback's landing Booking /login?reason=forbidden, reached with no
//     e-mail field on the way: the one sign-in was reused, Booking then said no. The role mode fails on that landing
//     with a message naming the missing preset, instead of timing out.
//
// E2E account at rest (no preset):
//
//   cd apps/AA-CIS-App && AWS_PROFILE=aa365-admin E2ECS=1 EXPECT_BOOKING=refused \
//     npx playwright test --project=admin-evidence one-sign-in
//
// Real-account repeat (Done-when, the content + operations staff member): a person signs in by hand in a headed browser
// (the script never reads their mailbox) and the same hops run:
//
//   E2ECS=1 ONE_SIGN_IN_EMAIL=<their company address> EXPECT_BOOKING=none \
//     npx playwright test --project=admin-evidence one-sign-in --headed
//
// EXPECT_BOOKING=none: the "Content + Product & operations" preset gives NO Booking role until custom roles exist. The
// silent sign-in still completes without a second prompt, then the Booking callback refuses it (API 403 no_booking_role):
// no Booking session, the refresh token is revoked and the person lands on Booking /login?reason=no_booking_role, which
// shows the "no Booking role yet" message. That landing, with no e-mail field on screen, is the success state.
//
// Output (tests/e2e/results/admin-evidence/, gitignored): one-sign-in-*.png and one-sign-in-hops.json (hosts and paths
// of every top-level navigation, no query strings), and a Playwright trace one-sign-in-trace.zip that starts only AFTER
// the e-mail code was typed. The trace holds session cookies of the E2E account: keep it local, delete it after review.
import { test, expect, type Page } from '@playwright/test';
import fs from 'fs';
import path from 'path';
import { E2E_CIS_CS_EMAIL, MAILBOX_ADMIN_PREFIX } from '../helpers/mailbox';
import { STAFF_AUTH_HOST, signInByEmailCode } from '../helpers/cognitoSignIn';

const CIS = 'https://aa-cis.lumiguides.it.com';
const BOOKING = (process.env.BOOKING_BASE_URL || 'https://booking.lumiguides.it.com').replace(/\/+$/, '');
const OUT = 'tests/e2e/results/admin-evidence';
const MANUAL_EMAIL = (process.env.ONE_SIGN_IN_EMAIL || '').trim().toLowerCase();
// 'role' (default): a Booking staff session; 'none': the real-account no-role landing; 'refused': the E2E account
// without the Pricing team preset (see the header).
const EXPECT_BOOKING: 'role' | 'none' | 'refused' =
  process.env.EXPECT_BOOKING === 'none' ? 'none' : process.env.EXPECT_BOOKING === 'refused' ? 'refused' : 'role';

type Hop = { at: string; host: string; path: string };
// Module scope so afterEach can save them when a step fails (a failed run still leaves its hops and trace).
const hops: Hop[] = [];
let tracing = false;

async function shot(page: Page, name: string) {
  fs.mkdirSync(OUT, { recursive: true });
  await page.screenshot({ path: path.join(OUT, `one-sign-in-${name}.png`), fullPage: true });
}

/** The visible e-mail field of the managed login: its presence after the first sign-in IS the second prompt. */
async function emailPromptVisible(page: Page): Promise<boolean> {
  if (new URL(page.url()).host !== STAFF_AUTH_HOST) return false;
  return page.getByLabel(/email/i).first().isVisible().catch(() => false);
}

test.describe('AA-1150 — one sign-in opens CIS and Booking', () => {
  test.afterEach(async ({ context, page }) => {
    fs.mkdirSync(OUT, { recursive: true });
    fs.writeFileSync(path.join(OUT, 'one-sign-in-hops.json'), JSON.stringify(hops, null, 2));
    if (tracing) {
      // A failure screenshot, never on the managed-login host (a code may be on screen there).
      if (new URL(page.url()).host !== STAFF_AUTH_HOST) await shot(page, 'failure').catch(() => {});
      await context.tracing.stop({ path: path.join(OUT, 'one-sign-in-trace.zip') }).catch(() => {});
      tracing = false;
    }
  });
  test.skip(process.env.E2ECS !== '1', 'opt-in, agent-only: E2ECS=1, AWS_PROFILE=aa365-admin (or ONE_SIGN_IN_EMAIL, headed)');

  test('sign in once on CIS, then Booking opens without a second prompt', async ({ page, context }) => {
    test.setTimeout(MANUAL_EMAIL ? 600000 : 300000);
    hops.length = 0;
    page.on('framenavigated', (frame) => {
      if (frame !== page.mainFrame()) return;
      const u = new URL(frame.url());
      hops.push({ at: new Date().toISOString(), host: u.host, path: u.pathname });
    });

    // 1. The one sign-in, on CIS (no screenshot or trace while a code may be on screen).
    await page.goto(`${CIS}/login`, { waitUntil: 'domcontentloaded' });
    await page.getByRole('link', { name: /sign in with company email/i }).click({ timeout: 15000 });
    await page.waitForURL((u) => u.host === STAFF_AUTH_HOST, { timeout: 20000 });
    if (MANUAL_EMAIL) {
      // A real account: the person types their code in the headed browser; the script only waits.
      await page.getByLabel(/email/i).first().fill(MANUAL_EMAIL);
      await page.waitForURL((u) => u.origin === CIS && u.pathname.startsWith('/admin'), { timeout: 540000 });
    } else {
      await signInByEmailCode(page, { email: E2E_CIS_CS_EMAIL, prefix: MAILBOX_ADMIN_PREFIX });
      await page.waitForURL((u) => u.origin === CIS && u.pathname.startsWith('/admin'), { timeout: 30000 });
    }
    const signInsBefore = hops.filter((h) => h.host === STAFF_AUTH_HOST).length;
    await context.tracing.start({ screenshots: true, snapshots: true, title: 'AA-1150 one sign-in' });
    tracing = true;

    // 2. CIS opens for the signed-in person: the page the sign-in landed on (the role's landing page, lib/rbac.ts
    // landingPage), loaded again with the session cookie. Not bare /admin: it has no page rule, so the proxy fails
    // closed to /login (S286 live check F2).
    const landing = new URL(page.url()).pathname;
    await page.goto(`${CIS}${landing}`, { waitUntil: 'domcontentloaded' });
    await page.waitForURL((u) => u.origin === CIS && u.pathname === landing, { timeout: 30000 });
    await expect(page.getByRole('button', { name: 'Account menu' })).toBeVisible({ timeout: 30000 });
    await shot(page, 'cis-admin');

    // 3. Booking: its login hands off to the managed login, which must send the person straight back (no e-mail field).
    await page.goto(`${BOOKING}/login`, { waitUntil: 'domcontentloaded' });
    const go = page.getByRole('link', { name: /sign in with company email/i })
      .or(page.getByRole('button', { name: /sign in with company email/i })).first();
    if (await go.isVisible({ timeout: 10000 }).catch(() => false)) await go.click();
    // Success state per mode: 'role' = back on a Booking staff page; 'none' = the callback's refusal landing,
    // Booking /login?reason=no_booking_role; 'refused' = Booking /login?reason=forbidden (the plain /login we started
    // from does not count).
    const landed = EXPECT_BOOKING === 'none' ? 'no-role-landing'
      : EXPECT_BOOKING === 'refused' ? 'refused-landing' : 'back-on-booking';
    await expect.poll(async () => {
      if (await emailPromptVisible(page)) return 'second-prompt';
      const u = new URL(page.url());
      if (u.origin !== BOOKING) return 'redirecting';
      if (u.pathname.startsWith('/login')) {
        const reason = u.searchParams.get('reason');
        if (reason === 'no_booking_role') return 'no-role-landing';
        if (reason === 'forbidden') return 'refused-landing';
        return 'redirecting';
      }
      return 'back-on-booking';
    }, {
      timeout: 45000, intervals: [500, 1000, 2000],
      message: EXPECT_BOOKING === 'role'
        ? 'Booking did not open a staff session. Landing on reason=forbidden means e2e-cis-cs@ holds no Pricing team '
          + 'preset: an owner applies it on the Staff page first, or run EXPECT_BOOKING=refused (see the header).'
        : `Booking did not end on the ${landed} state`,
    }).toBe(landed);

    if (EXPECT_BOOKING === 'refused') {
      // Booking refused the account after the silent hand-off: no Booking session (the BFF answers 401).
      const me = await context.request.get(`${BOOKING}/api/booking/staff/me`, { timeout: 20000 });
      await expect(page.getByText(/refused this account/i)).toBeVisible({ timeout: 15000 });
      expect(me.status(), 'no Booking session after a refused sign-in').toBe(401);
    } else if (EXPECT_BOOKING === 'none') {
      // The refusal message is on screen, and no Booking session was set (the BFF answers 401, not a staff profile).
      await expect(page.getByText(/no Booking role yet/i)).toBeVisible({ timeout: 15000 });
      const me = await context.request.get(`${BOOKING}/api/booking/staff/me`, { timeout: 20000 });
      expect(me.status(), 'no Booking session after a no-role sign-in').toBe(401);
    } else {
      const me = await context.request.get(`${BOOKING}/api/booking/staff/me`, { timeout: 20000 });
      expect(me.status()).toBe(200);
      const body = await me.json();
      expect(body.email?.toLowerCase()).toBe(E2E_CIS_CS_EMAIL);
      expect(body.role).toBe('contracting');
    }
    await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
    await shot(page, EXPECT_BOOKING === 'none' ? 'booking-no-role'
      : EXPECT_BOOKING === 'refused' ? 'booking-refused' : 'booking-staff');

    await context.tracing.stop({ path: path.join(OUT, 'one-sign-in-trace.zip') });
    tracing = false;
    fs.writeFileSync(path.join(OUT, 'one-sign-in-hops.json'), JSON.stringify(hops, null, 2));
    // The managed login was passed through at most once more (the silent hand-off), never shown again.
    const handOffs = hops.filter((h) => h.host === STAFF_AUTH_HOST).length - signInsBefore;
    expect(handOffs, 'silent hand-offs to the managed login after the first sign-in').toBeLessThanOrEqual(2);
  });
});
