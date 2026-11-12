// AA-1094 (S278, root ADR 0006a A10b-4): the Dev-only E2E CIS admin (A10b) gives a SYNTHETIC agency the TripPlanner
// add-on through Tenant 360 > Subscription "Add an add-on" (the tenant_provision saga, the same staff path as any
// admin), so TripPlanner serves the agency's /{slug} pages; the same attach on a real tenant is refused 403
// e2e_read_only. Agent-only, NEVER CI (codes in inbound-admin/, which only the MFA admin role reads; no workflow sets
// E2EADMIN):
//
//   cd apps/AA-CIS-App && AWS_PROFILE=aa365-admin E2EADMIN=1 TENANT_SYNTHETIC=<tenant uuid> [TENANT_REAL=<uuid>] \
//     [TP_PLAN=tripplanner.hosted] [E2E_DRY_RUN=1] [E2E_CANCEL=1] npx playwright test --project admin-evidence tp-addon
//
// TENANT_SYNTHETIC = a synthetic tenant (shared.tenants.is_canary true, not the pilot), in service. TENANT_REAL = a
// real tenant (optional, the 403 check: the route refuses it before a job is enqueued). E2E_DRY_RUN=1 signs in and
// reads the Subscription tab only. E2E_CANCEL=1 cancels the add-on instead (clean-up; the subscription row stays,
// status canceled). Writes on Dev (not a dry run): one tenant_provision job, the tenant's tripplanner subscription,
// its subscription.changed / entitlement.changed feed rows and the audit row naming the job, the AA-884 staff.request
// row with the A10b admin as the actor.
import { test, expect, type Page } from '@playwright/test';
import fs from 'fs';
import { E2E_CIS_ADMIN_EMAIL, MAILBOX_ADMIN_PREFIX } from '../helpers/mailbox';
import { STAFF_AUTH_HOST, signInByEmailCode } from '../helpers/cognitoSignIn';

const CIS = 'https://aa-cis.lumiguides.it.com';
const SHOTS = 'tests/e2e/results/admin-evidence';
const SYNTHETIC = process.env.TENANT_SYNTHETIC ?? '';
const REAL = process.env.TENANT_REAL ?? '';
const PLAN = process.env.TP_PLAN ?? 'tripplanner.hosted';
const DRY_RUN = process.env.E2E_DRY_RUN === '1';
const CANCEL = process.env.E2E_CANCEL === '1';
const LIVE = ['trialing', 'active', 'past_due'];

type Sub = {
  tenant: { name: string; kind: string; account_status: string; in_service: boolean };
  subscriptions: { product: string; plan_key: string; status: string }[];
  entitlements: { feature_key: string; source: string }[];
};

async function subscription(page: Page, tenantId: string): Promise<Sub> {
  const res = await page.request.get(`/api/admin/tenants/${tenantId}/subscription`);
  expect(res.status()).toBe(200);
  return (await res.json()) as Sub;
}

/** A provision call from the signed-in page itself (same-origin fetch through the BFF, as the drawer sends it). */
async function post(page: Page, path: string, body: unknown) {
  return page.evaluate(
    async ({ path, body }) => {
      const res = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify(body),
        credentials: 'same-origin',
      });
      let json: Record<string, unknown> | null = null;
      try {
        json = (await res.json()) as Record<string, unknown>;
      } catch {
        json = null;
      }
      return { status: res.status, body: json };
    },
    { path, body },
  );
}

async function waitForJob(page: Page, jobId: string): Promise<string> {
  for (let i = 0; i < 60; i += 1) {
    const res = await page.request.get(`/api/admin/tenants/provision/${jobId}`);
    expect(res.status()).toBe(200);
    const job = (await res.json()) as { status: string; error?: string | null };
    if (!['queued', 'running'].includes(job.status)) return job.status;
    await page.waitForTimeout(3000);
  }
  return 'timeout';
}

const key = (what: string) => `e2e-aa1094-${what}-${Date.now()}`;

test('A10b admin: TripPlanner add-on of a synthetic tenant; refused on a real tenant', async ({ page }) => {
  test.skip(process.env.E2EADMIN !== '1' || !SYNTHETIC, 'opt-in: E2EADMIN=1 TENANT_SYNTHETIC=<synthetic tenant uuid>');
  test.setTimeout(360000);
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/login', { waitUntil: 'domcontentloaded' });
  await page.getByRole('link', { name: /sign in with company email/i }).click({ timeout: 15000 });
  await page.waitForURL((u) => u.host === STAFF_AUTH_HOST, { timeout: 20000 });
  await signInByEmailCode(page, { email: E2E_CIS_ADMIN_EMAIL, prefix: MAILBOX_ADMIN_PREFIX });
  await page.waitForURL((u) => u.origin === CIS && u.pathname.startsWith('/admin'), { timeout: 30000 });

  const tab = `/admin/tenants/${SYNTHETIC}?tab=subscription`;
  await page.goto(tab, { waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('tenant-subscriptions')).toBeVisible({ timeout: 30000 });
  const before = await subscription(page, SYNTHETIC);
  console.log(`[tp-addon] ${before.tenant.name}: kind ${before.tenant.kind}, ${before.tenant.account_status}, ` +
    `subscriptions ${before.subscriptions.map((s) => `${s.product}:${s.status}`).join(', ')}`);
  await page.screenshot({ path: `${SHOTS}/aa1094-tp-addon-before-1440-light.png`, fullPage: true });
  if (DRY_RUN) {
    console.log('[tp-addon] dry run: read only, no write');
    return;
  }

  if (REAL) {
    const refused = await post(page, `/api/admin/tenants/${REAL}/provision`,
      { request_key: key('real'), add_ons: [{ product: 'tripplanner', plan_key: PLAN, seats: null }] });
    expect([refused.status, refused.body?.detail]).toEqual([403, 'e2e_read_only']);
  }

  if (CANCEL) {
    const res = await post(page, `/api/admin/tenants/${SYNTHETIC}/provision/cancel`,
      { request_key: key('cancel'), products: ['tripplanner'] });
    expect(res.status, JSON.stringify(res.body)).toBe(202);
    expect(await waitForJob(page, String(res.body?.job_id))).toBe('succeeded');
    const after = await subscription(page, SYNTHETIC);
    expect(after.subscriptions.find((s) => s.product === 'tripplanner')?.status).toBe('canceled');
    return;
  }

  // The staff path itself: Add an add-on > TripPlanner > plan > Add (the drawer runs the saga and waits for it).
  await page.getByTestId('addon-add').click();
  const drawer = page.getByTestId('addon-drawer');
  await expect(drawer).toBeVisible();
  const selects = drawer.locator('select');
  await selects.nth(0).selectOption('tripplanner');
  await selects.nth(1).selectOption(PLAN);
  await page.screenshot({ path: `${SHOTS}/aa1094-tp-addon-drawer-1440-light.png`, fullPage: true });
  const jobResponse = page.waitForResponse(
    (r) => r.request().method() === 'POST' && r.url().endsWith(`/api/admin/tenants/${SYNTHETIC}/provision`));
  await page.getByTestId('addon-save').click();
  const posted = await jobResponse;
  expect(posted.status()).toBe(202);
  const job = (await posted.json()) as { job_id: string };
  expect(await waitForJob(page, job.job_id)).toBe('succeeded');

  const after = await subscription(page, SYNTHETIC);
  const tp = after.subscriptions.find((s) => s.product === 'tripplanner');
  expect(tp?.plan_key).toBe(PLAN);
  expect(LIVE).toContain(tp?.status);
  expect(after.entitlements.map((e) => e.feature_key)).toContain('tp.hosted');
  await page.goto(tab, { waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('tenant-subscriptions')).toBeVisible({ timeout: 30000 });
  for (const [w, h] of [[1440, 900], [390, 844]] as const) {
    await page.setViewportSize({ width: w, height: h });
    await page.screenshot({ path: `${SHOTS}/aa1094-tp-addon-after-${w}-light.png`, fullPage: true });
  }
});
