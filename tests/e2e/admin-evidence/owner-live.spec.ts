// AA-1176 (root ADR 0006a A10h): the Dev-only E2E owner e2e-owner@ (cis-admin + booking-admin, owner marker, NOT
// read-only) runs the owner-only live checks the tech lead asked for, in ONE browser (one company sign-in):
//
//   1. CIS sign-in by e-mail code (codes in inbound-admin/, MFA admin role only) — CIS FIRST: it links the Cognito sub,
//      which Booking needs to see the owner marker (shared.v_staff_booking_grant lists linked rows only);
//   2. CIS view as Content (POST /api/view-as/roles 201), banner screenshot, Back (DELETE);
//   3. Platform > Compliance > Refresh (POST /admin/platform/compliance/refresh 200; the CRM evidence must not be
//      mfa_required);
//   4. optional, E2EOWNER_INVITE=1: Platform > Staff > Add info@adventure.asia as the CIS custom role Content
//      (custom_content), no Booking role, send the invite (201). A 409 (the row exists) stops the step: report it.
//      This mails the real company mailbox; the tech lead asked for it once — never set it on a re-run;
//   5. Booking, same browser (silent reuse): /booking/staff/me role admin; view as sales and Back;
//   6. optional, BOOKING_SYNTHETIC_PLAN=<source id of a SYNTHETIC trip plan, A10e-2 / A10e-3 only, never a catalogue
//      tour>: read its operability, record a sale exception for one blocking rule (SALE_EXCEPTION_RULE, default the
//      first blocking finding), then revoke it.
//
// Agent-only, NEVER CI (no workflow sets E2EOWNER; tests/unit/test_aa816_e2e_cis_admin.py checks it):
//
//   cd apps/AA-CIS-App && AWS_PROFILE=aa365-admin E2EOWNER=1 [E2EOWNER_INVITE=1] [BOOKING_SYNTHETIC_PLAN=<id>] \
//     npx playwright test --project=admin-evidence owner-live
//
// Writes on Dev: two view-as sessions (started and ended), one compliance refresh, optionally one staff row + invite job
// and one sale exception (recorded then revoked). Every audit row names the E2E owner ("E2E owner (agent, Dev only)").
// Output: tests/e2e/results/admin-evidence/owner-live-*.png and owner-live.json (statuses and ids only, no tokens).
import { test, expect, type Page } from '@playwright/test';
import fs from 'fs';
import { E2E_OWNER_EMAIL, MAILBOX_ADMIN_PREFIX } from '../helpers/mailbox';
import { STAFF_AUTH_HOST, signInByEmailCode } from '../helpers/cognitoSignIn';

const CIS = 'https://aa-cis.lumiguides.it.com';
const BOOKING = (process.env.BOOKING_BASE_URL || 'https://booking.lumiguides.it.com').replace(/\/+$/, '');
const SHOTS = 'tests/e2e/results/admin-evidence';
const INVITE = process.env.E2EOWNER_INVITE === '1';
const PLAN = (process.env.BOOKING_SYNTHETIC_PLAN || '').trim();
const INVITEE = 'info@adventure.asia';
const REASON = 'Live check of the E2E owner account (agent, Dev only)';

async function shot(page: Page, name: string): Promise<void> {
  await page.screenshot({ path: `${SHOTS}/owner-live-${name}.png`, fullPage: true });
}

test('A10h E2E owner: owner-only live checks in CIS and Booking', async ({ page }) => {
  test.skip(process.env.E2EOWNER !== '1', 'opt-in: E2EOWNER=1');
  test.setTimeout(420000);
  fs.mkdirSync(SHOTS, { recursive: true });
  const report: Record<string, unknown> = {};
  await page.setViewportSize({ width: 1440, height: 900 });

  // 1. CIS sign-in (links the sub on the first sign-in).
  await page.goto(`${CIS}/login`, { waitUntil: 'domcontentloaded' });
  await page.getByRole('link', { name: /sign in with company email/i }).click({ timeout: 15000 });
  await page.waitForURL((u) => u.host === STAFF_AUTH_HOST, { timeout: 20000 });
  await signInByEmailCode(page, { email: E2E_OWNER_EMAIL, prefix: MAILBOX_ADMIN_PREFIX });
  await page.waitForURL((u) => u.origin === CIS && u.pathname.startsWith('/admin'), { timeout: 30000 });
  const session = await page.request.get(`${CIS}/api/admin/session`);
  report.cis_session = session.status();
  await page.goto(`${CIS}/admin/platform/roles`, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('[data-testid^="role-view-as-"]').first()).toBeVisible({ timeout: 30000 });
  await shot(page, 'cis-roles');

  // 2. CIS view as Content, then Back.
  const start = await page.request.post(`${CIS}/api/view-as/roles`,
    { data: { role: 'content', reason: REASON, minutes: 5 } });
  report.cis_view_as_start = start.status();
  expect(start.status(), await start.text()).toBe(201);
  await page.goto(`${CIS}/admin`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1500);
  await shot(page, 'cis-view-as-banner');
  const back = await page.request.delete(`${CIS}/api/view-as/roles`);
  report.cis_view_as_end = back.status();
  expect(back.ok()).toBeTruthy();

  // 3. Compliance Refresh.
  const refresh = await page.request.post(`${CIS}/api/admin/platform/compliance/refresh`, { data: {} });
  report.compliance_refresh = refresh.status();
  const refreshBody = await refresh.json().catch(() => ({}));
  report.compliance_refresh_body_keys = Object.keys(refreshBody || {});
  expect(refresh.status(), JSON.stringify(refreshBody)).toBe(200);
  expect(JSON.stringify(refreshBody)).not.toContain('mfa_required');
  await page.goto(`${CIS}/admin/platform/compliance`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1500);
  await shot(page, 'cis-compliance');

  // 4. Optional: invite info@adventure.asia as Content (the CIS custom role).
  if (INVITE) {
    const list = await page.request.get(`${CIS}/api/admin/platform/staff`);
    const rows = ((await list.json().catch(() => ({}))) as { staff?: { email?: string }[] }).staff || [];
    if (rows.some((r) => (r.email || '').toLowerCase() === INVITEE)) {
      report.invite = 'skipped: a row for the invitee exists';
    } else {
      const created = await page.request.post(`${CIS}/api/admin/platform/staff`, {
        data: { email: INVITEE, display_name: 'Adventure Asia info', role: 'custom_content', booking_role: null,
          send_invite: true },
      });
      report.invite = created.status();
      expect(created.status(), await created.text()).toBe(201);
    }
    await page.goto(`${CIS}/admin/platform/staff`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(1500);
    await shot(page, 'cis-staff');
  }

  // 5. Booking: one sign-in reused; role admin; view as sales and Back.
  await page.goto(`${BOOKING}/login`, { waitUntil: 'domcontentloaded' });
  const go = page.getByRole('link', { name: /sign in with company email/i })
    .or(page.getByRole('button', { name: /sign in with company email/i })).first();
  if (await go.isVisible({ timeout: 10000 }).catch(() => false)) await go.click();
  await page.waitForURL((u) => u.origin === BOOKING && !u.pathname.startsWith('/login'), { timeout: 60000 });
  const me = await page.request.get(`${BOOKING}/api/booking/staff/me`);
  const meBody = (await me.json().catch(() => ({}))) as { role?: string };
  report.booking_me = { status: me.status(), role: meBody.role };
  expect(me.status()).toBe(200);
  expect(meBody.role).toBe('admin');
  const bStart = await page.request.post(`${BOOKING}/api/view-as`, { data: { role: 'sales', minutes: 5, reason: REASON } });
  report.booking_view_as_start = bStart.status();
  expect([200, 201]).toContain(bStart.status());
  await page.goto(`${BOOKING}/`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1500);
  await shot(page, 'booking-view-as-banner');
  const bBack = await page.request.delete(`${BOOKING}/api/view-as`);
  report.booking_view_as_end = bBack.status();
  expect(bBack.ok()).toBeTruthy();

  // 6. Optional: a sale exception on a SYNTHETIC trip plan, then its revoke.
  if (PLAN) {
    const op = await page.request.get(`${BOOKING}/api/booking/catalog/trip-plans/${encodeURIComponent(PLAN)}/operability`);
    const opBody = (await op.json().catch(() => ({}))) as { status?: string; findings?: { rule?: string }[] };
    report.operability_before = { status: op.status(), state: opBody.status };
    expect(op.status()).toBe(200);
    const rule = process.env.SALE_EXCEPTION_RULE
      || opBody.findings?.map((f) => f.rule).find((r) => r === 'N1' || r === 'N3') || 'N1';
    const ex = await page.request.post(
      `${BOOKING}/api/booking/catalog/trip-plans/${encodeURIComponent(PLAN)}/sell-exception`,
      { data: { rules: [rule], reason: 'Live check of the admin sale exception on a synthetic tour' } });
    report.sale_exception = ex.status();
    expect(ex.status(), await ex.text()).toBe(200);
    const rv = await page.request.post(
      `${BOOKING}/api/booking/catalog/trip-plans/${encodeURIComponent(PLAN)}/sell-exception/revoke`,
      { data: { reason: 'Live check done: revoke the synthetic sale exception' } });
    report.sale_exception_revoke = rv.status();
    expect(rv.status(), await rv.text()).toBe(200);
  }

  fs.writeFileSync(`${SHOTS}/owner-live.json`, JSON.stringify(report, null, 2));
});
