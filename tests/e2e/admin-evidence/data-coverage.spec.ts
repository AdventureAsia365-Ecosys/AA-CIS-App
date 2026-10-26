// tests/e2e/admin-evidence/data-coverage.spec.ts
// AA-992 — live evidence of /admin/data-coverage as the Dev-only E2E CIS admin (A10b, root ADR 0006a). Agent-only,
// NEVER CI (the sign-in code lands under inbound-admin/, readable only by the MFA admin role), like admin-pages.spec.ts:
//
//   cd apps/AA-CIS-App && AWS_PROFILE=aa365-admin E2EADMIN=1 npx playwright test --project admin-evidence \
//     tests/e2e/admin-evidence/data-coverage.spec.ts
//
// Steps: sign in (company email → managed login → code) → API check through the BFF with the session cookie
// (context.request, lesson S243/S251: never read a body the page itself does not read): 200, every stage present,
// every source ok or one of the documented n/a, built under 5 s, no id-like key → the page at 1440x900 (grown to the
// inner scroll region) and 390x844, light and dark → no console error, no 4xx/5xx on the page's own calls →
// Refresh changes "As of" only after the 15 s throttle → CSV export downloads every row → sign out.
// Screenshots + the CSV go to tests/e2e/results/admin-evidence/ (gitignored); copy them to the root repo's
// docs/audits/ for evidence. The code is a live credential: never logged; no trace/video on staff-auth.
import { test, expect, type Page, type TestInfo } from '@playwright/test';
import fs from 'fs';
import path from 'path';
import { E2E_CIS_ADMIN_EMAIL, MAILBOX_ADMIN_PREFIX } from '../helpers/mailbox';
import { STAFF_AUTH_HOST, onStaffAuth, signInByEmailCode } from '../helpers/cognitoSignIn';
import { growViewportToContent } from '../smoke/smoke-helpers';

const CIS_PROD_ORIGIN = 'https://aa-cis.lumiguides.it.com';
const SHOTS_DIR = 'tests/e2e/results/admin-evidence';
const PAGE_PATH = '/admin/data-coverage';
const API_PATH = '/api/admin/data-coverage';
const GROUPS = ['inputs', 'raw', 'master', 'aseries', 'places', 'tenants', 'tripplanner', 'booking', 'crm'];
// Sources allowed to be n/a on Dev today, with why (anything else failing fails the run):
//   tp_* / bk_* contract views until tp-020 / bk-025 are applied (contract_view_missing).
const ALLOWED_NA = /^(unavailable: contract_view_missing|no_data_yet)$/;

async function shot(page: Page, name: string, fullPage = false) {
  fs.mkdirSync(SHOTS_DIR, { recursive: true });
  await page.screenshot({ path: path.join(SHOTS_DIR, `${name}.png`), fullPage });
}

function idLikeKeys(value: unknown, found: Set<string> = new Set()): Set<string> {
  if (Array.isArray(value)) value.forEach((v) => idLikeKeys(v, found));
  else if (value && typeof value === 'object') {
    for (const [k, v] of Object.entries(value)) {
      if (/_ids?$/.test(k) || k.startsWith('sample') || k === 'name') found.add(k);
      idLikeKeys(v, found);
    }
  }
  return found;
}

test.describe('AA-992 — data coverage evidence as the Dev-only E2E CIS admin', () => {
  test.skip(process.env.E2EADMIN !== '1', 'opt-in, agent-only: E2EADMIN=1 with AWS_PROFILE=aa365-admin (MFA admin role)');

  test.afterEach(async ({ page }, testInfo: TestInfo) => {
    if (testInfo.status === testInfo.expectedStatus) return;
    if (onStaffAuth(page)) {
      await testInfo.attach('managed-login-state', { body: `url=${page.url().split('?')[0]}`, contentType: 'text/plain' });
    } else {
      await shot(page, 'data-coverage-99-failure').catch(() => {});
    }
  });

  test('signs in, checks the API, screenshots the page, refresh and CSV', async ({ page }) => {
    test.setTimeout(300000);
    const consoleErrors: string[] = [];
    const badResponses: string[] = [];
    page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text().slice(0, 200)); });
    page.on('response', (r) => {
      const u = new URL(r.url());
      if (u.origin === CIS_PROD_ORIGIN && r.status() >= 400) badResponses.push(`${r.status()} ${u.pathname}`);
    });

    // ── sign in ──
    await page.goto('/login', { waitUntil: 'domcontentloaded' });
    await page.getByRole('link', { name: /sign in with company email/i }).click({ timeout: 15000 });
    await page.waitForURL((u) => u.host === STAFF_AUTH_HOST, { timeout: 20000 });
    await signInByEmailCode(page, { email: E2E_CIS_ADMIN_EMAIL, prefix: MAILBOX_ADMIN_PREFIX });
    await page.waitForURL((u) => u.origin === CIS_PROD_ORIGIN && u.pathname.startsWith('/admin'), { timeout: 30000 });
    const account = page.getByRole('button', { name: 'Account menu' });
    await expect(account).toBeVisible({ timeout: 15000 });

    // ── API through the BFF (same cookie) ──
    const res = await page.context().request.get(`${CIS_PROD_ORIGIN}${API_PATH}`, { timeout: 30000 });
    expect(res.status()).toBe(200);
    const body = await res.json();
    fs.mkdirSync(SHOTS_DIR, { recursive: true });
    fs.writeFileSync(path.join(SHOTS_DIR, 'data-coverage-api.json'), JSON.stringify(body, null, 1));
    expect(body.groups.map((g: { id: string }) => g.id)).toEqual(GROUPS);
    expect(body.generated_ms).toBeLessThan(5000);
    expect([...idLikeKeys(body)]).toEqual([]);
    const failing = Object.entries(body.sources as Record<string, { status: string; reason: string | null }>)
      .filter(([, s]) => s.status !== 'ok' && !ALLOWED_NA.test(`${s.status}: ${s.reason ?? ''}`) && s.reason !== 'contract_view_missing')
      .map(([k, s]) => `${k}=${s.status}:${s.reason}`);
    expect(failing, 'sources that did not answer').toEqual([]);
    expect(body.sources.crm.status, 'a Cognito session reads the CRM rows').toBe('ok');

    // ── the page: 1440 (grown) + 390, light + dark ──
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(PAGE_PATH, { waitUntil: 'domcontentloaded' });
    expect(new URL(page.url()).pathname).toBe(PAGE_PATH);
    await expect(page.getByTestId('data-coverage-ready')).toBeVisible({ timeout: 30000 });
    for (const g of GROUPS) await expect(page.getByTestId(`dc-group-${g}`)).toBeVisible();
    for (const scheme of ['light', 'dark'] as const) {
      await page.emulateMedia({ colorScheme: scheme });
      await page.setViewportSize({ width: 1440, height: 900 });
      await page.waitForTimeout(300);
      await growViewportToContent(page, 1440, 900);
      await shot(page, `data-coverage-desktop-${scheme}`, true);
      await page.setViewportSize({ width: 390, height: 844 });
      await page.waitForTimeout(300);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      expect(overflow, 'no sideways page scroll at 390').toBeLessThanOrEqual(0);
      await shot(page, `data-coverage-mobile-${scheme}`);
    }
    await page.emulateMedia({ colorScheme: 'light' });
    await page.setViewportSize({ width: 1440, height: 900 });

    // ── Refresh: throttled 15 s, then a new "As of" ──
    const asOf = page.getByTestId('data-coverage-as-of');
    const before = await asOf.textContent();
    const refresh = page.getByRole('button', { name: /refresh/i });
    await page.waitForTimeout(16000);   // the backend recomputes only a copy older than 15 s
    await refresh.click();
    await expect(refresh).toBeDisabled();
    await expect.poll(async () => asOf.textContent(), { timeout: 30000 }).not.toBe(before);

    // ── CSV ──
    const [download] = await Promise.all([
      page.waitForEvent('download', { timeout: 15000 }),
      page.getByRole('button', { name: /export csv/i }).click(),
    ]);
    const csvPath = path.join(SHOTS_DIR, 'data-coverage.csv');
    await download.saveAs(csvPath);
    const lines = fs.readFileSync(csvPath, 'utf8').trim().split('\n');
    const rowCount = body.groups.reduce((n: number, g: { rows: unknown[] }) => n + g.rows.length, 0);
    expect(lines.length).toBe(rowCount + 1);

    expect(consoleErrors, 'console errors').toEqual([]);
    expect(badResponses, '4xx/5xx on CIS').toEqual([]);

    // ── sign out ──
    await account.click();
    await page.getByRole('button', { name: 'Sign out' }).click({ timeout: 5000 });
    await page.waitForURL((u) => u.origin === CIS_PROD_ORIGIN && u.pathname === '/login', { timeout: 30000 });
  });
});
