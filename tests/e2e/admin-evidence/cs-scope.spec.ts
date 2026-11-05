// AA-1035 A10d (root ADR 0006a A10d): the Dev-only read-only customer_success E2E account sees only its tenant book.
// Agent-only, NEVER CI (codes land in inbound-admin/, which only the MFA admin role reads; no workflow sets E2ECS):
//
//   cd apps/AA-CIS-App && AWS_PROFILE=aa365-admin E2ECS=1 TENANT_A=<uuid> TENANT_B=<uuid> npx playwright test --project admin-evidence cs-scope
//
// TENANT_A = the synthetic tenant in the cs account's book, TENANT_B = another synthetic tenant outside it.
// Proves the AA-1035 Done-when "a customer_success E2E user sees only the assigned tenants (403 on another tenant)"
// with the real path: WORKFORCE email-code sign-in -> pre token trigger (cis-customer_success only) -> /auth/verify-admin
// role customer_success -> BFF -> API scope. Screenshots: tests/e2e/results/admin-evidence/cs-*.png. Writes nothing:
// both write attempts (an assignment through /api/admin, a view-as start through the /api/view-as BFF route, which is
// on the A10b allow-list for the E2E admin only) are refused 403 e2e_read_only by the API.
// AA-1035 (directory capability UI): on /admin/tenants the role sees no write control it cannot use (no New tenant, no
// Offboard / Restore, a read-only status pill), Detail still opens Tenant 360, and the page makes no 4xx call (the
// admin-only unmapped-markets banner is not fetched).
// Hygiene as admin-pages.spec.ts: retries 0, one sign-in per run, run specs one at a time.
import { test, expect, type Page, type Response as PwResponse } from '@playwright/test';
import fs from 'fs';
import path from 'path';
import { E2E_CIS_CS_EMAIL, MAILBOX_ADMIN_PREFIX } from '../helpers/mailbox';
import { STAFF_AUTH_HOST, signInByEmailCode } from '../helpers/cognitoSignIn';
import { growViewportToContent } from '../smoke/smoke-helpers';

const CIS = 'https://aa-cis.lumiguides.it.com';
const SHOTS = 'tests/e2e/results/admin-evidence';
const A = process.env.TENANT_A ?? '';
const B = process.env.TENANT_B ?? '';
const OUT_OF_SCOPE = 'Tenant outside your scope';

async function shot(page: Page, name: string) {
  fs.mkdirSync(SHOTS, { recursive: true });
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.waitForTimeout(300);
    await growViewportToContent(page, 1440, 900);
    await page.screenshot({ path: path.join(SHOTS, `cs-${name}-desktop-${scheme}.png`), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(300);
    await page.screenshot({ path: path.join(SHOTS, `cs-${name}-mobile-${scheme}.png`) });
  }
  await page.emulateMedia({ colorScheme: 'light' });
  await page.setViewportSize({ width: 1440, height: 900 });
}

async function bff(page: Page, method: string, apiPath: string, body?: unknown) {
  return page.evaluate(async ({ method, apiPath, body }) => {
    const res = await fetch(apiPath, { method, headers: { 'content-type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body) });
    return { status: res.status, body: await res.text() };
  }, { method, apiPath, body });
}

test.describe('AA-1035 — customer_success E2E staff sees only its book', () => {
  test.skip(process.env.E2ECS !== '1' || !A || !B, 'opt-in, agent-only: E2ECS=1 TENANT_A TENANT_B, AWS_PROFILE=aa365-admin');

  test('signs in as customer_success; own tenant 200, other tenant 403; read-only', async ({ page }) => {
    test.setTimeout(300000);
    await page.goto('/login', { waitUntil: 'domcontentloaded' });
    await page.getByRole('link', { name: /sign in with company email/i }).click({ timeout: 15000 });
    await page.waitForURL((u) => u.host === STAFF_AUTH_HOST, { timeout: 20000 });
    await signInByEmailCode(page, { email: E2E_CIS_CS_EMAIL, prefix: MAILBOX_ADMIN_PREFIX });
    // customer_success lands on /admin/tenants (frontend/lib/rbac.ts home for the desk roles)
    await page.waitForURL((u) => u.origin === CIS && u.pathname.startsWith('/admin'), { timeout: 30000 });

    // ── API through the BFF (the real Cognito session) ──
    const dir = await bff(page, 'GET', '/api/admin/tenants');
    expect(dir.status).toBe(200);
    const ids: string[] = (JSON.parse(dir.body).tenants ?? JSON.parse(dir.body)).map((t: { tenant_id: string }) => t.tenant_id);
    expect(ids, 'directory = the book only').toEqual([A]);
    expect((await bff(page, 'GET', `/api/admin/tenants/${A}/members`)).status).toBe(200);
    for (const leaf of ['members', 'assignments']) {
      const r = await bff(page, 'GET', `/api/admin/tenants/${B}/${leaf}`);
      expect(r.status, `other tenant ${leaf}`).toBe(403);
      expect(r.body).toContain(OUT_OF_SCOPE);
    }
    const asg = JSON.parse((await bff(page, 'GET', '/api/admin/tenant-assignments')).body);
    expect(asg.scope).toBe('scoped');
    expect(asg.tenants.map((t: { tenant_id: string }) => t.tenant_id)).toEqual([A]);
    // CRM desk relay (crm.ops.v1 staff-scope, S269): the agencies list holds only the book
    const ag = await bff(page, 'GET', '/api/admin/platform/crm/agencies');
    expect(ag.status).toBe(200);
    expect(ag.body).not.toContain(B);
    // read-only cap: a customer_success write (tenant.book.write) is refused for the E2E identity
    const w = await bff(page, 'PUT', `/api/admin/tenants/${A}/assignments/00000000-0000-4000-8000-00000000a10d`, { kind: 'backup' });
    expect(w.status).toBe(403);
    expect(w.body).toContain('e2e_read_only');
    // no A10b allow-list for this identity: a view-as start (allow-listed for the A10b admin) is refused as well.
    // Through the real view-as BFF route: /api/admin refuses every start path itself (view_as_bff_only) before the API,
    // while /api/view-as forwards to CIS POST /admin/tenants/{id}/view-as and passes the upstream status + body back.
    const v = await bff(page, 'POST', '/api/view-as', { tenant_id: A, reason: 'A10d live check', minutes: 5 });
    expect(v.status).toBe(403);
    expect(v.body).toContain('e2e_read_only');

    // ── Pages ──
    // The Directory: every same-origin response while it loads, to prove it made no refused (4xx) call.
    const dirErrors: string[] = [];
    const onResponse = (r: PwResponse) => {
      const u = new URL(r.url());
      if (u.origin === CIS && r.status() >= 400) dirErrors.push(`${r.status()} ${u.pathname}`);
    };
    page.on('response', onResponse);
    await page.goto('/admin/tenants', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('table tbody tr')).toHaveCount(1, { timeout: 30000 });
    const row = page.locator('table tbody tr').first();
    await expect(row.getByRole('button', { name: /^detail$/i })).toBeVisible();
    await page.waitForTimeout(2000);   // let the page's other reads settle before counting
    await expect(page.getByRole('button', { name: /new tenant/i }), 'no create for customer_success').toHaveCount(0);
    await expect(page.getByRole('button', { name: /^(offboard|restore)$/i }), 'no offboard / restore').toHaveCount(0);
    await expect(row.getByRole('button', { name: /^(active|suspended|offboarded)$/i }), 'status pill read-only')
      .toHaveCount(0);
    page.off('response', onResponse);
    expect(dirErrors, 'the Directory made no 4xx call (no unmapped-market-requests)').toEqual([]);
    await shot(page, 'tenants-directory');
    // Detail still opens Tenant 360 of the own tenant
    await row.getByRole('button', { name: /^detail$/i }).click();
    await page.waitForURL((u) => u.pathname === `/admin/tenants/${A}`, { timeout: 30000 });
    await page.goto(`/admin/tenants/${A}`, { waitUntil: 'domcontentloaded' });
    await expect(page.locator('[data-testid="tenant360-ready"]').first()).toBeVisible({ timeout: 30000 });
    await shot(page, 'tenant360-own');
    await page.goto(`/admin/tenants/${B}`, { waitUntil: 'domcontentloaded' });
    await expect(page.getByText(/outside your (scope|tenant book)|not allowed|no access/i).first()).toBeVisible({ timeout: 30000 });
    await shot(page, 'tenant360-other-refused');
    for (const p of ['/admin/tenants/assignments', '/admin/tenants/agencies', '/admin/tenant-qa/quality']) {
      await page.goto(p, { waitUntil: 'domcontentloaded' });
      await expect(page.locator('h1').first()).toBeVisible({ timeout: 30000 });
      await page.waitForTimeout(1500);
      await shot(page, p.replace(/^\/admin\//, '').replace(/\//g, '-'));
    }
    // an admin-only page bounces this role
    await page.goto('/admin/platform/staff', { waitUntil: 'domcontentloaded' });
    await expect.poll(() => new URL(page.url()).pathname).not.toBe('/admin/platform/staff');
    await shot(page, 'admin-only-bounced');

    await page.getByRole('button', { name: 'Account menu' }).click();
    await page.getByRole('button', { name: 'Sign out' }).click({ timeout: 5000 });
    await page.waitForURL((u) => u.origin === CIS && u.pathname === '/login', { timeout: 30000 });
  });
});
