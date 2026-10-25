// tests/e2e/admin-evidence/admin-pages.spec.ts
// AA-816 A10b (S252, root ADR 0006a A10b) — agent-only evidence run as the Dev-only E2E CIS admin
// (e2e-cis-admin@e2e-mail.lumiguides.it.com, cis-admin only). NEVER CI: the code mail lands under inbound-admin/,
// which only the MFA admin role can read (bucket policy Deny for every other principal), so this runs on the
// orchestrator's machine with the 8 h MFA session:
//
//   cd apps/AA-CIS-App && AWS_PROFILE=aa365-admin E2EADMIN=1 npx playwright test --project admin-evidence
//
// Steps: /login → "Sign in with company email" → managed login → the admin address → code from inbound-admin/ →
// back under /admin (cis_admin_token cookie, Account menu) → /admin/content-quality and /admin/writing-memory at
// 1440x900 (grown to the inner scroll region) and 390x844, light and dark → API checks through the BFF that write
// nothing: a refused write (403 e2e_read_only), the allow-listed sentence apply on a missing piece (404, so it passed
// every auth layer), the same route with mode rewrite (403) → the Booking staff admin refuses the same WORKFORCE user
// (/login?reason=no_booking_role) → sign out.
// Screenshots go to tests/e2e/results/admin-evidence/ (gitignored); copy them to the root repo's docs/audits/ for
// evidence.
//
// Hygiene (same as cognito-login.spec.ts): the code is a live credential, never logged or printed; no screenshot,
// trace or video while on staff-auth after the code is typed (the project sets them off; the failure hook only
// attaches the URL and heading there); retries 0 (a retry asks for another code); one sign-in per run.
import { test, expect, type Page, type TestInfo } from '@playwright/test';
import fs from 'fs';
import path from 'path';
import { E2E_CIS_ADMIN_EMAIL, MAILBOX_ADMIN_PREFIX } from '../helpers/mailbox';
import { STAFF_AUTH_HOST, onStaffAuth, signInByEmailCode } from '../helpers/cognitoSignIn';
import { growViewportToContent } from '../smoke/smoke-helpers';

const CIS_PROD_ORIGIN = 'https://aa-cis.lumiguides.it.com';
const SHOTS_DIR = 'tests/e2e/results/admin-evidence';
const PAGES = ['/admin/content-quality', '/admin/writing-memory'] as const;
// Ids that do not exist: nothing is ever written by this spec.
const MISSING_CARD = '00000000-0000-4000-8000-00000000a10b';
const MISSING_PIECE = '00000000-0000-4000-8000-00000000a10c';
const BOOKING_ORIGIN = 'https://booking.lumiguides.it.com';

/** A same-origin request through the BFF from the page (sends the session cookie and the Origin header). */
async function bff(page: Page, method: string, apiPath: string, body: unknown) {
  return page.evaluate(
    async ({ method, apiPath, body }) => {
      const res = await fetch(apiPath, {
        method,
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      });
      return { status: res.status, body: await res.text() };
    },
    { method, apiPath, body },
  );
}

async function shot(page: Page, name: string, fullPage = false) {
  fs.mkdirSync(SHOTS_DIR, { recursive: true });
  await page.screenshot({ path: path.join(SHOTS_DIR, `${name}.png`), fullPage });
}

async function waitForPageData(page: Page) {
  await expect(page.locator('main').first()).toBeVisible({ timeout: 20000 });
  // First data row or the kit's empty state; skeletons gone.
  await expect
    .poll(
      async () => {
        const rows = await page.locator('main table tbody tr, main [role="row"]').count().catch(() => 0);
        const empty = await page.getByText(/no (results|data|items|pieces|rules|signals)|nothing (here|yet)/i)
          .first().isVisible().catch(() => false);
        const loading = await page.locator('[aria-busy="true"], .aa-skeleton').count().catch(() => 0);
        return (rows > 0 || empty) && loading === 0 ? 'ready' : 'waiting';
      },
      { timeout: 30000, intervals: [500, 1000, 2000] },
    )
    .toBe('ready');
}

test.describe('AA-816 A10b — admin evidence as the Dev-only E2E CIS admin', () => {
  test.skip(process.env.E2EADMIN !== '1', 'opt-in, agent-only: E2EADMIN=1 with AWS_PROFILE=aa365-admin (MFA admin role)');

  test.afterEach(async ({ page }, testInfo: TestInfo) => {
    if (testInfo.status === testInfo.expectedStatus) return;
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

  test('signs in as admin, screenshots the admin-only pages, a write is refused', async ({ page }) => {
    test.setTimeout(300000);
    // ── 1. CIS login → managed login ──
    await page.goto('/login', { waitUntil: 'domcontentloaded' });
    const companyEmail = page.getByRole('link', { name: /sign in with company email/i });
    await expect(companyEmail).toBeVisible({ timeout: 15000 });
    await companyEmail.click();
    await page.waitForURL((u) => u.host === STAFF_AUTH_HOST, { timeout: 20000 });

    // ── 2-6. Address → baseline of inbound-admin/ → Next → code from inbound-admin/ → submit ──
    await signInByEmailCode(page, { email: E2E_CIS_ADMIN_EMAIL, prefix: MAILBOX_ADMIN_PREFIX });
    await page.waitForURL((u) => u.origin === CIS_PROD_ORIGIN && u.pathname.startsWith('/admin'), {
      timeout: 30000,
    });
    const cookies = await page.context().cookies(CIS_PROD_ORIGIN);
    expect(cookies.some((c) => c.name === 'cis_admin_token' && c.value !== '')).toBe(true);
    const account = page.getByRole('button', { name: 'Account menu' });
    await expect(account).toBeVisible({ timeout: 15000 });

    // ── 7. Admin-only pages: 1440 (grown to the inner scroll region) + 390, light + dark ──
    for (const pagePath of PAGES) {
      const slug = pagePath.replace(/^\/admin\//, '').replace(/\//g, '-');
      await page.setViewportSize({ width: 1440, height: 900 });
      await page.goto(pagePath, { waitUntil: 'domcontentloaded' });
      expect(new URL(page.url()).pathname).toBe(pagePath);   // not bounced: the session is an admin
      await waitForPageData(page);
      for (const scheme of ['light', 'dark'] as const) {
        await page.emulateMedia({ colorScheme: scheme });
        await page.setViewportSize({ width: 1440, height: 900 });
        await page.waitForTimeout(300);
        await growViewportToContent(page, 1440, 900);
        await shot(page, `${slug}-desktop-${scheme}`, true);
        await page.setViewportSize({ width: 390, height: 844 });
        await page.waitForTimeout(300);
        await shot(page, `${slug}-mobile-${scheme}`);
      }
      await page.emulateMedia({ colorScheme: 'light' });
    }

    // ── API checks through the BFF; none of them writes anything ──
    await page.setViewportSize({ width: 1440, height: 900 });
    const refused = await bff(page, 'PATCH', `/api/admin/content-quality/place-cards/${MISSING_CARD}`,
      { status: 'approved' });
    expect(refused.status, 'a write outside the allow-list').toBe(403);
    expect(refused.body).toContain('e2e_read_only');
    const span = { start: 0, end: 5, text: 'Hello', reason_code: null };
    const allowed = await bff(page, 'POST', `/api/admin/content-quality/pieces/${MISSING_PIECE}/sentences/apply`,
      { ...span, mode: 'cut', replacement: '' });
    expect(allowed.status, 'the allow-listed sentence apply reaches the handler').toBe(404);
    expect(allowed.body).toContain('Piece not found');
    const rewrite = await bff(page, 'POST', `/api/admin/content-quality/pieces/${MISSING_PIECE}/sentences/apply`,
      { ...span, mode: 'rewrite', replacement: 'Hi there' });
    expect(rewrite.status, 'mode rewrite is refused').toBe(403);
    expect(rewrite.body).toContain('e2e_read_only');
    const suggest = await bff(page, 'POST', `/api/admin/content-quality/pieces/${MISSING_PIECE}/sentences/suggest`,
      { ...span, problem: 'check' });
    expect(suggest.status, 'the paid suggest call is refused').toBe(403);

    // ── Booking refuses the same WORKFORCE user (no booking-* group) ──
    const cisAdminUrl = page.url();
    await page.goto(`${BOOKING_ORIGIN}/login`, { waitUntil: 'domcontentloaded' });
    await page.getByRole('link', { name: /sign in with company email/i }).click({ timeout: 15000 });
    // Silent: the managed-login session is shared by every WORKFORCE client. If it asks for the address instead,
    // stop here (a second code would be needed): the CIS evidence above is already saved.
    await page.waitForURL((u) => (u.origin === BOOKING_ORIGIN && u.pathname === '/login' && u.searchParams.has('reason'))
      || u.host === STAFF_AUTH_HOST, { timeout: 30000 });
    if (!onStaffAuth(page)) {
      expect(new URL(page.url()).searchParams.get('reason')).toBe('no_booking_role');
      await shot(page, 'booking-refused-no-role');
    } else {
      test.info().annotations.push({ type: 'note', description: 'Booking asked for the address again; not checked' });
    }
    await page.goto(cisAdminUrl, { waitUntil: 'domcontentloaded' });
    await expect(account).toBeVisible({ timeout: 15000 });

    // ── 8. Sign out ──
    await account.click();
    await page.getByRole('button', { name: 'Sign out' }).click({ timeout: 5000 });
    await page.waitForURL((u) => u.origin === CIS_PROD_ORIGIN && u.pathname === '/login', { timeout: 30000 });
  });
});
